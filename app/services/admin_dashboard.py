"""Read-only platform aggregates. The unique commission source ledger is the
financial source of truth; wallet top-ups and transfers are never sales.

Current operational statuses are grouped by creation (orders) or visit date
(reservations). This is not a historical status-transition report.
"""
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import String, and_, case, cast, func, literal, or_, select, union_all

from app.models.notifications import Notification
from app.models.orders import FoodOrder
from app.models.reservation import Reservation
from app.models.restaurant import Restaurant, RestaurantStatus
from app.models.revenue import PlatformCommission as PC, PlatformCommissionPayout as Payout
from app.models.user import User, UserRole
from app.models.wallet import WalletAccount
from app.schemas.admin_dashboard import AdminDashboardResponse
from app.services.commission import money
from app.services.manager_dashboard import (
    CANCELLED, ORDER_STATUSES, RESERVATION_STATUSES, PAYMENT_SOURCES,
    _bucket, _dates, _local, utc_bounds,
)
from app.services.table_availability import RESTAURANT_TIMEZONE

ZERO = Decimal("0.00")
NOTES = [
    "Restaurant, customer and manager counts are current all-time snapshots; operational statuses are current statuses within the selected creation/visit cohort.",
    "GTV and refunds use unique recorded payment sources (orders, reservation fees and preorders). Wallet top-ups and internal transfers are excluded. Unledgered paid records are reported separately; historical commission rates are never estimated.",
    "Commission earned is generated commission less reversals dated in the period. Collected commission is actual internal wallet settlement less collection reversals, not bank receipts. Outstanding is the unpaid, unreversed balance at the end of the selected period, including earlier receivables.",
    "Digital payments include wallet-funded purchases and verified Razorpay captures. The database has no bank settlement ledger, so bank receipts are unavailable. Historical direct-payment commissions already settled remain collected.",
    "Orders use creation date; reservations use scheduled visit date; payment and commission trends use their recorded event timestamps. All boundaries use Asia/Kolkata. Pending/failed payments use the source creation cohort; retry attempt history is unavailable.",
    "Recent activity uses stored registrations, reservation creations, completion timestamps, payment sources, commission payouts and admin approval notifications. Missing historical events are not reconstructed.",
]


def period(params):
    days = (params.end_date - params.start_date).days + 1
    group = params.group_by
    if group == "auto":
        group = "day" if days <= 90 else "week" if days <= 366 else "month" if days <= 1464 else "year"
    return dict(start_date=params.start_date, end_date=params.end_date,
                timezone=str(RESTAURANT_TIMEZONE), group_by=group)


def bounds(params):
    return utc_bounds(params.start_date, params.end_date)


def scope(column, params):
    low, high = bounds(params)
    return and_(column >= low, column < high)


def total(db, expression, *filters):
    return money(db.query(func.coalesce(func.sum(expression), 0)).filter(*filters).scalar())


def outstanding_expression(at):
    return and_(PC.source_type.in_(PAYMENT_SOURCES), PC.occurred_at < at,
                or_(PC.refunded_at.is_(None), PC.refunded_at >= at),
                or_(PC.payout_id.is_(None), Payout.created_at >= at))


def outstanding(db, at):
    return money(db.query(func.coalesce(func.sum(PC.commission_amount), 0))
                 .outerjoin(Payout, PC.payout_id == Payout.id)
                 .filter(outstanding_expression(at)).scalar())


def financial_events():
    # Sum payouts once, never once per commission in a historical batch payout.
    def event(at, gross=ZERO, refunds=ZERO, generated=ZERO, reversed_=ZERO,
              collected=ZERO, collection_reversals=ZERO):
        return [at.label("at"), *[e.label(name) if hasattr(e, "label") else literal(e).label(name)
                for name, e in zip(("gross", "refunds", "generated", "reversed", "collected", "collection_reversals"),
                                   (gross, refunds, generated, reversed_, collected, collection_reversals))]]
    return union_all(
        select(*event(PC.occurred_at, gross=PC.gross_amount, generated=PC.commission_amount)).where(PC.source_type.in_(PAYMENT_SOURCES)),
        select(*event(PC.refunded_at, refunds=PC.gross_amount, reversed_=PC.commission_amount,
                      collection_reversals=case((PC.payout_id.is_not(None), PC.commission_amount), else_=0)))
            .where(PC.source_type.in_(PAYMENT_SOURCES), PC.refunded_at.is_not(None)),
        select(*event(Payout.created_at, collected=Payout.amount)),
    ).subquery()


