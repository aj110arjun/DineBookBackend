from datetime import date

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import String, case, cast, func, literal, or_, select, union_all
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.models.orders import FoodOrder
from app.models.reservation import Reservation
from app.models.restaurant import Restaurant
from app.models.user import User
from app.models.wallet import WalletTopUp, WalletTransaction
from app.routers.customer_auth import current_customer

router = APIRouter(prefix="/api/customer", tags=["customer transactions"])


def transaction_status(value: WalletTransaction) -> str:
    if value.transaction_type == "REFUND":
        return "REFUNDED"
    return "CREDITED" if value.amount > 0 else "DEBITED"


@router.get("/transactions")
def customer_transactions(
    q: str | None = Query(default=None, max_length=120),
    transaction_type: str | None = Query(default=None, pattern="^(RESERVATION_FEE|FOOD_ORDER|WALLET)$"),
    wallet_direction: str | None = Query(default=None, pattern="^(CREDIT|DEBIT)$"),
    status: str | None = Query(default=None, max_length=24),
    date_from: date | None = None,
    date_to: date | None = None,
    sort: str = Query(default="newest", pattern="^(newest|oldest|amount-low|amount-high)$"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1, le=100),
    customer: User = Depends(current_customer),
    db: Session = Depends(get_db),
) -> dict:
    """Return the signed-in customer's reservation, order, and wallet transactions."""
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="Start date must be on or before end date.")

    reservation_rows = select(
        cast(Reservation.id, String).label("id"),
        literal("RESERVATION_FEE").label("transaction_type"),
        literal("Reservation fee").label("description"),
        Reservation.fee_amount.label("amount"),
        Reservation.payment_method.label("payment_method"),
        Reservation.payment_status.label("status"),
        Reservation.created_at.label("created_at"),
        Reservation.razorpay_payment_id.label("provider_reference"),
        Reservation.razorpay_order_id.label("provider_order_reference"),
        cast(Reservation.id, String).label("related_id"),
        Restaurant.name.label("restaurant_name"),
    ).join(Restaurant, Restaurant.id == Reservation.restaurant_id).where(Reservation.user_id == customer.id)

    order_rows = select(
        cast(FoodOrder.id, String).label("id"),
        literal("FOOD_ORDER").label("transaction_type"),
        literal("Food order payment").label("description"),
        FoodOrder.total.label("amount"),
        FoodOrder.payment_method.label("payment_method"),
        FoodOrder.payment_status.label("status"),
        FoodOrder.created_at.label("created_at"),
        cast(None, String).label("provider_reference"),
        cast(None, String).label("provider_order_reference"),
        cast(FoodOrder.reservation_id, String).label("related_id"),
        Restaurant.name.label("restaurant_name"),
    ).join(Restaurant, Restaurant.id == FoodOrder.restaurant_id).where(FoodOrder.customer_id == customer.id)

    wallet_rows = select(
        cast(WalletTransaction.id, String).label("id"),
        literal("WALLET").label("transaction_type"),
        WalletTransaction.description.label("description"),
        func.abs(WalletTransaction.amount).label("amount"),
        literal("DINEBOOK_WALLET").label("payment_method"),
        case(
            (WalletTransaction.transaction_type == "REFUND", "REFUNDED"),
            (WalletTransaction.amount > 0, "CREDITED"),
            else_="DEBITED",
        ).label("status"),
        WalletTransaction.created_at.label("created_at"),
        WalletTransaction.reference.label("provider_reference"),
        cast(None, String).label("provider_order_reference"),
        cast(WalletTransaction.reservation_id, String).label("related_id"),
        cast(None, String).label("restaurant_name"),
    ).where(WalletTransaction.user_id == customer.id)

    failed_topup_rows = select(
        cast(WalletTopUp.id, String).label("id"),
        literal("WALLET").label("transaction_type"),
        literal("Wallet top-up payment failed").label("description"),
        WalletTopUp.amount.label("amount"),
        literal("RAZORPAY").label("payment_method"),
        literal("FAILED").label("status"),
        WalletTopUp.created_at.label("created_at"),
        WalletTopUp.razorpay_payment_id.label("provider_reference"),
        WalletTopUp.razorpay_order_id.label("provider_order_reference"),
        cast(None, String).label("related_id"),
        cast(None, String).label("restaurant_name"),
    ).where(WalletTopUp.user_id == customer.id, WalletTopUp.status == "FAILED")

    combined = union_all(reservation_rows, order_rows, wallet_rows, failed_topup_rows).subquery("customer_transactions")
    query = select(combined)
    count_query = select(func.count()).select_from(combined)
    conditions = []
    if transaction_type:
        conditions.append(combined.c.transaction_type == transaction_type)
    if wallet_direction == "CREDIT":
        conditions.append(combined.c.status.in_(["CREDITED", "REFUNDED"]))
    elif wallet_direction == "DEBIT":
        conditions.append(combined.c.status.in_(["DEBITED", "FAILED"]))
    if status:
        conditions.append(func.upper(combined.c.status) == status.strip().upper())
    if date_from:
        conditions.append(func.date(combined.c.created_at) >= date_from)
    if date_to:
        conditions.append(func.date(combined.c.created_at) <= date_to)
    if q and q.strip():
        needle = f"%{q.strip()}%"
        conditions.append(or_(
            combined.c.id.ilike(needle), combined.c.description.ilike(needle),
            combined.c.provider_reference.ilike(needle), combined.c.provider_order_reference.ilike(needle),
            combined.c.restaurant_name.ilike(needle),
        ))
    if conditions:
        query = query.where(*conditions)
        count_query = count_query.where(*conditions)

    total = db.execute(count_query).scalar_one()
    ordering = combined.c.amount.asc() if sort == "amount-low" else combined.c.amount.desc() if sort == "amount-high" else combined.c.created_at.asc() if sort == "oldest" else combined.c.created_at.desc()
    rows = db.execute(
        query.order_by(ordering, combined.c.id.desc())
        .offset((page - 1) * page_size).limit(page_size)
    ).mappings().all()
    transactions = [{
        **dict(row),
        "amount": float(row["amount"] or 0),
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
    } for row in rows]
    return {"transactions": transactions, "total": total, "page": page, "page_size": page_size}


