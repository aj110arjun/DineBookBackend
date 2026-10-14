import base64
import hashlib
import hmac
import json
import uuid
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import String, or_
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from app.db.database import get_db
from app.core.config import settings
from app.models.user import User, UserRole
from app.models.wallet import WalletAccount, WalletTopUp, WalletTransaction, WalletTransfer
from app.models.realtime import RealtimeEvent
from app.services.notifications import create_notification
from app.routers.admin_auth import current_admin
from app.routers.customer_auth import current_customer
from app.routers.manager_auth import current_manager

customer_router = APIRouter(prefix="/api/customer/wallet", tags=["customer wallet"])
admin_router = APIRouter(prefix="/api/admin/wallet", tags=["wallet administration"])
manager_router = APIRouter(prefix="/api/manager/wallet", tags=["manager wallet"])
MIN_TOPUP = Decimal("100.00")
MAX_TOPUP = Decimal("50000.00")


def razorpay_request(method: str, path: str, body: dict | None = None) -> dict:
    if not settings.razorpay_key_id or not settings.razorpay_key_secret:
        raise HTTPException(status_code=503, detail="Wallet top-ups are not configured yet.")
    token = base64.b64encode(f"{settings.razorpay_key_id}:{settings.razorpay_key_secret}".encode()).decode()
    payload = json.dumps(body).encode() if body is not None else None
    request = Request(f"https://api.razorpay.com/v1/{path}", data=payload, method=method,
        headers={"Authorization": f"Basic {token}", "Content-Type": "application/json"})
    try:
        with urlopen(request, timeout=15) as response:
            return json.loads(response.read())
    except HTTPError as exc:
        raise HTTPException(status_code=502, detail="The payment provider could not process this wallet request.") from exc
    except (URLError, TimeoutError, ValueError) as exc:
        raise HTTPException(status_code=502, detail="Could not reach the payment provider. Please try again.") from exc


def wallet_data(account: WalletAccount, transactions: list[WalletTransaction], topups: list[WalletTopUp] | None = None) -> dict:
    topup_by_payment = {item.razorpay_payment_id: item for item in (topups or []) if item.razorpay_payment_id}
    return {
        "balance": float(account.balance),
        "transactions": [{
            "id": str(item.id), "amount": float(item.amount),
            "type": item.transaction_type, "description": item.description,
            "reference": item.reference,
            "razorpay_order_id": topup_by_payment[item.reference].razorpay_order_id if item.reference in topup_by_payment else None,
            "payment_status": topup_by_payment[item.reference].status if item.reference in topup_by_payment else None,
            "reservation_id": str(item.reservation_id) if item.reservation_id else None,
            "created_at": item.created_at.isoformat(),
        } for item in transactions],
    }


@customer_router.get("/activity")
def get_wallet_activity(
    q: str | None = Query(default=None, max_length=120),
    transaction_type: str | None = Query(default=None, pattern="^(CREDIT|DEBIT)$"),
    date_from: date | None = None,
    date_to: date | None = None,
    sort: str = Query(default="newest", pattern="^(newest|oldest|amount-low|amount-high)$"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1, le=100),
    customer: User = Depends(current_customer),
    db: Session = Depends(get_db),
) -> dict:
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="Start date must be on or before end date.")
    query = db.query(WalletTransaction).filter(WalletTransaction.user_id == customer.id)
    if q and q.strip():
        needle = f"%{q.strip()}%"
        query = query.filter(or_(
            WalletTransaction.description.ilike(needle),
            WalletTransaction.reference.ilike(needle),
            WalletTransaction.reservation_id.cast(String).ilike(needle),
        ))
    if transaction_type == "CREDIT":
        query = query.filter(or_(WalletTransaction.transaction_type.in_(["CREDIT", "REFUND"]), WalletTransaction.amount > 0))
    elif transaction_type == "DEBIT":
        query = query.filter(or_(WalletTransaction.transaction_type == "DEBIT", WalletTransaction.amount < 0))
    if date_from:
        query = query.filter(WalletTransaction.created_at >= datetime.combine(date_from, time.min, tzinfo=timezone.utc))
    if date_to:
        query = query.filter(WalletTransaction.created_at < datetime.combine(date_to + timedelta(days=1), time.min, tzinfo=timezone.utc))
    total = query.count()
    if sort == "amount-low":
        query = query.order_by(WalletTransaction.amount.asc(), WalletTransaction.created_at.desc())
    elif sort == "amount-high":
        query = query.order_by(WalletTransaction.amount.desc(), WalletTransaction.created_at.desc())
    elif sort == "oldest":
        query = query.order_by(WalletTransaction.created_at.asc(), WalletTransaction.id.asc())
    else:
        query = query.order_by(WalletTransaction.created_at.desc(), WalletTransaction.id.desc())
    rows = query.offset((page - 1) * page_size).limit(page_size).all()
    payment_refs = [row.reference for row in rows if row.reference]
    topups = db.query(WalletTopUp).filter(WalletTopUp.user_id == customer.id,
        WalletTopUp.razorpay_payment_id.in_(payment_refs)).all() if payment_refs else []
    transactions = wallet_data(WalletAccount(user_id=customer.id, balance=Decimal("0.00")), rows, topups)["transactions"]
    return {"transactions": transactions, "total": total, "page": page, "page_size": page_size}


