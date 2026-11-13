"""Bounded kitchen projections and SQL aggregates for an authorized restaurant."""
from datetime import datetime, time, timedelta, timezone

from sqlalchemy import case, func, or_

from app.models.dining import DiningTable, RestaurantFloor
from app.models.orders import FoodOrder
from app.models.realtime import RealtimeEvent
from app.models.reservation import Reservation, ReservationTable
from app.routers.commerce.orders import CHEF_TRANSITIONS
from app.schemas.chef_dashboard import ChefDashboardResponse
from app.services.table_availability import RESTAURANT_TIMEZONE

ACTIVE = ("PLACED", "ACCEPTED", "PREPARING", "READY")
PREPARED = ("READY", "SERVED", "COMPLETED")
STATUSES = (*ACTIVE, "SERVED", "COMPLETED", "CANCELLED", "REJECTED")


def aware(value):
    # PostgreSQL returns aware instants. SQLite fixtures lose their UTC offset.
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def midnight(day):
    return datetime.combine(day, time.min, RESTAURANT_TIMEZONE).astimezone(timezone.utc)


def bucket(db, column, hourly=False):
    if db.get_bind().dialect.name == "postgresql":
        return func.to_char(func.timezone(str(RESTAURANT_TIMEZONE), column),
                            'YYYY-MM-DD"T"HH24:00' if hourly else "YYYY-MM-DD")
    return func.strftime("%Y-%m-%dT%H:00" if hourly else "%Y-%m-%d", column, "+330 minutes")


def kitchen_items(items):
    # Explicit allowlist strips prices, payment fields, and snapshot metadata.
    return [{"food_name": item.get("food_name") or item.get("name") or "Unnamed item",
             "variant_name": item.get("variant_name"), "quantity": item.get("quantity", 0)}
            for item in (items or [])]


def table_maps(db, rid, orders, visits):
    reservation_ids = {row.reservation_id for row in orders if row.reservation_id} | {row.id for row in visits}
    table_ids = {row.table_id for row in orders if row.table_id}
    by_visit, by_table = {}, {}
    columns = (DiningTable.id, DiningTable.table_number, RestaurantFloor.floor_name)
    base = db.query(*columns).join(RestaurantFloor, RestaurantFloor.id == DiningTable.floor_id).filter(
        DiningTable.restaurant_id == rid, RestaurantFloor.restaurant_id == rid)

    def projection(row):
        return {"id": str(row.id), "table_number": row.table_number, "floor_name": row.floor_name}

    if reservation_ids:
        for row in base.add_columns(ReservationTable.reservation_id).join(
                ReservationTable, ReservationTable.table_id == DiningTable.id).join(
                Reservation, Reservation.id == ReservationTable.reservation_id).filter(
                Reservation.restaurant_id == rid, ReservationTable.reservation_id.in_(reservation_ids)
                ).order_by(DiningTable.table_number).all():
            by_visit.setdefault(row.reservation_id, []).append(projection(row))
    if table_ids:
        by_table = {row.id: [projection(row)] for row in base.filter(DiningTable.id.in_(table_ids)).all()}
    return by_visit, by_table


