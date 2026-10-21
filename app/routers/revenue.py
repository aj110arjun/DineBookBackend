import uuid
from datetime import date, datetime, time, timezone
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.models.orders import FoodOrder
from app.models.reservation import Reservation
from app.models.restaurant import Restaurant
from app.models.revenue import PlatformCommission, PlatformCommissionPayout, PlatformRevenueSettings
from app.models.user import User, UserRole
from app.models.wallet import WalletAccount, WalletTransaction, WalletTransfer
from app.routers.admin_auth import current_admin
from app.routers.chef_auth import current_chef
from app.routers.manager_auth import current_manager

admin_router = APIRouter(prefix="/api/admin/revenue", tags=["revenue wallets"])
manager_router = APIRouter(prefix="/api/manager/revenue", tags=["revenue wallets"])
chef_router = APIRouter(prefix="/api/chef/revenue", tags=["revenue wallets"])
INR_TZ = ZoneInfo("Asia/Kolkata")
CENT = Decimal("0.01")
PREORDER_ORDER_NAMESPACE = uuid.UUID("89dd0385-bbad-4aeb-b600-bf4377641e10")


class CommissionUpdate(BaseModel):
    commission_percent: Decimal = Field(ge=0, le=100, max_digits=5, decimal_places=2)


class CommissionPayoutRequest(BaseModel):
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)


def settings_row(db: Session) -> PlatformRevenueSettings:
    row = db.query(PlatformRevenueSettings).filter(PlatformRevenueSettings.id == 1).first()
    if row is None:
        row = PlatformRevenueSettings(id=1, commission_percent=Decimal("10.00"))
        db.add(row)
        db.flush()
    return row


def period_bounds(period: str) -> tuple[datetime, datetime]:
    today = datetime.now(INR_TZ).date()
    start_date = date(2000, 1, 1) if period == "all" else (date(today.year, 1, 1) if period == "year" else date(today.year, today.month, 1))
    end_date = date(today.year + 1, 1, 1) if period == "year" else (
        date(today.year + 1, 1, 1) if today.month == 12 else date(today.year, today.month + 1, 1)
    )
    return (
        datetime.combine(start_date, time.min, tzinfo=INR_TZ).astimezone(timezone.utc),
        datetime.combine(end_date, time.min, tzinfo=INR_TZ).astimezone(timezone.utc),
    )


def gross_revenue(db: Session, manager_ids: list[uuid.UUID] | None, period: str) -> Decimal:
    start, end = period_bounds(period)
    fee_paid_in_period = and_(
        Reservation.payment_status == "PAID",
        Reservation.payment_method.in_(("WALLET", "RAZORPAY", "PAY_AT_DESK")),
        func.coalesce(Reservation.paid_at, Reservation.created_at) >= start,
        func.coalesce(Reservation.paid_at, Reservation.created_at) < end,
    )
    preorder_paid_in_period = and_(
        Reservation.preorder_payment_status == "PAID",
        Reservation.preorder_payment_method.in_(("WALLET", "RAZORPAY", "PAY_AT_DESK")),
        func.coalesce(Reservation.preorder_paid_at, Reservation.paid_at, Reservation.created_at) >= start,
        func.coalesce(Reservation.preorder_paid_at, Reservation.paid_at, Reservation.created_at) < end,
    )
    reservation_query = db.query(Reservation).filter(or_(fee_paid_in_period, preorder_paid_in_period))
    order_query = db.query(FoodOrder).filter(
        FoodOrder.payment_status == "PAID",
        FoodOrder.payment_method.in_(("WALLET", "RAZORPAY", "PAY_AT_DESK")),
        FoodOrder.status.notin_(("CANCELLED", "REJECTED")),
        func.coalesce(FoodOrder.paid_at, FoodOrder.created_at) >= start,
        func.coalesce(FoodOrder.paid_at, FoodOrder.created_at) < end,
    )
    if manager_ids is not None:
        restaurant_ids = [row.id for row in db.query(Restaurant.id).filter(Restaurant.manager_id.in_(manager_ids)).all()]
        if not restaurant_ids:
            return Decimal("0.00")
        reservation_query = reservation_query.filter(Reservation.restaurant_id.in_(restaurant_ids))
        order_query = order_query.filter(FoodOrder.restaurant_id.in_(restaurant_ids))

    total = Decimal("0.00")
    reservations = reservation_query.all()
    for row in reservations:
        fee_paid_at = row.paid_at or row.created_at
        if fee_paid_at.tzinfo is None:
            fee_paid_at = fee_paid_at.replace(tzinfo=timezone.utc)
        if row.payment_status == "PAID" and start <= fee_paid_at < end:
            total += Decimal(row.fee_amount or 0)
        preorder_paid_at = row.preorder_paid_at or row.paid_at or row.created_at
        if preorder_paid_at.tzinfo is None:
            preorder_paid_at = preorder_paid_at.replace(tzinfo=timezone.utc)
        if row.preorder_payment_status == "PAID" and start <= preorder_paid_at < end:
            total += Decimal(row.preorder_total or 0)

    for row in order_query.all():
        if row.reservation_id and row.idempotency_key == uuid.uuid5(PREORDER_ORDER_NAMESPACE, str(row.reservation_id)):
            continue
        total += Decimal(row.total or 0)
    return total.quantize(CENT, rounding=ROUND_HALF_UP)