@customer_router.get("")
def get_wallet(customer: User = Depends(current_customer), db: Session = Depends(get_db)) -> dict:
    account = db.query(WalletAccount).filter(WalletAccount.user_id == customer.id).first()
    if account is None:
        account = WalletAccount(user_id=customer.id, balance=Decimal("0.00"))
        db.add(account)
        db.commit()
        db.refresh(account)
    transactions = db.query(WalletTransaction).filter(WalletTransaction.user_id == customer.id).order_by(WalletTransaction.created_at.desc()).limit(100).all()
    payment_refs = [item.reference for item in transactions if item.reference]
    topups = db.query(WalletTopUp).filter(WalletTopUp.user_id == customer.id,
        WalletTopUp.razorpay_payment_id.in_(payment_refs)).all() if payment_refs else []
    data = wallet_data(account, transactions, topups)
    data["manual_topups_enabled"] = bool(settings.razorpay_key_id and settings.razorpay_key_id.startswith("rzp_test_"))
    return data


class AdminWalletCredit(BaseModel):
    customer_id: uuid.UUID
    amount: Decimal = Field(gt=0, le=Decimal("100000.00"), decimal_places=2)
    description: str = Field(default="Wallet credit", min_length=1, max_length=240)
    reference: str | None = Field(default=None, max_length=500)


class WalletTopUpRequest(BaseModel):
    amount: Decimal = Field(ge=MIN_TOPUP, le=MAX_TOPUP, decimal_places=2)


@customer_router.post("/manual-topup")
def manual_topup(payload: WalletTopUpRequest, customer: User = Depends(current_customer), db: Session = Depends(get_db)) -> dict:
    if not settings.razorpay_key_id or not settings.razorpay_key_id.startswith("rzp_test_"):
        raise HTTPException(status_code=403, detail="Manual wallet credits are only enabled with Razorpay test keys.")
    if payload.amount.as_tuple().exponent < -2:
        raise HTTPException(status_code=422, detail="Enter an amount with no more than two decimal places.")
    account = db.query(WalletAccount).filter(WalletAccount.user_id == customer.id).with_for_update().first()
    if account is None:
        account = WalletAccount(user_id=customer.id, balance=Decimal("0.00"))
        db.add(account)
        db.flush()
    account.balance = Decimal(account.balance) + payload.amount
    db.add(WalletTransaction(user_id=customer.id, amount=payload.amount, transaction_type="CREDIT",
        description="Manual wallet top-up (test mode)", reference="Test mode credit; no payment collected"))
    db.commit()
    db.refresh(account)
    return {"message": "Test funds added to wallet.", "balance": float(account.balance)}


class WalletTopUpVerification(BaseModel):
    razorpay_order_id: str = Field(min_length=8, max_length=80)
    razorpay_payment_id: str = Field(min_length=8, max_length=80)
    razorpay_signature: str = Field(min_length=32, max_length=256)


