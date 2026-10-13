import hashlib
import hmac
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.database import get_db
from app.models.realtime import RealtimeEvent, WebhookReceipt
from app.models.user import User
from app.models.wallet import WalletAccount, WalletTopUp, WalletTransaction
from app.models.reservation import Reservation

router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])
logger = logging.getLogger(__name__)
MAX_WEBHOOK_BYTES = 1_048_576


def verify_razorpay_signature(body: bytes, signature: str, secret: str) -> bool:
    if not re.fullmatch(r"[a-fA-F0-9]{64}", signature):
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.lower())


def _signature_matches_configured_secrets(body: bytes, signature: str) -> bool:
    secrets = (settings.razorpay_webhook_secret, settings.razorpay_webhook_previous_secret)
    return any(secret and verify_razorpay_signature(body, signature, secret) for secret in secrets)


async def _read_bounded_body(request: Request) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > MAX_WEBHOOK_BYTES:
                raise HTTPException(status_code=413, detail="Webhook payload is too large.")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid content length.") from exc
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_WEBHOOK_BYTES:
            raise HTTPException(status_code=413, detail="Webhook payload is too large.")
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/razorpay", status_code=status.HTTP_200_OK)
async def razorpay_webhook(request: Request, db: Session = Depends(get_db)) -> dict[str, str]:
    if not settings.razorpay_webhook_secret:
        raise HTTPException(status_code=503, detail="Webhook integration is not configured.")

    body = await _read_bounded_body(request)
    signature = request.headers.get("x-razorpay-signature", "")
    if not _signature_matches_configured_secrets(body, signature):
        raise HTTPException(status_code=401, detail="Webhook signature verification failed.")

    provider_event_id = request.headers.get("x-razorpay-event-id", "").strip()
    if not provider_event_id or len(provider_event_id) > 100:
        raise HTTPException(status_code=400, detail="Webhook event identifier is missing or invalid.")
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="Webhook payload is malformed.") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("event"), str):
        raise HTTPException(status_code=400, detail="Webhook payload is malformed.")

    event_type = payload["event"][:100]
    if db.query(WebhookReceipt.event_id).filter(WebhookReceipt.event_id == provider_event_id).first():
        return {"status": "duplicate"}

    receipt = WebhookReceipt(event_id=provider_event_id, provider="razorpay", event_type=event_type, outcome="IGNORED")
    db.add(receipt)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        if db.query(WebhookReceipt.event_id).filter(WebhookReceipt.event_id == provider_event_id).first():
            return {"status": "duplicate"}
        raise HTTPException(status_code=503, detail="Webhook could not be recorded. Please retry.")

    if event_type != "payment.captured":
        receipt.outcome = "IGNORED"
        db.commit()
        return {"status": "ignored"}

    try:
        payment_entity = payload["payload"]["payment"]["entity"]
        order_id = payment_entity["order_id"]
        payment_id = payment_entity["id"]
        amount = payment_entity["amount"]
        currency = payment_entity["currency"]
        payment_status = payment_entity["status"]
    except (KeyError, TypeError) as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail="Captured payment data is malformed.") from exc

    if (not isinstance(order_id, str) or not order_id or len(order_id) > 80
            or not isinstance(payment_id, str) or not payment_id or len(payment_id) > 80
            or not isinstance(amount, int) or isinstance(amount, bool)
            or currency != "INR" or payment_status != "captured"):
        db.rollback()
        raise HTTPException(status_code=400, detail="Captured payment data is invalid.")

    topup = db.query(WalletTopUp).filter(WalletTopUp.razorpay_order_id == order_id).with_for_update().first()
    if topup is None:
        reservation = db.query(Reservation).filter(Reservation.razorpay_order_id == order_id).with_for_update().first()
        if reservation is None:
            # A valid event for an unrelated Razorpay order is acknowledged but not applied.
            receipt.outcome = "IGNORED"
            db.commit()
            return {"status": "ignored"}
        if amount != int(Decimal(reservation.fee_amount) * 100):
            db.rollback()
            logger.warning("Razorpay captured event amount mismatch for event %s", provider_event_id)
            raise HTTPException(status_code=400, detail="Captured payment does not match the reservation.")
        if reservation.payment_status != "PAID":
            if reservation.status != "PENDING_PAYMENT":
                receipt.outcome = "REVIEW"
                db.commit()
                return {"status": "review_required"}
            reservation.razorpay_payment_id = payment_id
            reservation.payment_status = "PAID"
            reservation.status = "CONFIRMED"
            db.add(RealtimeEvent(user_id=reservation.user_id, restaurant_id=reservation.restaurant_id,
                event_type="booking.updated", resource_id=str(reservation.id),
                data={"status": "CONFIRMED", "payment_status": "PAID",
                      "occurred_at": datetime.now(timezone.utc).isoformat()}))
            db.add(RealtimeEvent(user_id=reservation.user_id, restaurant_id=reservation.restaurant_id,
                event_type="payment.status_changed", resource_id=str(reservation.id),
                data={"status": "PAID", "resource_type": "reservation"}))
            receipt.outcome = "PROCESSED"
        else:
            receipt.outcome = "DUPLICATE_PAYMENT"
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            logger.info("Razorpay event %s was already applied", provider_event_id)
            return {"status": "duplicate"}
        except Exception:
            db.rollback()
            logger.exception("Razorpay event processing failed for event %s", provider_event_id)
            raise HTTPException(status_code=503, detail="Webhook processing failed. Please retry.")
        return {"status": "processed"}

    expected_amount = int(Decimal(topup.amount) * 100)
    if amount != expected_amount:
        db.rollback()
        logger.warning("Razorpay captured event amount mismatch for event %s", provider_event_id)
        raise HTTPException(status_code=400, detail="Captured payment does not match the pending top-up.")

    if topup.status != "PAID":
        # Serialize first-wallet creation and concurrent top-ups for one user.
        db.query(User.id).filter(User.id == topup.user_id).with_for_update().first()
        account = db.query(WalletAccount).filter(WalletAccount.user_id == topup.user_id).with_for_update().first()
        if account is None:
            account = WalletAccount(user_id=topup.user_id, balance=Decimal("0.00"))
            db.add(account)
            db.flush()
        account.balance = Decimal(account.balance) + Decimal(topup.amount)
        topup.status = "PAID"
        topup.razorpay_payment_id = payment_id
        topup.paid_at = datetime.now(timezone.utc)
        db.add(WalletTransaction(user_id=topup.user_id, amount=topup.amount, transaction_type="CREDIT",
            description="Wallet top-up", reference=payment_id))
        db.add(RealtimeEvent(user_id=topup.user_id, event_type="payment.status_changed",
            resource_id=str(topup.id), data={"status": "PAID", "resource_type": "wallet_topup"}))
        receipt.outcome = "PROCESSED"
    else:
        receipt.outcome = "DUPLICATE_PAYMENT"

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        # Unique payment ID / concurrent delivery already applied this payment.
        logger.info("Razorpay event %s was already applied", provider_event_id)
        return {"status": "duplicate"}
    except Exception:
        db.rollback()
        logger.exception("Razorpay event processing failed for event %s", provider_event_id)
        raise HTTPException(status_code=503, detail="Webhook processing failed. Please retry.")

    return {"status": "processed"}