def revenue(db, params):
    events = financial_events()
    group = period(params)["group_by"]
    bucket = _bucket(db, _local(db, events.c.at), group)
    keys = ("gross", "refunds", "generated", "reversed", "collected", "collection_reversals")
    rows = db.query(bucket, *[func.sum(events.c[key]) for key in keys]).filter(
        scope(events.c.at, params)).group_by(bucket).all()
    chart = {day: dict(date=day, **dict.fromkeys(keys, ZERO)) for day in _dates(params.start_date, params.end_date, group)}
    for row in rows:
        chart[row[0]].update({key: money(value) for key, value in zip(keys, row[1:])})
    start_balance = outstanding(db, bounds(params)[0])
    running = start_balance
    for point in chart.values():
        point["earned"] = point["generated"] - point["reversed"]
        point["collected_net"] = point["collected"] - point["collection_reversals"]
        running = money(running + point["earned"] - point["collected_net"])
        point["outstanding"] = running
    summary = {key: money(sum((point[key] for point in chart.values()), ZERO))
               for key in (*keys, "earned", "collected_net")}
    summary.update(net_transaction_value=summary["gross"] - summary["refunds"],
                   outstanding_start=start_balance, outstanding_end=outstanding(db, bounds(params)[1]))
    return dict(summary=summary, trend=list(chart.values()))


def operations(db, params, *, reservations=False, now=None):
    model = Reservation if reservations else FoodOrder
    expression = model.reservation_date if reservations else model.created_at
    selected = and_(expression >= params.start_date, expression <= params.end_date) if reservations else scope(expression, params)
    group = period(params)["group_by"]
    bucket = _bucket(db, expression if reservations else _local(db, expression), group)
    rows = db.query(bucket, model.status, func.count(model.id)).filter(selected).group_by(bucket, model.status).all()
    statuses = dict.fromkeys(RESERVATION_STATUSES if reservations else ORDER_STATUSES, 0)
    chart = {day: dict(date=day, total=0, completed=0, pending=0, cancelled=0)
             for day in _dates(params.start_date, params.end_date, group)}
    for day, status, count in rows:
        statuses[status] = statuses.get(status, 0) + count
        point = chart[day]
        point["total"] += count
        point["completed" if status == "COMPLETED" else "cancelled" if status in (*CANCELLED, "NO_SHOW") else "pending"] += count
    result = dict(total=sum(statuses.values()), completed=statuses["COMPLETED"],
                  cancelled=sum(statuses[s] for s in CANCELLED),
                  statuses=[dict(status=s, count=c) for s, c in statuses.items()], trend=list(chart.values()))
    if reservations:
        local_now = (now or datetime.now(timezone.utc)).astimezone(RESTAURANT_TIMEZONE)
        upcoming = db.query(func.count(Reservation.id)).filter(selected,
            Reservation.status.in_(("PENDING_PAYMENT", "CONFIRMED")),
            or_(Reservation.reservation_date > local_now.date(),
                and_(Reservation.reservation_date == local_now.date(), Reservation.start_time >= local_now.time().replace(tzinfo=None)))).scalar()
        result.update(upcoming=upcoming, no_show=statuses["NO_SHOW"])
    else:
        result.update(pending=statuses["PLACED"] + statuses["ACCEPTED"], preparing=statuses["PREPARING"],
                      refunded=db.query(func.count(FoodOrder.id)).filter(selected, FoodOrder.payment_status == "REFUNDED").scalar())
    return result


def customer_activity():
    # Generated kitchen preorders represent the same reservation interaction.
    return union_all(
        select(FoodOrder.customer_id.label("customer"), FoodOrder.created_at.label("at")).where(
            FoodOrder.customer_id.is_not(None), FoodOrder.status.notin_(CANCELLED), FoodOrder.payment_status != "REFUNDED",
            ~generated_preorder()),
        select(Reservation.user_id, Reservation.created_at).where(Reservation.status.in_(("CONFIRMED", "SEATED", "COMPLETED"))),
    ).subquery()