def sync_commission_records(db: Session, manager_ids: list[uuid.UUID] | None,
                            commission_percent: Decimal) -> None:
    """Snapshot a commission row once for each paid reservation fee, preorder, or food order."""
    reservation_query = db.query(Reservation).filter(
        Reservation.payment_status == "PAID",
        Reservation.payment_method.in_(("WALLET", "RAZORPAY", "PAY_AT_DESK")),
    )
    order_query = db.query(FoodOrder).filter(
        FoodOrder.payment_status == "PAID",
        FoodOrder.payment_method.in_(("WALLET", "RAZORPAY", "PAY_AT_DESK")),
        FoodOrder.status.notin_(("CANCELLED", "REJECTED")),
    )
    if manager_ids is not None:
        restaurant_ids = [row.id for row in db.query(Restaurant.id).filter(Restaurant.manager_id.in_(manager_ids)).all()]
        if not restaurant_ids:
            return
        reservation_query = reservation_query.filter(Reservation.restaurant_id.in_(restaurant_ids))
        order_query = order_query.filter(FoodOrder.restaurant_id.in_(restaurant_ids))

    existing = {(row.source_type, row.source_id) for row in db.query(
        PlatformCommission.source_type, PlatformCommission.source_id
    ).all()}
    restaurants = {row.id: row.manager_id for row in db.query(Restaurant.id, Restaurant.manager_id).all()}

    def add_record(source_type: str, source_id: uuid.UUID, restaurant_id: uuid.UUID,
                   amount: Decimal, occurred_at: datetime | None) -> None:
        manager_id = restaurants.get(restaurant_id)
        key = (source_type, source_id)
        amount = Decimal(amount or 0).quantize(CENT, rounding=ROUND_HALF_UP)
        if manager_id is None or key in existing or amount <= 0:
            return
        rate = Decimal(commission_percent)
        commission = (amount * rate / Decimal("100")).quantize(CENT, rounding=ROUND_HALF_UP)
        timestamp = occurred_at or datetime.now(timezone.utc)
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        db.add(PlatformCommission(manager_id=manager_id, restaurant_id=restaurant_id,
            source_type=source_type, source_id=source_id, gross_amount=amount,
            commission_percent=rate, commission_amount=commission, occurred_at=timestamp))
        existing.add(key)

    reservations = reservation_query.all()
    for row in reservations:
        fee_paid_at = row.paid_at or row.created_at
        add_record("RESERVATION_FEE", row.id, row.restaurant_id, Decimal(row.fee_amount or 0), fee_paid_at)
        preorder_paid_at = row.preorder_paid_at or row.paid_at or row.created_at
        if (row.preorder_payment_status == "PAID" and
                row.preorder_payment_method in ("WALLET", "RAZORPAY", "PAY_AT_DESK")):
            add_record("PREORDER", row.id, row.restaurant_id, Decimal(row.preorder_total or 0), preorder_paid_at)

    for row in order_query.all():
        if row.reservation_id and row.idempotency_key == uuid.uuid5(PREORDER_ORDER_NAMESPACE, str(row.reservation_id)):
            continue
        add_record("FOOD_ORDER", row.id, row.restaurant_id, Decimal(row.total or 0), row.paid_at or row.created_at)