class WalletTopUpFailure(BaseModel):
    razorpay_order_id: str = Field(min_length=8, max_length=80)
    razorpay_payment_id: str | None = Field(default=None, min_length=8, max_length=80)


@manager_router.post("/topups/failure")
def record_manager_topup_failure(payload: WalletTopUpFailure, manager: User = Depends(current_manager), db: Session = Depends(get_db)) -> dict:
    topup = db.query(WalletTopUp).filter(
        WalletTopUp.razorpay_order_id == payload.razorpay_order_id,
        WalletTopUp.user_id == manager.id,
    ).with_for_update().first()
    if topup is None:
        raise HTTPException(status_code=404, detail="Wallet top-up order not found.")
    if topup.status == "PAID":
        return {"message": "This top-up was already completed.", "status": topup.status}
    topup.status = "FAILED"
    if payload.razorpay_payment_id:
        topup.razorpay_payment_id = payload.razorpay_payment_id
    db.add(RealtimeEvent(user_id=manager.id, event_type="payment.status_changed",
        resource_id=str(topup.id), data={"status": "FAILED", "resource_type": "wallet_topup"}))
    db.commit()
    return {"message": "Failed wallet payment recorded.", "status": topup.status}


class WalletTransferRequest(BaseModel):
    chef_id: uuid.UUID
    amount: Decimal = Field(gt=0, le=Decimal("50000.00"), decimal_places=2)
    idempotency_key: uuid.UUID


def get_or_create_wallet(db: Session, user_id: uuid.UUID, *, lock: bool = False) -> WalletAccount:
    query = db.query(WalletAccount).filter(WalletAccount.user_id == user_id)
    if lock:
        query = query.with_for_update()
    account = query.first()
    if account is None:
        account = WalletAccount(user_id=user_id, balance=Decimal("0.00"))
        db.add(account)
        db.flush()
    return account


def manager_wallet_data(manager: User, db: Session) -> dict:
    account = get_or_create_wallet(db, manager.id)
    transactions = db.query(WalletTransaction).filter(WalletTransaction.user_id == manager.id).order_by(WalletTransaction.created_at.desc()).limit(100).all()
    return wallet_data(account, transactions)


@manager_router.get("")
def get_manager_wallet(manager: User = Depends(current_manager), db: Session = Depends(get_db)) -> dict:
    data = manager_wallet_data(manager, db)
    db.commit()
    return data


@manager_router.get("/transactions")
def get_manager_wallet_transactions(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    manager: User = Depends(current_manager),
    db: Session = Depends(get_db),
) -> dict:
    query = db.query(WalletTransaction).filter(WalletTransaction.user_id == manager.id)
    total = query.count()
    rows = query.order_by(WalletTransaction.created_at.desc(), WalletTransaction.id.desc()) \
        .offset((page - 1) * page_size).limit(page_size).all()
    refs = [row.reference for row in rows if row.reference]
    topups = db.query(WalletTopUp).filter(WalletTopUp.user_id == manager.id,
        WalletTopUp.razorpay_payment_id.in_(refs)).all() if refs else []
    account = get_or_create_wallet(db, manager.id)
    db.commit()
    return {**wallet_data(account, rows, topups),
        "total": total, "page": page, "page_size": page_size}


@manager_router.post("/topups/order")
def create_manager_topup_order(payload: WalletTopUpRequest, manager: User = Depends(current_manager), db: Session = Depends(get_db)) -> dict:
    if payload.amount.as_tuple().exponent < -2:
        raise HTTPException(status_code=422, detail="Enter an amount with no more than two decimal places.")
    topup_id = uuid.uuid4()
    order = razorpay_request("POST", "orders", {
        "amount": int(payload.amount * 100), "currency": "INR", "receipt": f"wallet_{topup_id.hex[:32]}",
        "notes": {"manager_id": str(manager.id), "wallet_topup_id": str(topup_id)},
    })
    if order.get("currency") != "INR" or int(order.get("amount", -1)) != int(payload.amount * 100):
        raise HTTPException(status_code=502, detail="The payment provider returned an unexpected order amount.")
    db.add(WalletTopUp(id=topup_id, user_id=manager.id, razorpay_order_id=order["id"], amount=payload.amount))
    db.commit()
    return {"key_id": settings.razorpay_key_id, "order_id": order["id"], "amount": order["amount"],
        "currency": order["currency"], "name": "DineBook", "description": "Add money to your manager wallet"}


