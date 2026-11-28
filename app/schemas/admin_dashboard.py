"""Admin analytics contract. Monetary values are Decimal strings, never floats."""
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.manager_dashboard import StatusCount, TrendPoint
from app.services.table_availability import RESTAURANT_TIMEZONE


class AdminPeriodQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start_date: date | None = None
    end_date: date | None = None
    group_by: Literal["auto", "day", "week", "month", "year"] = "auto"

    @model_validator(mode="after")
    def validate_dates(self):
        if (self.start_date is None) != (self.end_date is None):
            raise ValueError("Provide both start_date and end_date.")
        if self.start_date is None:
            self.end_date = datetime.now(RESTAURANT_TIMEZONE).date()
            self.start_date = self.end_date - timedelta(days=29)
        if not date(2000, 1, 1) <= self.start_date <= self.end_date <= date(2099, 12, 31):
            raise ValueError("Use an ordered date range between 2000 and 2099.")
        days = (self.end_date - self.start_date).days + 1
        if days > 3660 or (self.group_by == "day" and days > 366):
            raise ValueError("Use at most 3660 days, or 366 days for daily charts.")
        return self


class AdminPeriod(BaseModel):
    start_date: date
    end_date: date
    timezone: str
    group_by: str


class RestaurantQuery(AdminPeriodQuery):
    q: str = Field(default="", max_length=120)
    sort: Literal["gross", "orders", "completed", "reservations", "commission", "outstanding", "name"] = "gross"
    direction: Literal["asc", "desc"] = "desc"
    page: int = Field(default=1, ge=1, le=100000)
    page_size: int = Field(default=10, ge=1, le=100)


class ActivityQuery(AdminPeriodQuery):
    page: int = Field(default=1, ge=1, le=100000)
    page_size: int = Field(default=10, ge=1, le=100)


class KPIs(BaseModel):
    restaurants: int
    active_restaurants: int
    pending_approvals: int
    customers: int
    managers: int
    reservations: int
    orders: int
    completed_orders: int
    gross: Decimal
    earned_commission: Decimal
    outstanding_commission: Decimal


class RevenuePoint(BaseModel):
    date: date
    gross: Decimal = Decimal(0)
    refunds: Decimal = Decimal(0)
    generated: Decimal = Decimal(0)
    reversed: Decimal = Decimal(0)
    collected: Decimal = Decimal(0)
    collection_reversals: Decimal = Decimal(0)
    earned: Decimal = Decimal(0)
    collected_net: Decimal = Decimal(0)
    outstanding: Decimal = Decimal(0)


class RevenueSummary(BaseModel):
    gross: Decimal
    refunds: Decimal
    net_transaction_value: Decimal
    generated: Decimal
    reversed: Decimal
    earned: Decimal
    collected: Decimal
    collection_reversals: Decimal
    collected_net: Decimal
    outstanding_start: Decimal
    outstanding_end: Decimal


class RevenueAnalytics(BaseModel):
    summary: RevenueSummary
    trend: list[RevenuePoint]


class OrderAnalytics(BaseModel):
    total: int
    pending: int
    preparing: int
    completed: int
    cancelled: int
    refunded: int
    statuses: list[StatusCount]
    trend: list[TrendPoint]


class ReservationAnalytics(BaseModel):
    total: int
    upcoming: int
    completed: int
    cancelled: int
    no_show: int
    statuses: list[StatusCount]
    trend: list[TrendPoint]


class RegistrationPoint(BaseModel):
    date: date
    new: int


class CustomerAnalytics(BaseModel):
    total: int
    new: int
    active: int
    repeat: int
    activity_definition: str
    trend: list[RegistrationPoint]


class PaymentChannel(BaseModel):
    method: str
    count: int
    amount: Decimal


class PaymentState(BaseModel):
    count: int
    amount: Decimal


class WalletBalance(BaseModel):
    role: str
    accounts: int
    balance: Decimal
    positive_balances: Decimal
    negative_balances: Decimal


class PaymentAnalytics(BaseModel):
    channels: list[PaymentChannel]
    pending: PaymentState
    failed: PaymentState
    unledgered_paid: PaymentState
    refunded: Decimal
    wallets: list[WalletBalance]
    commission_receivables: Decimal
    commission_settlements: Decimal
    bank_settlement_amount: Decimal | None = None
    bank_settlement_note: str


class RestaurantPerformance(BaseModel):
    id: str
    name: str
    manager_name: str
    status: str
    orders: int
    completed: int
    reservations: int
    gross: Decimal
    commission: Decimal
    outstanding: Decimal


class RestaurantPage(BaseModel):
    items: list[RestaurantPerformance]
    total: int
    page: int
    page_size: int
    period: AdminPeriod
    leaders: list[RestaurantPerformance]


class ActivityItem(BaseModel):
    id: str
    event_type: str
    title: str
    subject: str | None = None
    occurred_at: datetime


class ActivityPage(BaseModel):
    items: list[ActivityItem]
    total: int
    page: int
    page_size: int


class AdminDashboardResponse(BaseModel):
    admin_name: str
    as_of: datetime
    period: AdminPeriod
    kpis: KPIs
    revenue: RevenueAnalytics
    orders: OrderAnalytics
    reservations: ReservationAnalytics
    customers: CustomerAnalytics
    payments: PaymentAnalytics
    notes: list[str]
