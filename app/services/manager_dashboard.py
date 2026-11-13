"""Read-only SQL aggregates for one authorized restaurant.

Financial cash flow comes exclusively from the unique payment-source ledger in
PlatformCommission, never from wallet entries or from joining order line items.
"""
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

from sqlalchemy import DateTime, Integer, Numeric, String, and_, case, cast, func, or_, select, true, union_all
from sqlalchemy.orm import Session

from app.models.dining import DiningTable, RestaurantFloor
from app.models.menu import Food, FoodVariant
from app.models.orders import FoodOrder
from app.models.reservation import Reservation, ReservationTable
from app.models.revenue import PlatformCommission
from app.models.reviews import Review
from app.schemas.manager_dashboard import DashboardQuery, DashboardResponse
from app.services.commission import money
from app.services.table_availability import RESTAURANT_TIMEZONE

ORDER_STATUSES = ("PLACED", "ACCEPTED", "PREPARING", "READY", "SERVED", "COMPLETED", "CANCELLED", "REJECTED")
RESERVATION_STATUSES = ("PENDING_PAYMENT", "CONFIRMED", "SEATED", "COMPLETED", "CANCELLED", "REJECTED", "NO_SHOW")
CANCELLED = ("CANCELLED", "REJECTED")
ELIGIBLE_VISITS = ("CONFIRMED", "SEATED", "COMPLETED")
PAYMENT_SOURCES = ("FOOD_ORDER", "RESERVATION_FEE", "PREORDER")


def percent(numerator, denominator):
    return money(Decimal(numerator) * 100 / Decimal(denominator)) if denominator else Decimal("0.00")


def metric(current, previous):
    current, previous = money(current), money(previous)
    return {"value": current, "previous": previous,
            "change_percent": percent(current - previous, abs(previous)) if previous else (Decimal(0) if not current else None)}


def utc_bounds(start: date, end: date):
    return (datetime.combine(start, time.min, RESTAURANT_TIMEZONE).astimezone(timezone.utc),
            datetime.combine(end + timedelta(days=1), time.min, RESTAURANT_TIMEZONE).astimezone(timezone.utc))


def _local(db, column):
    if db.get_bind().dialect.name == "postgresql":
        return func.timezone(str(RESTAURANT_TIMEZONE), column)
    return func.datetime(column, "+330 minutes")


def _day(db, expression):
    return (func.to_char(expression, "YYYY-MM-DD") if db.get_bind().dialect.name == "postgresql"
            else func.date(expression))


def _bucket(db, expression, group):
    if db.get_bind().dialect.name == "postgresql":
        return func.to_char(func.date_trunc(group, cast(expression, DateTime)), "YYYY-MM-DD")
    modifiers = {"day": (), "week": ("-6 days", "weekday 1"), "month": ("start of month",), "year": ("start of year",)}
    return func.date(expression, *modifiers[group])


def _bucket_date(day, group):
    if group == "week":
        return day - timedelta(days=day.weekday())
    if group == "month":
        return day.replace(day=1)
    if group == "year":
        return day.replace(month=1, day=1)
    return day


def _dates(start, end, group):
    # Bounded by query validation; only zero-filled aggregate buckets reach Python.
    return sorted({_bucket_date(start + timedelta(days=i), group).isoformat()
                   for i in range((end - start).days + 1)})