@manager_router.post("/topups/verify")
def verify_manager_topup(payload: WalletTopUpVerification, manager: User = Depends(current_manager), db: Session = Depends(get_db)) -> dict:
    topup = db.query(WalletTopUp).filter(
        WalletTopUp.razorpay_order_id == payload.razorpay_order_id,
        WalletTopUp.user_id == manager.id,
    ).with_for_update().first()
    if topup is None:
        raise HTTPException(status_code=404, detail="Wallet top-up order not found.")
    if topup.status == "PAID":
        account = get_or_create_wallet(db, manager.id)
        return {"message": "Wallet top-up already applied.", "balance": float(account.balance)}
    if not settings.razorpay_key_secret:
        raise HTTPException(status_code=503, detail="Wallet top-ups are not configured yet.")
    signed_payload = f"{topup.razorpay_order_id}|{payload.razorpay_payment_id}".encode()
    expected_signature = hmac.new(settings.razorpay_key_secret.encode(), signed_payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected_signature, payload.razorpay_signature):
        raise HTTPException(status_code=400, detail="Payment verification failed. No wallet funds were added.")
    payment = razorpay_request("GET", f"payments/{payload.razorpay_payment_id}")
    expected_minor_units = int(Decimal(topup.amount) * 100)
    if payment.get("order_id") != topup.razorpay_order_id or payment.get("amount") != expected_minor_units or payment.get("currency") != "INR" or payment.get("status") != "captured":
        raise HTTPException(status_code=409, detail="Payment is not captured yet. Your wallet has not been credited.")
    account = get_or_create_wallet(db, manager.id, lock=True)
    account.balance = Decimal(account.balance) + Decimal(topup.amount)
    topup.status = "PAID"
    topup.razorpay_payment_id = payload.razorpay_payment_id
    topup.paid_at = datetime.now(timezone.utc)
    db.add(WalletTransaction(user_id=manager.id, amount=topup.amount, transaction_type="CREDIT",
        description="Manager wallet top-up", reference=payload.razorpay_payment_id))
    db.add(RealtimeEvent(user_id=manager.id, event_type="payment.status_changed",
        resource_id=str(topup.id), data={"status": "PAID", "resource_type": "wallet_topup"}))
    db.commit()
    db.refresh(account)
    return {"message": "Wallet top-up successful.", "balance": float(account.balance)}


