import uuid
from itertools import combinations
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session, joinedload

from app.db.database import get_db
from app.models.dining import DiningTable, RestaurantFloor
from app.models.menu import Category, Food, FoodVariant
from app.models.orders import FoodOrder
from app.models.reservation import Reservation, ReservationTable
from app.models.restaurant import Restaurant, RestaurantStatus
from app.models.user import User
from app.models.wallet import WalletAccount, WalletTransaction
from app.models.realtime import RealtimeEvent
from app.routers.customer_auth import current_customer
from app.routers.chef_auth import current_chef
from app.routers.manager_auth import current_manager
from app.routers.operating_hours import effective_periods

customer_router = APIRouter(prefix="/api/customer/reservations", tags=["reservations"])
manager_router = APIRouter(prefix="/api/manager/reservations", tags=["manager reservations"])
chef_router = APIRouter(prefix="/api/chef/reservations", tags=["chef reservations"])
BLOCKING = ("PENDING_PAYMENT", "CONFIRMED", "SEATED")
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
PREORDER_ORDER_NAMESPACE = uuid.UUID("89dd0385-bbad-4aeb-b600-bf4377641e10")
RESTAURANT_TIMEZONE = ZoneInfo("Asia/Kolkata")


def record_reservation_event(db: Session, item: Reservation, previous_status: str | None = None,
                            actor: User | None = None, reason: str | None = None) -> None:
    data = {"status": item.status, "payment_status": item.payment_status,
            "preorder_payment_status": item.preorder_payment_status,
            "actor_id": str(actor.id) if actor else None,
            "occurred_at": datetime.now(timezone.utc).isoformat()}
    if previous_status is not None:
        data.update({"previous_status": previous_status, "reason": reason})
    db.add(RealtimeEvent(user_id=item.user_id, restaurant_id=item.restaurant_id,
        event_type="booking.updated", resource_id=str(item.id),
        data=data))


class PreorderItem(BaseModel):
    variant_id: uuid.UUID
    quantity: int = Field(ge=1, le=99)


class ManagerPreorderItem(BaseModel):
    variant_id: uuid.UUID
    quantity: int = Field(ge=1, le=99)


class ManagerReservationStatus(BaseModel):
    status: str = Field(pattern="^(SEATED|COMPLETED|NO_SHOW|REJECTED)$")
    reason: str | None = Field(default=None, max_length=500)


