import uuid
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.models.restaurant import Restaurant, RestaurantStatus
from app.models.restaurant_hours import RestaurantHours, RestaurantHoursException
from app.models.reservation import Reservation
from app.models.user import User
from app.routers.auth.admin_auth import current_admin
from app.routers.auth.chef_auth import current_chef
from app.routers.auth.manager_auth import current_manager
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
RESTAURANT_TIMEZONE = ZoneInfo("Asia/Kolkata")


def restaurant_today() -> date:
    return datetime.now(RESTAURANT_TIMEZONE).date()


def restaurant_for_staff(staff: User, db: Session) -> Restaurant:
    manager_id = staff.manager_id or staff.id
    restaurant = db.query(Restaurant).filter(Restaurant.manager_id == manager_id).first()
    if restaurant is None:
        raise HTTPException(status_code=404, detail="No restaurant is linked to this account.")
    return restaurant

customer_router = APIRouter(prefix="/api/customer/restaurants", tags=["operating hours"])
manager_router = APIRouter(prefix="/api/manager/operating-hours", tags=["manager operating hours"])
chef_router = APIRouter(prefix="/api/chef/service-hours", tags=["chef service hours"])
admin_router = APIRouter(prefix="/api/admin/restaurants", tags=["admin operating hours"])


class Period(BaseModel):
    opens: time
    closes: time

    @field_validator("opens", "closes")
    @classmethod
    def local_minute(cls, value: time) -> time:
        if value.tzinfo is not None or value.second or value.microsecond:
            raise ValueError("Use local restaurant times with minute precision and no timezone offset.")
        return value

    @model_validator(mode="after")
    def valid_interval(self):
        if self.closes <= self.opens:
            raise ValueError("Closing time must be later than opening time; overnight periods are not supported.")
        return self


def clean_periods(periods: list[Period]) -> list[dict[str, str]]:
    ordered = sorted(periods, key=lambda item: item.opens)
    for previous, current in zip(ordered, ordered[1:]):
        if current.opens < previous.closes:
            raise HTTPException(status_code=422, detail="Operating periods may not overlap.")
    if len(ordered) > 4:
        raise HTTPException(status_code=422, detail="A day may have at most four operating periods.")
    return [{"opens": item.opens.strftime("%H:%M"), "closes": item.closes.strftime("%H:%M")} for item in ordered]


class DaySchedule(BaseModel):
    day: str
    periods: list[Period] = Field(default_factory=list, max_length=4)

    @field_validator("day")
    @classmethod
    def valid_day(cls, value: str) -> str:
        normalized = value.lower()
        if normalized not in WEEKDAYS:
            raise ValueError("Day must be a weekday name.")
        return normalized


class WeeklySchedule(BaseModel):
    days: list[DaySchedule] = Field(min_length=7, max_length=7)
    confirm_affected_reservations: bool = False

    @model_validator(mode="after")
    def all_unique_days(self):
        if {item.day for item in self.days} != set(WEEKDAYS):
            raise ValueError("Provide exactly one schedule for each weekday.")
        for day in self.days:
            clean_periods(day.periods)
        return self


class ExceptionPayload(BaseModel):
    start_date: date
    end_date: date
    is_closed: bool = False
    periods: list[Period] = Field(default_factory=list, max_length=4)
    reason: str | None = Field(default=None, max_length=500)
    internal_note: str | None = Field(default=None, max_length=1000)
    confirm_affected_reservations: bool = False

    @model_validator(mode="after")
    def validate_exception(self):
        if self.end_date < self.start_date:
            raise ValueError("End date must be on or after start date.")
        if (self.end_date - self.start_date).days > 366:
            raise ValueError("An exception may span at most 367 days.")
        if self.is_closed and self.periods:
            raise ValueError("Closed exceptions cannot include operating periods.")
        if not self.is_closed and not self.periods:
            raise ValueError("Provide operating periods or mark the exception as closed.")
        clean_periods(self.periods)
        return self


def periods_for_weekday(db: Session, restaurant_id: uuid.UUID, day: date) -> list[dict[str, str]]:
    row = db.query(RestaurantHours).filter(
        RestaurantHours.restaurant_id == restaurant_id,
        RestaurantHours.day_of_week == WEEKDAYS[day.weekday()],
    ).first()
    if row is None or not row.enabled:
        return []
    if row.periods is not None:
        return row.periods
    if row.open_time is None or row.close_time is None:
        return []
    return [{"opens": row.open_time.strftime("%H:%M"), "closes": row.close_time.strftime("%H:%M")}]


