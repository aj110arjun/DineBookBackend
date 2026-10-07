import uuid
from itertools import combinations
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session, joinedload

from app.db.database import get_db
from app.models.dining import DiningTable, RestaurantFloor
from app.models.menu import Category, Food, FoodVariant
from app.models.reservation import Reservation, ReservationTable
from app.models.restaurant import Restaurant, RestaurantStatus
from app.models.restaurant_hours import RestaurantHours
from app.models.user import User
from app.routers.customer_auth import current_customer
from app.routers.chef_auth import current_chef
from app.routers.manager_auth import current_manager

customer_router = APIRouter(prefix="/api/customer/reservations", tags=["reservations"])
manager_router = APIRouter(prefix="/api/manager/reservations", tags=["manager reservations"])
chef_router = APIRouter(prefix="/api/chef/reservations", tags=["chef reservations"])
BLOCKING = ("PENDING_PAYMENT", "CONFIRMED", "SEATED")
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


class PreorderItem(BaseModel):
    variant_id: uuid.UUID
    quantity: int = Field(ge=1, le=99)


class ManagerPreorderItem(BaseModel):
    variant_id: uuid.UUID
    quantity: int = Field(ge=1, le=99)


class ReservationCreate(BaseModel):
    restaurant_id: uuid.UUID
    reservation_date: date
    start_time: time
    number_of_guests: int = Field(ge=1, le=16)
    table_id: uuid.UUID | None = None
    table_ids: list[uuid.UUID] | None = Field(default=None, min_length=1, max_length=2)
    special_request: str | None = Field(default=None, max_length=1000)
    fulfillment_type: str = Field(default="TABLE_ONLY", pattern="^(TABLE_ONLY|PREORDER)$")
    payment_method: str = Field(default="PAY_AT_DESK", pattern="^PAY_AT_DESK$")
    preorder_items: list[PreorderItem] = Field(default_factory=list, max_length=50)


def restaurant_for_staff(staff: User, db: Session) -> Restaurant:
    restaurant = db.query(Restaurant).filter(Restaurant.manager_id == staff.manager_id if staff.manager_id else Restaurant.manager_id == staff.id).first()
    if restaurant is None:
        raise HTTPException(status_code=404, detail="No restaurant is linked to this account.")
    return restaurant


def reservation_data(item: Reservation, db: Session) -> dict:
    customer = db.query(User).filter(User.id == item.user_id).first()
    restaurant = db.query(Restaurant).filter(Restaurant.id == item.restaurant_id).first()
    assignments = db.query(ReservationTable).options(joinedload(ReservationTable.table).joinedload(DiningTable.floor)).filter(ReservationTable.reservation_id == item.id).all()
    return {
        "id": str(item.id), "restaurant_id": str(item.restaurant_id), "restaurant_name": restaurant.name if restaurant else "Restaurant",
        "restaurant_location": ", ".join(part for part in (restaurant.city, restaurant.state) if part) if restaurant else None,
        "customer_name": customer.name if customer else "Customer",
        "customer_email": customer.email if customer else None, "reservation_date": item.reservation_date.isoformat(),
        "start_time": item.start_time.strftime("%H:%M"), "end_time": item.end_time.strftime("%H:%M"),
        "number_of_guests": item.number_of_guests, "status": item.status,
        "payment_status": item.payment_status, "fee_amount": float(item.fee_amount),
        "fulfillment_type": item.fulfillment_type, "payment_method": item.payment_method,
        "preorder_items": item.preorder_items or [], "preorder_total": float(item.preorder_total or 0),
        "special_request": item.special_request,
        "tables": [{"id": str(a.table.id), "table_number": a.table.table_number,
                    "capacity": a.table.capacity, "floor_id": str(a.table.floor_id),
                    "floor_name": a.table.floor.floor_name} for a in assignments],
    }


