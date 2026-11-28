"""Short transactions for customer-owned chat history and generation leases."""
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.customer_ai import CustomerAIConversation as Conversation, CustomerAITurn as Turn, CustomerAIMessage as Message
from app.models.customer_ai import AIMessageSender, AITurnStatus
from app.schemas.ai_chat import AIAnswer, AIChatResponse


def now():
    return datetime.now(timezone.utc)


def aware(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


class ConversationRepository:
    def __init__(self, bind, user_id):
        self.bind, self.user_id = bind, user_id

    def owned(self, db, conversation_id, lock=False):
        query = db.query(Conversation).filter(Conversation.id == conversation_id, Conversation.user_id == self.user_id,
            Conversation.updated_at >= now() - timedelta(days=settings.ai_support_retention_days))
        row = (query.with_for_update() if lock else query).first()
        if not row:
            raise HTTPException(404, "Conversation not found.")
        return row

    @staticmethod
    def view(row):
        return {key: getattr(row, key) for key in ("id", "title", "language", "created_at", "updated_at")}

    def create(self, language):
        with Session(self.bind, expire_on_commit=False) as db:
            self.purge(db)
            if db.query(Conversation).filter(Conversation.user_id == self.user_id).count() >= 20:
                raise HTTPException(429, "Delete an older conversation before starting another.")
            row = Conversation(user_id=self.user_id, language=language)
            db.add(row)
            db.commit()
            return self.view(row)

    def purge(self, db):
        ids = [row.id for row in db.query(Conversation.id).filter(Conversation.user_id == self.user_id,
            Conversation.updated_at < now() - timedelta(days=settings.ai_support_retention_days)).all()]
        self.delete_ids(db, ids)

    @staticmethod
    def delete_ids(db, ids):
        if ids:
            db.query(Message).filter(Message.conversation_id.in_(ids)).delete(synchronize_session=False)
            db.query(Turn).filter(Turn.conversation_id.in_(ids)).delete(synchronize_session=False)
            db.query(Conversation).filter(Conversation.id.in_(ids)).delete(synchronize_session=False)

    def list(self):
        with Session(self.bind) as db:
            self.purge(db)
            db.commit()
            return [self.view(row) for row in db.query(Conversation).filter(Conversation.user_id == self.user_id)
                .order_by(Conversation.updated_at.desc()).limit(20).all()]

    def history(self, conversation_id):
        with Session(self.bind) as db:
            conversation = self.owned(db, conversation_id)
            rows = db.query(Message).filter(Message.conversation_id == conversation.id).order_by(Message.created_at, Message.id).limit(100).all()
            return {"conversation": self.view(conversation), "messages": [{"id": row.id, "sender": row.sender,
                "content": row.content, "answer": row.answer, "created_at": row.created_at} for row in rows]}

    def delete(self, conversation_id):
        with Session(self.bind) as db:
            row = self.owned(db, conversation_id, lock=True)
            if row.lease_until and aware(row.lease_until) > now():
                raise HTTPException(409, "Stop the current reply before deleting this conversation.")
            self.delete_ids(db, [row.id])
            db.commit()

    @staticmethod
    def response(message):
        return AIChatResponse(**AIAnswer.model_validate(message.answer).model_dump(),
            conversation_id=message.conversation_id, turn_id=message.turn_id, message_id=message.id)

    def begin(self, conversation_id, payload):
        with Session(self.bind, expire_on_commit=False) as db:
            conversation = self.owned(db, conversation_id, lock=True)
            turn = db.query(Turn).filter(Turn.conversation_id == conversation.id, Turn.client_message_id == payload.client_message_id).first()
            request_data = payload.model_dump(mode="json", exclude={"client_message_id", "message"})
            if turn:
                user_message = db.query(Message).filter(
                    Message.turn_id == turn.id, Message.sender == AIMessageSender.USER
                ).one()
                if user_message.content != payload.message or turn.tool_metadata.get("request") != request_data:
                    raise HTTPException(409, "This message identifier has already been used.")
                if turn.status == AITurnStatus.COMPLETE:
                    return {"cached": self.response(db.query(Message).filter(
                        Message.turn_id == turn.id, Message.sender == AIMessageSender.ASSISTANT
                    ).one())}
            elif db.query(Turn).filter(Turn.conversation_id == conversation.id).count() >= 50:
                raise HTTPException(422, "Start a new conversation to continue.")
            if not turn:
                count = db.query(Turn).join(Conversation).filter(Conversation.user_id == self.user_id,
                    Turn.created_at >= now() - timedelta(minutes=1)).count()
                if count >= 8:
                    raise HTTPException(429, "Please wait a minute before sending more messages.")
            turn_id = turn.id if turn else uuid.uuid4()
            leased = db.query(Conversation).filter(Conversation.id == conversation.id,
                or_(Conversation.lease_until.is_(None), Conversation.lease_until <= now())).update({
                    "lease_token": str(turn_id), "lease_until": now() + timedelta(seconds=settings.ai_support_turn_timeout_seconds + 15),
                }, synchronize_session=False)
            if not leased:
                raise HTTPException(409, "A reply is already being prepared in this conversation.")
            if not turn:
                turn = Turn(id=turn_id, conversation_id=conversation.id, client_message_id=payload.client_message_id,
                            tool_metadata={"request": request_data})
                db.add(turn)
                db.flush()
                db.add(Message(conversation_id=conversation.id, turn_id=turn.id,
                               sender=AIMessageSender.USER, content=payload.message))
            turn.status, turn.error_code = AITurnStatus.RUNNING, None
            history = [{"role": row.sender, "content": row.content[:400]} for row in db.query(Message)
                .filter(Message.conversation_id == conversation.id, Message.turn_id != turn.id)
                .order_by(Message.created_at.desc()).limit(settings.ai_support_max_history_messages).all()][::-1]
            last_turn = db.query(Turn).filter(
                Turn.conversation_id == conversation.id, Turn.status == AITurnStatus.COMPLETE
            )
            last_turn = last_turn.order_by(Turn.created_at.desc()).first()
            previous = last_turn.tool_metadata.get("plan", {}) if last_turn else {}
            previous_answer = db.query(Message).filter(
                Message.conversation_id == conversation.id, Message.sender == AIMessageSender.ASSISTANT
            )
            previous_answer = previous_answer.order_by(Message.created_at.desc()).first()
            language = previous_answer.answer.get("language", "en") if previous_answer and previous_answer.answer else "en"
            conversation.title = conversation.title if history else payload.message[:80]
            conversation.language, conversation.updated_at = payload.language, now()
            db.commit()
            return {"turn_id": turn_id, "history": history, "language": language, "previous": previous}

    def finish(self, conversation_id, turn_id, answer, plan):
        with Session(self.bind, expire_on_commit=False) as db:
            row = self.owned(db, conversation_id, lock=True)
            if row.lease_token != str(turn_id):
                raise HTTPException(409, "This reply has expired. Please retry.")
            turn = db.get(Turn, turn_id)
            turn.status = AITurnStatus.COMPLETE
            turn.tool_metadata = dict(turn.tool_metadata, plan=plan.model_dump(mode="json"))
            message = Message(conversation_id=row.id, turn_id=turn.id,
                              sender=AIMessageSender.ASSISTANT, content=answer.reply,
                              answer=answer.model_dump(mode="json"))
            db.add(message)
            row.updated_at = now()
            row.lease_token, row.lease_until = None, None
            db.commit()
            return self.response(message)

    def fail(self, conversation_id, turn_id, code):
        with Session(self.bind) as db:
            row = db.query(Conversation).filter(Conversation.id == conversation_id, Conversation.user_id == self.user_id,
                                               Conversation.lease_token == str(turn_id)).first()
            if row:
                turn = db.get(Turn, turn_id)
                turn.status = (
                    AITurnStatus.CANCELLED if code == "cancelled" else AITurnStatus.FAILED
                )
                turn.error_code = code
                row.lease_token, row.lease_until = None, None
                db.commit()