def effective_periods(db: Session, restaurant_id: uuid.UUID, day: date) -> tuple[list[dict[str, str]], RestaurantHoursException | None]:
    exception = db.query(RestaurantHoursException).filter(
        RestaurantHoursException.restaurant_id == restaurant_id,
        RestaurantHoursException.start_date <= day,
        RestaurantHoursException.end_date >= day,
    ).first()
    if exception:
        return ([] if exception.is_closed else (exception.periods or [])), exception
    return periods_for_weekday(db, restaurant_id, day), None


def serialize_periods(periods: list[dict]) -> list[dict[str, str]]:
    return [{"opens": str(item["opens"])[:5], "closes": str(item["closes"])[:5]} for item in periods]


def serialize_exception(row: RestaurantHoursException, include_internal: bool = True) -> dict:
    result = {"id": str(row.id), "start_date": row.start_date.isoformat(), "end_date": row.end_date.isoformat(),
              "is_closed": row.is_closed, "periods": serialize_periods(row.periods or []), "reason": row.reason,
              "created_at": row.created_at.isoformat() if row.created_at else None,
              "updated_at": row.updated_at.isoformat() if row.updated_at else None}
    if include_internal:
        result["internal_note"] = row.internal_note
        result["created_by"] = str(row.created_by) if row.created_by else None
    return result


def periods_contain(periods: list[dict], start_time: time, end_time: time) -> bool:
    start = start_time.hour * 60 + start_time.minute
    end = end_time.hour * 60 + end_time.minute
    if end < start:
        end += 24 * 60
    for item in periods:
        opens = time.fromisoformat(item["opens"])
        closes = time.fromisoformat(item["closes"])
        opening = opens.hour * 60 + opens.minute
        closing = closes.hour * 60 + closes.minute
        if closing <= opening:
            closing += 24 * 60
        if opening <= start and end <= closing:
            return True
    return False


def affected_reservations(db: Session, restaurant_id: uuid.UUID, schedule_by_day: dict[str, list[dict]] | None = None,
                          exception_start: date | None = None, exception_end: date | None = None,
                          exception_periods: list[dict] | None = None) -> list[Reservation]:
    query = db.query(Reservation).filter(Reservation.restaurant_id == restaurant_id,
        Reservation.status.in_(("PENDING_PAYMENT", "CONFIRMED", "SEATED")),
        Reservation.reservation_date >= restaurant_today()).order_by(Reservation.reservation_date, Reservation.start_time)
    affected = []
    for reservation in query.all():
        if exception_start and exception_end and exception_start <= reservation.reservation_date <= exception_end:
            if exception_periods is not None:
                periods = exception_periods
            elif schedule_by_day is not None:
                periods = schedule_by_day[WEEKDAYS[reservation.reservation_date.weekday()]]
            else:
                periods = periods_for_weekday(db, restaurant_id, reservation.reservation_date)
        elif schedule_by_day is not None:
            override = db.query(RestaurantHoursException).filter(
                RestaurantHoursException.restaurant_id == restaurant_id,
                RestaurantHoursException.start_date <= reservation.reservation_date,
                RestaurantHoursException.end_date >= reservation.reservation_date,
            ).first()
            periods = (override.periods or []) if override and not override.is_closed else ([] if override else schedule_by_day[WEEKDAYS[reservation.reservation_date.weekday()]])
        else:
            continue
        if not periods_contain(periods, reservation.start_time, reservation.end_time):
            affected.append(reservation)
    return affected


def affected_by_exception_update(db: Session, restaurant_id: uuid.UUID, old_start: date, old_end: date,
                                 new_start: date, new_end: date, new_periods: list[dict]) -> list[Reservation]:
    start = min(old_start, new_start)
    end = max(old_end, new_end)
    rows = db.query(Reservation).filter(Reservation.restaurant_id == restaurant_id,
        Reservation.status.in_(("PENDING_PAYMENT", "CONFIRMED", "SEATED")),
        Reservation.reservation_date >= max(start, restaurant_today()),
        Reservation.reservation_date <= end).order_by(Reservation.reservation_date, Reservation.start_time).all()
    affected = []
    for reservation in rows:
        if new_start <= reservation.reservation_date <= new_end:
            periods = new_periods
        elif old_start <= reservation.reservation_date <= old_end:
            periods = periods_for_weekday(db, restaurant_id, reservation.reservation_date)
        else:
            continue
        if not periods_contain(periods, reservation.start_time, reservation.end_time):
            affected.append(reservation)
    return affected


