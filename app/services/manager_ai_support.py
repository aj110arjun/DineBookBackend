"""Manager-owned SELECT-only answers. Ollama classifies questions, never account facts."""
import re
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import and_, or_, text
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.models.orders import FoodOrder
from app.models.reservation import Reservation
from app.models.wallet import WalletAccount, WalletTransaction
from app.routers.auth.manager_auth import current_manager
from app.routers.restaurants.dining import get_manager_restaurant
from app.models.restaurant import RestaurantStatus
from app.schemas.ai_chat import AICard, AISource
from app.schemas.manager_ai import ManagerAIPlan, ManagerAIResponse
from app.schemas.manager_dashboard import DashboardQuery
from app.services.manager_dashboard import dashboard, _live, utc_bounds
from app.services.ollama_service import ollama_service, OllamaServiceError
from app.services.table_availability import RESTAURANT_TIMEZONE

REFERENCE = re.compile(r"\b[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}\b", re.I)
ACTIVE_ORDERS = ("PLACED", "ACCEPTED", "PREPARING", "READY", "SERVED")
ACTIVE_VISITS = ("PENDING_PAYMENT", "CONFIRMED", "SEATED")
HELP = {
    "reservations": ("Reservations", "/manager/reservations", "Open Reservations to review arrivals, check guests in, or manage a booking. The assistant only reads booking information."),
    "orders": ("Orders", "/manager/reservations", "Open a reservation to manage its orders. Use New walk-in order for a walk-in. Accepting, preparing, completing or cancelling orders uses the existing order controls."),
    "revenue": ("Revenue & wallet", "/manager/revenue", "Revenue reflects the recorded collection ledger, with refunds deducted on their reversal date. Net revenue also subtracts commission. Wallet balance is separate from revenue; open Revenue & wallet for transfers and payouts."),
    "payments": ("Payments", "/manager/payments", "Open Payments to check recorded payment details. Pending payments below are application records, not an independent bank or payment-provider inquiry."),
    "tables": ("Tables & floors", "/manager/tables", "Open Tables & floors to manage your seating layout. Live table counts follow the dashboard's active-floor, maintenance, seating and reservation rules."),
    "menu": ("Menu", "/manager/menu", "Open Menu to manage dishes and availability. The assistant does not change menu items, prices or stock."),
    "hours": ("Opening hours", "/manager/operating-hours", "Open Opening hours to manage operating periods and date exceptions. Changes affect booking availability."),
    "staff": ("Kitchen team", "/manager/staff", "Open Kitchen team to manage staff and chef invitations. The assistant cannot create accounts or change staff access."),
    "offers": ("Offers", "/manager/offers", "Open Offers to manage restaurant discounts. The assistant cannot create or apply discounts."),
    "settings": ("Settings", "/manager/settings", "Open Settings to manage your account preferences. The assistant does not change account or notification settings."),
}


def authorize(context, db):
    manager = current_manager(token=context.token, db=db)
    restaurant = get_manager_restaurant(manager, db)
    if restaurant.status != RestaurantStatus.APPROVED or restaurant.id != context.restaurant_id:
        raise HTTPException(403, "Your restaurant access has changed. Please sign in again.")
    return manager, restaurant


def period_bounds(period, now):
    today = now.astimezone(RESTAURANT_TIMEZONE).date()
    if period == "yesterday":
        return today - timedelta(days=1), today - timedelta(days=1)
    if period == "last_7_days":
        return today - timedelta(days=6), today
    if period == "this_month":
        return today.replace(day=1), today
    return today, today