@manager_router.post("/transfers")
def transfer_to_chef(payload: WalletTransferRequest, manager: User = Depends(current_manager), db: Session = Depends(get_db)) -> dict:
    existing = db.query(WalletTransfer).filter(WalletTransfer.idempotency_key == payload.idempotency_key).first()
    if existing is not None:
        if existing.manager_id != manager.id or existing.chef_id != payload.chef_id or Decimal(existing.amount) != payload.amount:
            raise HTTPException(status_code=409, detail="This transfer reference was already used for another payment.")
        account = get_or_create_wallet(db, manager.id)
        return {"message": "Transfer already recorded.", "balance": float(account.balance), "transfer_id": str(existing.idempotency_key)}
    chef = db.query(User).filter(
        User.id == payload.chef_id, User.manager_id == manager.id, User.role == UserRole.CHEF
    ).first()
    if chef is None:
        raise HTTPException(status_code=404, detail="Chef not found for this restaurant.")
    manager_account = get_or_create_wallet(db, manager.id, lock=True)
    chef_account = get_or_create_wallet(db, chef.id, lock=True)
    if Decimal(manager_account.balance) < payload.amount:
        raise HTTPException(status_code=409, detail="Your manager wallet does not have enough balance for this transfer.")
    transfer = WalletTransfer(idempotency_key=payload.idempotency_key, manager_id=manager.id,
        chef_id=chef.id, amount=payload.amount)
    manager_account.balance = Decimal(manager_account.balance) - payload.amount
    chef_account.balance = Decimal(chef_account.balance) + payload.amount
    db.add(transfer)
    db.add(WalletTransaction(user_id=manager.id, amount=-payload.amount, transaction_type="DEBIT",
        description=f"Transfer to Chef {chef.name}", reference=f"wallet-transfer:{payload.idempotency_key}"))
    db.add(WalletTransaction(user_id=chef.id, amount=payload.amount, transaction_type="CREDIT",
        description=f"Wallet transfer from Manager {manager.name}", reference=f"wallet-transfer:{payload.idempotency_key}"))
    db.add(RealtimeEvent(user_id=manager.id, event_type="wallet.transfer_sent",
        resource_id=str(payload.idempotency_key), data={"amount": float(payload.amount), "chef_id": str(chef.id)}))
    db.add(RealtimeEvent(user_id=chef.id, event_type="wallet.transfer_received",
        resource_id=str(payload.idempotency_key), data={"amount": float(payload.amount), "manager_id": str(manager.id)}))
    create_notification(db, chef, notification_type="wallet.transfer.received", category="PAYMENT",
        title="Money received in your wallet", message=f"{manager.name} transferred ₹{payload.amount:.2f} to your DineBook wallet.",
        entity_type="wallet_transfer", entity_id=payload.idempotency_key, action_url="/chef/revenue",
        dedupe_key=f"wallet-transfer:{payload.idempotency_key}:received")
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.query(WalletTransfer).filter(WalletTransfer.idempotency_key == payload.idempotency_key).first()
        if (existing is None or existing.manager_id != manager.id or existing.chef_id != payload.chef_id
                or Decimal(existing.amount) != payload.amount):
            raise HTTPException(status_code=409, detail="This transfer could not be recorded. Refresh the wallet and try again.")
    db.refresh(manager_account)
    return {"message": "Money sent to chef wallet.", "balance": float(manager_account.balance), "transfer_id": str(payload.idempotency_key)}


@customer_router.post("/topups/order")
def create_topup_order(payload: WalletTopUpRequest, customer: User = Depends(current_customer), db: Session = Depends(get_db)) -> dict:
    if payload.amount.as_tuple().exponent < -2:
        raise HTTPException(status_code=422, detail="Enter a top-up amount with no more than two decimal places.")
    topup_id = uuid.uuid4()
    order = razorpay_request("POST", "orders", {
        "amount": int(payload.amount * 100), "currency": "INR", "receipt": f"wallet_{topup_id.hex[:32]}",
        "notes": {"customer_id": str(customer.id), "wallet_topup_id": str(topup_id)},
    })
    if order.get("currency") != "INR" or int(order.get("amount", -1)) != int(payload.amount * 100):
        raise HTTPException(status_code=502, detail="The payment provider returned an unexpected order amount.")
    db.add(WalletTopUp(id=topup_id, user_id=customer.id, razorpay_order_id=order["id"], amount=payload.amount))
    db.commit()
    return {"key_id": settings.razorpay_key_id, "order_id": order["id"], "amount": order["amount"],
        "currency": order["currency"], "name": "DineBook", "description": "Add money to your wallet"}


@customer_router.post("/topups/failure")
def record_topup_failure(payload: WalletTopUpFailure, customer: User = Depends(current_customer), db: Session = Depends(get_db)) -> dict:
    topup = db.query(WalletTopUp).filter(
        WalletTopUp.razorpay_order_id == payload.razorpay_order_id,
        WalletTopUp.user_id == customer.id,
    ).with_for_update().first()
    if topup is None:
        raise HTTPException(status_code=404, detail="Wallet top-up order not found.")
    if topup.status == "PAID":
        return {"message": "This top-up was already completed.", "status": topup.status}
    topup.status = "FAILED"
    if payload.razorpay_payment_id:
        topup.razorpay_payment_id = payload.razorpay_payment_id
    db.add(RealtimeEvent(user_id=customer.id, event_type="payment.status_changed",
        resource_id=str(topup.id), data={"status": "FAILED", "resource_type": "wallet_topup"}))
    db.commit()
    return {"message": "Failed wallet payment recorded.", "status": topup.status}


