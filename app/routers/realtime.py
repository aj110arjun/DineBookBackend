import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from threading import BoundedSemaphore

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import or_

from app.core.security import decode_access_token
from app.db.database import SessionLocal
from app.models.realtime import RealtimeEvent
from app.models.restaurant import Restaurant
from app.models.user import User, UserRole
from app.routers.admin_auth import ACCESS_COOKIE as AUTH_COOKIE
from app.routers.chef_auth import current_chef
from app.routers.customer_auth import current_customer
from app.routers.manager_auth import current_manager

router = APIRouter(prefix="/api/events", tags=["realtime events"])
logger = logging.getLogger(__name__)
POLL_SECONDS = 2
MAX_EVENTS_PER_POLL = 100
MAX_STREAMS_PER_PROCESS = 200
_stream_slots = BoundedSemaphore(MAX_STREAMS_PER_PROCESS)


def _authorize_stream(request: Request) -> tuple[uuid.UUID, uuid.UUID | None]:
    token = request.cookies.get(AUTH_COOKIE)
    if not token:
        raise HTTPException(status_code=401, detail="Please sign in to continue.")
    db = SessionLocal()
    try:
        try:
            user_id = uuid.UUID(decode_access_token(token).get("sub", ""))
        except (ValueError, TypeError, AttributeError):
            raise HTTPException(status_code=401, detail="Please sign in to continue.")
        user = db.query(User).filter(User.id == user_id).first()
        if user is None:
            raise HTTPException(status_code=401, detail="Please sign in to continue.")

        restaurant_id = None
        if user.role == UserRole.CUSTOMER:
            current_customer(token=token, db=db)
        elif user.role == UserRole.MANAGER:
            manager = current_manager(token=token, db=db)
            restaurant_id = db.query(Restaurant.id).filter(Restaurant.manager_id == manager.id).scalar()
            if restaurant_id is None:
                raise HTTPException(status_code=403, detail="No restaurant is linked to this account.")
        elif user.role == UserRole.CHEF:
            chef = current_chef(token=token, db=db)
            if chef.manager_id is None:
                raise HTTPException(status_code=403, detail="No restaurant is linked to this account.")
            restaurant_id = db.query(Restaurant.id).filter(Restaurant.manager_id == chef.manager_id).scalar()
            if restaurant_id is None:
                raise HTTPException(status_code=403, detail="No restaurant is linked to this account.")
        else:
            raise HTTPException(status_code=403, detail="Real-time events are not enabled for this account.")
        return user_id, restaurant_id
    finally:
        db.close()


def _read_events(user_id: uuid.UUID, restaurant_id: uuid.UUID | None, after_id: int) -> list[RealtimeEvent]:
    db = SessionLocal()
    try:
        scope = [RealtimeEvent.user_id == user_id]
        if restaurant_id is not None:
            scope.append(RealtimeEvent.restaurant_id == restaurant_id)
        return db.query(RealtimeEvent).filter(
            RealtimeEvent.id > after_id, or_(*scope)
        ).order_by(RealtimeEvent.id.asc()).limit(MAX_EVENTS_PER_POLL).all()
    finally:
        db.close()


@router.get("/stream")
async def event_stream(request: Request, after: int = 0) -> StreamingResponse:
    user_id, restaurant_id = _authorize_stream(request)
    last_event_header = request.headers.get("last-event-id")
    if last_event_header:
        try:
            after = int(last_event_header)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid event cursor.") from exc
    if after < 0 or after > 2**63 - 1:
        raise HTTPException(status_code=400, detail="Invalid event cursor.")
    if not _stream_slots.acquire(blocking=False):
        raise HTTPException(status_code=503, detail="Realtime capacity is temporarily full. Please retry.")

    async def generate() -> AsyncIterator[str]:
        cursor = after
        poll_delay = POLL_SECONDS
        last_auth_check = time.monotonic()
        try:
            yield "retry: 3000\n\n"
            while not await request.is_disconnected():
                try:
                    if time.monotonic() - last_auth_check >= 30:
                        try:
                            current_scope = await asyncio.to_thread(_authorize_stream, request)
                        except HTTPException:
                            yield "event: auth.expired\ndata: {}\n\n"
                            break
                        if current_scope != (user_id, restaurant_id):
                            yield "event: auth.expired\ndata: {}\n\n"
                            break
                        last_auth_check = time.monotonic()
                    events = await asyncio.to_thread(_read_events, user_id, restaurant_id, cursor)
                    poll_delay = POLL_SECONDS
                    for row in events:
                        # Only fields explicitly approved for realtime delivery are serialized.
                        safe_data = {
                            "event_id": str(row.event_id),
                            "event_type": row.event_type,
                            "schema_version": 1,
                            "occurred_at": row.created_at.isoformat(),
                            "resource_id": row.resource_id,
                            "data": row.data,
                        }
                        yield f"id: {row.id}\nevent: {row.event_type}\ndata: {json.dumps(safe_data, separators=(',', ':'))}\n\n"
                        cursor = row.id
                    if not events:
                        yield ": keep-alive\n\n"
                except Exception:
                    logger.exception("Realtime event stream poll failed")
                    yield ": temporary-error\n\n"
                    poll_delay = min(max(POLL_SECONDS, poll_delay * 2), 30)
                await asyncio.sleep(poll_delay)
        finally:
            _stream_slots.release()

    return StreamingResponse(generate(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    })