def _counts(db, model, restaurant_id, expression, start, end, previous_start, group, dates):
    scheduled = model is Reservation
    low, high = (previous_start, end) if scheduled else utc_bounds(previous_start, end)
    current_start = start if scheduled else utc_bounds(start, end)[0]
    current = expression >= current_start
    bucket = _bucket(db, expression if scheduled else _local(db, expression), group)
    rows = db.query(current.label("current"), bucket.label("bucket"), model.status, func.count(model.id)).filter(
        model.restaurant_id == restaurant_id, expression >= low,
        expression <= high if scheduled else expression < high,
    ).group_by(current, bucket, model.status).all()
    trend = {day: {"date": day, "total": 0, "completed": 0, "pending": 0, "cancelled": 0} for day in dates}
    statuses = dict.fromkeys(RESERVATION_STATUSES if scheduled else ORDER_STATUSES, 0)
    previous = 0
    for is_current, day, status, count in rows:
        if not is_current:
            previous += count
            continue
        statuses[status] = statuses.get(status, 0) + count
        point = trend[day]
        point["total"] += count
        category = ("completed" if status == "COMPLETED" else "cancelled" if status in (*CANCELLED, "NO_SHOW") else "pending")
        point[category] += count
    return list(trend.values()), statuses, previous


def _revenue(db, rid, start, end, previous_start, group, dates):
    pc = PlatformCommission
    low, high = utc_bounds(previous_start, end)
    current_start = utc_bounds(start, end)[0]
    scope = (pc.restaurant_id == rid, pc.source_type.in_(PAYMENT_SOURCES))
    # Two indexed branches: settlement time and reversal time. A refund may be
    # in a different period from its capture; its original commission is reversed.
    captures = select(pc.occurred_at.label("at"), pc.gross_amount.label("gross"),
                      (pc.gross_amount * 0).label("refunds"), pc.commission_amount.label("commissions")).where(
        *scope, pc.occurred_at >= low, pc.occurred_at < high)
    refunds = select(pc.refunded_at, pc.gross_amount * 0, pc.gross_amount, -pc.commission_amount).where(
        *scope, pc.refunded_at >= low, pc.refunded_at < high)
    events = union_all(captures, refunds).subquery()
    current = events.c.at >= current_start
    bucket = _bucket(db, _local(db, events.c.at), group)
    rows = db.query(current, bucket, func.sum(events.c.gross), func.sum(events.c.refunds),
                    func.sum(events.c.commissions)).group_by(current, bucket).all()
    chart = {day: {"date": day, "gross": Decimal(0), "refunds": Decimal(0), "commissions": Decimal(0)} for day in dates}
    previous = Decimal(0)
    for is_current, day, gross, refund, commission in rows:
        if is_current:
            chart[day].update(gross=money(gross), refunds=money(refund), commissions=money(commission))
        else:
            previous += money(gross) - money(refund)
    for point in chart.values():
        point["revenue"] = point["gross"] - point["refunds"]
        point["net"] = point["revenue"] - point["commissions"]
    summary = {key: sum((p[key] for p in chart.values()), Decimal(0))
               for key in ("gross", "refunds", "commissions", "revenue", "net")}
    return summary, list(chart.values()), previous


def _customers(db, rid, start, end, previous_start, group, dates):
    # Full restaurant history, not just the selected period. Walk-ins with no
    # customer account cannot be deduplicated and are intentionally excluded.
    order_activity = select(FoodOrder.customer_id.label("customer"),
                            _day(db, _local(db, FoodOrder.created_at)).label("day")).where(
        FoodOrder.restaurant_id == rid, FoodOrder.customer_id.is_not(None),
        FoodOrder.status.notin_(CANCELLED), FoodOrder.payment_status != "REFUNDED")
    visit_activity = select(Reservation.user_id, cast(Reservation.reservation_date, String)).where(
        Reservation.restaurant_id == rid, Reservation.status.in_(ELIGIBLE_VISITS))
    activity = union_all(order_activity, visit_activity).subquery()
    first = select(activity.c.customer, func.min(activity.c.day).label("first_day")).group_by(activity.c.customer).subquery()
    joined = activity.join(first, activity.c.customer == first.c.customer)
    current = activity.c.day >= start.isoformat()
    rows = db.query(current, func.count(func.distinct(activity.c.customer)),
                    func.count(func.distinct(case((first.c.first_day >= start.isoformat(), activity.c.customer))))).select_from(joined).filter(
        activity.c.day >= previous_start.isoformat(), activity.c.day <= end.isoformat()).group_by(current).all()
    total = new = previous = 0
    for is_current, count, new_count in rows:
        if is_current:
            total, new = count, new_count
        else:
            previous = count
    bucket = _bucket(db, activity.c.day, group)
    # First day within this bucket distinguishes new versus existing customers.
    rows = db.query(bucket, func.count(func.distinct(activity.c.customer)),
                    func.count(func.distinct(case((first.c.first_day >= bucket, activity.c.customer))))).select_from(joined).filter(
        activity.c.day >= start.isoformat(), activity.c.day <= end.isoformat()).group_by(bucket).all()
    points = {day: {"date": day, "total": 0, "new": 0, "returning": 0} for day in dates}
    for day, count, new_count in rows:
        points[day].update(total=count, new=new_count, returning=count - new_count)
    return total, previous, {"new": new, "returning": total - new,
                             "retention_percent": percent(total - new, total), "trend": list(points.values())}