def schedule_data(db: Session, restaurant_id: uuid.UUID, include_history: bool = False) -> dict:
    rows = db.query(RestaurantHours).filter(RestaurantHours.restaurant_id == restaurant_id).all()
    by_day = {row.day_of_week.lower(): row for row in rows}
    days = []
    for weekday in WEEKDAYS:
        row = by_day.get(weekday)
        if row is None or not row.enabled:
            periods = []
        else:
            periods = row.periods if row.periods is not None else ([{"opens": row.open_time.strftime("%H:%M"), "closes": row.close_time.strftime("%H:%M")}] if row.open_time and row.close_time else [])
        days.append({"day": weekday, "periods": serialize_periods(periods),
                     "updated_at": row.updated_at.isoformat() if row and row.updated_at else None,
                     "updated_by": str(row.updated_by) if row and row.updated_by else None})
    exception_query = db.query(RestaurantHoursException).filter(RestaurantHoursException.restaurant_id == restaurant_id)
    if not include_history:
        exception_query = exception_query.filter(RestaurantHoursException.end_date >= restaurant_today())
    exceptions = exception_query.order_by(RestaurantHoursException.start_date.desc() if include_history else RestaurantHoursException.start_date).all()
    return {"restaurant_id": str(restaurant_id), "timezone": "Asia/Kolkata", "days": days,
            "exceptions": [serialize_exception(row) for row in exceptions]}


def ensure_no_exception_conflict(db: Session, restaurant_id: uuid.UUID, start_date: date, end_date: date, exclude_id: uuid.UUID | None = None) -> None:
    query = db.query(RestaurantHoursException.id).filter(
        RestaurantHoursException.restaurant_id == restaurant_id,
        RestaurantHoursException.start_date <= end_date,
        RestaurantHoursException.end_date >= start_date,
    )
    if exclude_id:
        query = query.filter(RestaurantHoursException.id != exclude_id)
    if query.first():
        raise HTTPException(status_code=409, detail="This date range overlaps an existing schedule exception.")


@customer_router.get("/{restaurant_id}/operating-hours")
def customer_operating_hours(restaurant_id: uuid.UUID, day: date | None = Query(default=None), db: Session = Depends(get_db)) -> dict:
    restaurant = db.query(Restaurant).filter(Restaurant.id == restaurant_id, Restaurant.status == RestaurantStatus.APPROVED).first()
    if restaurant is None:
        raise HTTPException(status_code=404, detail="Restaurant not found.")
    if day:
        periods, exception = effective_periods(db, restaurant_id, day)
        return {"restaurant_id": str(restaurant_id), "date": day.isoformat(), "timezone": "Asia/Kolkata",
                "is_open": bool(periods), "periods": serialize_periods(periods), "exception": bool(exception),
                "exception_reason": exception.reason if exception else None}
    schedule = schedule_data(db, restaurant_id)
    schedule["exceptions"] = [serialize_exception(row, include_internal=False) for row in db.query(RestaurantHoursException).filter(
        RestaurantHoursException.restaurant_id == restaurant_id, RestaurantHoursException.end_date >= restaurant_today(),
    ).order_by(RestaurantHoursException.start_date).all()]
    for exception in schedule["exceptions"]:
        exception.pop("created_at", None)
        exception.pop("updated_at", None)
    return {"restaurant_id": str(restaurant_id), "timezone": "Asia/Kolkata", "days": schedule["days"], "exceptions": schedule["exceptions"]}


@manager_router.get("")
def manager_schedule(manager: User = Depends(current_manager), db: Session = Depends(get_db)) -> dict:
    restaurant = restaurant_for_staff(manager, db)
    return schedule_data(db, restaurant.id)