def customers(db, params):
    count = db.query(func.count(User.id)).filter(User.role == UserRole.CUSTOMER).scalar()
    group = period(params)["group_by"]
    bucket = _bucket(db, _local(db, User.created_at), group)
    rows = db.query(bucket, func.count(User.id)).filter(User.role == UserRole.CUSTOMER,
        scope(User.created_at, params)).group_by(bucket).all()
    chart = {day: dict(date=day, new=0) for day in _dates(params.start_date, params.end_date, group)}
    for day, number in rows:
        chart[day]["new"] = number
    events = customer_activity()
    low, high = bounds(params)
    activity = select(events.c.customer, func.count().label("visits"),
                      func.max(case((events.c.at >= low, 1), else_=0)).label("active")).where(
        events.c.at < high).group_by(events.c.customer).subquery()
    active, repeat = db.query(func.count(), func.coalesce(func.sum(case((activity.c.visits >= 2, 1), else_=0)), 0)).select_from(
        activity).join(User, User.id == activity.c.customer).filter(User.role == UserRole.CUSTOMER, activity.c.active == 1).one()
    return dict(total=count, new=sum(p["new"] for p in chart.values()), active=active, repeat=repeat,
                activity_definition="Active: a registered customer who created a non-cancelled, non-refunded order or a confirmed/seated/completed reservation in the selected period. Repeat: an active customer with at least two such interactions by period end. Kitchen preorder copies and guest walk-ins are excluded. This does not measure logins.",
                trend=list(chart.values()))


def generated_preorder():
    # Existing queue_reservation_preorder writes this reserved snapshot marker.
    # Financial capture/refund amounts never rely on this marker: the canonical
    # unique commission source already deduplicates the reservation and order.
    return and_(FoodOrder.reservation_id.is_not(None), FoodOrder.idempotency_key.is_not(None),
                func.coalesce(FoodOrder.customer_note, "") == "Reservation preorder")


def payment_sources():
    def record(source, model, amount, method, status, at):
        return select(literal(source).label("source_type"), model.id.label("source_id"),
                      amount.label("amount"), method.label("method"), status.label("status"), at.label("created_at"))
    return union_all(
        record("FOOD_ORDER", FoodOrder, FoodOrder.total, FoodOrder.payment_method, FoodOrder.payment_status, FoodOrder.created_at).where(~generated_preorder()),
        record("RESERVATION_FEE", Reservation, Reservation.fee_amount, Reservation.payment_method, Reservation.payment_status, Reservation.created_at).where(Reservation.fee_amount > 0),
        record("PREORDER", Reservation, Reservation.preorder_total, Reservation.preorder_payment_method, Reservation.preorder_payment_status, Reservation.created_at).where(Reservation.preorder_total > 0),
    ).subquery()


def payment_ledger():
    # One row per immutable payment source; no fan-out into wallet entries.
    method = case((PC.source_type == "FOOD_ORDER", FoodOrder.payment_method),
                  (PC.source_type == "RESERVATION_FEE", Reservation.payment_method),
                  (PC.source_type == "PREORDER", Reservation.preorder_payment_method), else_="UNKNOWN")
    return select(PC.id, PC.occurred_at, PC.gross_amount, func.coalesce(method, "UNKNOWN").label("method")).select_from(PC).outerjoin(
        FoodOrder, and_(PC.source_type == "FOOD_ORDER", PC.source_id == FoodOrder.id)).outerjoin(
        Reservation, and_(PC.source_type.in_(("RESERVATION_FEE", "PREORDER")), PC.source_id == Reservation.id)).where(
            PC.source_type.in_(PAYMENT_SOURCES)).subquery()


