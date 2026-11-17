from datetime import date, datetime
from decimal import Decimal
from typing import Literal
import uuid

from pydantic import BaseModel

from app.schemas.manager_ledger import LedgerExportQuery, LedgerQuery


class AdminLedgerEntry(BaseModel):
    id: str
    occurred_at: datetime
    kind: Literal["COLLECTION", "REFUND", "SETTLEMENT"]
    restaurant_id: uuid.UUID | None
    restaurant_name: str
    source_type: str
    reference: str
    gross: Decimal
    refunds: Decimal
    earned: Decimal
    collected_net: Decimal
    net: Decimal
    balance: Decimal


class AdminLedgerSummary(BaseModel):
    opening_balance: Decimal
    gross: Decimal
    refunds: Decimal
    generated: Decimal
    reversed: Decimal
    earned: Decimal
    collected: Decimal
    collection_reversals: Decimal
    collected_net: Decimal
    net: Decimal
    closing_balance: Decimal


class AdminLedgerReport(BaseModel):
    start_date: date
    end_date: date
    timezone: str = "Asia/Kolkata"
    currency: str = "INR"
    as_of: datetime
    summary: AdminLedgerSummary
    items: list[AdminLedgerEntry]
    total: int
    page: int
    page_size: int
