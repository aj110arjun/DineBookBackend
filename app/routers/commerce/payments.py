import uuid
from datetime import date, datetime, time, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import String, cast, func, literal, or_, select
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.models.reservation import Reservation
from app.models.orders import FoodOrder
from app.models.restaurant import Restaurant
from app.models.user import User
from app.routers.auth.admin_auth import current_admin
from app.routers.auth.manager_auth import current_manager

manager_router = APIRouter(prefix="/api/manager/payments", tags=["manager payments"])
transactions_router = APIRouter(prefix="/api/admin/transactions", tags=["admin transactions"])
admin_router = APIRouter(prefix="/api/admin/restaurants/{restaurant_id}/payments", tags=["admin restaurant payments"])


def payment_view(item: Reservation) -> dict:
    return {
        "id": str(item.id), "transaction_id": str(item.id),
        "reservation_id": str(item.id), "restaurant_id": str(item.restaurant_id),
        "type": "RESERVATION_FEE", "amount": float(item.fee_amount), "currency": "INR",
        "payment_method": item.payment_method, "status": item.payment_status,
        "refund_status": "REFUNDED" if item.payment_status == "REFUNDED" else "NONE",
        "razorpay_order_id": item.razorpay_order_id,
        "razorpay_payment_id": item.razorpay_payment_id,
        "created_at": item.created_at.isoformat() if item.created_at else None,
        "updated_at": item.updated_at.isoformat() if item.updated_at else None,
        "reservation_date": item.reservation_date.isoformat(),
        "reservation_status": item.status,
    }


def order_payment_view(item: FoodOrder) -> dict:
    return {
        "id": str(item.id), "transaction_id": str(item.id), "order_id": str(item.id),
        "reservation_id": str(item.reservation_id) if item.reservation_id else None,
        "restaurant_id": str(item.restaurant_id), "type": "ORDER_PAYMENT",
        "amount": float(item.total), "currency": "INR", "payment_method": item.payment_method,
        "status": item.payment_status,
        "refund_status": "REFUNDED" if item.payment_status == "REFUNDED" else "NONE",
        "razorpay_order_id": None, "razorpay_payment_id": None,
        "created_at": item.created_at.isoformat() if item.created_at else None,
        "updated_at": item.updated_at.isoformat() if item.updated_at else None,
        "reservation_date": None, "reservation_status": None,
    }


def list_payments(db: Session, restaurant_id: uuid.UUID | None, *, q: str | None, payment_status: str | None,
                  transaction_type: str | None, payment_method: str | None, refund_status: str | None,
                  date_from: date | None, date_to: date | None, sort: str, page: int, page_size: int) -> dict:
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="Start date must be on or before end date.")
    reservation_rows = select(
        cast(Reservation.id, String).label("id"), literal("RESERVATION_FEE").label("type"),
        Reservation.fee_amount.label("amount"), Reservation.payment_method.label("payment_method"),
        Reservation.payment_status.label("status"), Reservation.razorpay_order_id.label("razorpay_order_id"),
        Reservation.razorpay_payment_id.label("razorpay_payment_id"), Reservation.created_at.label("created_at"),
        Reservation.updated_at.label("updated_at"), cast(Reservation.id, String).label("reservation_id"),
        literal(None, type_=String).label("order_id"),
        cast(Restaurant.id, String).label("restaurant_id"), Restaurant.name.label("restaurant_name"),
    ).join(Restaurant, Restaurant.id == Reservation.restaurant_id)
    if restaurant_id is not None:
        reservation_rows = reservation_rows.where(Reservation.restaurant_id == restaurant_id)
    order_rows = select(
        cast(FoodOrder.id, String).label("id"), literal("ORDER_PAYMENT").label("type"),
        FoodOrder.total.label("amount"), FoodOrder.payment_method.label("payment_method"),
        FoodOrder.payment_status.label("status"), literal(None, type_=String).label("razorpay_order_id"),
        literal(None, type_=String).label("razorpay_payment_id"), FoodOrder.created_at.label("created_at"),
        FoodOrder.updated_at.label("updated_at"), cast(FoodOrder.reservation_id, String).label("reservation_id"),
        cast(FoodOrder.id, String).label("order_id"),
        cast(Restaurant.id, String).label("restaurant_id"), Restaurant.name.label("restaurant_name"),
    ).join(Restaurant, Restaurant.id == FoodOrder.restaurant_id)
    if restaurant_id is not None:
        order_rows = order_rows.where(FoodOrder.restaurant_id == restaurant_id)
    records = reservation_rows.union_all(order_rows).subquery()
    query = db.query(records)
    if q and q.strip():
        term = f"%{q.strip()}%"
        displayed_id_suffix = q.strip().split("-")[-1] if q.strip().upper().startswith(("TXN-", "ORD-", "RES-")) else ""
        query = query.filter(or_(records.c.restaurant_name.ilike(term), records.c.id.ilike(term), records.c.order_id.ilike(term),
            records.c.reservation_id.ilike(term), records.c.razorpay_order_id.ilike(term),
            records.c.razorpay_payment_id.ilike(term),
            records.c.id.ilike(f"%{displayed_id_suffix}%") if displayed_id_suffix else literal(False)))
    if payment_status:
        query = query.filter(records.c.status == payment_status)
    if transaction_type:
        query = query.filter(records.c.type == transaction_type)
    if payment_method:
        query = query.filter(records.c.payment_method == payment_method)
    if refund_status == "REFUNDED":
        query = query.filter(records.c.status == "REFUNDED")
    elif refund_status == "NONE":
        query = query.filter(records.c.status != "REFUNDED")
    if date_from:
        query = query.filter(records.c.created_at >= datetime.combine(date_from, time.min, tzinfo=timezone.utc))
    if date_to:
        query = query.filter(records.c.created_at < datetime.combine(date_to + timedelta(days=1), time.min, tzinfo=timezone.utc))
    total = query.count()
    ordering = records.c.amount.asc() if sort == "amount-low" else records.c.amount.desc() if sort == "amount-high" else records.c.created_at.asc() if sort == "oldest" else records.c.created_at.desc()
    if sort in ("restaurant-asc", "restaurant-desc"):
        name = func.lower(records.c.restaurant_name)
        ordering = name.asc() if sort == "restaurant-asc" else name.desc()
    items = query.order_by(ordering, records.c.created_at.desc(), records.c.type.asc(), records.c.id.desc()).offset((page - 1) * page_size).limit(page_size).all()
    return {"transactions": [{**dict(item._mapping), "amount": float(item.amount), "currency": "INR",
            "refund_status": "REFUNDED" if item.status == "REFUNDED" else "NONE",
            "transaction_id": item.id} for item in items], "total": total,
            "page": page, "page_size": page_size}