@router.get("/transactions/{transaction_type}/{transaction_id}")
def customer_transaction_details(
    transaction_type: str,
    transaction_id: uuid.UUID,
    customer: User = Depends(current_customer),
    db: Session = Depends(get_db),
) -> dict:
    transaction_type = transaction_type.replace("-", "_").upper()
    if transaction_type == "RESERVATION_FEE":
        item = db.query(Reservation).filter(Reservation.id == transaction_id, Reservation.user_id == customer.id).first()
        if item is None:
            raise HTTPException(status_code=404, detail="Transaction not found.")
        restaurant = db.query(Restaurant).filter(Restaurant.id == item.restaurant_id).first()
        return {
            "id": str(item.id), "transaction_type": transaction_type, "description": f"Reservation fee for {item.reservation_date.isoformat()}",
            "amount": float(item.fee_amount), "subtotal": float(item.fee_amount), "discount_amount": 0,
            "payment_method": item.payment_method, "status": item.payment_status, "created_at": item.created_at.isoformat() if item.created_at else None,
            "provider_reference": item.razorpay_payment_id, "provider_order_reference": item.razorpay_order_id,
            "related_id": str(item.id), "restaurant_name": restaurant.name if restaurant else "DineBook",
            "restaurant_location": ", ".join(part for part in (restaurant.city, restaurant.state) if part) if restaurant else None,
            "restaurant_address": restaurant.address if restaurant else None, "restaurant_email": restaurant.email if restaurant else None,
            "restaurant_phone": restaurant.phone if restaurant else None, "customer_name": customer.name, "customer_email": customer.email,
            "currency": "INR", "refund_status": "REFUNDED" if item.payment_status == "REFUNDED" else "NONE",
        }

    if transaction_type == "FOOD_ORDER":
        item = db.query(FoodOrder).filter(FoodOrder.id == transaction_id, FoodOrder.customer_id == customer.id).first()
        if item is None:
            raise HTTPException(status_code=404, detail="Transaction not found.")
        restaurant = db.query(Restaurant).filter(Restaurant.id == item.restaurant_id).first()
        return {
            "id": str(item.id), "transaction_type": transaction_type, "description": "Food order payment",
            "amount": float(item.total), "subtotal": float(item.subtotal), "discount_amount": float(item.discount_amount or 0),
            "items": item.items, "payment_method": item.payment_method, "status": item.payment_status,
            "created_at": item.created_at.isoformat() if item.created_at else None, "provider_reference": None,
            "provider_order_reference": None, "related_id": str(item.reservation_id) if item.reservation_id else str(item.id),
            "order_id": str(item.id), "reservation_id": str(item.reservation_id) if item.reservation_id else None,
            "restaurant_name": restaurant.name if restaurant else "DineBook",
            "restaurant_location": ", ".join(part for part in (restaurant.city, restaurant.state) if part) if restaurant else None,
            "restaurant_address": restaurant.address if restaurant else None, "restaurant_email": restaurant.email if restaurant else None,
            "restaurant_phone": restaurant.phone if restaurant else None, "customer_name": customer.name, "customer_email": customer.email,
            "currency": "INR", "refund_status": "REFUNDED" if item.payment_status == "REFUNDED" else "NONE",
        }

    if transaction_type == "WALLET":
        item = db.query(WalletTransaction).filter(WalletTransaction.id == transaction_id, WalletTransaction.user_id == customer.id).first()
        if item is None:
            topup = db.query(WalletTopUp).filter(
                WalletTopUp.id == transaction_id, WalletTopUp.user_id == customer.id,
                WalletTopUp.status == "FAILED",
            ).first()
            if topup is None:
                raise HTTPException(status_code=404, detail="Transaction not found.")
            return {
                "id": str(topup.id), "transaction_type": transaction_type,
                "description": "Wallet top-up payment failed", "amount": float(topup.amount),
                "subtotal": float(topup.amount), "discount_amount": 0, "payment_method": "RAZORPAY",
                "status": "FAILED", "created_at": topup.created_at.isoformat() if topup.created_at else None,
                "provider_reference": topup.razorpay_payment_id, "provider_order_reference": topup.razorpay_order_id,
                "related_id": None, "customer_name": customer.name, "customer_email": customer.email,
                "restaurant_name": "DineBook", "currency": "INR", "refund_status": "NONE",
                "category": "wallet_top_up",
            }
        topup = db.query(WalletTopUp).filter(WalletTopUp.user_id == customer.id, WalletTopUp.razorpay_payment_id == item.reference).first()
        return {
            "id": str(item.id), "transaction_type": transaction_type, "description": item.description,
            "amount": abs(float(item.amount)), "subtotal": abs(float(item.amount)), "discount_amount": 0,
            "payment_method": "RAZORPAY" if topup else "DINEBOOK_WALLET", "status": transaction_status(item),
            "created_at": item.created_at.isoformat() if item.created_at else None,
            "provider_reference": topup.razorpay_payment_id if topup else item.reference,
            "provider_order_reference": topup.razorpay_order_id if topup else None,
            "related_id": str(item.reservation_id) if item.reservation_id else None,
            "customer_name": customer.name, "customer_email": customer.email, "restaurant_name": "DineBook",
            "currency": "INR", "refund_status": "REFUNDED" if transaction_status(item) == "REFUNDED" else "NONE",
            "category": "wallet_top_up" if topup else "wallet",
        }

    raise HTTPException(status_code=404, detail="Transaction not found.")