def _paid_orders(rid):
    reversed_payment = select(PlatformCommission.id).where(
        PlatformCommission.restaurant_id == rid, PlatformCommission.source_type == "FOOD_ORDER",
        PlatformCommission.source_id == FoodOrder.id, PlatformCommission.refunded_at.is_not(None),
    ).exists()
    return (FoodOrder.restaurant_id == rid, FoodOrder.payment_status == "PAID",
            FoodOrder.status.notin_(CANCELLED), ~reversed_payment)


def _aov(db, rid, start, end, previous_start):
    low, high = utc_bounds(previous_start, end)
    current = FoodOrder.created_at >= utc_bounds(start, end)[0]
    rows = db.query(current, func.sum(FoodOrder.total), func.count(FoodOrder.id)).filter(
        *_paid_orders(rid), FoodOrder.created_at >= low, FoodOrder.created_at < high).group_by(current).all()
    values = {bool(flag): money(total / count) for flag, total, count in rows}
    return metric(values.get(True, 0), values.get(False, 0))


def _popular(db, rid, start, end, sort):
    low, high = utc_bounds(start, end)
    # Orders store immutable JSON line snapshots, not a FoodOrderItem table.
    # Expand and aggregate in SQL; never load every order into Python.
    postgres = db.get_bind().dialect.name == "postgresql"
    if postgres:
        lines = func.jsonb_array_elements(FoodOrder.items).table_valued("value").lateral()
        field = lambda name: lines.c.value.op("->>")(name)
    else:
        lines = func.json_each(FoodOrder.items).table_valued("value")
        field = lambda name: func.json_extract(lines.c.value, f"$.{name}")
    variant = field("variant_id")
    name = func.coalesce(field("food_name"), Food.name, "Unnamed item")
    variant_name = func.coalesce(field("variant_name"), FoodVariant.name, "")
    key = func.coalesce(variant, name)
    quantity = func.sum(cast(field("quantity"), Integer))
    # Allocate the saved order discount proportionally, excluding tax. This is
    # item sales revenue, not commission-adjusted restaurant cash flow.
    allocation = (FoodOrder.subtotal - FoodOrder.discount_amount) / func.nullif(FoodOrder.subtotal, 0)
    revenue = func.sum(cast(field("line_total"), Numeric(18, 4)) * allocation)
    rows = db.query(key, func.max(name), func.max(variant_name), quantity.label("quantity"), revenue.label("revenue")).select_from(FoodOrder).join(
        lines, true()).outerjoin(FoodVariant, and_(
            FoodVariant.food_id.in_(select(Food.id).where(Food.restaurant_id == rid)),
            func.replace(cast(FoodVariant.id, String), "-", "") == func.replace(variant, "-", ""))).outerjoin(
        Food, and_(Food.id == FoodVariant.food_id, Food.restaurant_id == rid)).filter(
        *_paid_orders(rid), FoodOrder.created_at >= low, FoodOrder.created_at < high,
        cast(field("quantity"), Integer) > 0,
    ).group_by(key).order_by(
        (revenue if sort == "revenue" else quantity).desc(), key.asc()).limit(10).all()
    return [{"key": key, "name": f"{name} · {variant_name}" if variant_name else name,
             "quantity": quantity, "revenue": money(revenue)} for key, name, variant_name, quantity, revenue in rows]