def manager_restaurant(manager: User, db: Session) -> Restaurant:
    owner_id = manager.manager_id or manager.id
    restaurant = db.query(Restaurant).filter(Restaurant.manager_id == owner_id).first()
    if restaurant is None:
        raise HTTPException(status_code=404, detail="No restaurant is linked to this account.")
    return restaurant


@manager_router.get("")
def manager_payments(q: str | None = Query(default=None, max_length=120),
                     payment_status: str | None = Query(default=None, pattern="^(PENDING|PAID|FAILED|REFUNDED)$"),
                     transaction_type: str | None = Query(default=None, pattern="^(RESERVATION_FEE|ORDER_PAYMENT)$"),
                     payment_method: str | None = Query(default=None, max_length=30),
                     refund_status: str | None = Query(default=None, pattern="^(NONE|REFUNDED)$"),
                     date_from: date | None = None, date_to: date | None = None,
                     sort: str = Query(default="newest", pattern="^(newest|oldest|amount-low|amount-high)$"),
                     page: int = Query(default=1, ge=1), page_size: int = Query(default=20, ge=1, le=100),
                     manager: User = Depends(current_manager), db: Session = Depends(get_db)) -> dict:
    restaurant = manager_restaurant(manager, db)
    return list_payments(db, restaurant.id, q=q, payment_status=payment_status,
        transaction_type=transaction_type, payment_method=payment_method, refund_status=refund_status,
        date_from=date_from, date_to=date_to, sort=sort, page=page, page_size=page_size)


@manager_router.get("/{transaction_id}")
def manager_payment_details(transaction_id: uuid.UUID, manager: User = Depends(current_manager),
                            db: Session = Depends(get_db)) -> dict:
    restaurant = manager_restaurant(manager, db)
    item = db.query(Reservation).filter(Reservation.id == transaction_id,
        Reservation.restaurant_id == restaurant.id).first()
    if item is not None:
        customer = db.query(User).filter(User.id == item.user_id).first()
        return {**payment_view(item), "restaurant_name": restaurant.name,
            "restaurant_location": ", ".join(part for part in (restaurant.city, restaurant.state) if part) or None,
            "restaurant_address": restaurant.address, "restaurant_email": restaurant.email,
            "restaurant_phone": restaurant.phone,
            "customer_name": customer.name if customer else "Customer",
            "customer_email": customer.email if customer else None,
            "description": f"Reservation fee for {item.reservation_date.isoformat()}"}
    order = db.query(FoodOrder).filter(FoodOrder.id == transaction_id,
        FoodOrder.restaurant_id == restaurant.id).first()
    if order is None:
        raise HTTPException(status_code=404, detail="Transaction not found.")
    customer = db.query(User).filter(User.id == order.customer_id).first() if order.customer_id else None
    return {**order_payment_view(order), "restaurant_name": restaurant.name,
        "restaurant_location": ", ".join(part for part in (restaurant.city, restaurant.state) if part) or None,
        "restaurant_address": restaurant.address, "restaurant_email": restaurant.email,
        "restaurant_phone": restaurant.phone,
        "customer_name": customer.name if customer else (order.guest_name or "Customer"),
        "customer_email": customer.email if customer else None,
        "items": order.items, "subtotal": float(order.subtotal),
        "discount_amount": float(order.discount_amount),
        "tax_amount": float(order.tax_amount or 0), "tax_percent": float(order.tax_percent or 0)}