def payments(db, params, revenue_summary=None):
    ledger = payment_ledger()
    rows = db.query(ledger.c.method, func.count(), func.sum(ledger.c.gross_amount)).filter(
        scope(ledger.c.occurred_at, params)).group_by(ledger.c.method).all()
    channels = [dict(method=method, count=count, amount=money(amount)) for method, count, amount in rows]
    sources = payment_sources()
    states = {"PENDING": dict(count=0, amount=ZERO), "FAILED": dict(count=0, amount=ZERO)}
    for status, count, amount in db.query(sources.c.status, func.count(), func.sum(sources.c.amount)).filter(
            scope(sources.c.created_at, params), sources.c.status.in_(states)).group_by(sources.c.status).all():
        states[status] = dict(count=count, amount=money(amount))
    has_ledger = select(PC.id).where(PC.source_type == sources.c.source_type, PC.source_id == sources.c.source_id).exists()
    unledgered = db.query(func.count(), func.coalesce(func.sum(sources.c.amount), 0)).filter(
        scope(sources.c.created_at, params), sources.c.status.in_(("PAID", "REFUNDED")), ~has_ledger).one()
    wallets = []
    for role, accounts, balance, positive, negative in db.query(User.role, func.count(WalletAccount.user_id),
            func.sum(WalletAccount.balance), func.sum(case((WalletAccount.balance > 0, WalletAccount.balance), else_=0)),
            func.sum(case((WalletAccount.balance < 0, WalletAccount.balance), else_=0))).select_from(WalletAccount).join(
                User, User.id == WalletAccount.user_id).group_by(User.role).all():
        wallets.append(dict(role=role.value, accounts=accounts, balance=money(balance), positive_balances=money(positive), negative_balances=money(negative)))
    summary = revenue_summary or revenue(db, params)["summary"]
    return dict(channels=channels, pending=states["PENDING"], failed=states["FAILED"],
                unledgered_paid=dict(count=unledgered[0], amount=money(unledgered[1])), refunded=summary["refunds"],
                wallets=wallets, commission_receivables=summary["outstanding_end"], commission_settlements=summary["collected_net"],
                bank_settlement_amount=None, bank_settlement_note="Unavailable: no bank settlement records are stored. Wallet settlements and captured customer payments do not confirm bank receipt.")


def snapshot_kpis(db):
    restaurants, active, pending = db.query(func.count(Restaurant.id),
        func.coalesce(func.sum(case((Restaurant.status == RestaurantStatus.APPROVED, 1), else_=0)), 0),
        func.coalesce(func.sum(case((Restaurant.status == RestaurantStatus.PENDING, 1), else_=0)), 0)).one()
    roles = dict(db.query(User.role, func.count()).group_by(User.role).all())
    return dict(restaurants=restaurants, active_restaurants=active, pending_approvals=pending,
                customers=roles.get(UserRole.CUSTOMER, 0), managers=roles.get(UserRole.MANAGER, 0))


def dashboard(db, admin, params, *, now=None):
    now = now or datetime.now(timezone.utc)
    r = revenue(db, params)
    orders = operations(db, params)
    reservations = operations(db, params, reservations=True, now=now)
    kpis = dict(**snapshot_kpis(db), orders=orders["total"], completed_orders=orders["completed"],
                reservations=reservations["total"], gross=r["summary"]["gross"], earned_commission=r["summary"]["earned"],
                outstanding_commission=r["summary"]["outstanding_end"])
    return AdminDashboardResponse(admin_name=admin.name, as_of=now, period=period(params), kpis=kpis,
        revenue=r, orders=orders, reservations=reservations, customers=customers(db, params),
        payments=payments(db, params, r["summary"]), notes=NOTES)


