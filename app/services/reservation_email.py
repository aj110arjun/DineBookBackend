from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy.orm import Session

from app.core.email import send_branded_email
from app.db.database import SessionLocal
from app.models.dining import DiningTable
from app.models.reservation import Reservation, ReservationTable
from app.models.restaurant import Restaurant
from app.models.user import User
from app.services.reservation_invoice_pdf import build_reservation_invoice_pdf

logger = logging.getLogger(__name__)


def send_reservation_confirmation_in_background(reservation_id: uuid.UUID) -> None:
    """Send a reservation confirmation with a task-owned database session."""
    db = SessionLocal()
    try:
        send_reservation_confirmation(db, reservation_id)
    finally:
        db.close()


def _money(value: Decimal | float | int | None) -> str:
    return f"INR {Decimal(str(value or 0)):.2f}"


def _status(method: str | None, status: str | None, amount: Decimal | float | int | None) -> str:
    method_name = {"WALLET": "DineBook wallet", "RAZORPAY": "Razorpay", "PAY_AT_DESK": "restaurant"}.get(method or "", "payment method")
    if status == "PAID":
        return f"Paid via {method_name}"
    if method == "PAY_AT_DESK":
        return f"Pay at restaurant · {_money(amount)} due"
    return f"{(status or 'PENDING').replace('_', ' ').title()} · {_money(amount)} due"


def _invoice_totals(item: Reservation) -> tuple[bool, Decimal, Decimal, Decimal, Decimal, Decimal, list[tuple[str, str]]]:
    has_preorder = item.fulfillment_type == "PREORDER" and bool(item.preorder_items)
    preorder_amount = Decimal(item.preorder_total or 0)
    preorder_discount = Decimal(item.preorder_discount or 0)
    preorder_tax = Decimal(item.preorder_tax_amount or 0)
    preorder_subtotal = preorder_amount - preorder_tax + preorder_discount
    preorder_paid = has_preorder and item.preorder_payment_status == "PAID"
    due = Decimal("0") if preorder_paid or not has_preorder else preorder_amount
    paid = Decimal(item.fee_amount or 0) + (preorder_amount if preorder_paid else Decimal("0"))
    rows = [
        ("Reservation fee", _status(item.payment_method, item.payment_status, item.fee_amount)),
        ("Preorder", "Not ordered" if not has_preorder else _status(item.preorder_payment_method, item.preorder_payment_status, preorder_amount)),
        ("Amount paid", f"INR {paid:.2f}"),
        ("Amount due", f"INR {due:.2f}"),
        ("Restaurant", ""),
        ("Reservation time", f"{item.reservation_date.isoformat()} · {item.start_time.strftime('%I:%M %p')}"),
        ("Party size", f"{item.number_of_guests} guests"),
    ]
    if item.preorder_offer_code:
        rows.append(("Offer applied", f"{item.preorder_offer_code} · discount INR {preorder_discount:.2f}"))
    if has_preorder and preorder_tax:
        rows.append((f"GST ({item.preorder_tax_percent}%)", f"INR {preorder_tax:.2f}"))
    return has_preorder, preorder_amount, preorder_discount, preorder_subtotal, paid, due, rows


def send_reservation_confirmation(db: Session, reservation_id: uuid.UUID) -> bool:
    """Send a branded confirmation once, after the mandatory reservation fee is paid."""
    item = db.query(Reservation).filter(Reservation.id == reservation_id).with_for_update().first()
    if item is None or item.status != "CONFIRMED" or item.payment_status != "PAID" or item.confirmation_email_sent_at:
        return False
    customer = db.query(User).filter(User.id == item.user_id).first()
    restaurant = db.query(Restaurant).filter(Restaurant.id == item.restaurant_id).first()
    if customer is None or restaurant is None or not customer.email:
        logger.warning("Cannot send reservation confirmation email; customer or restaurant is missing for %s", reservation_id)
        return False
    assignments = db.query(ReservationTable).filter(ReservationTable.reservation_id == item.id).all()
    table_ids = [assignment.table_id for assignment in assignments]
    tables = db.query(DiningTable).filter(DiningTable.id.in_(table_ids)).all() if table_ids else []
    has_preorder, preorder_amount, preorder_discount, preorder_subtotal, paid, due, summary_rows = _invoice_totals(item)
    reservation_code = f"RES-{item.id.hex[:8].upper()}"
    summary_rows = [(label, restaurant.name if label == "Restaurant" else value) for label, value in summary_rows]
    invoice = build_reservation_invoice_pdf(
        item, restaurant, customer, tables, reservation_code, preorder_amount,
        preorder_discount, preorder_subtotal, paid, due, has_preorder,
    )
    preorder_message = (
        f"Your preorder is included. Its payment status is {_status(item.preorder_payment_method, item.preorder_payment_status, item.preorder_total)}."
        if has_preorder else "No preorder was added to this reservation."
    )
    intro = f"Hi {customer.name}, your reservation at {restaurant.name} is confirmed. The reservation fee has been paid. Your booking reference is {reservation_code}."
    detail = f"{preorder_message} Your invoice is attached as a PDF."
    plain = "\n".join([
        f"Hi {customer.name},", "", f"Your reservation at {restaurant.name} is confirmed. Reference: {reservation_code}.",
        f"Date: {item.reservation_date.isoformat()} at {item.start_time.strftime('%I:%M %p')}",
        f"Reservation fee: {_status(item.payment_method, item.payment_status, item.fee_amount)}",
        preorder_message, "The invoice is attached as a PDF.", "", "Warmly,", "The DineBook team",
    ])
    try:
        send_branded_email(
            to=customer.email,
            subject=f"Reservation confirmed · {restaurant.name}",
            title="Your reservation is confirmed",
            intro=intro,
            detail=detail,
            plain_text=plain,
            error_detail="Reservation confirmation email could not be sent.",
            highlight_label="Reservation code",
            highlight_value=reservation_code,
            summary_rows=summary_rows,
            attachments=[("DineBook-reservation-invoice.pdf", invoice, "application", "pdf")],
        )
    except Exception:
        logger.exception("Failed to send reservation confirmation email for %s", reservation_id)
        db.rollback()
        return False
    item.confirmation_email_sent_at = datetime.now(timezone.utc)
    try:
        db.commit()
    except Exception:
        logger.exception("Could not record reservation confirmation email delivery for %s", reservation_id)
        db.rollback()
        return False
    return True
