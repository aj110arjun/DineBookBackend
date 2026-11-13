"""Kitchen-only projections; customer identities and payment data are excluded."""
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field


class ChefDashboardQuery(BaseModel):
    period: Literal["today", "last7", "last30"] = "today"
    status: Literal["ACTIVE", "PLACED", "ACCEPTED", "PREPARING", "READY"] = "ACTIVE"
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=8, ge=1, le=50)


class KitchenItem(BaseModel):
    food_name: str
    variant_name: str | None = None
    quantity: int


class KitchenTable(BaseModel):
    id: str
    table_number: str
    floor_name: str | None = None


class KitchenOrder(BaseModel):
    id: str
    status: str
    service_type: str
    created_at: datetime
    accepted_at: datetime | None
    preparation_started_at: datetime | None
    ready_at: datetime | None
    elapsed_seconds: int
    age_rank: int
    items: list[KitchenItem]
    item_count: int
    tables: list[KitchenTable]
    customer_note: str | None
    allowed_actions: list[str]


class KitchenQueue(BaseModel):
    items: list[KitchenOrder]
    total: int
    page: int
    page_size: int


class KitchenReservation(BaseModel):
    id: str
    arrival_at: datetime
    number_of_guests: int
    status: str
    tables: list[KitchenTable]
    preorder_items: list[KitchenItem]


class KitchenSummary(BaseModel):
    today_orders: int
    yesterday_orders: int
    orders_change_percent: float | None
    pending_orders: int
    preparing_orders: int
    ready_orders: int
    completed_orders: int
    average_preparation_minutes: float | None


class VolumePoint(BaseModel):
    date: str
    total: int


class KitchenStatusCount(BaseModel):
    status: str
    count: int


class PreparationPoint(BaseModel):
    date: date
    prepared_orders: int
    measured_orders: int
    average_minutes: float | None


class KitchenPerformance(BaseModel):
    prepared_today: int
    measured_today: int
    average_preparation_minutes: float | None
    longest_active_seconds: int | None
    delayed_orders: int | None
    delay_threshold_minutes: int | None


class KitchenActivity(BaseModel):
    id: str
    order_id: str
    status: str
    description: str
    occurred_at: datetime


class ChefDashboardResponse(BaseModel):
    as_of: datetime
    timezone: str
    today: date
    start_date: date
    end_date: date
    group_by: Literal["hour", "day"]
    summary: KitchenSummary
    order_volume: list[VolumePoint]
    status_distribution: list[KitchenStatusCount]
    preparation_performance: list[PreparationPoint]
    active_orders: KitchenQueue
    upcoming_reservations: list[KitchenReservation]
    performance: KitchenPerformance
    recent_activity: list[KitchenActivity]
    notes: list[str]