def simple_plan(payload):
    value = payload.message.casefold()
    action = payload.action
    if not action:
        topics = [("support", ("human support", "contact support", "support agent")),
                  ("wallet", ("wallet", "balance", "transfer", "payout")),
                  ("revenue", ("revenue", "earning", "commission", "sales", "collection")),
                  ("help", ("refund",)),
                  ("payments", ("payment", "unpaid")),
                  ("reservations", ("reservation", "booking", "arrival", "guest")),
                  ("orders", ("order", "kitchen", "preparing")),
                  ("tables", ("table", "occupancy", "seating")),
                  ("overview", ("summary", "summarize", "overview", "dashboard")),
                  ("help", ("help", "menu", "staff", "hours", "settings", "offer"))]
        action = next((key for key, words in topics if any(word in value for word in words)), None)
    period = next((key for key, words in [("yesterday", ("yesterday",)), ("last_7_days", ("last 7 days", "past 7 days", "past week", "last week")),
                                        ("this_month", ("this month",)), ("today", ("today",))] if any(word in value for word in words)), None)
    if not action and payload.context and (period or re.search(r"\b(completed|cancelled|canceled|active|all)\b", value)):
        action = payload.context.action
    state = "completed" if "completed" in value else "cancelled" if "cancelled" in value or "canceled" in value else "active" if any(word in value for word in ("active", "pending", "preparing", "upcoming")) else "all" if period or "history" in value or "all orders" in value else "active"
    return ManagerAIPlan(action=action or "help", period=period, state=state), action is not None


async def route_plan(payload):
    plan, recognized = simple_plan(payload)
    if payload.action or recognized:
        return plan, False
    try:
        raw = await ollama_service.chat([
            {"role": "system", "content": "Classify a DineBook restaurant manager question using only the supplied JSON schema. Never answer or invent facts. Allowed actions: overview, reservations, orders, payments, revenue, wallet, tables, help, support. Use help for unrelated, unsupported, administrative or how-to questions. period must be null unless explicitly requested: today, yesterday, last_7_days or this_month. No SQL, identifiers or restaurant selection is allowed."},
            *[{"role": "user", "content": REFERENCE.sub("[reference]", message)[:400]} for message in payload.history[-2:]],
            {"role": "user", "content": payload.message},
        ], ManagerAIPlan.model_json_schema())
        inferred = ManagerAIPlan.model_validate_json(raw)
        # The model selects a topic only; dates and filters come from the request.
        return plan.model_copy(update={"action": inferred.action}), False
    except (OllamaServiceError, ValidationError):
        return plan, True


def card(title, kind, fields, href):
    return AICard(title=title, kind=kind, fields={key: str(value) for key, value in fields.items()}, href=href)


def currency(value):
    return f"INR {Decimal(value or 0):.2f}"


def help_answer(message):
    value = message.casefold()
    aliases = {"booking": "reservations", "reservation": "reservations", "order": "orders", "wallet": "revenue", "commission": "revenue", "transfer": "revenue", "payout": "revenue", "payment": "payments", "refund": "payments", "table": "tables", "floor": "tables", "dish": "menu", "chef": "staff", "invitation": "staff", "discount": "offers", "notification": "settings"}
    keys = [key for key in HELP if key in value]
    keys += [topic for word, topic in aliases.items() if word in value]
    keys = list(dict.fromkeys(keys))[:2] or ["reservations", "revenue"]
    return "\n\n".join(HELP[key][2] for key in keys), [AISource(id=f"manager-help:{key}", title=HELP[key][0], href=HELP[key][1]) for key in keys]