def _payments(db, rid, start, end):
    pc = PlatformCommission
    low, high = utc_bounds(start, end)
    method = func.coalesce(case((pc.source_type == "FOOD_ORDER", FoodOrder.payment_method),
                               (pc.source_type == "RESERVATION_FEE", Reservation.payment_method),
                               (pc.source_type == "PREORDER", Reservation.preorder_payment_method)), "UNKNOWN")
    rows = db.query(method, func.count(pc.id), func.sum(pc.gross_amount)).select_from(pc).outerjoin(
        FoodOrder, and_(pc.source_type == "FOOD_ORDER", pc.source_id == FoodOrder.id, FoodOrder.restaurant_id == rid)).outerjoin(
        Reservation, and_(pc.source_type.in_(("RESERVATION_FEE", "PREORDER")), pc.source_id == Reservation.id,
                          Reservation.restaurant_id == rid)).filter(
        pc.restaurant_id == rid, pc.source_type.in_(PAYMENT_SOURCES), pc.occurred_at >= low, pc.occurred_at < high,
    ).group_by(method).order_by(func.sum(pc.gross_amount).desc(), method).all()
    return [{"method": method, "count": count, "amount": money(amount)} for method, count, amount in rows]


def _reviews(db, rid, start, end, previous_start):
    low, high = utc_bounds(previous_start, end)
    current = Review.created_at >= utc_bounds(start, end)[0]
    rows = db.query(current, Review.rating, func.count(Review.id)).filter(
        Review.restaurant_id == rid, Review.status == "PUBLISHED", Review.created_at >= low, Review.created_at < high,
    ).group_by(current, Review.rating).all()
    ratings = {True: dict.fromkeys(range(1, 6), 0), False: dict.fromkeys(range(1, 6), 0)}
    for flag, rating, count in rows:
        ratings[bool(flag)][rating] = count
    totals = {flag: sum(values.values()) for flag, values in ratings.items()}
    averages = {flag: money(sum(rating * count for rating, count in values.items()) / Decimal(totals[flag]))
                if totals[flag] else Decimal(0) for flag, values in ratings.items()}
    recent = db.query(Review).filter(Review.restaurant_id == rid, Review.status == "PUBLISHED",
        Review.created_at >= utc_bounds(start, end)[0], Review.created_at < high).order_by(Review.created_at.desc(), Review.id).limit(5).all()
    return metric(averages[True], averages[False]), {
        "total": totals[True], "average": averages[True],
        "distribution": [{"status": str(rating), "count": count} for rating, count in ratings[True].items()],
        "recent": [{"id": str(r.id), "rating": r.rating, "content": r.content, "created_at": r.created_at} for r in recent]}


def _peaks(db, model, rid, start, end, expression):
    if model is FoodOrder:
        low, high = utc_bounds(start, end)
        filters = (model.created_at >= low, model.created_at < high, model.status.notin_(CANCELLED))
    else:
        filters = (model.reservation_date >= start, model.reservation_date <= end, model.status.in_(ELIGIBLE_VISITS))
    return [{"label": str(label), "count": count} for label, count in db.query(expression, func.count(model.id)).filter(
        model.restaurant_id == rid, *filters).group_by(expression).order_by(func.count(model.id).desc(), expression).limit(3).all()]


