"""Restaurant payment credits and automatic platform commission settlement."""
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.orders import FoodOrder
from app.models.restaurant import Restaurant
from app.models.revenue import PlatformCommission
from app.models.wallet import WalletTransaction
from app.services.commission import (
    locked_wallets, money, record_wallet_entry, reverse_payment_commission,
    recipient_admin, settings_row, settle_payment_commission,
)


PREORDER_ORDER_NAMESPACE = uuid.UUID("89dd0385-bbad-4aeb-b600-bf4377641e10")


def payment_source(db: Session, restaurant_id: uuid.UUID, reference: str) -> tuple[str, uuid.UUID]:
    parts = reference.split(":")
    if (len(parts) == 4 and parts[0] in {"customer-payment", "customer-refund"}
            and parts[1] == "reservation" and parts[3] in {"fee", "preorder"}):
        return ("RESERVATION_FEE" if parts[3] == "fee" else "PREORDER"), uuid.UUID(parts[2])
    if (len(parts) == 3 and parts[0] in {"customer-payment", "customer-refund"}
            and parts[1] == "order"):
        order_id = uuid.UUID(parts[2])
        order = db.query(FoodOrder).filter(FoodOrder.id == order_id).first()
        if order is not None:
            if order.restaurant_id != restaurant_id:
                raise HTTPException(status_code=409, detail="Payment source belongs to another restaurant.")
            if (order.reservation_id and order.idempotency_key == uuid.uuid5(
                    PREORDER_ORDER_NAMESPACE, str(order.reservation_id))):
                return "PREORDER", order.reservation_id
        return "FOOD_ORDER", order_id
    raise ValueError(f"Unsupported restaurant payment reference: {reference}")


def locked_restaurant(db: Session, restaurant_id: uuid.UUID) -> Restaurant:
    restaurant = db.query(Restaurant).filter(Restaurant.id == restaurant_id).with_for_update().first()
    if restaurant is None or restaurant.manager_id is None:
        raise HTTPException(status_code=409, detail="A restaurant manager is required.")
    return restaurant


def commission_record(db: Session, restaurant_id: uuid.UUID, source_type: str,
                      source_id: uuid.UUID, amount: Decimal) -> PlatformCommission | None:
    record = db.query(PlatformCommission).filter(
        PlatformCommission.source_type == source_type, PlatformCommission.source_id == source_id,
    ).populate_existing().with_for_update().first()
    if record is not None:
        if record.restaurant_id != restaurant_id:
            raise HTTPException(status_code=409, detail="Payment source belongs to another restaurant.")
        if money(record.gross_amount) != amount:
            raise HTTPException(status_code=409, detail="Payment amount differs from its recorded amount.")
    return record


def record_restaurant_payment(db: Session, restaurant_id: uuid.UUID, amount: Decimal,
                              description: str, reference: str, *, credit_gross: bool = True,
                              occurred_at: datetime | None = None) -> None:
    """Credit digital funds and settle commission atomically; cash has no gross wallet credit."""
    amount = money(amount)
    if amount <= 0:
        return
    restaurant = locked_restaurant(db, restaurant_id)
    # SessionLocal disables autoflush; make newly created order IDs visible first.
    db.flush()
    source_type, source_id = payment_source(db, restaurant_id, reference)
    record = commission_record(db, restaurant_id, source_type, source_id, amount)
    if record is not None:
        if record.refunded_at is not None:
            raise HTTPException(status_code=409, detail="This payment has already been refunded.")
        # Historical commission records already represent a credited/collected payment.
        # Settle only their unpaid commission, without crediting the gross again.
        settle_payment_commission(db, record)
        return

    rate = Decimal(settings_row(db).commission_percent)
    record = PlatformCommission(manager_id=restaurant.manager_id, restaurant_id=restaurant_id,
        source_type=source_type, source_id=source_id, gross_amount=amount,
        commission_percent=rate, commission_amount=money(amount * rate / Decimal("100")),
        occurred_at=occurred_at or datetime.now(timezone.utc))
    db.add(record)
    db.flush()
    if credit_gross:
        admin = recipient_admin(db)
        if admin is None:
            raise HTTPException(status_code=503, detail="An active admin is required to settle commission.")
        account = locked_wallets(db, [restaurant.manager_id, admin.id])[restaurant.manager_id]
        # A pre-cutover gross ledger entry must never be credited a second time.
        previous = db.query(WalletTransaction.id).filter(
            WalletTransaction.user_id == restaurant.manager_id,
            WalletTransaction.reference == reference,
            WalletTransaction.transaction_type == "CREDIT",
        ).first()
        if previous is None:
            record_wallet_entry(db, account, amount, description, reference)
    settle_payment_commission(db, record, require_balance=not credit_gross)


def reverse_restaurant_payment(db: Session, restaurant_id: uuid.UUID, amount: Decimal,
                               description: str, reference: str) -> None:
    """Reverse one full payment component, including its settled commission."""
    amount = money(amount)
    if amount <= 0:
        return
    restaurant = locked_restaurant(db, restaurant_id)
    db.flush()
    source_type, source_id = payment_source(db, restaurant_id, reference)
    record = commission_record(db, restaurant_id, source_type, source_id, amount)
    if record is not None and record.refunded_at is not None:
        return
    manager_id = record.manager_id if record is not None else restaurant.manager_id
    previous = db.query(WalletTransaction.id).filter(
        WalletTransaction.user_id == manager_id, WalletTransaction.reference == reference,
        WalletTransaction.transaction_type == "DEBIT",
    ).first()
    if previous is not None:
        return
    if record is not None:
        reverse_payment_commission(db, record)
    account = locked_wallets(db, [manager_id])[manager_id]
    record_wallet_entry(db, account, -amount, description, reference)
    db.flush()
