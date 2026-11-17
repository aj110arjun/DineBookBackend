"""Read-only platform commission receivables, using the admin dashboard rules."""
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import case, func, literal, select, union_all

from app.models.restaurant import Restaurant
from app.models.revenue import PlatformCommission as PC, PlatformCommissionPayout as Payout
from app.schemas.admin_ledger import AdminLedgerReport
from app.services.manager_dashboard import PAYMENT_SOURCES, utc_bounds
from app.services.manager_ledger import ledger_reference, money

EXPORT_LIMIT = 50_000
AMOUNTS = ("gross", "refunds", "generated", "reversed", "collected", "collection_reversals")


def financial_entries():
    def event(record, at, kind, source, reference, **amounts):
        return [record.label("record_id"), at.label("at"), literal(kind).label("kind"),
                Restaurant.id.label("restaurant_id"), Restaurant.name.label("restaurant_name"),
                source.label("source_type"), reference.label("reference"),
                *[(amounts[key] if key in amounts else literal(0)).label(key) for key in AMOUNTS]]

    captures = select(*event(PC.id, PC.occurred_at, "COLLECTION", PC.source_type, PC.source_id,
        gross=PC.gross_amount, generated=PC.commission_amount)).join(
        Restaurant, Restaurant.id == PC.restaurant_id).where(PC.source_type.in_(PAYMENT_SOURCES))
    refunds = select(*event(PC.id, PC.refunded_at, "REFUND", PC.source_type, PC.source_id,
        refunds=PC.gross_amount, reversed=PC.commission_amount,
        collection_reversals=case((PC.payout_id.is_not(None), PC.commission_amount), else_=0))).join(
        Restaurant, Restaurant.id == PC.restaurant_id).where(PC.source_type.in_(PAYMENT_SOURCES), PC.refunded_at.is_not(None))
    # A historical payout can cover many payment records. Read each payout once.
    settlements = select(*event(Payout.id, Payout.created_at, "SETTLEMENT", literal("COMMISSION_SETTLEMENT"),
        Payout.id, collected=Payout.amount)).outerjoin(Restaurant, Restaurant.manager_id == Payout.manager_id)
    raw = union_all(captures, refunds, settlements).subquery()
    earned = raw.c.generated - raw.c.reversed
    collected_net = raw.c.collected - raw.c.collection_reversals
    return select(raw, earned.label("earned"), collected_net.label("collected_net"),
                  (earned - collected_net).label("net")).subquery()


def ledger_report(db, params, *, export=False, now=None):
    events = financial_entries()
    low, high = utc_bounds(params.start_date, params.end_date)
    opening = money(db.query(func.sum(events.c.net)).filter(events.c.at < low).scalar())
    selected = (events.c.at >= low, events.c.at < high)
    keys = (*AMOUNTS, "earned", "collected_net", "net")
    totals = db.query(func.count(), *[func.sum(events.c[key]) for key in keys]).filter(*selected).one()
    total = totals[0]
    if export and total > EXPORT_LIMIT:
        raise HTTPException(422, "This range contains more than 50,000 ledger entries. Choose a shorter range to export.")
    values = dict(zip(keys, map(money, totals[1:])))
    ordering = (events.c.at, events.c.record_id, events.c.kind)
    running = (literal(opening) + func.sum(events.c.net).over(order_by=ordering, rows=(None, 0))).label("balance")
    query = db.query(events, running).filter(*selected).order_by(*ordering)
    query = query.limit(EXPORT_LIMIT) if export else query.offset((params.page - 1) * params.page_size).limit(params.page_size)
    entries = []
    for row in query.all():
        at = row.at if row.at.tzinfo else row.at.replace(tzinfo=timezone.utc)
        entries.append(dict(id=f"{row.record_id}:{row.kind}", occurred_at=at, kind=row.kind,
            restaurant_id=row.restaurant_id, restaurant_name=row.restaurant_name or "Unassigned restaurant",
            source_type=row.source_type, reference=ledger_reference(row.source_type, row.reference),
            **{key: money(getattr(row, key)) for key in ("gross", "refunds", "earned", "collected_net", "net", "balance")}))
    return AdminLedgerReport(start_date=params.start_date, end_date=params.end_date, as_of=now or datetime.now(timezone.utc),
        summary=dict(opening_balance=opening, **values, closing_balance=opening + values["net"]),
        items=entries, total=total, page=1 if export else params.page, page_size=total if export else params.page_size)
