import base64
import hashlib
import hmac
import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.core.config import settings
from app.models.user import User, UserRole
from app.models.wallet import WalletAccount, WalletTopUp, WalletTransaction
from app.routers.admin_auth import current_admin
from app.routers.customer_auth import current_customer

customer_router = APIRouter(prefix="/api/customer/wallet", tags=["customer wallet"])
admin_router = APIRouter(prefix="/api/admin/wallet", tags=["wallet administration"])
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


def wallet_data(account: WalletAccount, transactions: list[WalletTransaction]) -> dict:
    return {
        "balance": float(account.balance),
        "transactions": [{
            "id": str(item.id), "amount": float(item.amount),
            "type": item.transaction_type, "description": item.description,
            "reservation_id": str(item.reservation_id) if item.reservation_id else None,
            "created_at": item.created_at.isoformat(),
        } for item in transactions],
    }


@customer_router.get("")
def get_wallet(customer: User = Depends(current_customer), db: Session = Depends(get_db)) -> dict:
    account = db.query(WalletAccount).filter(WalletAccount.user_id == customer.id).first()
    if account is None:
        account = WalletAccount(user_id=customer.id, balance=Decimal("0.00"))
        db.add(account)
        db.commit()
        db.refresh(account)
    transactions = db.query(WalletTransaction).filter(WalletTransaction.user_id == customer.id).order_by(WalletTransaction.created_at.desc()).limit(100).all()
    data = wallet_data(account, transactions)
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
