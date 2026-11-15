"""Customer-scoped SELECT-only projections. Never call wallet/payment mutators."""
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import or_

from app.models.orders import FoodOrder
from app.models.reservation import Reservation
from app.models.restaurant import Restaurant, RestaurantStatus
from app.models.offers import FoodOrderOffer
from app.models.wallet import WalletAccount, WalletTransaction, WalletTopUp
from app.routers.restaurants.operating_hours import effective_periods, serialize_periods
from app.routers.restaurants.reservations import suitable_table_groups, within_operating_hours, ReservationCreate
from app.routers.commerce.orders import customer_offer_uses
from app.schemas.ai_chat import AICard, AIPlan

LOCAL = ZoneInfo("Asia/Kolkata")


def money(value):
    return f"INR {Decimal(value or 0).quantize(Decimal('0.01'))}"


def stamp(value):
    if value is None:
        return "Not recorded"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


class CustomerReadTools:
    def __init__(self, db, customer_id: uuid.UUID):
        self.db = db
        self.customer_id = customer_id

    def restaurant(self, restaurant_id):
        row = self.db.query(Restaurant).filter(Restaurant.id == restaurant_id, Restaurant.status == RestaurantStatus.APPROVED).first()
        if row is None:
            raise HTTPException(404, "Restaurant not found.")
        return row

    def reservations(self, plan):
        query = self.db.query(Reservation).filter(Reservation.user_id == self.customer_id)
        if plan.resource_id:
            query = query.filter(Reservation.id == plan.resource_id)
        if plan.restaurant_id:
            query = query.filter(Reservation.restaurant_id == plan.restaurant_id)
        if plan.day:
            query = query.filter(Reservation.reservation_date == plan.day)
        rows = query.order_by(Reservation.created_at.desc()).limit(5).all()
        if plan.resource_id and not rows:
            raise HTTPException(404, "Reservation not found in your account.")
        cards = []
        for row in rows:
            restaurant = self.db.get(Restaurant, row.restaurant_id)
            cards.append(AICard(title=restaurant.name if restaurant else "Reservation", kind="reservation",
                restaurant_id=str(row.restaurant_id), href=f"/customer/profile/reservations/{row.id}", fields={
                    "Booking ID": str(row.id), "Status": row.status, "Date": row.reservation_date.isoformat(),
                    "Start time": row.start_time.strftime("%H:%M"), "Timezone": "Asia/Kolkata", "Guests": str(row.number_of_guests),
                    "Reservation fee": money(row.fee_amount), "Fee payment": row.payment_status,
                    "Payment method": row.payment_method, "Preorder payment": row.preorder_payment_status or "Not recorded",
                    "Preorder total": money(row.preorder_total),
                    "Created": stamp(row.created_at),
                }))
        return cards

    def orders(self, plan):
        query = self.db.query(FoodOrder).filter(FoodOrder.customer_id == self.customer_id)
        if plan.resource_id:
            query = query.filter(FoodOrder.id == plan.resource_id)
        if plan.restaurant_id:
            query = query.filter(FoodOrder.restaurant_id == plan.restaurant_id)
        rows = query.order_by(FoodOrder.created_at.desc()).limit(5).all()
        if plan.resource_id and not rows:
            raise HTTPException(404, "Order not found in your account.")
        cards = []
        for row in rows:
            restaurant = self.db.get(Restaurant, row.restaurant_id)
            cards.append(AICard(title=restaurant.name if restaurant else "Food order", kind="order",
                restaurant_id=str(row.restaurant_id), href="/customer/profile/orders", fields={
                    "Order ID": str(row.id), "Status": row.status, "Service": row.service_type,
                    "Total": money(row.total), "Payment": row.payment_status, "Payment method": row.payment_method,
                    "Subtotal": money(row.subtotal), "Discount": money(row.discount_amount), "Tax": money(row.tax_amount),
                    "Placed": stamp(row.created_at), "Preparation started": stamp(row.preparation_started_at),
                    "Ready": stamp(row.ready_at),
                }))
        return cards

    def wallet(self, plan):
        account = self.db.get(WalletAccount, self.customer_id)
        cards = [AICard(title="DineBook wallet", kind="wallet", href="/customer/profile/wallet",
                        fields={"Balance": money(account.balance if account else 0)})]
        rows = self.db.query(WalletTransaction).filter(WalletTransaction.user_id == self.customer_id).order_by(WalletTransaction.created_at.desc()).limit(5).all()
        for row in rows:
            cards.append(AICard(title="Wallet activity", kind="wallet", href="/customer/profile/wallet", fields={
                "Transaction ID": str(row.id), "Type": row.transaction_type, "Amount": money(row.amount), "Recorded": stamp(row.created_at),
            }))
        return cards

    def payments(self, plan):
        # Each source is authorized independently, including when IDs are supplied.
        if plan.resource_id and plan.source == "wallet":
            query = self.db.query(WalletTransaction).filter(WalletTransaction.user_id == self.customer_id, WalletTransaction.id == plan.resource_id)
            row = query.first()
            if not row:
                topup = self.db.query(WalletTopUp).filter(WalletTopUp.user_id == self.customer_id, WalletTopUp.id == plan.resource_id).first()
                if not topup:
                    raise HTTPException(404, "Transaction not found in your account.")
                return [AICard(title="Wallet top-up", kind="payment", href="/customer/profile/transactions", fields={
                    "Transaction ID": str(topup.id), "Amount": money(topup.amount), "Status": topup.status,
                })]
            return [AICard(title="Wallet transaction", kind="payment", href="/customer/profile/transactions", fields={
                "Transaction ID": str(row.id), "Type": row.transaction_type, "Amount": money(row.amount), "Recorded": stamp(row.created_at),
            })]
        cards = []
        if not plan.resource_id or plan.source == "reservation":
            cards.extend(self.reservations(plan))
        if not plan.resource_id or plan.source == "order":
            cards.extend(self.orders(plan))
        if not plan.resource_id:
            for row in self.db.query(WalletTopUp).filter(WalletTopUp.user_id == self.customer_id).order_by(WalletTopUp.created_at.desc()).limit(5).all():
                cards.append(AICard(title="Wallet top-up", kind="payment", href="/customer/profile/transactions", fields={
                    "Transaction ID": str(row.id), "Amount": money(row.amount), "Status": row.status, "Created": stamp(row.created_at),
                }))
        return sorted(cards, key=lambda card: card.fields.get("Created", card.fields.get("Placed", "")), reverse=True)[:5]

    def refunds(self, plan):
        # Status flags and ledger entries are evidence, never an instruction to refund.
        if plan.resource_id:
            return self.payments(plan)
        rows = self.db.query(WalletTransaction).filter(WalletTransaction.user_id == self.customer_id,
            WalletTransaction.transaction_type == "REFUND").order_by(WalletTransaction.created_at.desc()).limit(5).all()
        cards = [AICard(title="Recorded wallet refund", kind="refund", href="/customer/profile/transactions", fields={
            "Transaction ID": str(row.id), "Amount": money(row.amount), "Destination": "DineBook wallet", "Recorded": stamp(row.created_at),
        }) for row in rows]
        if not cards:
            for row in self.db.query(FoodOrder).filter(FoodOrder.customer_id == self.customer_id, FoodOrder.payment_status == "REFUNDED").order_by(FoodOrder.updated_at.desc()).limit(5).all():
                cards.append(AICard(title="Recorded order refund status", kind="refund", href="/customer/profile/orders", fields={
                    "Order ID": str(row.id), "Status": row.payment_status, "Order total": money(row.total), "Destination": "Check transaction details",
                }))
            for row in (self.db.query(Reservation).filter(Reservation.user_id == self.customer_id,
                or_(Reservation.payment_status == "REFUNDED", Reservation.preorder_payment_status == "REFUNDED"))
                .order_by(Reservation.updated_at.desc()).limit(5).all()):
                cards.append(AICard(title="Recorded reservation refund status", kind="refund", href=f"/customer/profile/reservations/{row.id}", fields={
                    "Booking ID": str(row.id), "Fee payment": row.payment_status, "Reservation fee": money(row.fee_amount),
                    "Preorder payment": row.preorder_payment_status or "Not recorded", "Preorder total": money(row.preorder_total),
                }))
        return cards[:5]

    def restaurants(self, plan):
        query = self.db.query(Restaurant).filter(Restaurant.status == RestaurantStatus.APPROVED)
        if plan.action == "restaurant" and plan.restaurant_id:
            query = query.filter(Restaurant.id == plan.restaurant_id)
        if plan.query.strip():
            term = "%" + plan.query.strip().replace("%", "\\%").replace("_", "\\_") + "%"
            query = query.filter(or_(Restaurant.name.ilike(term, escape="\\"), Restaurant.city.ilike(term, escape="\\"), Restaurant.cuisine_type.ilike(term, escape="\\")))
        rows = query.order_by(Restaurant.name, Restaurant.id).limit(5).all()
        if plan.action == "restaurant" and plan.restaurant_id and not rows:
            raise HTTPException(404, "Restaurant not found.")
        return [AICard(title=row.name, kind="restaurant", restaurant_id=str(row.id), href=f"/customer/restaurants/{row.id}", fields={
            "Restaurant ID": str(row.id), "Cuisine": row.cuisine_type or "Not specified", "City": row.city,
            "Address": row.address, "Phone": row.phone or "Not provided", "Email": row.email or "Not provided",
        }) for row in rows]

    def hours(self, plan):
        row = self.restaurant(plan.restaurant_id)
        day = plan.day or datetime.now(LOCAL).date()
        periods, exception = effective_periods(self.db, row.id, day)
        return [AICard(title=row.name, kind="hours", restaurant_id=str(row.id), href=f"/customer/restaurants/{row.id}", fields={
            "Date": day.isoformat(), "Timezone": "Asia/Kolkata", "Hours": "; ".join(f"{p['opens']}–{p['closes']}" for p in serialize_periods(periods)) or "Closed / no configured hours",
            "Date exception": "Yes" if exception else "No",
        })]

    def availability(self, plan):
        row = self.restaurant(plan.restaurant_id)
        if not plan.day or not plan.start:
            return []
        today = datetime.now(LOCAL).date()
        if not today < plan.day <= today + timedelta(days=90):
            raise HTTPException(422, "Choose a reservation date from tomorrow through the next 90 days.")
        try:
            ReservationCreate.local_booking_slot(plan.start)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        end = (datetime.combine(plan.day, plan.start) + timedelta(minutes=plan.duration_minutes)).time()
        within_hours = within_operating_hours(self.db, row.id, plan.day, plan.start, end)
        groups = suitable_table_groups(self.db, row.id, plan.day, plan.start, plan.guests,
            duration_minutes=plan.duration_minutes) if within_hours else []
        return [AICard(title=row.name, kind="availability", restaurant_id=str(row.id), href=f"/customer/restaurants/{row.id}/booking", fields={
            "Date": plan.day.isoformat(), "Time": plan.start.strftime("%H:%M"), "Timezone": "Asia/Kolkata",
            "Guests": str(plan.guests), "Duration (minutes)": str(plan.duration_minutes),
            "Availability": "Available now; complete booking to reserve" if groups else "Not available for this time and party size",
        })]

    def offers(self, plan):
        row = self.restaurant(plan.restaurant_id)
        now = datetime.now(timezone.utc)
        offers = self.db.query(FoodOrderOffer).filter(FoodOrderOffer.restaurant_id == row.id, FoodOrderOffer.is_active.is_(True),
            or_(FoodOrderOffer.starts_at.is_(None), FoodOrderOffer.starts_at <= now),
            or_(FoodOrderOffer.ends_at.is_(None), FoodOrderOffer.ends_at >= now),
            or_(FoodOrderOffer.max_uses.is_(None), FoodOrderOffer.uses < FoodOrderOffer.max_uses)).order_by(FoodOrderOffer.created_at.desc()).limit(5).all()
        cards = []
        for offer in offers:
            used = customer_offer_uses(self.db, offer.id, self.customer_id)
            cards.append(AICard(title=offer.title, kind="offer", restaurant_id=str(row.id), href=f"/customer/restaurants/{row.id}", fields={
                "Code": offer.code, "Discount": money(offer.discount_percent) if offer.discount_type == "FIXED" else f"{offer.discount_percent}%",
                "Minimum subtotal": money(offer.minimum_subtotal), "Applies to": offer.applicability,
                "Your usage limit": "Reached" if offer.per_customer_limit is not None and used >= offer.per_customer_limit else "Check eligible items at checkout",
                "Eligibility": "Checkout preview confirms the discount for your cart",
            }))
        return cards

    def execute(self, plan: AIPlan):
        allowed = {"reservations": self.reservations, "orders": self.orders, "wallet": self.wallet,
            "payments": self.payments, "refunds": self.refunds, "restaurants": self.restaurants,
            "restaurant": self.restaurants, "hours": self.hours, "availability": self.availability, "offers": self.offers}
        if plan.action not in allowed:
            raise HTTPException(422, "Unsupported customer lookup.")
        return allowed[plan.action](plan)