def _live(db, rid, now):
    local_now = now.astimezone(RESTAURANT_TIMEZONE)
    today, clock = local_now.date(), local_now.time().replace(tzinfo=None)
    low, high = utc_bounds(today, today)
    assignment = (ReservationTable.table_id == DiningTable.id, Reservation.restaurant_id == rid)
    seated = select(ReservationTable.id).join(Reservation).where(*assignment, Reservation.status == "SEATED").exists()
    overlap = or_(
        and_(Reservation.reservation_date == today, Reservation.start_time <= clock,
             or_(Reservation.end_time > clock, Reservation.end_time <= Reservation.start_time)),
        and_(Reservation.reservation_date == today - timedelta(days=1),
             Reservation.end_time <= Reservation.start_time, Reservation.end_time > clock))
    reserved = select(ReservationTable.id).join(Reservation).where(
        *assignment, Reservation.status.in_(("PENDING_PAYMENT", "CONFIRMED")), overlap).exists()
    manual_today = and_(DiningTable.updated_at >= low, DiningTable.updated_at < high)
    status = case((DiningTable.status == "maintenance", "maintenance"), (seated, "occupied"),
                  (and_(DiningTable.status == "occupied", manual_today), "occupied"),
                  (reserved, "reserved"), (and_(DiningTable.status == "reserved", manual_today), "reserved"),
                  else_="available")
    query = db.query(DiningTable.id, DiningTable.table_number, DiningTable.capacity,
                     RestaurantFloor.floor_name, status.label("live_status")).join(RestaurantFloor).filter(
        DiningTable.restaurant_id == rid, RestaurantFloor.restaurant_id == rid,
        RestaurantFloor.is_active.is_(True), DiningTable.status != "inactive")
    counts = dict(db.query(status, func.count(DiningTable.id)).select_from(DiningTable).join(RestaurantFloor).filter(
        DiningTable.restaurant_id == rid, RestaurantFloor.restaurant_id == rid,
        RestaurantFloor.is_active.is_(True), DiningTable.status != "inactive").group_by(status).all())
    total = sum(counts.values())
    preview = query.order_by(RestaurantFloor.floor_number, DiningTable.table_number, DiningTable.id).limit(20).all()
    upcoming = db.query(Reservation).filter(Reservation.restaurant_id == rid,
        Reservation.status.in_(("CONFIRMED", "PENDING_PAYMENT")),
        or_(Reservation.reservation_date > today, and_(Reservation.reservation_date == today, Reservation.start_time >= clock)),
    ).order_by(Reservation.reservation_date, Reservation.start_time, Reservation.id).limit(5).all()
    active = db.query(FoodOrder).filter(FoodOrder.restaurant_id == rid,
        FoodOrder.status.in_(("PLACED", "ACCEPTED", "PREPARING", "READY", "SERVED")),
    ).order_by(FoodOrder.created_at.desc(), FoodOrder.id).limit(5).all()
    return {"as_of": now, "tables": {"total": total, **{key: counts.get(key, 0) for key in
            ("occupied", "available", "reserved", "maintenance")}, "occupancy_percent": percent(counts.get("occupied", 0), total)},
        "table_preview": [{"id": str(id), "name": name, "seats": seats, "floor": floor, "status": state}
                          for id, name, seats, floor, state in preview],
        "upcoming_reservations": [{"id": str(r.id), "date": r.reservation_date, "time": r.start_time.strftime("%H:%M"),
                                    "guests": r.number_of_guests, "status": r.status} for r in upcoming],
        "active_orders": [{"id": str(o.id), "status": o.status, "amount": o.total, "created_at": o.created_at} for o in active]}