def overlapping(db: Session, table_id: uuid.UUID, day: date, start: time, end: time) -> bool:
    return db.query(Reservation.id).join(ReservationTable).filter(
        ReservationTable.table_id == table_id,
        Reservation.reservation_date == day,
        Reservation.status.in_(BLOCKING),
        Reservation.start_time < end,
        Reservation.end_time > start,
    ).first() is not None


def within_operating_hours(db: Session, restaurant_id: uuid.UUID, day: date, start: time, end: time) -> bool:
    hours = db.query(RestaurantHours).filter(
        RestaurantHours.restaurant_id == restaurant_id,
        RestaurantHours.day_of_week == WEEKDAYS[day.weekday()],
    ).first()
    if hours is None or not hours.enabled or hours.open_time is None or hours.close_time is None:
        return False
    opens = hours.open_time.hour * 60 + hours.open_time.minute
    closes = hours.close_time.hour * 60 + hours.close_time.minute
    starts = start.hour * 60 + start.minute
    ends = end.hour * 60 + end.minute
    if closes <= opens:
        closes += 24 * 60
    if ends < starts:
        ends += 24 * 60
    return opens <= starts and ends <= closes


def suitable_table_groups(db: Session, restaurant_id: uuid.UUID, day: date, start: time, guests: int, lock: bool = False) -> list[tuple[DiningTable, ...]]:
    end = (datetime.combine(day, start) + timedelta(hours=1, minutes=30)).time()
    query = db.query(DiningTable).join(RestaurantFloor, DiningTable.floor_id == RestaurantFloor.id).filter(
        DiningTable.restaurant_id == restaurant_id, DiningTable.status == "available",
        RestaurantFloor.is_active.is_(True),
    ).order_by(DiningTable.capacity, DiningTable.reservation_fee, DiningTable.table_number)
    if lock:
        query = query.with_for_update()
    candidates = [table for table in query.all() if not overlapping(db, table.id, day, start, end)]
    if guests <= 2:
        groups = [(table,) for table in candidates if table.capacity >= guests]
    elif guests <= 6:
        long_tables = [table for table in candidates if getattr(table.table_type, "value", table.table_type) == "rectangle"]
        groups = [(table,) for table in long_tables if table.capacity >= guests]
        groups.extend(
            pair for pair in combinations(candidates, 2)
            if pair[0].floor_id == pair[1].floor_id
            and any(getattr(table.table_type, "value", table.table_type) == "rectangle" for table in pair)
            and sum(table.capacity for table in pair) >= guests
        )
    else:
        long_tables = [table for table in candidates if getattr(table.table_type, "value", table.table_type) == "rectangle"]
        groups = [pair for pair in combinations(long_tables, 2)
            if pair[0].floor_id == pair[1].floor_id and sum(table.capacity for table in pair) >= guests]
    return sorted(groups, key=lambda group: (len(group), sum(table.reservation_fee for table in group), sum(table.capacity for table in group)))


def table_group_data(group: tuple[DiningTable, ...]) -> dict:
    floor_names = list(dict.fromkeys(table.floor.floor_name for table in group))
    table_types = [getattr(table.table_type, "value", table.table_type) for table in group]
    return {
        "id": ",".join(str(table.id) for table in group),
        "table_ids": [str(table.id) for table in group],
        "table_number": " + ".join(table.table_number for table in group),
        "floor_name": " / ".join(floor_names),
        "capacity": sum(table.capacity for table in group),
        "table_type": " + ".join(table_types),
        "reservation_fee": float(sum((Decimal(table.reservation_fee) for table in group), Decimal("0.00"))),
    }