class ManagerReservationCancellation(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


class ReservationCreate(BaseModel):
    restaurant_id: uuid.UUID
    reservation_date: date
    start_time: time
    number_of_guests: int = Field(ge=1, le=16)
    table_id: uuid.UUID | None = None
    table_ids: list[uuid.UUID] | None = Field(default=None, min_length=1, max_length=2)
    special_request: str | None = Field(default=None, max_length=1000)
    fulfillment_type: str = Field(default="TABLE_ONLY", pattern="^(TABLE_ONLY|PREORDER)$")
    payment_method: str = Field(default="WALLET", pattern="^WALLET$")
    preorder_payment_method: str | None = Field(default=None, pattern="^(PAY_AT_DESK|WALLET)$")
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
    food_orders = db.query(FoodOrder).filter(FoodOrder.reservation_id == item.id).order_by(FoodOrder.created_at.asc()).all()
    preorder_payment_method = item.preorder_payment_method
    preorder_payment_status = item.preorder_payment_status
    if item.fulfillment_type == "PREORDER" and preorder_payment_status is None:
        legacy_paid = item.payment_method == "WALLET" and item.payment_status == "PAID"
        preorder_payment_method = "WALLET" if legacy_paid else "PAY_AT_DESK"
        preorder_payment_status = "PAID" if legacy_paid else "PENDING"
    return {
        "id": str(item.id), "restaurant_id": str(item.restaurant_id), "restaurant_name": restaurant.name if restaurant else "Restaurant",
        "restaurant_location": ", ".join(part for part in (restaurant.city, restaurant.state) if part) if restaurant else None,
        "customer_name": customer.name if customer else "Customer",
        "customer_email": customer.email if customer else None, "reservation_date": item.reservation_date.isoformat(),
        "start_time": item.start_time.strftime("%H:%M"), "end_time": item.end_time.strftime("%H:%M"),
        "number_of_guests": item.number_of_guests, "status": item.status,
        "created_at": item.created_at.isoformat() if item.created_at else None,
        "checked_in_at": item.checked_in_at.isoformat() if item.checked_in_at else None,
        "completed_at": item.completed_at.isoformat() if item.completed_at else None,
        "status_reason": item.status_reason,
        "payment_status": item.payment_status, "fee_amount": float(item.fee_amount),
        "preorder_payment_method": preorder_payment_method,
        "preorder_payment_status": preorder_payment_status,
        "preorder_due_at_desk": float(item.preorder_total or 0) if item.fulfillment_type == "PREORDER" and preorder_payment_method == "PAY_AT_DESK" and preorder_payment_status == "PENDING" else 0,
        "fulfillment_type": item.fulfillment_type, "payment_method": item.payment_method,
        "preorder_items": item.preorder_items or [], "preorder_total": float(item.preorder_total or 0),
        "special_request": item.special_request,
        "food_orders": [{"id": str(order.id), "status": order.status,
                         "status_reason": order.status_reason, "payment_status": order.payment_status,
                         "total": float(order.total), "items": order.items or [],
                         "accepted_at": order.accepted_at.isoformat() if order.accepted_at else None,
                         "preparation_started_at": order.preparation_started_at.isoformat() if order.preparation_started_at else None,
                         "ready_at": order.ready_at.isoformat() if order.ready_at else None,
                         "completed_at": order.completed_at.isoformat() if order.completed_at else None,
                         "created_at": order.created_at.isoformat() if order.created_at else None}
                        for order in food_orders],
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
    starts = start.hour * 60 + start.minute
    ends = end.hour * 60 + end.minute
    if ends < starts:
        ends += 24 * 60
    periods, _ = effective_periods(db, restaurant_id, day)
    for period in periods:
        opens = time.fromisoformat(period["opens"])
        closes = time.fromisoformat(period["closes"])
        opening = opens.hour * 60 + opens.minute
        closing = closes.hour * 60 + closes.minute
        if closing <= opening:
            closing += 24 * 60
        if opening <= starts and ends <= closing:
            return True
    return False


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
    if reservation_date <= datetime.now(RESTAURANT_TIMEZONE).date():
        return {"restaurant_id": str(restaurant_id), "reservation_date": reservation_date.isoformat(),
                "start_time": start_time.strftime("%H:%M"), "end_time": end.strftime("%H:%M"),
                "available": False, "tables": [], "reason": "Reservations must be made at least one day in advance."}
    if not within_operating_hours(db, restaurant_id, reservation_date, start_time, end):
        return {"restaurant_id": str(restaurant_id), "reservation_date": reservation_date.isoformat(),
                "start_time": start_time.strftime("%H:%M"), "end_time": end.strftime("%H:%M"),
                "available": False, "tables": [], "reason": "The restaurant is closed or this time is outside its operating hours."}
    tables = suitable_table_groups(db, restaurant_id, reservation_date, start_time, number_of_guests)
    return {"restaurant_id": str(restaurant_id), "reservation_date": reservation_date.isoformat(),
            "start_time": start_time.strftime("%H:%M"), "end_time": end.strftime("%H:%M"),
            "available": bool(tables), "tables": [table_group_data(group) for group in tables]}


@customer_router.get("/time-slots")
def reservation_time_slots(restaurant_id: uuid.UUID, reservation_date: date,
                           number_of_guests: int = Query(ge=1, le=16),
                           db: Session = Depends(get_db)) -> dict:
    """Return bookable 30-minute start times for a date and party size."""
    restaurant = db.query(Restaurant).filter(
        Restaurant.id == restaurant_id, Restaurant.status == RestaurantStatus.APPROVED
    ).first()
    if restaurant is None:
        raise HTTPException(status_code=404, detail="Restaurant not found.")
    operating_periods, _ = effective_periods(db, restaurant_id, reservation_date)
    slots = []
    if operating_periods:
        now = datetime.now(RESTAURANT_TIMEZONE)
        for period in operating_periods:
            opening_time = time.fromisoformat(period["opens"])
            closing_time = time.fromisoformat(period["closes"])
            opening = opening_time.hour * 60 + opening_time.minute
            closing = closing_time.hour * 60 + closing_time.minute
            if closing <= opening:
                closing += 24 * 60
            for start_minute in range(opening, closing, 30):
                end_minute = start_minute + 90
                if end_minute > closing:
                    break
                slot_day = reservation_date + timedelta(days=start_minute // (24 * 60))
                minute_of_day = start_minute % (24 * 60)
                start = time(minute_of_day // 60, minute_of_day % 60)
                if slot_day <= now.date() or (slot_day == now.date() and start <= now.replace(tzinfo=None).time()):
                    continue
                groups = suitable_table_groups(db, restaurant_id, reservation_date, start, number_of_guests)
                slots.append({"time": start.strftime("%H:%M"), "available": bool(groups),
                              "tables": [table_group_data(group) for group in groups]})
    return {"restaurant_id": str(restaurant_id), "reservation_date": reservation_date.isoformat(), "slots": slots}


@customer_router.post("", status_code=status.HTTP_201_CREATED)
def create_reservation(payload: ReservationCreate, customer: User = Depends(current_customer), db: Session = Depends(get_db)) -> dict:
    now = datetime.now(RESTAURANT_TIMEZONE)
    if payload.reservation_date <= now.date():
        raise HTTPException(status_code=422, detail="Reservations must be made at least one day in advance.")
    if payload.fulfillment_type != "TABLE_ONLY" or payload.preorder_items:
        raise HTTPException(status_code=422, detail="Food orders are handled by the restaurant manager at reception.")
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
            Food.is_available.is_(True), Food.deleted_at.is_(None), FoodVariant.is_available.is_(True), FoodVariant.deleted_at.is_(None),
            Category.is_active.is_(True), Category.deleted_at.is_(None),
        ).all()
        variants_by_id = {variant.id: variant for variant in variants}
        if len(variants_by_id) != len(variant_ids):
            raise HTTPException(status_code=409, detail="One or more selected menu options are no longer available.")
        for entry in payload.preorder_items:
            variant = variants_by_id[entry.variant_id]
            food = variant.food
            unit_price = Decimal(variant.price).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            line_total = (unit_price * entry.quantity).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            preorder_total += line_total
            preorder_snapshot.append({"variant_id": str(variant.id), "food_name": food.name,
                "description": food.description,
                "image_url": food.image.image_url if food.image else None,
                "variant_name": variant.name, "quantity": entry.quantity,
                "unit_price": float(unit_price), "line_total": float(line_total)})
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
        fee_amount = sum((Decimal(table.reservation_fee) for table in selected_group), Decimal("0.00"))
        preorder_method = payload.preorder_payment_method or "PAY_AT_DESK"
        if payload.fulfillment_type != "PREORDER":
            preorder_method = None
        amount_due_now = fee_amount + (preorder_total if preorder_method == "WALLET" else Decimal("0.00"))
        wallet_account = db.query(WalletAccount).filter(WalletAccount.user_id == customer.id).with_for_update().first()
        if wallet_account is None or Decimal(wallet_account.balance) < amount_due_now:
            db.rollback()
            raise HTTPException(status_code=409, detail="Your wallet balance must cover the reservation fee and any preorder food you choose to pay now.")
        wallet_account.balance = Decimal(wallet_account.balance) - amount_due_now
        item = Reservation(user_id=customer.id, restaurant_id=restaurant.id,
            reservation_date=payload.reservation_date, start_time=payload.start_time, end_time=end,
            number_of_guests=payload.number_of_guests, special_request=payload.special_request,
            status="CONFIRMED", payment_status="PAID",
            preorder_payment_status=("PAID" if preorder_method == "WALLET" else "PENDING") if preorder_method else None,
            preorder_payment_method=preorder_method,
            fulfillment_type=payload.fulfillment_type, payment_method="WALLET",
            preorder_items=preorder_snapshot or None, preorder_total=preorder_total,
            fee_amount=fee_amount)
        db.add(item)
        db.flush()
        record_reservation_event(db, item, actor=customer)
        db.add_all([ReservationTable(reservation_id=item.id, table_id=table.id) for table in selected_group])
        db.add(WalletTransaction(user_id=customer.id, reservation_id=item.id, amount=-amount_due_now,
            transaction_type="DEBIT", description=f"Reservation at {restaurant.name}"))
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
    item = db.query(Reservation).filter(Reservation.id == reservation_id, Reservation.user_id == customer.id).with_for_update().first()
    if item is None:
        raise HTTPException(status_code=404, detail="Reservation not found.")
    if item.status not in ("PENDING_PAYMENT", "CONFIRMED"):
        raise HTTPException(status_code=409, detail="This reservation can no longer be cancelled.")
    active_orders = db.query(FoodOrder.id).filter(
        FoodOrder.reservation_id == item.id,
        FoodOrder.status.notin_(("CANCELLED", "REJECTED")),
    ).first()
    if active_orders:
        raise HTTPException(status_code=409, detail="Cancel or complete the linked food orders before cancelling this reservation.")
    previous_status = item.status
    refund_amount = refund_wallet_reservation(db, item)
    item.status = "CANCELLED"
    record_reservation_event(db, item, previous_status, customer)
    db.commit()
    return {"message": "Reservation cancelled.", "wallet_refund": float(refund_amount)}


def refund_wallet_reservation(db: Session, item: Reservation) -> Decimal:
    existing_refund = db.query(WalletTransaction.id).filter(
        WalletTransaction.reservation_id == item.id,
        WalletTransaction.transaction_type == "REFUND",
        ((WalletTransaction.reference == f"reservation:{item.id}") | (WalletTransaction.reference.is_(None))),
    ).first()
    if existing_refund:
        return Decimal("0.00")
    booking_time = item.created_at
    if booking_time is not None and booking_time.tzinfo is None:
        booking_time = booking_time.replace(tzinfo=timezone.utc)
    within_refund_window = booking_time is not None and datetime.now(timezone.utc) <= booking_time + timedelta(days=1)
    fee_refundable = item.payment_method == "WALLET" and item.payment_status == "PAID" and within_refund_window
    preorder_paid_from_wallet = item.preorder_payment_status == "PAID" and item.preorder_payment_method == "WALLET"
    # Legacy preorder records used the reservation payment fields for both charges.
    legacy_preorder_paid = (item.preorder_payment_status is None and item.fulfillment_type == "PREORDER"
                            and item.payment_method == "WALLET" and item.payment_status == "PAID")
    preorder_refundable = preorder_paid_from_wallet or legacy_preorder_paid
    refund_amount = (Decimal(item.fee_amount) if fee_refundable else Decimal("0.00"))
    if preorder_refundable:
        refund_amount += Decimal(item.preorder_total or 0)
    if refund_amount <= 0:
        return Decimal("0.00")
    account = db.query(WalletAccount).filter(WalletAccount.user_id == item.user_id).with_for_update().first()
    if account is None:
        raise HTTPException(status_code=409, detail="Wallet account is unavailable; contact support to complete this refund.")
    account.balance = Decimal(account.balance) + refund_amount
    if fee_refundable:
        item.payment_status = "REFUNDED"
    if preorder_refundable:
        item.preorder_payment_status = "REFUNDED"
    db.add(WalletTransaction(user_id=item.user_id, reservation_id=item.id, amount=refund_amount,
        transaction_type="REFUND", description="Wallet refund for cancelled reservation",
        reference=f"reservation:{item.id}"))
    return refund_amount


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
    item = db.query(Reservation).filter(Reservation.id == reservation_id, Reservation.restaurant_id == restaurant.id).with_for_update().first()
    if item is None:
        raise HTTPException(status_code=404, detail="Reservation not found for this restaurant.")
    return reservation_data(item, db)


@manager_router.patch("/{reservation_id}/status")
def manager_update_reservation_status(reservation_id: uuid.UUID, payload: ManagerReservationStatus,
                                      manager: User = Depends(current_manager), db: Session = Depends(get_db)) -> dict:
    restaurant = restaurant_for_staff(manager, db)
    item = db.query(Reservation).filter(
        Reservation.id == reservation_id, Reservation.restaurant_id == restaurant.id
    ).with_for_update().first()
    if item is None:
        raise HTTPException(status_code=404, detail="Reservation not found for this restaurant.")
    transitions = {"PENDING_PAYMENT": {"REJECTED"}, "CONFIRMED": {"SEATED", "NO_SHOW"}, "SEATED": {"COMPLETED"}}
    if payload.status not in transitions.get(item.status, set()):
        raise HTTPException(status_code=409, detail=f"Reservation cannot move from {item.status} to {payload.status}.")
    if payload.status == "COMPLETED":
        pending_orders = db.query(FoodOrder.id).filter(
            FoodOrder.reservation_id == item.id,
            ((FoodOrder.status != "COMPLETED") | (FoodOrder.payment_status != "PAID")),
        ).first()
        if pending_orders:
            raise HTTPException(status_code=409, detail="Complete and pay all orders before completing this reservation.")
    previous_status = item.status
    refund_amount = refund_wallet_reservation(db, item) if payload.status == "REJECTED" else Decimal("0.00")
    item.status = payload.status
    if payload.status == "SEATED":
        item.checked_in_at = datetime.now(timezone.utc)
    if payload.status == "COMPLETED":
        item.completed_at = datetime.now(timezone.utc)
    if payload.status in {"NO_SHOW", "REJECTED"}:
        item.status_reason = payload.reason
    record_reservation_event(db, item, previous_status, manager, payload.reason)
    db.commit()
    db.refresh(item)
    result = reservation_data(item, db)
    result["wallet_refund"] = float(refund_amount)
    return result


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
        Food.is_available.is_(True), Food.deleted_at.is_(None), FoodVariant.is_available.is_(True), FoodVariant.deleted_at.is_(None),
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
        existing.setdefault("image_url", variant.food.image.image_url if variant.food.image else None)
        existing.setdefault("description", variant.food.description)
    else:
        preorder_items.append({"variant_id": str(variant.id), "food_name": variant.food.name,
            "description": variant.food.description,
            "image_url": variant.food.image.image_url if variant.food.image else None,
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
    item = db.query(Reservation).filter(Reservation.id == reservation_id, Reservation.restaurant_id == restaurant.id).with_for_update().first()
    if item is None:
        raise HTTPException(status_code=404, detail="Reservation not found.")
    if item.status != "PENDING_PAYMENT":
        raise HTTPException(status_code=409, detail="Only reservations awaiting payment can be confirmed.")
    previous_status = item.status
    # This is a manager attestation of payment received outside DineBook.
    item.payment_status = "PAID"
    if item.fulfillment_type == "PREORDER":
        item.preorder_payment_method = "PAY_AT_DESK"
        item.preorder_payment_status = "PAID"
    item.status = "CONFIRMED"
    record_reservation_event(db, item, previous_status, manager)
    db.commit()
    db.refresh(item)
    return reservation_data(item, db)


@manager_router.post("/{reservation_id}/confirm-preorder-payment")
def manager_confirm_preorder_payment(reservation_id: uuid.UUID, manager: User = Depends(current_manager), db: Session = Depends(get_db)) -> dict:
    restaurant = restaurant_for_staff(manager, db)
    item = db.query(Reservation).filter(
        Reservation.id == reservation_id, Reservation.restaurant_id == restaurant.id
    ).with_for_update().first()
    if item is None:
        raise HTTPException(status_code=404, detail="Reservation not found for this restaurant.")
    if item.fulfillment_type != "PREORDER" or item.preorder_payment_method != "PAY_AT_DESK" or item.preorder_payment_status != "PENDING":
        raise HTTPException(status_code=409, detail="This preorder is not awaiting desk payment.")
    if item.status not in ("CONFIRMED", "SEATED"):
        raise HTTPException(status_code=409, detail="Only an active reservation can have its preorder payment recorded.")
    item.preorder_payment_status = "PAID"
    preorder_order = db.query(FoodOrder).filter(
        FoodOrder.reservation_id == item.id,
        FoodOrder.idempotency_key == uuid.uuid5(PREORDER_ORDER_NAMESPACE, str(item.id)),
    ).with_for_update().first()
    if preorder_order is not None and preorder_order.status not in {"CANCELLED", "REJECTED"}:
        preorder_order.payment_status = "PAID"
        db.add(RealtimeEvent(user_id=preorder_order.customer_id, restaurant_id=preorder_order.restaurant_id,
            event_type="order.status_changed", resource_id=str(preorder_order.id),
            data={"status": preorder_order.status, "payment_status": preorder_order.payment_status,
                  "actor_id": str(manager.id), "occurred_at": datetime.now(timezone.utc).isoformat()}))
    record_reservation_event(db, item, actor=manager)
    db.commit()
    db.refresh(item)
    return reservation_data(item, db)


@manager_router.post("/{reservation_id}/cancel")
def manager_cancel_reservation(reservation_id: uuid.UUID, payload: ManagerReservationCancellation | None = None,
                               manager: User = Depends(current_manager), db: Session = Depends(get_db)) -> dict:
    restaurant = restaurant_for_staff(manager, db)
    item = db.query(Reservation).filter(Reservation.id == reservation_id, Reservation.restaurant_id == restaurant.id).with_for_update().first()
    if item is None:
        raise HTTPException(status_code=404, detail="Reservation not found.")
    if item.status in ("SEATED", "CANCELLED", "COMPLETED", "NO_SHOW", "REJECTED"):
        raise HTTPException(status_code=409, detail="This reservation can no longer be cancelled.")
    active_orders = db.query(FoodOrder.id).filter(
        FoodOrder.reservation_id == item.id,
        FoodOrder.status.notin_(("CANCELLED", "REJECTED")),
    ).first()
    if active_orders:
        raise HTTPException(status_code=409, detail="Cancel or complete the linked food orders before cancelling this reservation.")
    previous_status = item.status
    reason = payload.reason.strip() if payload and payload.reason else None
    refund_amount = refund_wallet_reservation(db, item)
    item.status = "CANCELLED"
    item.status_reason = reason
    record_reservation_event(db, item, previous_status, manager, reason)
    db.commit()
    return {"message": "Reservation cancelled.", "wallet_refund": float(refund_amount)}


@chef_router.get("/upcoming")
def chef_upcoming(chef: User = Depends(current_chef), db: Session = Depends(get_db)) -> list[dict]:
    restaurant = restaurant_for_staff(chef, db)
    today = datetime.now().date()
    items = db.query(Reservation).filter(
        Reservation.restaurant_id == restaurant.id, Reservation.reservation_date >= today,
        Reservation.status.in_(("CONFIRMED", "SEATED")),
    ).order_by(Reservation.reservation_date, Reservation.start_time).all()
    return [reservation_data(item, db) for item in items]
