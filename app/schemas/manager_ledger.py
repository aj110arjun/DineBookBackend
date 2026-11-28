from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.payment_values import LedgerEntryType
from app.services.table_availability import RESTAURANT_TIMEZONE


class LedgerQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start_date: date | None = None
    end_date: date | None = None
    page: int = Field(default=1, ge=1, le=1_000_000)
    page_size: int = Field(default=20, ge=1, le=100)

    @model_validator(mode="after")
    def period(self):
        if (self.start_date is None) != (self.end_date is None):
            raise ValueError("Provide both start and end dates.")
        if self.start_date is None:
            self.end_date = datetime.now(RESTAURANT_TIMEZONE).date()
            self.start_date = self.end_date - timedelta(days=29)
        if not date(2000, 1, 1) <= self.start_date <= self.end_date <= date(2099, 12, 31):
            raise ValueError("Choose an ordered date range between 2000 and 2099.")
        if (self.end_date - self.start_date).days >= 3660:
            raise ValueError("Choose a range of at most 3660 days.")
        return self


class LedgerExportQuery(LedgerQuery):
    format: Literal["pdf", "xlsx", "csv"]


class LedgerEntry(BaseModel):
    id: str
    occurred_at: datetime
    kind: LedgerEntryType
    source_type: str
    reference: str
    collected: Decimal
    refunded: Decimal
    commission: Decimal
    net: Decimal
    balance: Decimal


class LedgerSummary(BaseModel):
    opening_balance: Decimal
    collected: Decimal
    refunded: Decimal
    commission: Decimal
    net: Decimal
    closing_balance: Decimal


class LedgerReport(BaseModel):
    restaurant_name: str
    start_date: date
    end_date: date
    timezone: str = "Asia/Kolkata"
    currency: str = "INR"
    as_of: datetime
    summary: LedgerSummary
    items: list[LedgerEntry]
    total: int
    page: int
    page_size: int
