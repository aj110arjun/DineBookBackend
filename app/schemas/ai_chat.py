
import re
import uuid
from datetime import date, datetime, time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.customer_ai import AIMessageSender

Language = Literal["auto", "en", "ml", "hi"]
Action = Literal["faq", "reservations", "orders", "wallet", "payments", "refunds", "restaurants", "restaurant", "hours", "availability", "offers", "support"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AIConversationCreate(StrictModel):
    language: Language = "auto"


class AIChatRequest(StrictModel):
    message: str = Field(min_length=1, max_length=1000)
    client_message_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    language: Language = "auto"
    action: Action | None = None
    restaurant_id: uuid.UUID | None = None

    @field_validator("message")
    @classmethod
    def clean_message(cls, value: str) -> str:
        value = value.strip()
        if not value or len(value.encode("utf-8")) > 3000:
            raise ValueError("Please send a shorter message.")
        if re.search(r"eyJ[\w-]+\.[\w-]+\.[\w-]+|\b\d{13,19}\b|(?:password|otp|secret)\s*[:=]\s*\S+", value, re.I):
            raise ValueError("Do not include passwords, OTPs, card numbers, or access tokens.")
        return value


class AIPlan(StrictModel):
    action: Action = "faq"
    resource_id: uuid.UUID | None = None
    source: Literal["reservation", "order", "wallet"] = "reservation"
    restaurant_id: uuid.UUID | None = None
    query: str = Field(default="", max_length=100)
    day: date | None = None
    start: time | None = None
    guests: int = Field(default=2, ge=1, le=16)
    duration_minutes: int = Field(default=90, ge=30, le=180, multiple_of=30)

    @field_validator("start")
    @classmethod
    def local_time(cls, value):
        if value is not None and (value.tzinfo or value.second or value.microsecond):
            raise ValueError("Use local hours and minutes.")
        return value


class AISource(StrictModel):
    id: str
    title: str
    href: str


class AICard(StrictModel):
    title: str
    kind: str
    fields: dict[str, str]
    href: str
    restaurant_id: str | None = None


class AIAnswer(StrictModel):
    reply: str
    language: Literal["en", "ml", "hi"]
    cards: list[AICard] = Field(default_factory=list)
    sources: list[AISource] = Field(default_factory=list)
    escalation: bool = False
    limited: bool = False
    as_of: datetime


class AIChatResponse(AIAnswer):
    conversation_id: uuid.UUID
    turn_id: uuid.UUID
    message_id: uuid.UUID


class AIConversationView(StrictModel):
    id: uuid.UUID
    title: str
    language: Language
    created_at: datetime
    updated_at: datetime


class AIMessageView(StrictModel):
    id: uuid.UUID
    sender: AIMessageSender
    content: str
    created_at: datetime
    answer: AIAnswer | None = None