@manager_router.put("")
def update_manager_schedule(payload: WeeklySchedule, manager: User = Depends(current_manager), db: Session = Depends(get_db)) -> dict:
    restaurant = restaurant_for_staff(manager, db)
    proposed = {item.day: clean_periods(item.periods) for item in payload.days}
    affected = affected_reservations(db, restaurant.id, schedule_by_day=proposed)
    if affected and not payload.confirm_affected_reservations:
        summary = ", ".join(f"{item.id} ({item.reservation_date.isoformat()} {item.start_time.strftime('%H:%M')})" for item in affected[:5])
        raise HTTPException(status_code=409, detail=f"This change affects {len(affected)} existing reservation(s): {summary}{' …' if len(affected) > 5 else ''}. Review and confirm to apply; reservations will remain unchanged.")
    existing = {row.day_of_week.lower(): row for row in db.query(RestaurantHours).filter(RestaurantHours.restaurant_id == restaurant.id).all()}
    for item in payload.days:
        row = existing.get(item.day)
        if row is None:
            row = RestaurantHours(restaurant_id=restaurant.id, day_of_week=item.day)
            db.add(row)
        periods = clean_periods(item.periods)
        row.enabled = bool(periods)
        row.periods = periods
        row.updated_by = manager.id
        # Preserve compatibility with existing profile/admin views and clients.
        row.open_time = time.fromisoformat(periods[0]["opens"]) if periods else None
        row.close_time = time.fromisoformat(periods[-1]["closes"]) if periods else None
    db.commit()
    return schedule_data(db, restaurant.id)


@manager_router.post("/exceptions", status_code=status.HTTP_201_CREATED)
def create_manager_exception(payload: ExceptionPayload, manager: User = Depends(current_manager), db: Session = Depends(get_db)) -> dict:
    restaurant = restaurant_for_staff(manager, db)
    ensure_no_exception_conflict(db, restaurant.id, payload.start_date, payload.end_date)
    affected = affected_reservations(db, restaurant.id, exception_start=payload.start_date,
        exception_end=payload.end_date, exception_periods=[] if payload.is_closed else clean_periods(payload.periods))
    if affected and not payload.confirm_affected_reservations:
        summary = ", ".join(f"{item.id} ({item.reservation_date.isoformat()} {item.start_time.strftime('%H:%M')})" for item in affected[:5])
        raise HTTPException(status_code=409, detail=f"This exception affects {len(affected)} existing reservation(s): {summary}{' …' if len(affected) > 5 else ''}. Review and confirm to apply; reservations will remain unchanged.")
    row = RestaurantHoursException(restaurant_id=restaurant.id, created_by=manager.id,
        start_date=payload.start_date, end_date=payload.end_date, is_closed=payload.is_closed,
        periods=clean_periods(payload.periods), reason=payload.reason, internal_note=payload.internal_note)
    db.add(row)
    db.commit()
    db.refresh(row)
    return serialize_exception(row)


