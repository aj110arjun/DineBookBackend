"""Authenticated customer support. Admin, manager and chef accounts are excluded."""
import asyncio
import json
import uuid
from dataclasses import dataclass

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.core.config import settings
from app.core.security import decode_access_token
from app.db.database import get_db
from app.models.user import User, UserRole
from app.models.customer_ai import CustomerAIConversation
from app.routers.auth.customer_auth import ACCESS_COOKIE, current_customer
from app.schemas.ai_chat import AIChatRequest, AIChatResponse, AIConversationCreate, AIConversationView
from app.services.customer_ai_conversations import ConversationRepository
from app.services.customer_ai_support import answer_customer
from app.services.ollama_service import ollama_service
from app.services.ai_support_runtime import turn_slots as _turn_slots

router = APIRouter(prefix='/api/customer/ai', tags=['customer AI support'])
# This application gate bounds CPU work; production must also cap the inference
# server globally (OLLAMA_NUM_PARALLEL=1) when using multiple API workers.


@dataclass(frozen=True)
class CustomerContext:
    user_id: uuid.UUID
    token: str
    bind: object


def support_customer(token: str | None = Cookie(default=None, alias=ACCESS_COOKIE), db: Session = Depends(get_db)):
    try:
        uid = uuid.UUID(decode_access_token(token or '').get('sub', ''))
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(401, 'Please sign in to use customer support.')
    user = db.get(User, uid)
    if user is not None and user.role != UserRole.CUSTOMER:
        raise HTTPException(403, 'AI support is available only in the customer portal.')
    customer = current_customer(token=token, db=db)
    context = CustomerContext(customer.id, token, db.get_bind())
    db.rollback()  # End the authentication read transaction before inference.
    if not settings.ai_support_enabled:
        raise HTTPException(503, 'Customer support is temporarily unavailable.')
    return context


def reauthorize(context):
    with Session(context.bind) as db:
        current_customer(token=context.token, db=db)


def repository(context):
    return ConversationRepository(context.bind, context.user_id)


def storage_ready(context):
    try:
        with Session(context.bind) as db:
            db.execute(select(CustomerAIConversation.id).limit(1))
        return True
    except SQLAlchemyError:
        return False


@router.get('/status')
async def support_status(context=Depends(support_customer)):
    return {'enabled': True, 'ready': await run_in_threadpool(storage_ready, context),
            'model_available': await ollama_service.available(), 'languages': ['auto', 'en', 'ml', 'hi'],
            'max_message_length': 1000, 'reply_timeout_seconds': settings.ai_support_turn_timeout_seconds + 5,
            'support_url': settings.ai_support_contact_url}


@router.post('/conversations', response_model=AIConversationView, status_code=201)
def create_conversation(payload: AIConversationCreate, context=Depends(support_customer)):
    try:
        return repository(context).create(payload.language)
    except SQLAlchemyError as exc:
        raise HTTPException(503, 'Chat history is unavailable. Please try again shortly.') from exc


@router.get('/conversations', response_model=list[AIConversationView])
def list_conversations(context=Depends(support_customer)):
    try:
        return repository(context).list()
    except SQLAlchemyError as exc:
        raise HTTPException(503, 'Chat history is unavailable. Please try again shortly.') from exc


@router.get('/conversations/{conversation_id}')
def conversation_history(conversation_id: uuid.UUID, context=Depends(support_customer)):
    try:
        return repository(context).history(conversation_id)
    except SQLAlchemyError as exc:
        raise HTTPException(503, 'Chat history is unavailable. Please try again shortly.') from exc


@router.delete('/conversations/{conversation_id}', status_code=204)
def delete_conversation(conversation_id: uuid.UUID, context=Depends(support_customer)):
    try:
        repository(context).delete(conversation_id)
    except SQLAlchemyError as exc:
        raise HTTPException(503, 'Unable to delete this conversation. Please try again.') from exc
    return Response(status_code=204)


async def run_turn(context, conversation_id, payload):
    if not _turn_slots.acquire(blocking=False):
        raise HTTPException(429, 'Support is preparing another reply. Please try again shortly.', headers={'Retry-After': '3'})
    repo, state = repository(context), None
    async def fail(code):
        if state and state.get('turn_id'):
            try:
                await asyncio.shield(run_in_threadpool(repo.fail, conversation_id, state['turn_id'], code))
            except SQLAlchemyError:
                pass
    try:
        async with asyncio.timeout(settings.ai_support_turn_timeout_seconds):
            state = await run_in_threadpool(repo.begin, conversation_id, payload)
            if state.get('cached'):
                return state['cached']
            await run_in_threadpool(reauthorize, context)
            answer, plan = await answer_customer(context.bind, context.user_id, payload, state)
            await run_in_threadpool(reauthorize, context)
            return await run_in_threadpool(repo.finish, conversation_id, state['turn_id'], answer, plan)
    except asyncio.CancelledError:
        await fail('cancelled')
        raise
    except TimeoutError as exc:
        await fail('timeout')
        raise HTTPException(504, 'This reply took too long. Please try again.') from exc
    except HTTPException:
        await fail('request_failed')
        raise
    except SQLAlchemyError as exc:
        await fail('storage_unavailable')
        raise HTTPException(503, 'Your support records are temporarily unavailable. Please retry.') from exc
    except Exception as exc:
        await fail('reply_failed')
        raise HTTPException(503, 'Unable to prepare a reply. Please retry.') from exc
    finally:
        _turn_slots.release()


@router.post('/conversations/{conversation_id}/messages', response_model=AIChatResponse)
async def send_message(conversation_id: uuid.UUID, payload: AIChatRequest, context=Depends(support_customer)):
    return await run_turn(context, conversation_id, payload)


@router.post('/chat', response_model=AIChatResponse)
async def customer_ai_chat(payload: AIChatRequest, context=Depends(support_customer)):
    conversation = await run_in_threadpool(create_conversation, AIConversationCreate(language=payload.language), context)
    return await run_turn(context, conversation['id'], payload)


def event(name, data):
    return f'event: {name}\ndata: {json.dumps(data, ensure_ascii=False, separators=(",", ":"))}\n\n'


@router.post('/conversations/{conversation_id}/messages/stream')
async def stream_message(conversation_id: uuid.UUID, payload: AIChatRequest, request: Request, context=Depends(support_customer)):
    async def generate():
        task = asyncio.create_task(run_turn(context, conversation_id, payload))
        try:
            yield event('status', {'message': 'Preparing your reply'})
            while not task.done():
                if await request.is_disconnected():
                    return
                done, _ = await asyncio.wait({task}, timeout=1)
                if not done:
                    yield ': keep-alive\n\n'
            try:
                result = task.result()
                # Only the validated answer and account cards cross this boundary.
                yield event('done', result.model_dump(mode='json'))
            except HTTPException as exc:
                yield event('error', {'status': exc.status_code, 'message': str(exc.detail)})
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    return StreamingResponse(generate(), media_type='text/event-stream', headers={
        'Cache-Control': 'no-store, no-transform', 'X-Accel-Buffering': 'no',
    })