@customer_router.get("/availability")
def availability(restaurant_id: uuid.UUID, reservation_date: date, start_time: time,
                 number_of_guests: int = Query(ge=1, le=16), db: Session = Depends(get_db)) -> dict:
    restaurant = db.query(Restaurant).filter(Restaurant.id == restaurant_id, Restaurant.status == RestaurantStatus.APPROVED).first()
    if restaurant is None:
        raise HTTPException(status_code=404, detail="Restaurant not found.")
    end = (datetime.combine(reservation_date, start_time) + timedelta(hours=1, minutes=30)).time()
    if not within_operating_hours(db, restaurant_id, reservation_date, start_time, end):
        return {"restaurant_id": str(restaurant_id), "reservation_date": reservation_date.isoformat(),
                "start_time": start_time.strftime("%H:%M"), "end_time": end.strftime("%H:%M"),
                "available": False, "tables": [], "reason": "The restaurant is closed or this time is outside its operating hours."}
    tables = suitable_table_groups(db, restaurant_id, reservation_date, start_time, number_of_guests)
    return {"restaurant_id": str(restaurant_id), "reservation_date": reservation_date.isoformat(),
            "start_time": start_time.strftime("%H:%M"), "end_time": end.strftime("%H:%M"),
            "available": bool(tables), "tables": [table_group_data(group) for group in tables]}


@customer_router.post("", status_code=status.HTTP_201_CREATED)
def create_reservation(payload: ReservationCreate, customer: User = Depends(current_customer), db: Session = Depends(get_db)) -> dict:
    now = datetime.now(timezone.utc)
    if payload.reservation_date < now.date() or (payload.reservation_date == now.date() and payload.start_time <= now.replace(tzinfo=None).time()):
        raise HTTPException(status_code=422, detail="Choose a future date and time.")
    restaurant = db.query(Restaurant).filter(Restaurant.id == payload.restaurant_id, Restaurant.status == RestaurantStatus.APPROVED).first()
    if restaurant is None:
        raise HTTPException(status_code=404, detail="Restaurant not found.")
    end = (datetime.combine(payload.reservation_date, payload.start_time) + timedelta(hours=1, minutes=30)).time()
    if not within_operating_hours(db, restaurant.id, payload.reservation_date, payload.start_time, end):
        raise HTTPException(status_code=409, detail="The restaurant is closed or this time is outside its operating hours.")
    if payload.fulfillment_type == "PREORDER" and not payload.preorder_items:
        raise HTTPException(status_code=422, detail="Add at least one menu item to your preorder.")
    if payload.fulfillment_type == "TABLE_ONLY" and payload.preorder_items:
        raise HTTPException(status_code=422, detail="Menu items can only be added to a preorder reservation.")
    preorder_snapshot = []
    preorder_total = Decimal("0.00")
    if payload.preorder_items:
        variant_ids = [entry.variant_id for entry in payload.preorder_items]
        if len(set(variant_ids)) != len(variant_ids):
            raise HTTPException(status_code=422, detail="Each menu option can only appear once in the preorder.")
        variants = db.query(FoodVariant).join(Food, FoodVariant.food_id == Food.id).join(Category, Food.category_id == Category.id).filter(
            FoodVariant.id.in_(variant_ids), Food.restaurant_id == restaurant.id,
            Food.is_available.is_(True), Food.deleted_at.is_(None), FoodVariant.is_available.is_(True),
            Category.is_active.is_(True), Category.deleted_at.is_(None),
        ).all()
        variants_by_id = {variant.id: variant for variant in variants}
        if len(variants_by_id) != len(variant_ids):
            raise HTTPException(status_code=409, detail="One or more selected menu options are no longer available.")
        for entry in payload.preorder_items:
            variant = variants_by_id[entry.variant_id]
            food = variant.food
            line_total = Decimal(variant.price) * entry.quantity
            preorder_total += line_total
            preorder_snapshot.append({"variant_id": str(variant.id), "food_name": food.name,
                "variant_name": variant.name, "quantity": entry.quantity,
                "unit_price": float(variant.price), "line_total": float(line_total)})
    try:
        requested_table_ids = payload.table_ids or ([payload.table_id] if payload.table_id else [])
        if not requested_table_ids or len(set(requested_table_ids)) != len(requested_table_ids):
            raise HTTPException(status_code=422, detail="Choose a valid table setup for your party size.")
        table_groups = suitable_table_groups(db, restaurant.id, payload.reservation_date, payload.start_time, payload.number_of_guests, lock=True)
        if not table_groups:
            db.rollback()
            raise HTTPException(status_code=409, detail="No suitable table is available for this time.")
        selected_group = next((group for group in table_groups if {table.id for table in group} == set(requested_table_ids)), None)
        if selected_group is None:
            db.rollback()
            raise HTTPException(status_code=409, detail="That table setup is no longer available. Search again to choose another option.")
        item = Reservation(user_id=customer.id, restaurant_id=restaurant.id,
            reservation_date=payload.reservation_date, start_time=payload.start_time, end_time=end,
            number_of_guests=payload.number_of_guests, special_request=payload.special_request,
            status="PENDING_PAYMENT", payment_status="PENDING",
            fulfillment_type=payload.fulfillment_type, payment_method=payload.payment_method,
            preorder_items=preorder_snapshot or None, preorder_total=preorder_total,
            fee_amount=sum((Decimal(table.reservation_fee) for table in selected_group), Decimal("0.00")))
        db.add(item)
        db.flush()
        db.add_all([ReservationTable(reservation_id=item.id, table_id=table.id) for table in selected_group])
        db.commit()
        db.refresh(item)
        return reservation_data(item, db)
    except HTTPException:
        raise
    except Exception:
        db.rollback()
        raise


