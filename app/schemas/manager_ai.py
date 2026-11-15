"""Fixed read-only operations; restaurant ownership never comes from the request."""
import uuid
from typing import Literal

from pydantic import Field, field_validator

from app.schemas.ai_chat import AIAnswer, AIChatRequest, StrictModel

ManagerAIAction = Literal["overview", "reservations", "orders", "payments", "revenue", "wallet", "tables", "help", "support"]
ManagerAIPeriod = Literal["today", "yesterday", "last_7_days", "this_month"]


class ManagerAIPlan(StrictModel):
    action: ManagerAIAction = "help"
    period: ManagerAIPeriod | None = None
    state: Literal["active", "all", "completed", "cancelled"] = "active"


class ManagerAIRequest(AIChatRequest):
    action: ManagerAIAction | None = None
    language: Literal["auto", "en"] = "en"
    # A caller cannot select any restaurant or provide model-generated facts.
    restaurant_id: None = None
    context: ManagerAIPlan | None = None
    history: list[str] = Field(default_factory=list, max_length=6)

    @field_validator("history")
    @classmethod
    def clean_history(cls, value):
        return [AIChatRequest(message=message).message for message in value]


class ManagerAIResponse(AIAnswer):
    client_message_id: uuid.UUID
    message_id: uuid.UUID
    restaurant_id: uuid.UUID
    context: ManagerAIPlan
