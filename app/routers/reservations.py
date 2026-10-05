import uuid
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session, joinedload

from app.db.database import get_db
from app.models.dining import DiningTable, RestaurantFloor
from app.models.reservation import Reservation, ReservationTable
from app.models.restaurant import Restaurant, RestaurantStatus
from app.models.user import User
from app.routers.customer_auth import current_customer
from app.routers.chef_auth import current_chef
from app.routers.manager_auth import current_manager

customer_router = APIRouter(prefix="/api/customer/reservations", tags=["reservations"])
manager_router = APIRouter(prefix="/api/manager/reservations", tags=["manager reservations"])
chef_router = APIRouter(prefix="/api/chef/reservations", tags=["chef reservations"])
BLOCKING = ("PENDING_PAYMENT", "CONFIRMED", "SEATED")


class ReservationCreate(BaseModel):
    restaurant_id: uuid.UUID
    reservation_date: date
    start_time: time
    number_of_guests: int = Field(ge=1, le=50)
    table_id: uuid.UUID
    special_request: str | None = Field(default=None, max_length=1000)


def restaurant_for_staff(staff: User, db: Session) -> Restaurant:
    restaurant = db.query(Restaurant).filter(Restaurant.manager_id == staff.manager_id if staff.manager_id else Restaurant.manager_id == staff.id).first()
    if restaurant is None:
        raise HTTPException(status_code=404, detail="No restaurant is linked to this account.")
    return restaurant


def reservation_data(item: Reservation, db: Session) -> dict:
    customer = db.query(User).filter(User.id == item.user_id).first()
    assignments = db.query(ReservationTable).options(joinedload(ReservationTable.table).joinedload(DiningTable.floor)).filter(ReservationTable.reservation_id == item.id).all()
    return {
        "id": str(item.id), "restaurant_id": str(item.restaurant_id), "customer_name": customer.name if customer else "Customer",
        "customer_email": customer.email if customer else None, "reservation_date": item.reservation_date.isoformat(),
        "start_time": item.start_time.strftime("%H:%M"), "end_time": item.end_time.strftime("%H:%M"),
        "number_of_guests": item.number_of_guests, "status": item.status,
        "payment_status": item.payment_status, "fee_amount": float(item.fee_amount),
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


def suitable_tables(db: Session, restaurant_id: uuid.UUID, day: date, start: time, guests: int, lock: bool = False) -> list[DiningTable]:
    end = (datetime.combine(day, start) + timedelta(hours=1, minutes=30)).time()
    query = db.query(DiningTable).join(RestaurantFloor, DiningTable.floor_id == RestaurantFloor.id).filter(
        DiningTable.restaurant_id == restaurant_id, DiningTable.status == "available",
        DiningTable.capacity >= guests, RestaurantFloor.is_active.is_(True),
    ).order_by(DiningTable.capacity, DiningTable.reservation_fee, DiningTable.table_number)
    if lock:
        query = query.with_for_update()
    candidates = query.all()
    return [table for table in candidates if not overlapping(db, table.id, day, start, end)]


@customer_router.get("/availability")
def availability(restaurant_id: uuid.UUID, reservation_date: date, start_time: time,
                 number_of_guests: int = Query(ge=1, le=50), db: Session = Depends(get_db)) -> dict:
    restaurant = db.query(Restaurant).filter(Restaurant.id == restaurant_id, Restaurant.status == RestaurantStatus.APPROVED).first()
    if restaurant is None:
        raise HTTPException(status_code=404, detail="Restaurant not found.")
    end = (datetime.combine(reservation_date, start_time) + timedelta(hours=1, minutes=30)).time()
    tables = suitable_tables(db, restaurant_id, reservation_date, start_time, number_of_guests)
    return {"restaurant_id": str(restaurant_id), "reservation_date": reservation_date.isoformat(),
            "start_time": start_time.strftime("%H:%M"), "end_time": end.strftime("%H:%M"),
            "available": bool(tables), "tables": [{"id": str(t.id), "table_number": t.table_number,
            "floor_id": str(t.floor_id), "floor_name": t.floor.floor_name, "capacity": t.capacity,
            "reservation_fee": float(t.reservation_fee)} for t in tables]}


@customer_router.post("", status_code=status.HTTP_201_CREATED)
def create_reservation(payload: ReservationCreate, customer: User = Depends(current_customer), db: Session = Depends(get_db)) -> dict:
    now = datetime.now(timezone.utc)
    if payload.reservation_date < now.date() or (payload.reservation_date == now.date() and payload.start_time <= now.replace(tzinfo=None).time()):
        raise HTTPException(status_code=422, detail="Choose a future date and time.")
    restaurant = db.query(Restaurant).filter(Restaurant.id == payload.restaurant_id, Restaurant.status == RestaurantStatus.APPROVED).first()
    if restaurant is None:
        raise HTTPException(status_code=404, detail="Restaurant not found.")
    end = (datetime.combine(payload.reservation_date, payload.start_time) + timedelta(hours=1, minutes=30)).time()
    try:
        tables = suitable_tables(db, restaurant.id, payload.reservation_date, payload.start_time, payload.number_of_guests, lock=True)
        if not tables:
            db.rollback()
            raise HTTPException(status_code=409, detail="No suitable table is available for this time.")
        table = next((candidate for candidate in tables if candidate.id == payload.table_id), None)
        if table is None:
            db.rollback()
            raise HTTPException(status_code=409, detail="That table is no longer available. Search again to choose another table.")
        item = Reservation(user_id=customer.id, restaurant_id=restaurant.id,
            reservation_date=payload.reservation_date, start_time=payload.start_time, end_time=end,
            number_of_guests=payload.number_of_guests, special_request=payload.special_request,
            status="PENDING_PAYMENT", payment_status="PENDING", fee_amount=Decimal(table.reservation_fee))
        db.add(item)
        db.flush()
        db.add(ReservationTable(reservation_id=item.id, table_id=table.id))
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