def dashboard(db: Session, restaurant, manager, params: DashboardQuery, *, now=None) -> DashboardResponse:
    start, end = params.start_date, params.end_date
    days = (end - start).days + 1
    previous_start, previous_end = start - timedelta(days=days), start - timedelta(days=1)
    group = params.group_by
    if group == "auto":
        group = "day" if days <= 62 else "week" if days <= 366 else "month" if days <= 1096 else "year"
    dates = _dates(start, end, group)
    rid = restaurant.id
    order_trend, order_counts, previous_orders = _counts(db, FoodOrder, rid, FoodOrder.created_at, start, end, previous_start, group, dates)
    visit_trend, visit_counts, previous_visits = _counts(db, Reservation, rid, Reservation.reservation_date, start, end, previous_start, group, dates)
    revenue, revenue_chart, previous_revenue = _revenue(db, rid, start, end, previous_start, group, dates)
    customers, previous_customers, customer_summary = _customers(db, rid, start, end, previous_start, group, dates)
    rating, reviews = _reviews(db, rid, start, end, previous_start)
    guests, party_size = db.query(func.sum(Reservation.number_of_guests), func.avg(Reservation.number_of_guests)).filter(
        Reservation.restaurant_id == rid, Reservation.reservation_date >= start,
        Reservation.reservation_date <= end, Reservation.status.in_(ELIGIBLE_VISITS)).one()
    postgres = db.get_bind().dialect.name == "postgresql"
    order_hour = func.to_char(_local(db, FoodOrder.created_at), "HH24") if postgres else func.strftime("%H", _local(db, FoodOrder.created_at))
    visit_hour = func.to_char(Reservation.start_time, "HH24") if postgres else func.strftime("%H", Reservation.start_time)
    visit_day = func.to_char(Reservation.reservation_date, "ID") if postgres else func.strftime("%w", Reservation.reservation_date)
    weekdays = {str(i): label for i, label in enumerate(("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"))}
    weekdays["7"] = "Sunday"
    peak_days = _peaks(db, Reservation, rid, start, end, visit_day)
    for point in peak_days:
        point["label"] = weekdays[point["label"]]
    orders, visits = sum(order_counts.values()), sum(visit_counts.values())
    return DashboardResponse(
        manager_name=manager.name, restaurant_name=restaurant.name,
        period={"start_date": start, "end_date": end, "previous_start_date": previous_start,
                "previous_end_date": previous_end, "timezone": str(RESTAURANT_TIMEZONE), "group_by": group},
        overview={"revenue": metric(revenue["revenue"], previous_revenue), "orders": metric(orders, previous_orders),
                  "reservations": metric(visits, previous_visits), "customers": metric(customers, previous_customers),
                  "average_order_value": _aov(db, rid, start, end, previous_start), "rating": rating},
        revenue=revenue, revenue_chart=revenue_chart, customers=customer_summary,
        operations={"order_status": [{"status": status, "count": count} for status, count in order_counts.items()],
            "reservation_status": [{"status": status, "count": count} for status, count in visit_counts.items()],
            "order_trend": order_trend, "reservation_trend": visit_trend,
            "order_completion_percent": percent(order_counts.get("COMPLETED", 0), orders),
            "reservation_completion_percent": percent(visit_counts.get("COMPLETED", 0), visits),
            "average_party_size": money(party_size), "expected_guests": guests or 0,
            "peak_order_hours": _peaks(db, FoodOrder, rid, start, end, order_hour),
            "peak_reservation_hours": _peaks(db, Reservation, rid, start, end, visit_hour), "peak_reservation_days": peak_days},
        popular_items=_popular(db, rid, start, end, params.popular_sort), payment_methods=_payments(db, rid, start, end), reviews=reviews,
        live=_live(db, rid, now or datetime.now(timezone.utc)),
        notes=["Revenue uses recorded collections and subtracts refunds on their reversal date; net revenue also subtracts commission after reversals. Tax is included in collected revenue.",
               "Orders and item sales use order creation dates; reservations use scheduled arrival dates; payments use collection dates; reviews use publication record creation dates.",
               "Customer counts exclude anonymous walk-ins and cancelled, rejected or refunded orders; reservation activity includes confirmed, seated and completed bookings. Returning customers have earlier eligible restaurant history.",
               "Item sales allocate saved discounts before tax and commission; only paid, non-cancelled orders qualify. Reservation preorders appear after a kitchen order exists.",
               "Collections missing a commission ledger record are excluded from revenue. No payment method is inferred when its source is missing.",
               "Live seating uses active floors, seated bookings and today's manual table states. Maintenance tables remain in the active table total. Live panels do not follow the historical date filter."],
    )