@customer_router.get("")
def my_reservations(customer: User = Depends(current_customer), db: Session = Depends(get_db)) -> list[dict]:
    items = db.query(Reservation).filter(Reservation.user_id == customer.id).order_by(Reservation.reservation_date.desc(), Reservation.start_time.desc()).all()
    return [reservation_data(item, db) for item in items]


@customer_router.get("/{reservation_id}")
def get_my_reservation(reservation_id: uuid.UUID, customer: User = Depends(current_customer), db: Session = Depends(get_db)) -> dict:
    item = db.query(Reservation).filter(Reservation.id == reservation_id, Reservation.user_id == customer.id).first()
    if item is None:
        raise HTTPException(status_code=404, detail="Reservation not found.")
    return reservation_data(item, db)


@customer_router.post("/{reservation_id}/cancel")
def cancel_reservation(reservation_id: uuid.UUID, customer: User = Depends(current_customer), db: Session = Depends(get_db)) -> dict:
    item = db.query(Reservation).filter(Reservation.id == reservation_id, Reservation.user_id == customer.id).first()
    if item is None:
        raise HTTPException(status_code=404, detail="Reservation not found.")
    if item.status not in ("PENDING_PAYMENT", "CONFIRMED"):
        raise HTTPException(status_code=409, detail="This reservation can no longer be cancelled.")
    item.status = "CANCELLED"
    db.commit()
    return {"message": "Reservation cancelled."}


@manager_router.get("")
def manager_reservations(day: date | None = None, manager: User = Depends(current_manager), db: Session = Depends(get_db)) -> list[dict]:
    restaurant = restaurant_for_staff(manager, db)
    query = db.query(Reservation).filter(Reservation.restaurant_id == restaurant.id)
    if day:
        query = query.filter(Reservation.reservation_date == day)
    items = query.order_by(Reservation.reservation_date, Reservation.start_time).all()
    return [reservation_data(item, db) for item in items]


@manager_router.get("/{reservation_id}")
def manager_reservation_detail(reservation_id: uuid.UUID, manager: User = Depends(current_manager), db: Session = Depends(get_db)) -> dict:
    restaurant = restaurant_for_staff(manager, db)
    item = db.query(Reservation).filter(Reservation.id == reservation_id, Reservation.restaurant_id == restaurant.id).first()
    if item is None:
        raise HTTPException(status_code=404, detail="Reservation not found for this restaurant.")
    return reservation_data(item, db)