@admin_router.get("")
def admin_restaurant_payments(restaurant_id: uuid.UUID,
        q: str | None = Query(default=None, max_length=120),
        payment_status: str | None = Query(default=None, pattern="^(PENDING|PAID|FAILED|REFUNDED)$"),
        transaction_type: str | None = Query(default=None, pattern="^(RESERVATION_FEE|ORDER_PAYMENT)$"),
        payment_method: str | None = Query(default=None, max_length=30),
        refund_status: str | None = Query(default=None, pattern="^(NONE|REFUNDED)$"),
        date_from: date | None = None, date_to: date | None = None,
        sort: str = Query(default="newest", pattern="^(newest|oldest|amount-low|amount-high)$"),
        page: int = Query(default=1, ge=1), page_size: int = Query(default=20, ge=1, le=100),
        admin: User = Depends(current_admin), db: Session = Depends(get_db)) -> dict:
    if db.query(Restaurant.id).filter(Restaurant.id == restaurant_id).first() is None:
        raise HTTPException(status_code=404, detail="Restaurant not found.")
    return list_payments(db, restaurant_id, q=q, payment_status=payment_status,
        transaction_type=transaction_type, payment_method=payment_method, refund_status=refund_status,
        date_from=date_from, date_to=date_to, sort=sort, page=page, page_size=page_size)


@admin_router.get("/{transaction_id}")
def admin_restaurant_payment_details(restaurant_id: uuid.UUID, transaction_id: uuid.UUID,
        admin: User = Depends(current_admin), db: Session = Depends(get_db)) -> dict:
    restaurant = db.query(Restaurant).filter(Restaurant.id == restaurant_id).first()
    item = db.query(Reservation).filter(Reservation.id == transaction_id,
        Reservation.restaurant_id == restaurant_id).first()
    if item is not None:
        customer = db.query(User).filter(User.id == item.user_id).first()
        return {**payment_view(item), "restaurant_name": restaurant.name if restaurant else "DineBook",
            "restaurant_location": ", ".join(part for part in (restaurant.city, restaurant.state) if part) if restaurant else None,
            "restaurant_address": restaurant.address if restaurant else None,
            "restaurant_email": restaurant.email if restaurant else None,
            "restaurant_phone": restaurant.phone if restaurant else None,
            "customer_name": customer.name if customer else "Customer",
            "customer_email": customer.email if customer else None,
            "description": f"Reservation fee for {item.reservation_date.isoformat()}"}
    order = db.query(FoodOrder).filter(FoodOrder.id == transaction_id,
        FoodOrder.restaurant_id == restaurant_id).first()
    if order is None:
        raise HTTPException(status_code=404, detail="Transaction not found.")
    customer = db.query(User).filter(User.id == order.customer_id).first() if order.customer_id else None
    return {**order_payment_view(order), "restaurant_name": restaurant.name if restaurant else "DineBook",
        "restaurant_location": ", ".join(part for part in (restaurant.city, restaurant.state) if part) if restaurant else None,
        "restaurant_address": restaurant.address if restaurant else None,
        "restaurant_email": restaurant.email if restaurant else None,
        "restaurant_phone": restaurant.phone if restaurant else None,
        "customer_name": customer.name if customer else (order.guest_name or "Customer"),
        "customer_email": customer.email if customer else None,
        "items": order.items, "subtotal": float(order.subtotal),
        "discount_amount": float(order.discount_amount),
        "tax_amount": float(order.tax_amount or 0), "tax_percent": float(order.tax_percent or 0)}


@transactions_router.get("")
def admin_transactions(restaurant_id: uuid.UUID | None = None,
        q: str | None = Query(default=None, max_length=120),
        payment_status: str | None = Query(default=None, pattern="^(PENDING|PAID|FAILED|REFUNDED)$"),
        transaction_type: str | None = Query(default=None, pattern="^(RESERVATION_FEE|ORDER_PAYMENT)$"),
        payment_method: str | None = Query(default=None, max_length=30),
        refund_status: str | None = Query(default=None, pattern="^(NONE|REFUNDED)$"),
        date_from: date | None = None, date_to: date | None = None,
        sort: str = Query(default="newest", pattern="^(newest|oldest|amount-low|amount-high|restaurant-asc|restaurant-desc)$"),
        page: int = Query(default=1, ge=1), page_size: int = Query(default=20, ge=1, le=100),
        admin: User = Depends(current_admin), db: Session = Depends(get_db)) -> dict:
    if restaurant_id is not None and db.query(Restaurant.id).filter(Restaurant.id == restaurant_id).first() is None:
        raise HTTPException(status_code=404, detail="Restaurant not found.")
    return list_payments(db, restaurant_id, q=q, payment_status=payment_status,
        transaction_type=transaction_type, payment_method=payment_method, refund_status=refund_status,
        date_from=date_from, date_to=date_to, sort=sort, page=page, page_size=page_size)



@transactions_router.get("/restaurants")
def transaction_restaurants(admin: User = Depends(current_admin), db: Session = Depends(get_db)) -> list[dict]:
    return [{"id": str(item.id), "name": item.name} for item in
            db.query(Restaurant.id, Restaurant.name).order_by(func.lower(Restaurant.name), Restaurant.id).all()]