def read_answer(context, payload, plan, limited, *, now=None):
    now = now or datetime.now(timezone.utc)
    response = ManagerAIResponse(reply="", language="en", as_of=now, limited=limited,
        client_message_id=payload.client_message_id,
        message_id=uuid.uuid5(uuid.NAMESPACE_URL, f"manager:{context.user_id}:{context.restaurant_id}:{payload.client_message_id}"),
        restaurant_id=context.restaurant_id, context=plan)
    with Session(context.bind, autoflush=False) as db:
        if db.get_bind().dialect.name == "postgresql":
            db.execute(text("SET TRANSACTION READ ONLY")); db.execute(text("SET LOCAL statement_timeout = 2000"))
        manager, restaurant = authorize(context, db)
        rid = restaurant.id
        start, end = period_bounds(plan.period, now)
        reference = REFERENCE.search(payload.message)
        resource_id = uuid.UUID(reference[0]) if reference else None
        # Unsupported time windows get clarification rather than today's facts.
        unsupported = re.search(r"\b\d{4}-\d{2}-\d{2}\b|\b(tomorrow|last month|last year|next week|next month|this week|this year)\b", payload.message, re.I)
        relative = re.findall(r"\b(?:last|past|next)\s+\d+\s+(?:days?|weeks?|months?|years?)\b", payload.message, re.I)
        if unsupported or any(value.casefold() not in {"last 7 days", "past 7 days"} for value in relative):
            response.reply = "I can check today, yesterday, the last 7 days, or this month. Upcoming reservations shows the next scheduled bookings. Which view would you like?"
            return response
        mutation = re.search(r"\b(cancel|delete|update|edit|remove|accept|reject|complete|transfer|withdraw|pay|create|mark|refund|send)\b", payload.message, re.I)
        how_to = re.search(r"\b(how (?:do|can|to)|explain|guide|what (?:is|does))\b", payload.message, re.I)
        if plan.action == "support":
            response.reply = "Copy the chat summary to share with DineBook support using the configured contact link. No person has been contacted and no ticket has been created."
            response.escalation = True
        elif plan.action == "help" or mutation or how_to:
            response.reply, response.sources = help_answer(payload.message)
            if mutation:
                response.reply = "I can read restaurant information. Use the relevant portal controls to make changes.\n\n" + response.reply
        elif plan.action in {"overview", "revenue"}:
            data = dashboard(db, restaurant, manager, DashboardQuery(start_date=start, end_date=end), now=now)
            dates = {"From": start, "Through": end, "Timezone": str(RESTAURANT_TIMEZONE)}
            if plan.action == "revenue":
                response.reply = "Here are your recorded collections for this period. Refunds use their reversal date; net revenue also subtracts commission."
                response.cards = [card("Restaurant revenue", "manager_revenue", {**dates, "Gross collections": currency(data.revenue.gross), "Refunds": currency(data.revenue.refunds), "Commission": currency(data.revenue.commissions), "Revenue after refunds": currency(data.revenue.revenue), "Net revenue": currency(data.revenue.net)}, "/manager/revenue")]
            else:
                response.reply = "Here's your restaurant summary. Period totals use the dates below; active orders and table counts are live."
                response.cards = [card(restaurant.name, "manager_summary", {**dates, "Orders": int(data.overview.orders.value), "Reservations": int(data.overview.reservations.value), "Expected guests": data.operations.expected_guests, "Recorded revenue": currency(data.revenue.revenue)}, "/manager/dashboard"),
                    card("Live operations", "manager_summary", {"Active orders shown": len(data.live.active_orders), "Available tables": data.live.tables.available, "Occupied tables": data.live.tables.occupied, "Reserved tables": data.live.tables.reserved, "Maintenance tables": data.live.tables.maintenance}, "/manager/tables")]
        elif plan.action in {"orders", "reservations", "payments"}:
            if plan.action == "payments":
                response.reply = "Here are up to five pending order payments and five pending reservation fees. Open Payments for complete recorded payment details."
                queries = [(FoodOrder, (FoodOrder.payment_status == "PENDING",)), (Reservation, (Reservation.payment_status == "PENDING",))]
                if plan.period:
                    low, high = utc_bounds(start, end)
                    queries = [(FoodOrder, (*queries[0][1], FoodOrder.created_at >= low, FoodOrder.created_at < high)),
                               (Reservation, (*queries[1][1], Reservation.reservation_date >= start, Reservation.reservation_date <= end))]
                    response.reply += f" Orders placed and reservations scheduled from {start} through {end} (IST)."
            else:
                model = FoodOrder if plan.action == "orders" else Reservation
                states = ACTIVE_ORDERS if model is FoodOrder else ACTIVE_VISITS
                filters = []
                if not resource_id and plan.state != "all":
                    filters.append(model.status.in_(states if plan.state == "active" else ("COMPLETED",) if plan.state == "completed" else ("CANCELLED", "REJECTED", "NO_SHOW")))
                if not resource_id and plan.period:
                    if model is Reservation:
                        filters += [model.reservation_date >= start, model.reservation_date <= end]
                    else:
                        low, high = utc_bounds(start, end); filters += [model.created_at >= low, model.created_at < high]
                elif not resource_id and model is Reservation and plan.state == "active":
                    local = now.astimezone(RESTAURANT_TIMEZONE)
                    filters += [or_(Reservation.reservation_date > local.date(), and_(Reservation.reservation_date == local.date(), Reservation.start_time >= local.time().replace(tzinfo=None)))]
                queries = [(model, tuple(filters))]
                response.reply = f"Here are up to five {'orders' if model is FoodOrder else 'reservations'}" + (f" from {start} through {end} (IST)." if plan.period and not resource_id else ".")
            for model, filters in queries:
                query = db.query(model).filter(model.restaurant_id == rid, *filters)
                if resource_id:
                    query = query.filter(model.id == resource_id)
                ordering = ((Reservation.reservation_date, Reservation.start_time, Reservation.id) if plan.state == "active"
                            else (Reservation.reservation_date.desc(), Reservation.start_time.desc(), Reservation.id)) if model is Reservation else (FoodOrder.created_at.desc(), FoodOrder.id)
                for row in query.order_by(*ordering).limit(5).all():
                    if model is Reservation:
                        response.cards.append(card("Reservation", "reservation", {"Booking ID": row.id, "Status": row.status, "Date": row.reservation_date, "Start time": row.start_time.strftime("%H:%M"), "Timezone": str(RESTAURANT_TIMEZONE), "Guests": row.number_of_guests, "Fee payment": row.payment_status}, f"/manager/reservations?reservation_id={row.id}"))
                    else:
                        response.cards.append(card("Restaurant order", "order", {"Order ID": row.id, "Status": row.status, "Total": currency(row.total), "Payment": row.payment_status, "Service": row.service_type, "Placed": row.created_at.isoformat()}, f"/manager/orders/{row.id}"))
            if not response.cards:
                response.reply = "No matching records were found in your restaurant. Check the reference or try another supported period."
        elif plan.action == "wallet":
            wallet = db.get(WalletAccount, manager.id)
            response.reply = "Here's your manager wallet balance and up to five recent entries. Wallet balance is separate from restaurant revenue."
            response.cards = [card("Manager wallet", "wallet", {"Balance": currency(wallet.balance if wallet else 0)}, "/manager/revenue")]
            entries = db.query(WalletTransaction).filter(WalletTransaction.user_id == manager.id).order_by(WalletTransaction.created_at.desc(), WalletTransaction.id).limit(5).all()
            response.cards += [card("Wallet entry", "wallet", {"Type": row.transaction_type, "Amount": currency(row.amount), "Recorded": row.created_at.isoformat()}, "/manager/revenue") for row in entries]
        elif plan.action == "tables":
            if plan.period not in {None, "today"}:
                response.reply = "I can check the current table state. Historical table occupancy isn't available in chat. Ask for live table status to see the current seating view."
                response.sources = [AISource(id="manager-help:tables", title="Tables & floors", href="/manager/tables")]
                return response
            live = _live(db, rid, now)
            response.reply = "Here is the live table state using the dashboard's seating rules."
            response.cards = [card("Live table status", "manager_tables", {key.replace("_", " ").capitalize(): value for key, value in live["tables"].items()}, "/manager/tables")]
    return response


async def answer_manager(context, payload):
    plan, limited = await route_plan(payload)
    return await run_in_threadpool(read_answer, context, payload, plan, limited)