def restaurant_performance(db, params):
    order_stats = select(FoodOrder.restaurant_id, func.count().label("orders"),
        func.sum(case((FoodOrder.status == "COMPLETED", 1), else_=0)).label("completed")).where(
            scope(FoodOrder.created_at, params)).group_by(FoodOrder.restaurant_id).subquery()
    reservations = select(Reservation.restaurant_id, func.count().label("reservations")).where(
        Reservation.reservation_date >= params.start_date, Reservation.reservation_date <= params.end_date).group_by(Reservation.restaurant_id).subquery()
    fin = union_all(
        select(PC.restaurant_id, PC.gross_amount.label("gross"), PC.commission_amount.label("commission")).where(
            PC.source_type.in_(PAYMENT_SOURCES), scope(PC.occurred_at, params)),
        select(PC.restaurant_id, literal(ZERO), -PC.commission_amount).where(
            PC.source_type.in_(PAYMENT_SOURCES), scope(PC.refunded_at, params)),
    ).subquery()
    finances = select(fin.c.restaurant_id, func.sum(fin.c.gross).label("gross"), func.sum(fin.c.commission).label("commission")).group_by(fin.c.restaurant_id).subquery()
    due = select(PC.restaurant_id, func.sum(PC.commission_amount).label("outstanding")).outerjoin(
        Payout, Payout.id == PC.payout_id).where(outstanding_expression(bounds(params)[1])).group_by(PC.restaurant_id).subquery()
    columns = {"orders": func.coalesce(order_stats.c.orders, 0), "completed": func.coalesce(order_stats.c.completed, 0),
               "reservations": func.coalesce(reservations.c.reservations, 0), "gross": func.coalesce(finances.c.gross, 0),
               "commission": func.coalesce(finances.c.commission, 0), "outstanding": func.coalesce(due.c.outstanding, 0)}
    query = db.query(Restaurant.id, Restaurant.name, User.name.label("manager_name"), Restaurant.status,
                     *[v.label(k) for k, v in columns.items()]).join(User, User.id == Restaurant.manager_id)
    for sub in (order_stats, reservations, finances, due):
        query = query.outerjoin(sub, sub.c.restaurant_id == Restaurant.id)
    if params.q.strip():
        # Search text is literal, not a user-supplied LIKE wildcard expression.
        term = params.q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        query = query.filter(or_(Restaurant.name.ilike(f"%{term}%", escape="\\"), User.name.ilike(f"%{term}%", escape="\\")))
    ordering = func.lower(Restaurant.name) if params.sort == "name" else columns[params.sort]
    ordered = query.order_by(ordering.asc() if params.direction == "asc" else ordering.desc(), Restaurant.id)
    def serialize(row):
        values = dict(row._mapping)
        values.update(id=str(row.id), status=row.status.value)
        for key in ("gross", "commission", "outstanding"):
            values[key] = money(values[key])
        return values
    count = query.count()
    items = [serialize(row) for row in ordered.offset((params.page - 1) * params.page_size).limit(params.page_size).all()]
    # Leaders are independent of pagination, and follow the selected sort/search.
    leaders = [serialize(row) for row in ordered.limit(5).all()]
    return dict(items=items, total=count, page=params.page, page_size=params.page_size, period=period(params), leaders=leaders)


def activity(db, admin, params):
    def event(id_, kind, title, subject, at):
        return select(cast(id_, String).label("id"), literal(kind).label("event_type"),
                      (literal(title) if isinstance(title, str) else title).label("title"),
                      (literal(subject, type_=String) if subject is None or isinstance(subject, str) else subject).label("subject"), at.label("occurred_at"))
    events = union_all(
        event(Restaurant.id, "RESTAURANT_REGISTERED", "Restaurant registered", Restaurant.name, Restaurant.created_at),
        event(User.id, "CUSTOMER_REGISTERED", "Customer registered", "Customer account", User.created_at).where(User.role == UserRole.CUSTOMER),
        event(Reservation.id, "RESERVATION_CREATED", "Reservation created", Restaurant.name, Reservation.created_at).join(Restaurant, Reservation.restaurant_id == Restaurant.id),
        event(FoodOrder.id, "ORDER_COMPLETED", "Order completed", Restaurant.name, FoodOrder.completed_at).join(Restaurant, FoodOrder.restaurant_id == Restaurant.id).where(FoodOrder.status == "COMPLETED", FoodOrder.completed_at.is_not(None)),
        event(PC.id, "PAYMENT_RECORDED", "Payment recorded", Restaurant.name, PC.occurred_at).join(Restaurant, PC.restaurant_id == Restaurant.id).where(PC.source_type.in_(PAYMENT_SOURCES)),
        event(Payout.id, "COMMISSION_SETTLED", "Commission settled to admin wallet", Restaurant.name, Payout.created_at).join(Restaurant, Payout.manager_id == Restaurant.manager_id),
        event(Notification.id, "APPROVAL_REQUEST", Notification.title, Restaurant.name, Notification.created_at).outerjoin(Restaurant, Notification.restaurant_id == Restaurant.id).where(
            Notification.recipient_id == admin.id, Notification.notification_type == "restaurant.approval_requested"),
    ).subquery()
    query = db.query(events).filter(scope(events.c.occurred_at, params))
    items = [dict(row._mapping) for row in query.order_by(events.c.occurred_at.desc(), events.c.event_type, events.c.id)
             .offset((params.page - 1) * params.page_size).limit(params.page_size).all()]
    for item in items:
        item["id"] = f'{item["event_type"]}:{item["id"]}'
        if item["occurred_at"].tzinfo is None:
            item["occurred_at"] = item["occurred_at"].replace(tzinfo=timezone.utc)
    return dict(items=items, total=query.count(), page=params.page, page_size=params.page_size)