def transferred_total(db: Session, manager_ids: list[uuid.UUID] | None, period: str,
                      chef_id: uuid.UUID | None = None) -> Decimal:
    start, end = period_bounds(period)
    query = db.query(func.coalesce(func.sum(WalletTransfer.amount), 0)).filter(
        WalletTransfer.created_at >= start, WalletTransfer.created_at < end
    )
    if manager_ids is not None:
        if not manager_ids:
            return Decimal("0.00")
        query = query.filter(WalletTransfer.manager_id.in_(manager_ids))
    if chef_id is not None:
        query = query.filter(WalletTransfer.chef_id == chef_id)
    return Decimal(query.scalar() or 0).quantize(CENT, rounding=ROUND_HALF_UP)


def wallet_summary(db: Session, manager_ids: list[uuid.UUID] | None, settings: PlatformRevenueSettings,
                   chef_id: uuid.UUID | None = None) -> dict:
    commission_rate = Decimal(settings.commission_percent)
    sync_commission_records(db, manager_ids, commission_rate)
    gross_month = gross_revenue(db, manager_ids, "month")
    gross_year = gross_revenue(db, manager_ids, "year")
    gross_all = gross_revenue(db, manager_ids, "all")
    chefs_query = db.query(User).filter(User.role == UserRole.CHEF, User.is_active.is_(True))
    if manager_ids is not None:
        chefs_query = chefs_query.filter(User.manager_id.in_(manager_ids))
    chefs = chefs_query.order_by(User.name.asc()).all()

    if chef_id is not None:
        chef_month = transferred_total(db, None, "month", chef_id)
        chef_year = transferred_total(db, None, "year", chef_id)
        wallet = db.query(WalletAccount).filter(WalletAccount.user_id == chef_id).first()
        if wallet is None:
            wallet = WalletAccount(user_id=chef_id, balance=Decimal("0.00"))
            db.add(wallet)
            db.flush()
        transactions = db.query(WalletTransaction).filter(WalletTransaction.user_id == chef_id).order_by(WalletTransaction.created_at.desc()).limit(100).all()
        return {
            "role": "CHEF", "balance": float(wallet.balance), "earned_this_month": float(chef_month),
            "earned_this_year": float(chef_year),
            "commission_percent": float(commission_rate),
            "transactions": [{"id": str(row.id), "amount": float(row.amount), "type": row.transaction_type,
                "description": row.description, "created_at": row.created_at.isoformat()} for row in transactions],
        }

    chef_month = transferred_total(db, manager_ids, "month")
    chef_year = transferred_total(db, manager_ids, "year")
    commission_query = db.query(PlatformCommission)
    payout_query = db.query(PlatformCommissionPayout)
    if manager_ids is not None:
        commission_query = commission_query.filter(PlatformCommission.manager_id.in_(manager_ids))
        payout_query = payout_query.filter(PlatformCommissionPayout.manager_id.in_(manager_ids))
    start, end = period_bounds("month")
    year_start, year_end = period_bounds("year")
    admin_month = Decimal(commission_query.filter(PlatformCommission.occurred_at >= start,
        PlatformCommission.occurred_at < end).with_entities(func.coalesce(func.sum(PlatformCommission.commission_amount), 0)).scalar() or 0).quantize(CENT)
    admin_year = Decimal(commission_query.filter(PlatformCommission.occurred_at >= year_start,
        PlatformCommission.occurred_at < year_end).with_entities(func.coalesce(func.sum(PlatformCommission.commission_amount), 0)).scalar() or 0).quantize(CENT)
    admin_all = Decimal(commission_query.with_entities(func.coalesce(func.sum(PlatformCommission.commission_amount), 0)).scalar() or 0).quantize(CENT)
    pending_commission = Decimal(commission_query.filter(PlatformCommission.payout_id.is_(None)).with_entities(
        func.coalesce(func.sum(PlatformCommission.commission_amount), 0)).scalar() or 0).quantize(CENT)
    paid_out = Decimal(payout_query.with_entities(func.coalesce(func.sum(PlatformCommissionPayout.amount), 0)).scalar() or 0).quantize(CENT)
    recent_commissions = commission_query.order_by(PlatformCommission.occurred_at.desc()).limit(50).all()
    recent_payouts = payout_query.order_by(PlatformCommissionPayout.created_at.desc()).limit(20).all()
    payout_manager_ids = {row.manager_id for row in recent_payouts}
    payout_manager_names = {row.id: row.name for row in db.query(User.id, User.name).filter(
        User.id.in_(payout_manager_ids)
    ).all()} if payout_manager_ids else {}
    admin_wallet_balance = Decimal("0.00")
    if manager_ids is None:
        admin_user = db.query(User).filter(User.role == UserRole.ADMIN).order_by(User.created_at.asc()).first()
        if admin_user is not None:
            admin_wallet = db.query(WalletAccount).filter(WalletAccount.user_id == admin_user.id).first()
            if admin_wallet is not None:
                admin_wallet_balance = Decimal(admin_wallet.balance)
    manager_wallet_balance = None
    if manager_ids is not None and len(manager_ids) == 1:
        wallet = db.query(WalletAccount).filter(WalletAccount.user_id == manager_ids[0]).first()
        if wallet is None:
            wallet = WalletAccount(user_id=manager_ids[0], balance=Decimal("0.00"))
            db.add(wallet)
            db.flush()
        manager_wallet_balance = Decimal(wallet.balance)
    return {
        "role": "ADMIN" if manager_ids is None else "MANAGER",
        "balance": float(admin_wallet_balance if manager_ids is None else manager_wallet_balance),
        "gross_this_month": float(gross_month), "gross_this_year": float(gross_year),
        "gross_all_time": float(gross_all), "earned_this_month": float(admin_month if manager_ids is None else gross_month - admin_month),
        "earned_this_year": float(admin_year if manager_ids is None else gross_year - admin_year),
        "admin_commission_this_month": float(admin_month), "admin_commission_this_year": float(admin_year),
        "admin_commission_outstanding": float(pending_commission if manager_ids is not None else admin_all - paid_out),
        "admin_commission_paid_out": float(paid_out),
        "admin_received_this_month": float(Decimal(payout_query.filter(PlatformCommissionPayout.created_at >= start,
            PlatformCommissionPayout.created_at < end).with_entities(func.coalesce(func.sum(PlatformCommissionPayout.amount), 0)).scalar() or 0).quantize(CENT)),
        "commission_records": [{"id": str(row.id), "type": row.source_type, "gross_amount": float(row.gross_amount),
            "commission_amount": float(row.commission_amount), "commission_percent": float(row.commission_percent),
            "occurred_at": row.occurred_at.isoformat(), "paid": row.payout_id is not None} for row in recent_commissions],
        "commission_payouts": [{"id": str(row.id), "amount": float(row.amount), "created_at": row.created_at.isoformat(),
            "manager_name": payout_manager_names.get(row.manager_id)}
            for row in recent_payouts],
        "transferred_to_chefs_this_month": float(chef_month), "transferred_to_chefs_this_year": float(chef_year),
        "commission_percent": float(commission_rate), "chefs": [
            {"id": str(chef.id), "name": chef.name,
             "transferred_this_month": float(transferred_total(db, [chef.manager_id], "month", chef.id)),
             "transferred_this_year": float(transferred_total(db, [chef.manager_id], "year", chef.id))}
            for chef in chefs
        ],
    }