@customer_router.post("/topups/verify")
def verify_topup(payload: WalletTopUpVerification, customer: User = Depends(current_customer), db: Session = Depends(get_db)) -> dict:
    topup = db.query(WalletTopUp).filter(
        WalletTopUp.razorpay_order_id == payload.razorpay_order_id,
        WalletTopUp.user_id == customer.id,
    ).with_for_update().first()
    if topup is None:
        raise HTTPException(status_code=404, detail="Wallet top-up order not found.")
    if topup.status == "PAID":
        account = db.query(WalletAccount).filter(WalletAccount.user_id == customer.id).first()
        return {"message": "Wallet top-up already applied.", "balance": float(account.balance if account else 0)}

    signed_payload = f"{topup.razorpay_order_id}|{payload.razorpay_payment_id}".encode()
    expected_signature = hmac.new(settings.razorpay_key_secret.encode(), signed_payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected_signature, payload.razorpay_signature):
        raise HTTPException(status_code=400, detail="Payment verification failed. No wallet funds were added.")

    payment = razorpay_request("GET", f"payments/{payload.razorpay_payment_id}")
    expected_minor_units = int(Decimal(topup.amount) * 100)
    if payment.get("order_id") != topup.razorpay_order_id or payment.get("amount") != expected_minor_units or payment.get("currency") != "INR" or payment.get("status") != "captured":
        raise HTTPException(status_code=409, detail="Payment is not captured yet. Your wallet has not been credited.")

    account = db.query(WalletAccount).filter(WalletAccount.user_id == customer.id).with_for_update().first()
    if account is None:
        account = WalletAccount(user_id=customer.id, balance=Decimal("0.00"))
        db.add(account)
        db.flush()
    account.balance = Decimal(account.balance) + Decimal(topup.amount)
    topup.status = "PAID"
    topup.razorpay_payment_id = payload.razorpay_payment_id
    topup.paid_at = datetime.now(timezone.utc)
    db.add(WalletTransaction(user_id=customer.id, amount=topup.amount, transaction_type="CREDIT",
        description="Wallet top-up", reference=payload.razorpay_payment_id))
    db.add(RealtimeEvent(user_id=customer.id, event_type="payment.status_changed",
        resource_id=str(topup.id), data={"status": "PAID", "resource_type": "wallet_topup"}))
    create_notification(db, customer, notification_type="wallet.topup.success", category="PAYMENT",
        title="Wallet topped up", message="Your wallet top-up was successful.", entity_type="wallet_topup",
        entity_id=topup.id, action_url="/customer/profile/wallet", dedupe_key=f"wallet-topup:{topup.id}:paid")
    db.commit()
    db.refresh(account)
    return {"message": "Wallet top-up successful.", "balance": float(account.balance)}


@admin_router.post("/credit")
def credit_wallet(payload: AdminWalletCredit, admin: User = Depends(current_admin), db: Session = Depends(get_db)) -> dict:
    customer = db.query(User).filter(User.id == payload.customer_id, User.role == UserRole.CUSTOMER).first()
    if customer is None:
        raise HTTPException(status_code=404, detail="Customer not found.")
    account = db.query(WalletAccount).filter(WalletAccount.user_id == customer.id).with_for_update().first()
    if account is None:
        account = WalletAccount(user_id=customer.id, balance=Decimal("0.00"))
        db.add(account)
        db.flush()
    account.balance = Decimal(account.balance) + payload.amount
    db.add(WalletTransaction(user_id=customer.id, amount=payload.amount, transaction_type="CREDIT",
        description=payload.description, reference=payload.reference or f"Admin credit by {admin.email}"))
    db.commit()
    return {"message": "Wallet credited.", "balance": float(account.balance)}