@manager_router.put("/exceptions/{exception_id}")
def update_manager_exception(exception_id: uuid.UUID, payload: ExceptionPayload, manager: User = Depends(current_manager), db: Session = Depends(get_db)) -> dict:
    restaurant = restaurant_for_staff(manager, db)
    row = db.query(RestaurantHoursException).filter(RestaurantHoursException.id == exception_id, RestaurantHoursException.restaurant_id == restaurant.id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Schedule exception not found.")
    ensure_no_exception_conflict(db, restaurant.id, payload.start_date, payload.end_date, exception_id)
    affected = affected_by_exception_update(db, restaurant.id, row.start_date, row.end_date,
        payload.start_date, payload.end_date, [] if payload.is_closed else clean_periods(payload.periods))
    if affected and not payload.confirm_affected_reservations:
        summary = ", ".join(f"{item.id} ({item.reservation_date.isoformat()} {item.start_time.strftime('%H:%M')})" for item in affected[:5])
        raise HTTPException(status_code=409, detail=f"This exception affects {len(affected)} existing reservation(s): {summary}{' …' if len(affected) > 5 else ''}. Review and confirm to apply; reservations will remain unchanged.")
    for key in ("start_date", "end_date", "is_closed", "reason", "internal_note"):
        setattr(row, key, getattr(payload, key))
    row.periods = clean_periods(payload.periods)
    db.commit()
    db.refresh(row)
    return serialize_exception(row)


@manager_router.delete("/exceptions/{exception_id}")
def delete_manager_exception(exception_id: uuid.UUID, confirm_affected_reservations: bool = Query(default=False), manager: User = Depends(current_manager), db: Session = Depends(get_db)) -> dict:
    restaurant = restaurant_for_staff(manager, db)
    row = db.query(RestaurantHoursException).filter(RestaurantHoursException.id == exception_id, RestaurantHoursException.restaurant_id == restaurant.id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Schedule exception not found.")
    affected = affected_reservations(db, restaurant.id, exception_start=row.start_date, exception_end=row.end_date)
    if affected and not confirm_affected_reservations:
        summary = ", ".join(f"{item.id} ({item.reservation_date.isoformat()} {item.start_time.strftime('%H:%M')})" for item in affected[:5])
        raise HTTPException(status_code=409, detail=f"Removing this exception affects {len(affected)} existing reservation(s): {summary}{' …' if len(affected) > 5 else ''}. Confirm removal to proceed; reservations will remain unchanged.")
    db.delete(row)
    db.commit()
    return {"message": "Schedule exception deleted."}


@chef_router.get("")
def chef_schedule(chef: User = Depends(current_chef), db: Session = Depends(get_db)) -> dict:
    restaurant = restaurant_for_staff(chef, db)
    data = schedule_data(db, restaurant.id)
    data["exceptions"] = [row for row in data["exceptions"] if row["end_date"] >= restaurant_today().isoformat()]
    for row in data["exceptions"]:
        row.pop("internal_note", None)
    today = restaurant_today()
    effective, exception = effective_periods(db, restaurant.id, today)
    data["today"] = {"date": today.isoformat(), "is_open": bool(effective), "periods": serialize_periods(effective), "is_exception": bool(exception)}
    return data


@admin_router.get("/{restaurant_id}/operating-hours")
def admin_schedule(restaurant_id: uuid.UUID, day: date | None = Query(default=None), admin: User = Depends(current_admin), db: Session = Depends(get_db)) -> dict:
    restaurant = db.query(Restaurant).filter(Restaurant.id == restaurant_id).first()
    if restaurant is None:
        raise HTTPException(status_code=404, detail="Restaurant not found.")
    result = schedule_data(db, restaurant.id, include_history=True)
    if day:
        periods, exception = effective_periods(db, restaurant.id, day)
        result["effective"] = {"date": day.isoformat(), "is_open": bool(periods), "periods": serialize_periods(periods),
                               "is_exception": bool(exception), "exception_id": str(exception.id) if exception else None}
    result["restaurant_name"] = restaurant.name
    return result


@admin_router.get("/{restaurant_id}/availability-inspection")
def admin_availability_inspection(restaurant_id: uuid.UUID, reservation_date: date, start_time: time,
                                  number_of_guests: int = Query(default=2, ge=1, le=16),
                                  admin: User = Depends(current_admin), db: Session = Depends(get_db)) -> dict:
    restaurant = db.query(Restaurant).filter(Restaurant.id == restaurant_id).first()
    if restaurant is None:
        raise HTTPException(status_code=404, detail="Restaurant not found.")
    end_time = (datetime.combine(reservation_date, start_time) + timedelta(minutes=90)).time()
    periods, exception = effective_periods(db, restaurant_id, reservation_date)
    result = {"restaurant_id": str(restaurant_id), "date": reservation_date.isoformat(),
              "start_time": start_time.strftime("%H:%M"), "end_time": end_time.strftime("%H:%M"),
              "timezone": "Asia/Kolkata", "effective_periods": serialize_periods(periods),
              "exception_id": str(exception.id) if exception else None,
              "exception_reason": exception.reason if exception else None, "available": False, "reason": None, "tables": []}
    if not periods_contain(periods, start_time, end_time):
        result["reason"] = "The requested time is outside the effective operating periods."
        return result
    if reservation_date <= restaurant_today():
        result["reason"] = "New reservations must be made at least one day in advance."
        return result
    from app.routers.restaurants.reservations import suitable_table_groups, table_group_data
    groups = suitable_table_groups(db, restaurant_id, reservation_date, start_time, number_of_guests)
    result["available"] = bool(groups)
    result["tables"] = [table_group_data(group) for group in groups]
    if not groups:
        result["reason"] = "No suitable table group is free for the requested duration and party size."
    return result