@admin_router.get("")
def admin_revenue(admin: User = Depends(current_admin), db: Session = Depends(get_db)) -> dict:
    row = settings_row(db)
    db.commit()
    result = wallet_summary(db, None, row)
    db.commit()
    return {**result, "settings": {"commission_percent": float(row.commission_percent)}}


@admin_router.patch("/settings")
def update_commission(payload: CommissionUpdate, admin: User = Depends(current_admin), db: Session = Depends(get_db)) -> dict:
    row = settings_row(db)
    # Materialize all currently paid transactions with the rate they already had
    # before changing the setting. New paid transactions will use the new rate.
    sync_commission_records(db, None, Decimal(row.commission_percent))
    row.commission_percent = payload.commission_percent
    db.commit()
    return {"commission_percent": float(row.commission_percent)}


@manager_router.post("/commission-payouts")
def pay_admin_commission(payload: CommissionPayoutRequest, manager: User = Depends(current_manager), db: Session = Depends(get_db)) -> dict:
    settings = settings_row(db)
    sync_commission_records(db, [manager.id], Decimal(settings.commission_percent))
    commission_rows = db.query(PlatformCommission).filter(
        PlatformCommission.manager_id == manager.id,
        PlatformCommission.payout_id.is_(None),
    ).with_for_update().all()
    amount = sum((Decimal(row.commission_amount) for row in commission_rows), Decimal("0.00")).quantize(CENT)
    if amount <= 0:
        raise HTTPException(status_code=409, detail="There is no unpaid admin commission to transfer.")
    if amount != payload.amount:
        raise HTTPException(status_code=409, detail="The unpaid commission total changed. Refresh the page and confirm the updated amount.")

    manager_wallet = db.query(WalletAccount).filter(WalletAccount.user_id == manager.id).with_for_update().first()
    if manager_wallet is None or Decimal(manager_wallet.balance) < amount:
        available = Decimal(manager_wallet.balance) if manager_wallet is not None else Decimal("0.00")
        raise HTTPException(status_code=409, detail=f"Your wallet has {available:.2f}; {amount:.2f} is due to the admin.")
    admin = db.query(User).filter(User.role == UserRole.ADMIN, User.is_active.is_(True)).order_by(User.created_at.asc()).first()
    if admin is None:
        raise HTTPException(status_code=409, detail="An active admin wallet is not available.")
    admin_wallet = db.query(WalletAccount).filter(WalletAccount.user_id == admin.id).with_for_update().first()
    if admin_wallet is None:
        admin_wallet = WalletAccount(user_id=admin.id, balance=Decimal("0.00"))
        db.add(admin_wallet)
        db.flush()

    payout = PlatformCommissionPayout(manager_id=manager.id, admin_id=admin.id, amount=amount)
    db.add(payout)
    db.flush()
    for row in commission_rows:
        row.payout_id = payout.id
    manager_wallet.balance = Decimal(manager_wallet.balance) - amount
    admin_wallet.balance = Decimal(admin_wallet.balance) + amount
    db.add(WalletTransaction(user_id=manager.id, amount=-amount, transaction_type="DEBIT",
        description="Admin commission payout", reference=f"admin-commission-payout:{payout.id}"))
    db.add(WalletTransaction(user_id=admin.id, amount=amount, transaction_type="CREDIT",
        description=f"Admin commission from {manager.name}", reference=f"admin-commission-payout:{payout.id}"))
    db.commit()
    return {"message": "Admin commission transferred.", "amount": float(amount),
        "manager_balance": float(manager_wallet.balance), "admin_balance": float(admin_wallet.balance)}


@manager_router.get("")
def manager_revenue(manager: User = Depends(current_manager), db: Session = Depends(get_db)) -> dict:
    row = settings_row(db)
    result = wallet_summary(db, [manager.id], row)
    db.commit()
    return result


@chef_router.get("")
def chef_revenue(chef: User = Depends(current_chef), db: Session = Depends(get_db)) -> dict:
    row = settings_row(db)
    result = wallet_summary(db, [chef.manager_id] if chef.manager_id else [], row, chef.id)
    db.commit()
    return result