def dashboard(db, restaurant, params, *, now=None):
    now = aware(now or datetime.now(timezone.utc))
    today = now.astimezone(RESTAURANT_TIMEZONE).date()
    rid = restaurant.id
    day_start, day_end = midnight(today), midnight(today + timedelta(days=1))
    yesterday_start = midnight(today - timedelta(days=1))
    start = today - timedelta(days={"today": 0, "last7": 6, "last30": 29}[params.period])
    hourly = params.period == "today"
    scope = db.query(FoodOrder).filter(FoodOrder.restaurant_id == rid)

    # Daily receipt counts and live queue counts have deliberately different scopes.
    today_orders, yesterday_orders = scope.with_entities(
        func.count(case((FoodOrder.created_at >= day_start, 1))),
        func.count(case((FoodOrder.created_at < day_start, 1))),
    ).filter(FoodOrder.created_at >= yesterday_start, FoodOrder.created_at < day_end).one()
    live_counts = dict(scope.with_entities(FoodOrder.status, func.count()).filter(
        FoodOrder.status.in_(ACTIVE)).group_by(FoodOrder.status).all())
    completed_at = func.coalesce(FoodOrder.completed_at, FoodOrder.created_at)
    completed = scope.filter(FoodOrder.status == "COMPLETED", completed_at >= day_start, completed_at < day_end).count()

    order_bucket = bucket(db, FoodOrder.created_at, hourly)
    aggregates = scope.with_entities(order_bucket, FoodOrder.status, func.count()).filter(
        FoodOrder.created_at >= midnight(start), FoodOrder.created_at < day_end).group_by(order_bucket, FoodOrder.status).all()
    dates = ([f"{today.isoformat()}T{hour:02}:00" for hour in range(24)] if hourly else
             [(start + timedelta(days=index)).isoformat() for index in range((today - start).days + 1)])
    volume = dict.fromkeys(dates, 0)
    statuses = dict.fromkeys(STATUSES, 0)
    for key, status, count in aggregates:
        volume[key] += count
        statuses[status] = statuses.get(status, 0) + count

    # Durations use the actual start and ready instants, never updated_at or estimates.
    if db.get_bind().dialect.name == "postgresql":
        minutes = func.extract("epoch", FoodOrder.ready_at - FoodOrder.preparation_started_at) / 60.0
    else:
        minutes = (func.julianday(FoodOrder.ready_at) - func.julianday(FoodOrder.preparation_started_at)) * 1440.0
    valid = (FoodOrder.preparation_started_at.is_not(None)
             & (FoodOrder.preparation_started_at >= FoodOrder.created_at)
             & (FoodOrder.ready_at >= FoodOrder.preparation_started_at))
    prep_day = bucket(db, FoodOrder.ready_at)
    prep_rows = scope.with_entities(prep_day, func.count(), func.count(case((valid, 1))),
                                   func.avg(case((valid, minutes)))).filter(
        FoodOrder.status.in_(PREPARED), FoodOrder.ready_at >= midnight(today - timedelta(days=6)),
        FoodOrder.ready_at < day_end, FoodOrder.ready_at <= now,
    ).group_by(prep_day).all()
    prep = {key: (count, measured, round(float(average), 4) if average is not None else None)
            for key, count, measured, average in prep_rows}
    prep_points = []
    for index in range(7):
        day = today - timedelta(days=6 - index)
        count, measured, average = prep.get(day.isoformat(), (0, 0, None))
        prep_points.append({"date": day, "prepared_orders": count,
                            "measured_orders": measured, "average_minutes": average})
    prepared_today, measured_today, average_today = prep.get(today.isoformat(), (0, 0, None))

    active_scope = scope.filter(FoodOrder.status.in_(ACTIVE))
    oldest = active_scope.with_entities(func.min(FoodOrder.created_at)).scalar()
    # Rank before applying a status filter, so "oldest active" is true across the kitchen.
    ranked = active_scope.with_entities(FoodOrder.id.label("order_id"), func.row_number().over(
        order_by=(FoodOrder.created_at, FoodOrder.id)).label("age_rank")).subquery()
    filtered = scope.join(ranked, ranked.c.order_id == FoodOrder.id)
    if params.status != "ACTIVE":
        filtered = filtered.filter(FoodOrder.status == params.status)
    total = sum(live_counts.values()) if params.status == "ACTIVE" else live_counts.get(params.status, 0)
    page = min(params.page, max(1, (total + params.page_size - 1) // params.page_size))
    order_rows = filtered.add_columns(ranked.c.age_rank).order_by(FoodOrder.created_at, FoodOrder.id).offset(
        (page - 1) * params.page_size).limit(params.page_size).all()
    orders = [row[0] for row in order_rows]

    local_now = now.astimezone(RESTAURANT_TIMEZONE)
    visits = db.query(Reservation).filter(
        Reservation.restaurant_id == rid, Reservation.status == "CONFIRMED",
        or_(Reservation.reservation_date > today,
            (Reservation.reservation_date == today) & (Reservation.start_time >= local_now.time().replace(tzinfo=None))),
    ).order_by(Reservation.reservation_date, Reservation.start_time, Reservation.id).limit(8).all()
    by_visit, by_table = table_maps(db, rid, orders, visits)
    active = []
    for order, rank in order_rows:
        items = kitchen_items(order.items)
        active.append({"id": str(order.id), "status": order.status, "service_type": order.service_type,
            "created_at": aware(order.created_at), "accepted_at": aware(order.accepted_at) if order.accepted_at else None,
            "preparation_started_at": aware(order.preparation_started_at) if order.preparation_started_at else None,
            "ready_at": aware(order.ready_at) if order.ready_at else None,
            "elapsed_seconds": max(0, int((now - aware(order.created_at)).total_seconds())), "age_rank": rank,
            "items": items, "item_count": sum(item["quantity"] for item in items),
            "tables": by_visit.get(order.reservation_id, []) if order.reservation_id else by_table.get(order.table_id, []),
            "customer_note": order.customer_note, "allowed_actions": sorted(CHEF_TRANSITIONS.get(order.status, set()))})

    events = db.query(RealtimeEvent).filter(RealtimeEvent.restaurant_id == rid,
        RealtimeEvent.event_type == "order.status_changed", RealtimeEvent.created_at <= now,
    ).order_by(RealtimeEvent.created_at.desc(), RealtimeEvent.id.desc()).limit(12).all()
    descriptions = {"PLACED": "New order received", "ACCEPTED": "Order accepted",
                    "PREPARING": "Preparation started", "READY": "Order marked ready", "SERVED": "Order served",
                    "COMPLETED": "Order completed", "CANCELLED": "Order cancelled", "REJECTED": "Order rejected"}
    activity = []
    for event in events:
        data = event.data or {}
        status = data.get("status")
        # Payment updates also use this event type; they must not invent status changes.
        if status not in descriptions or data.get("previous_status") == status:
            continue
        if "previous_status" not in data and status not in {"PLACED", "ACCEPTED"}:
            continue
        activity.append({"id": str(event.event_id), "order_id": event.resource_id, "status": status,
            "description": descriptions[status] if "previous_status" in data else f"Order status recorded: {status.lower()}",
            "occurred_at": aware(event.created_at)})

    return ChefDashboardResponse(
        as_of=now, timezone=str(RESTAURANT_TIMEZONE), today=today, start_date=start, end_date=today,
        group_by="hour" if hourly else "day",
        summary={"today_orders": today_orders, "yesterday_orders": yesterday_orders,
            "orders_change_percent": round((today_orders - yesterday_orders) / yesterday_orders * 100, 2) if yesterday_orders else None,
            "pending_orders": live_counts.get("PLACED", 0) + live_counts.get("ACCEPTED", 0),
            "preparing_orders": live_counts.get("PREPARING", 0), "ready_orders": live_counts.get("READY", 0),
            "completed_orders": completed, "average_preparation_minutes": average_today},
        order_volume=[{"date": key, "total": value} for key, value in volume.items()],
        status_distribution=[{"status": key, "count": value} for key, value in statuses.items()],
        preparation_performance=prep_points,
        active_orders={"items": active, "total": total, "page": page, "page_size": params.page_size},
        upcoming_reservations=[{"id": str(row.id), "arrival_at": datetime.combine(row.reservation_date, row.start_time, RESTAURANT_TIMEZONE),
            "number_of_guests": row.number_of_guests, "status": row.status, "tables": by_visit.get(row.id, []),
            "preorder_items": kitchen_items(row.preorder_items) if row.fulfillment_type == "PREORDER" else []} for row in visits],
        performance={"prepared_today": prepared_today, "measured_today": measured_today,
            "average_preparation_minutes": average_today,
            "longest_active_seconds": max(0, int((now - aware(oldest)).total_seconds())) if oldest else None,
            "delayed_orders": None, "delay_threshold_minutes": None},
        recent_activity=activity,
        notes=[
            "Receipt counts use order creation dates, including subsequently cancelled or rejected orders. Yesterday is the full previous local day.",
            "Pending includes placed and accepted orders. Pending, preparing and ready are live across all receipt dates; the analytics filter only changes volume and status charts.",
            "Completed counts use completed_at, with receipt date for historical completed orders missing that timestamp.",
            "Prepared totals use ready_at for orders currently ready, served or completed. Preparation averages use only valid preparation_started_at to ready_at pairs, grouped by ready date; missing durations remain unavailable.",
            "All business dates follow the existing Asia/Kolkata restaurant timezone. No per-restaurant timezone or delay target is configured.",
            "The queue is oldest first. Priority reflects actual receipt age, without an invented service deadline. Upcoming reservations show the next eight confirmed arrivals.",
        ],
    )
