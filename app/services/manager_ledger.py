"""Read-only net collection ledger, with the dashboard's settlement/reversal rules."""
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import func, literal, select, union_all

from app.models.payment_values import LedgerEntryType, PaymentSource
from app.models.revenue import PlatformCommission
from app.schemas.manager_ledger import LedgerReport
from app.services.manager_dashboard import PAYMENT_SOURCES, utc_bounds

EXPORT_LIMIT = 50_000


def money(value):
    return Decimal(value or 0).quantize(Decimal("0.01"))


def ledger_reference(source_type, source_id):
    prefix = {
        PaymentSource.FOOD_ORDER.value: "ORD",
        PaymentSource.RESERVATION_FEE.value: "RES",
        PaymentSource.PREORDER.value: "RES",
    }.get(source_type)
    return f"{prefix}-{str(source_id).replace('-', '')[:8].upper()}" if prefix else str(source_id)


def ledger_report(db, restaurant, params, *, export=False, now=None):
    pc = PlatformCommission
    scope = (pc.restaurant_id == restaurant.id, pc.source_type.in_(PAYMENT_SOURCES))
    captures = select(pc.id.label("record_id"), pc.source_id.label("reference"), pc.source_type,
        pc.occurred_at.label("at"), literal(LedgerEntryType.COLLECTION.value).label("kind"),
        pc.gross_amount.label("collected"), (pc.gross_amount * 0).label("refunded"),
        pc.commission_amount.label("commission"), (pc.gross_amount - pc.commission_amount).label("net")).where(*scope)
    reversals = select(pc.id, pc.source_id, pc.source_type, pc.refunded_at,
        literal(LedgerEntryType.REFUND.value), pc.gross_amount * 0, pc.gross_amount,
        -pc.commission_amount, -pc.gross_amount + pc.commission_amount).where(*scope, pc.refunded_at.is_not(None))
    events = union_all(captures, reversals).subquery()
    low, high = utc_bounds(params.start_date, params.end_date)
    opening = money(db.query(func.sum(events.c.net)).filter(events.c.at < low).scalar())
    selected = (events.c.at >= low, events.c.at < high)
    totals = db.query(func.count(), *(func.sum(events.c[name]) for name in ("collected", "refunded", "commission", "net"))).filter(*selected).one()
    total = totals[0]
    if export and total > EXPORT_LIMIT:
        raise HTTPException(422, "This range contains more than 50,000 ledger entries. Choose a shorter range to export.")
    values = dict(zip(("collected", "refunded", "commission", "net"), map(money, totals[1:])))
    ordering = (events.c.at, events.c.record_id, events.c.kind)
    running = (literal(opening) + func.sum(events.c.net).over(order_by=ordering, rows=(None, 0))).label("balance")
    query = db.query(events, running).filter(*selected).order_by(*ordering)
    query = query.limit(EXPORT_LIMIT) if export else query.offset((params.page - 1) * params.page_size).limit(params.page_size)
    entries = []
    for row in query.all():
        at = row.at if row.at.tzinfo else row.at.replace(tzinfo=timezone.utc)
        entries.append({"id": f"{row.record_id}:{row.kind}", "occurred_at": at,
            "kind": row.kind, "source_type": row.source_type, "reference": ledger_reference(row.source_type, row.reference),
            **{name: money(getattr(row, name)) for name in ("collected", "refunded", "commission", "net", "balance")}})
    return LedgerReport(restaurant_name=restaurant.name, start_date=params.start_date, end_date=params.end_date,
        as_of=now or datetime.now(timezone.utc), summary={"opening_balance": opening, **values,
        "closing_balance": opening + values["net"]}, items=entries, total=total,
        page=1 if export else params.page, page_size=total if export else params.page_size)
