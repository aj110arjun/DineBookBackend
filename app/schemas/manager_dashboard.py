"""Bounded, restaurant-local dashboard contract. Money is serialized as decimals."""
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.services.table_availability import RESTAURANT_TIMEZONE


class DashboardQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_date: date | None = None
    end_date: date | None = None
    group_by: Literal["auto", "day", "week", "month", "year"] = "auto"
    popular_sort: Literal["quantity", "revenue"] = "quantity"

    @model_validator(mode="after")
    def validate_period(self):
        if (self.start_date is None) != (self.end_date is None):
            raise ValueError("Provide both start_date and end_date.")
        if self.start_date is None:
            self.end_date = datetime.now(RESTAURANT_TIMEZONE).date()
            self.start_date = self.end_date - timedelta(days=29)
        if not date(2000, 1, 1) <= self.start_date <= self.end_date <= date(2099, 12, 31):
            raise ValueError("Use an ordered date range between 2000 and 2099.")
        days = (self.end_date - self.start_date).days + 1
        if days > 3660:
            raise ValueError("Choose a range of at most 3660 days.")
        if self.group_by == "day" and days > 366:
            raise ValueError("Daily grouping supports at most 366 days; choose a larger interval.")
        return self


class Period(BaseModel):
    start_date: date
    end_date: date
    previous_start_date: date
    previous_end_date: date
    timezone: str
    group_by: str


class Metric(BaseModel):
    value: Decimal
    previous: Decimal
    change_percent: Decimal | None


class Overview(BaseModel):
    revenue: Metric
    orders: Metric
    reservations: Metric
    customers: Metric
    average_order_value: Metric
    rating: Metric


class StatusCount(BaseModel):
    status: str
    count: int


class TrendPoint(BaseModel):
    date: date
    total: int = 0
    completed: int = 0
    pending: int = 0
    cancelled: int = 0


class RevenuePoint(BaseModel):
    date: date
    gross: Decimal = Decimal(0)
    refunds: Decimal = Decimal(0)
    commissions: Decimal = Decimal(0)
    revenue: Decimal = Decimal(0)
    net: Decimal = Decimal(0)


class RevenueSummary(BaseModel):
    gross: Decimal
    refunds: Decimal
    commissions: Decimal
    revenue: Decimal
    net: Decimal


class PopularItem(BaseModel):
    key: str
    name: str
    quantity: int
    revenue: Decimal


class CustomerPoint(BaseModel):
    date: date
    total: int
    new: int
    returning: int


class CustomerSummary(BaseModel):
    new: int
    returning: int
    retention_percent: Decimal
    trend: list[CustomerPoint]


class PaymentMethod(BaseModel):
    method: str
    count: int
    amount: Decimal


class Peak(BaseModel):
    label: str
    count: int


class Operations(BaseModel):
    order_status: list[StatusCount]
    reservation_status: list[StatusCount]
    order_trend: list[TrendPoint]
    reservation_trend: list[TrendPoint]
    order_completion_percent: Decimal
    reservation_completion_percent: Decimal
    average_party_size: Decimal
    expected_guests: int
    peak_order_hours: list[Peak]
    peak_reservation_hours: list[Peak]
    peak_reservation_days: list[Peak]


class TableSummary(BaseModel):
    total: int
    occupied: int
    available: int
    reserved: int
    maintenance: int
    occupancy_percent: Decimal


class LiveTable(BaseModel):
    id: str
    name: str
    floor: str
    seats: int
    status: str


class UpcomingReservation(BaseModel):
    id: str
    date: date
    time: str
    guests: int
    status: str


class ActiveOrder(BaseModel):
    id: str
    status: str
    amount: Decimal
    created_at: datetime


class LiveData(BaseModel):
    as_of: datetime
    tables: TableSummary
    table_preview: list[LiveTable]
    upcoming_reservations: list[UpcomingReservation]
    active_orders: list[ActiveOrder]


class RecentReview(BaseModel):
    id: str
    rating: int
    content: str
    created_at: datetime


class ReviewSummary(BaseModel):
    total: int
    average: Decimal
    distribution: list[StatusCount]
    recent: list[RecentReview]


class DashboardResponse(BaseModel):
    manager_name: str
    restaurant_name: str
    period: Period
    overview: Overview
    revenue: RevenueSummary
    revenue_chart: list[RevenuePoint]
    operations: Operations
    popular_items: list[PopularItem]
    customers: CustomerSummary
    payment_methods: list[PaymentMethod]
    reviews: ReviewSummary
    live: LiveData
    notes: list[str] = Field(default_factory=list)
