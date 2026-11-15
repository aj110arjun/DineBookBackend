"""Authenticated, restaurant-scoped manager chat; history stays in the browser session."""
import asyncio
import uuid
from dataclasses import dataclass

from fastapi import APIRouter, Cookie, Depends, HTTPException
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.core.config import settings
from app.core.security import decode_access_token
from app.db.database import get_db
from app.models.restaurant import RestaurantStatus
from app.models.user import User, UserRole
from app.routers.auth.manager_auth import ACCESS_COOKIE, current_manager
from app.routers.restaurants.dining import get_manager_restaurant
from app.schemas.manager_ai import ManagerAIRequest, ManagerAIResponse
from app.services.ai_support_runtime import turn_slots
from app.services.manager_ai_support import answer_manager, authorize
from app.services.ollama_service import ollama_service

router = APIRouter(prefix="/api/manager/ai", tags=["manager AI support"])


@dataclass(frozen=True)
class ManagerContext:
    user_id: uuid.UUID
    restaurant_id: uuid.UUID
    token: str
    bind: object


def support_manager(token: str | None = Cookie(default=None, alias=ACCESS_COOKIE), db: Session = Depends(get_db)):
    try:
        uid = uuid.UUID(decode_access_token(token or "").get("sub", ""))
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(401, "Please sign in to use manager support.")
    user = db.get(User, uid)
    if user is not None and user.role != UserRole.MANAGER:
        raise HTTPException(403, "AI support is available only to manager accounts in this portal.")
    manager = current_manager(token=token, db=db)
    restaurant = get_manager_restaurant(manager, db)
    if restaurant.status != RestaurantStatus.APPROVED:
        raise HTTPException(403, "Your restaurant must be approved to use manager AI support.")
    context = ManagerContext(manager.id, restaurant.id, token, db.get_bind())
    db.rollback()
    if not settings.ai_support_enabled:
        raise HTTPException(503, "Manager support is temporarily unavailable.")
    return context


def reauthorize(context):
    with Session(context.bind) as db:
        authorize(context, db)


@router.get("/status")
async def support_status(context=Depends(support_manager)):
    available = await ollama_service.available()
    await run_in_threadpool(reauthorize, context)
    return {"enabled": True, "ready": True, "model_available": available,
            "restaurant_id": context.restaurant_id, "languages": ["en"],
            "max_message_length": 1000,
            "reply_timeout_seconds": settings.ai_support_turn_timeout_seconds + 5,
            "support_url": settings.ai_support_contact_url}


@router.post("/chat", response_model=ManagerAIResponse)
async def manager_ai_chat(payload: ManagerAIRequest, context=Depends(support_manager)):
    if not turn_slots.acquire(blocking=False):
        raise HTTPException(429, "Support is preparing another reply. Please try again shortly.", headers={"Retry-After": "3"})
    try:
        async with asyncio.timeout(settings.ai_support_turn_timeout_seconds):
            answer = await answer_manager(context, payload)
            await run_in_threadpool(reauthorize, context)
            return answer
    except TimeoutError as exc:
        raise HTTPException(504, "This reply took too long. Please try again.") from exc
    except SQLAlchemyError as exc:
        raise HTTPException(503, "Restaurant information is temporarily unavailable. Please retry.") from exc
    finally:
        turn_slots.release()