@manager_router.post("/{reservation_id}/preorder-items")
def manager_add_preorder_items(reservation_id: uuid.UUID, payload: ManagerPreorderItem,
                               manager: User = Depends(current_manager), db: Session = Depends(get_db)) -> dict:
    restaurant = restaurant_for_staff(manager, db)
    item = db.query(Reservation).filter(Reservation.id == reservation_id, Reservation.restaurant_id == restaurant.id).first()
    if item is None:
        raise HTTPException(status_code=404, detail="Reservation not found for this restaurant.")
    if item.status != "PENDING_PAYMENT":
        raise HTTPException(status_code=409, detail="Food can only be added before this reservation is confirmed.")
    variant = db.query(FoodVariant).join(Food, FoodVariant.food_id == Food.id).join(Category, Food.category_id == Category.id).filter(
        FoodVariant.id == payload.variant_id, Food.restaurant_id == restaurant.id,
        Food.is_available.is_(True), Food.deleted_at.is_(None), FoodVariant.is_available.is_(True),
        Category.is_active.is_(True), Category.deleted_at.is_(None),
    ).first()
    if variant is None:
        raise HTTPException(status_code=409, detail="This menu option is no longer available.")
    preorder_items = list(item.preorder_items or [])
    existing = next((entry for entry in preorder_items if entry.get("variant_id") == str(variant.id)), None)
    if sum(entry["quantity"] for entry in preorder_items) + payload.quantity > 99:
        raise HTTPException(status_code=422, detail="A reservation can include up to 99 preorder portions.")
    if existing:
        existing["quantity"] += payload.quantity
        existing["line_total"] = float(Decimal(variant.price) * existing["quantity"])
    else:
        preorder_items.append({"variant_id": str(variant.id), "food_name": variant.food.name,
            "variant_name": variant.name, "quantity": payload.quantity,
            "unit_price": float(variant.price), "line_total": float(Decimal(variant.price) * payload.quantity)})
    item.preorder_items = preorder_items
    item.preorder_total = sum((Decimal(str(entry["line_total"])) for entry in preorder_items), Decimal("0.00"))
    item.fulfillment_type = "PREORDER"
    db.commit()
    db.refresh(item)
    return reservation_data(item, db)


@manager_router.post("/{reservation_id}/confirm-payment")
def manager_confirm_payment(reservation_id: uuid.UUID, manager: User = Depends(current_manager), db: Session = Depends(get_db)) -> dict:
    restaurant = restaurant_for_staff(manager, db)
    item = db.query(Reservation).filter(Reservation.id == reservation_id, Reservation.restaurant_id == restaurant.id).first()
    if item is None:
        raise HTTPException(status_code=404, detail="Reservation not found.")
    if item.status != "PENDING_PAYMENT":
        raise HTTPException(status_code=409, detail="Only reservations awaiting payment can be confirmed.")
    # This is a manager attestation of payment received outside DineBook.
    item.payment_status = "PAID"
    item.status = "CONFIRMED"
    db.commit()
    return {"message": "Payment recorded and reservation confirmed."}


@manager_router.post("/{reservation_id}/cancel")
def manager_cancel_reservation(reservation_id: uuid.UUID, manager: User = Depends(current_manager), db: Session = Depends(get_db)) -> dict:
    restaurant = restaurant_for_staff(manager, db)
    item = db.query(Reservation).filter(Reservation.id == reservation_id, Reservation.restaurant_id == restaurant.id).first()
    if item is None:
        raise HTTPException(status_code=404, detail="Reservation not found.")
    if item.status in ("CANCELLED", "COMPLETED"):
        raise HTTPException(status_code=409, detail="This reservation can no longer be cancelled.")
    item.status = "CANCELLED"
    db.commit()
    return {"message": "Reservation cancelled."}


@chef_router.get("/upcoming")
def chef_upcoming(chef: User = Depends(current_chef), db: Session = Depends(get_db)) -> list[dict]:
    restaurant = restaurant_for_staff(chef, db)
    today = datetime.now().date()
    items = db.query(Reservation).filter(
        Reservation.restaurant_id == restaurant.id, Reservation.reservation_date >= today,
        Reservation.status == "CONFIRMED",
    ).order_by(Reservation.reservation_date, Reservation.start_time).all()
    return [reservation_data(item, db) for item in items]
