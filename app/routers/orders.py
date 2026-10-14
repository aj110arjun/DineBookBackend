import uuid
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy.orm import Session
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError

from app.db.database import get_db
from app.models.dining import DiningTable
from app.models.menu import Category, Food, FoodVariant
from app.models.orders import FoodOrder, FoodOrderOffer, OfferRedemption
from app.models.reservation import Reservation, ReservationTable
from app.models.restaurant import Restaurant, RestaurantStatus
from app.models.user import User
from app.models.wallet import WalletAccount, WalletTransaction
from app.models.realtime import RealtimeEvent
from app.services.notifications import notify_order_status
from app.services.restaurant_wallet import record_restaurant_payment, reverse_restaurant_payment
from app.routers.admin_auth import current_admin
from app.routers.chef_auth import current_chef
from app.routers.customer_auth import current_customer
from app.routers.manager_auth import current_manager

customer_router = APIRouter(prefix="/api/customer/orders", tags=["customer orders"])
customer_reservation_orders_router = APIRouter(prefix="/api/customer/reservations", tags=["reservation orders"])
manager_router = APIRouter(prefix="/api/manager/orders", tags=["manager orders"])
manager_reservation_orders_router = APIRouter(prefix="/api/manager/reservations", tags=["manager reservation orders"])
chef_router = APIRouter(prefix="/api/chef/orders", tags=["chef orders"])
chef_reservation_orders_router = APIRouter(prefix="/api/chef/reservations", tags=["chef reservation orders"])
admin_router = APIRouter(prefix="/api/admin/orders", tags=["admin order support"])
PREORDER_ORDER_NAMESPACE = uuid.UUID("89dd0385-bbad-4aeb-b600-bf4377641e10")
CENT = Decimal("0.01")
MANAGER_TRANSITIONS = {
    "PLACED": {"ACCEPTED", "REJECTED", "CANCELLED"},
    "ACCEPTED": {"CANCELLED"},
    "PREPARING": {"CANCELLED"},
    "READY": {"COMPLETED"},
}


def record_order_event(db: Session, order: FoodOrder, previous_status: str | None = None,
                       actor: User | None = None, reason: str | None = None) -> None:
    data = {"status": order.status, "payment_status": order.payment_status,
            "actor_id": str(actor.id) if actor else None,
            "occurred_at": datetime.now(timezone.utc).isoformat()}
    if previous_status is not None:
        data.update({"previous_status": previous_status, "reason": reason})
    db.add(RealtimeEvent(
        user_id=order.customer_id,
        restaurant_id=order.restaurant_id,
        event_type="order.status_changed",
        resource_id=str(order.id),
        data=data,
    ))
    notify_order_status(db, order, previous_status)


def reverse_offer_redemption(db: Session, order: FoodOrder) -> None:
    redemption = db.query(OfferRedemption).filter(OfferRedemption.order_id == order.id, OfferRedemption.status == "APPLIED").with_for_update().first()
    if redemption is None:
        return
    offer = db.query(FoodOrderOffer).filter(FoodOrderOffer.id == redemption.offer_id).with_for_update().first()
    if offer is not None:
        offer.uses = max(0, offer.uses - 1)
    redemption.status = "REVERSED"
CHEF_TRANSITIONS = {"ACCEPTED": {"PREPARING"}, "PREPARING": {"READY"}}


def utc_instant(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.utcoffset() is None else value.astimezone(timezone.utc)


def calculate_offer_discount(offer: FoodOrderOffer, eligible_subtotal: Decimal) -> Decimal:
    """Calculate one offer against the deduplicated eligible merchandise subtotal."""
    base = Decimal(eligible_subtotal).quantize(CENT, rounding=ROUND_HALF_UP)
    raw = base * Decimal(offer.discount_percent) / Decimal("100") if offer.discount_type == "PERCENTAGE" else Decimal(offer.discount_percent)
    if offer.max_discount is not None:
        raw = min(raw, Decimal(offer.max_discount))
    return min(raw, base).quantize(CENT, rounding=ROUND_HALF_UP)


class OrderLineInput(BaseModel):
    variant_id: uuid.UUID
    quantity: int = Field(ge=1, le=99)


class CheckoutInput(BaseModel):
    idempotency_key: uuid.UUID
    reservation_id: uuid.UUID | None = None
    restaurant_id: uuid.UUID | None = None
    items: list[OrderLineInput] = Field(min_length=1, max_length=50)
    offer_code: str | None = Field(default=None, max_length=40)
    payment_method: str = Field(default="PAY_AT_DESK", pattern="^(PAY_AT_DESK|WALLET)$")
    customer_note: str | None = Field(default=None, max_length=500)


class OfferValidationInput(BaseModel):
    reservation_id: uuid.UUID
    items: list[OrderLineInput] = Field(min_length=1, max_length=50)
    offer_code: str = Field(min_length=3, max_length=40)


class OfferPreviewInput(BaseModel):
    restaurant_id: uuid.UUID
    items: list[OrderLineInput] = Field(min_length=1, max_length=50)
    offer_code: str = Field(min_length=3, max_length=40)


class OfferPreviewInput(BaseModel):
    restaurant_id: uuid.UUID
    items: list[OrderLineInput] = Field(min_length=1, max_length=50)
    offer_code: str = Field(min_length=3, max_length=40)


class SpotOrderInput(BaseModel):
    idempotency_key: uuid.UUID
    table_id: uuid.UUID
    guest_name: str = Field(min_length=1, max_length=120)
    items: list[OrderLineInput] = Field(min_length=1, max_length=50)
    customer_note: str | None = Field(default=None, max_length=500)

    @field_validator("guest_name")
    @classmethod
    def clean_guest_name(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("Guest name is required.")
        return value


class StatusInput(BaseModel):
    status: str = Field(pattern="^(ACCEPTED|PREPARING|READY|COMPLETED|CANCELLED|REJECTED)$")
    reason: str | None = Field(default=None, max_length=500)

    @field_validator("reason")
    @classmethod
    def clean_reason(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None

    @model_validator(mode="after")
    def require_reason_for_terminal_action(self):
        if self.status in {"CANCELLED", "REJECTED"} and not self.reason:
            raise ValueError("A reason is required to cancel or reject an order.")
        return self


class CustomerOrderCancellation(BaseModel):
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("reason")
    @classmethod
    def clean_reason(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("A reason is required to cancel an order.")
        return cleaned


class OfferInput(BaseModel):
    code: str = Field(min_length=3, max_length=40, pattern=r"^[A-Za-z0-9_-]+$")
    discount_percent: Decimal = Field(gt=0, le=1000000)
    minimum_subtotal: Decimal = Field(default=Decimal("0.00"), ge=0)
    max_uses: int | None = Field(default=None, gt=0)
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    title: str = Field(default="Offer", min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=1000)
    discount_type: str = Field(default="PERCENTAGE", pattern="^(PERCENTAGE|FIXED)$")
    max_discount: Decimal | None = Field(default=None, gt=0)
    per_customer_limit: int | None = Field(default=None, gt=0)
    applicability: str = Field(default="RESTAURANT", pattern="^(RESTAURANT|ITEMS|CATEGORIES)$")
    eligible_food_ids: list[uuid.UUID] = Field(default_factory=list, max_length=100)
    eligible_category_ids: list[uuid.UUID] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def validate_rules(self):
        if self.applicability == "ITEMS" and not self.eligible_food_ids:
            raise ValueError("Select at least one eligible item.")
        if self.applicability == "CATEGORIES" and not self.eligible_category_ids:
            raise ValueError("Select at least one eligible category.")
        if self.applicability == "RESTAURANT" and (self.eligible_food_ids or self.eligible_category_ids):
            raise ValueError("Restaurant-wide offers cannot have item or category selections.")
        if self.discount_type == "PERCENTAGE" and self.discount_percent > 100:
            raise ValueError("Percentage discounts cannot exceed 100%.")
        return self

    @field_validator("starts_at", "ends_at")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() is None:
            raise ValueError("Offer timestamps must include a timezone; submit UTC timestamps.")
        return value


def staff_restaurant(staff: User, db: Session) -> Restaurant:
    owner_id = staff.manager_id or staff.id
    restaurant = db.query(Restaurant).filter(Restaurant.manager_id == owner_id).first()
    if restaurant is None:
        raise HTTPException(status_code=404, detail="No restaurant is linked to this account.")
    return restaurant


def serialize(order: FoodOrder, db: Session) -> dict:
    restaurant = db.query(Restaurant).filter(Restaurant.id == order.restaurant_id).first()
    customer = db.query(User).filter(User.id == order.customer_id).first() if order.customer_id else None
    reservation = db.query(Reservation).filter(Reservation.id == order.reservation_id).first() if order.reservation_id else None
    assigned_tables = []
    if reservation:
        assigned_tables = db.query(DiningTable).join(ReservationTable, ReservationTable.table_id == DiningTable.id).filter(
            ReservationTable.reservation_id == reservation.id
        ).order_by(DiningTable.table_number).all()
    elif order.table_id:
        table = db.query(DiningTable).filter(DiningTable.id == order.table_id).first()
        assigned_tables = [table] if table else []
    is_preorder = order.idempotency_key == uuid.uuid5(PREORDER_ORDER_NAMESPACE, str(order.reservation_id)) if order.reservation_id else False
    return {
        "id": str(order.id), "reservation_id": str(order.reservation_id) if order.reservation_id else None,
        "is_preorder": is_preorder,
        "order_source": "PREORDER" if is_preorder else ("RESERVATION" if order.reservation_id else "SPOT" if order.table_id else "WALK_IN"),
        "reservation": ({"id": str(reservation.id), "status": reservation.status,
            "reservation_date": reservation.reservation_date.isoformat(),
            "start_time": reservation.start_time.strftime("%H:%M"),
            "number_of_guests": reservation.number_of_guests,
            "special_request": reservation.special_request,
            "tables": [{"id": str(table.id), "table_number": table.table_number,
                        "floor_name": table.floor.floor_name if table.floor else None}
                       for table in assigned_tables]}
            if reservation else None),
        "tables": [{"id": str(table.id), "table_number": table.table_number,
                    "floor_name": table.floor.floor_name if table.floor else None} for table in assigned_tables],
        "restaurant_id": str(order.restaurant_id),
        "table_id": str(order.table_id) if order.table_id else None,
        "restaurant_name": restaurant.name if restaurant else "Restaurant",
        "restaurant_location": ", ".join(part for part in (restaurant.city, restaurant.state) if part) if restaurant else None,
        "restaurant_address": restaurant.address if restaurant else None,
        "restaurant_email": restaurant.email if restaurant else None,
        "restaurant_phone": restaurant.phone if restaurant else None,
        "customer_id": str(order.customer_id) if order.customer_id else None, "customer_name": customer.name if customer else (order.guest_name or "Customer"),
        "customer_email": customer.email if customer else None,
        "items": order.items, "subtotal": float(order.subtotal),
        "discount_amount": float(order.discount_amount), "offer_code": order.offer_code,
        "total": float(order.total), "status": order.status,
        "payment_method": order.payment_method, "payment_status": order.payment_status,
        "customer_note": order.customer_note, "created_at": order.created_at.isoformat(),
        "accepted_at": order.accepted_at.isoformat() if order.accepted_at else None,
        "preparation_started_at": order.preparation_started_at.isoformat() if order.preparation_started_at else None,
        "ready_at": order.ready_at.isoformat() if order.ready_at else None,
        "completed_at": order.completed_at.isoformat() if order.completed_at else None,
        "status_reason": order.status_reason,
        "updated_at": order.updated_at.isoformat(),
    }


def create_reserved_order(reservation_id: uuid.UUID, payload: CheckoutInput, customer: User, db: Session,
                          actor: User | None = None) -> dict:
    try:
        reservation = db.query(Reservation).filter(
            Reservation.id == reservation_id, Reservation.user_id == customer.id
        ).with_for_update().first()
        if reservation is None:
            raise HTTPException(status_code=404, detail="Reservation not found.")
        if reservation.status != "SEATED":
            raise HTTPException(status_code=409, detail="Orders can only be created while the reservation is seated.")
        if payload.restaurant_id and payload.restaurant_id != reservation.restaurant_id:
            raise HTTPException(status_code=400, detail="The order restaurant must match the reservation restaurant.")

        previous = db.query(FoodOrder).filter(FoodOrder.customer_id == customer.id,
            FoodOrder.idempotency_key == payload.idempotency_key).first()
        if previous is not None:
            if previous.reservation_id != reservation.id:
                raise HTTPException(status_code=409, detail="This checkout key was already used for another reservation.")
            return serialize(previous, db)

        restaurant = db.query(Restaurant).filter(
            Restaurant.id == reservation.restaurant_id, Restaurant.status == RestaurantStatus.APPROVED
        ).first()
        if restaurant is None:
            raise HTTPException(status_code=404, detail="Restaurant is not available for orders.")
        quantities: dict[uuid.UUID, int] = {}
        for line in payload.items:
            quantities[line.variant_id] = quantities.get(line.variant_id, 0) + line.quantity
        if len(quantities) != len(payload.items):
            raise HTTPException(status_code=422, detail="Each menu option can only appear once in your cart.")
        variants = db.query(FoodVariant).join(Food, FoodVariant.food_id == Food.id).join(Category, Food.category_id == Category.id).filter(
            FoodVariant.id.in_(quantities), Food.restaurant_id == reservation.restaurant_id,
            Food.is_available.is_(True), Food.deleted_at.is_(None), FoodVariant.is_available.is_(True), FoodVariant.deleted_at.is_(None),
            Category.is_active.is_(True), Category.deleted_at.is_(None),
        ).with_for_update().all()
        by_id = {variant.id: variant for variant in variants}
        if set(by_id) != set(quantities):
            raise HTTPException(status_code=409, detail="One or more menu items are unavailable at this restaurant.")

        snapshots = []
        subtotal = Decimal("0.00")
        for variant_id, quantity in quantities.items():
            variant = by_id[variant_id]
            price = Decimal(variant.price).quantize(CENT, rounding=ROUND_HALF_UP)
            line_total = (price * quantity).quantize(CENT, rounding=ROUND_HALF_UP)
            subtotal += line_total
            snapshots.append({"variant_id": str(variant.id), "food_name": variant.food.name,
                "description": variant.food.description,
                "image_url": variant.food.image.image_url if variant.food.image else None,
                "variant_name": variant.name, "quantity": quantity, "unit_price": float(price),
                "line_total": float(line_total)})
        subtotal = subtotal.quantize(CENT, rounding=ROUND_HALF_UP)
        offer = None
        discount = Decimal("0.00")
        code = payload.offer_code.strip().upper() if payload.offer_code else None
        if code:
            offer = db.query(FoodOrderOffer).filter(
                func.upper(FoodOrderOffer.code) == code, FoodOrderOffer.restaurant_id == reservation.restaurant_id,
                FoodOrderOffer.is_active.is_(True),
            ).with_for_update().first()
            now = datetime.now(timezone.utc)
            eligible_ids = set()
            for variant_id, quantity in quantities.items():
                variant = by_id[variant_id]
                eligible = (offer is not None and (offer.applicability == "RESTAURANT"
                    or (offer.applicability == "ITEMS" and str(variant.food.id) in (offer.eligible_food_ids or []))
                    or (offer.applicability == "CATEGORIES" and str(variant.food.category_id) in (offer.eligible_category_ids or []))))
                if eligible:
                    eligible_ids.add(variant_id)
            eligible_subtotal = sum((Decimal(by_id[vid].price) * quantities[vid] for vid in eligible_ids), Decimal("0.00")).quantize(CENT, rounding=ROUND_HALF_UP)
            customer_uses = (db.query(OfferRedemption.id).filter(OfferRedemption.offer_id == offer.id,
                OfferRedemption.customer_id == customer.id, OfferRedemption.status == "APPLIED").count() if offer else 0)
            if (offer is None or (offer.starts_at and utc_instant(offer.starts_at) > now)
                    or (offer.ends_at and utc_instant(offer.ends_at) < now)
                    or (offer.max_uses is not None and offer.uses >= offer.max_uses)
                    or (offer.per_customer_limit is not None and customer_uses >= offer.per_customer_limit)
                    or eligible_subtotal < Decimal(offer.minimum_subtotal)
                    or (offer.applicability != "RESTAURANT" and not eligible_ids)):
                raise HTTPException(status_code=422, detail="This offer is invalid, expired, exhausted, or does not match your eligible cart subtotal.")
            discount = calculate_offer_discount(offer, eligible_subtotal)
            offer.uses += 1
        total = max(Decimal("0.00"), subtotal - discount).quantize(CENT, rounding=ROUND_HALF_UP)
        payment_status = "PENDING"
        if payload.payment_method == "WALLET":
            account = db.query(WalletAccount).filter(WalletAccount.user_id == customer.id).with_for_update().first()
            if account is None or Decimal(account.balance) < total:
                raise HTTPException(status_code=409, detail="Your wallet balance is too low for this order.")
            account.balance = Decimal(account.balance) - total
            payment_status = "PAID"
        order = FoodOrder(customer_id=customer.id, reservation_id=reservation.id,
            restaurant_id=reservation.restaurant_id, idempotency_key=payload.idempotency_key,
            offer_id=offer.id if offer else None, offer_code=code, items=snapshots,
            subtotal=subtotal, discount_amount=discount, total=total, status="PLACED",
            payment_method=payload.payment_method, payment_status=payment_status,
            paid_at=datetime.now(timezone.utc) if payment_status == "PAID" else None,
            customer_note=payload.customer_note)
        db.add(order)
        db.flush()
        if offer is not None:
            db.add(OfferRedemption(offer_id=offer.id, order_id=order.id, customer_id=customer.id,
                discount_amount=discount, status="APPLIED"))
        record_order_event(db, order, actor=actor or customer)
        if payload.payment_method == "WALLET":
            db.add(WalletTransaction(user_id=customer.id, amount=-total, transaction_type="DEBIT",
                description=f"Food order at {restaurant.name}", reference=f"food-order:{order.id}"))
            record_restaurant_payment(db, restaurant.id, total,
                f"Food order ORD-{order.id.hex[:8].upper()} · DineBook wallet", f"customer-payment:order:{order.id}")
        db.commit()
        db.refresh(order)
        return serialize(order, db)
    except HTTPException:
        db.rollback()
        raise
    except IntegrityError:
        db.rollback()
        previous = db.query(FoodOrder).filter(FoodOrder.customer_id == customer.id,
            FoodOrder.idempotency_key == payload.idempotency_key).first()
        if previous is not None and previous.reservation_id == reservation_id:
            return serialize(previous, db)
        raise HTTPException(status_code=409, detail="Unable to place the order safely. Please retry checkout.")
    except Exception:
        db.rollback()
        raise


@customer_router.post("")
def checkout(payload: CheckoutInput, customer: User = Depends(current_customer), db: Session = Depends(get_db)) -> dict:
    if payload.reservation_id is None:
        raise HTTPException(status_code=422, detail="A seated reservation is required to place an order.")
    return create_reserved_order(payload.reservation_id, payload, customer, db)


@customer_reservation_orders_router.post("/{reservation_id}/orders")
def create_reservation_order(reservation_id: uuid.UUID, payload: CheckoutInput,
                             customer: User = Depends(current_customer), db: Session = Depends(get_db)) -> dict:
    return create_reserved_order(reservation_id, payload, customer, db)


def reservation_orders_for_customer(reservation_id: uuid.UUID, customer: User, db: Session) -> list[dict]:
    reservation = db.query(Reservation).filter(
        Reservation.id == reservation_id, Reservation.user_id == customer.id
    ).first()
    if reservation is None:
        raise HTTPException(status_code=404, detail="Reservation not found.")
    rows = db.query(FoodOrder).filter(FoodOrder.reservation_id == reservation.id).order_by(FoodOrder.created_at.asc()).all()
    return [serialize(row, db) for row in rows]


@customer_reservation_orders_router.get("/{reservation_id}/orders")
def customer_reservation_orders(reservation_id: uuid.UUID, customer: User = Depends(current_customer), db: Session = Depends(get_db)) -> list[dict]:
    return reservation_orders_for_customer(reservation_id, customer, db)


@manager_reservation_orders_router.get("/{reservation_id}/orders")
def manager_reservation_orders(reservation_id: uuid.UUID, manager: User = Depends(current_manager), db: Session = Depends(get_db)) -> list[dict]:
    restaurant = staff_restaurant(manager, db)
    reservation = db.query(Reservation).filter(Reservation.id == reservation_id, Reservation.restaurant_id == restaurant.id).first()
    if reservation is None:
        raise HTTPException(status_code=404, detail="Reservation not found for this restaurant.")
    rows = db.query(FoodOrder).filter(FoodOrder.reservation_id == reservation.id).order_by(FoodOrder.created_at.asc()).all()
    return [serialize(row, db) for row in rows]


@manager_reservation_orders_router.post("/{reservation_id}/orders")
def manager_create_reservation_order(reservation_id: uuid.UUID, payload: CheckoutInput,
                                     manager: User = Depends(current_manager), db: Session = Depends(get_db)) -> dict:
    restaurant = staff_restaurant(manager, db)
    reservation = db.query(Reservation).filter(
        Reservation.id == reservation_id,
        Reservation.restaurant_id == restaurant.id,
    ).first()
    if reservation is None:
        raise HTTPException(status_code=404, detail="Reservation not found for this restaurant.")
    if reservation.status != "SEATED":
        raise HTTPException(status_code=409, detail="Desk orders can only be added while the reservation is seated.")
    customer = db.query(User).filter(User.id == reservation.user_id).first()
    if customer is None:
        raise HTTPException(status_code=409, detail="The reservation customer is unavailable.")

    requested_items = {str(line.variant_id): line.quantity for line in payload.items}
    if len(requested_items) != len(payload.items):
        raise HTTPException(status_code=422, detail="Each menu option can only appear once in an order.")
    # Desk orders are paid at the restaurant. Ignore a client-supplied wallet method
    # so this manager action can never debit the customer's wallet.
    desk_payload = payload.model_copy(update={
        "reservation_id": reservation.id,
        "restaurant_id": restaurant.id,
        "payment_method": "PAY_AT_DESK",
    })
    return create_reserved_order(reservation.id, desk_payload, customer, db, actor=manager)


@manager_reservation_orders_router.post("/{reservation_id}/preorder-order")
def manager_queue_reservation_preorder(reservation_id: uuid.UUID,
                                       manager: User = Depends(current_manager), db: Session = Depends(get_db)) -> dict:
    restaurant = staff_restaurant(manager, db)
    try:
        reservation = db.query(Reservation).filter(
            Reservation.id == reservation_id,
            Reservation.restaurant_id == restaurant.id,
        ).with_for_update().first()
        if reservation is None:
            raise HTTPException(status_code=404, detail="Reservation not found for this restaurant.")
        if reservation.status != "SEATED":
            raise HTTPException(status_code=409, detail="A preorder can be sent to the kitchen only while the reservation is seated.")
        if reservation.fulfillment_type != "PREORDER" or not reservation.preorder_items:
            raise HTTPException(status_code=409, detail="This reservation does not have a preorder.")

        customer = db.query(User).filter(User.id == reservation.user_id).first()
        if customer is None:
            raise HTTPException(status_code=409, detail="The reservation customer is unavailable.")
        idempotency_key = uuid.uuid5(PREORDER_ORDER_NAMESPACE, str(reservation.id))
        existing = db.query(FoodOrder).filter(
            FoodOrder.customer_id == customer.id,
            FoodOrder.idempotency_key == idempotency_key,
        ).with_for_update().first()
        if existing is not None:
            if existing.reservation_id != reservation.id:
                raise HTTPException(status_code=409, detail="The preorder order key is already associated with another reservation.")
            db.commit()
            return serialize(existing, db)

        # The reservation snapshot is authoritative: it was priced and, when
        # selected, paid during reservation checkout. Do not reprice or charge it.
        snapshots = []
        subtotal = Decimal("0.00")
        for entry in reservation.preorder_items:
            try:
                quantity = int(entry["quantity"])
                unit_price = Decimal(str(entry["unit_price"])).quantize(CENT, rounding=ROUND_HALF_UP)
                line_total = Decimal(str(entry["line_total"])).quantize(CENT, rounding=ROUND_HALF_UP)
                variant_id = uuid.UUID(str(entry["variant_id"]))
                food_name = str(entry["food_name"])
                variant_name = str(entry["variant_name"])
            except (KeyError, TypeError, ValueError, ArithmeticError):
                raise HTTPException(status_code=409, detail="The saved preorder details are invalid and cannot be sent to the kitchen.")
            expected_total = (unit_price * quantity).quantize(CENT, rounding=ROUND_HALF_UP)
            if quantity < 1 or quantity > 99 or line_total != expected_total:
                raise HTTPException(status_code=409, detail="The saved preorder totals are inconsistent.")
            subtotal += line_total
            snapshots.append({"variant_id": str(variant_id), "food_name": food_name,
                "description": entry.get("description"),
                "image_url": entry.get("image_url"),
                "variant_name": variant_name, "quantity": quantity,
                "unit_price": float(unit_price), "line_total": float(line_total)})
        subtotal = subtotal.quantize(CENT, rounding=ROUND_HALF_UP)
        preorder_discount = Decimal(reservation.preorder_discount or 0).quantize(CENT, rounding=ROUND_HALF_UP)
        if subtotal != (Decimal(reservation.preorder_total).quantize(CENT, rounding=ROUND_HALF_UP) + preorder_discount):
            raise HTTPException(status_code=409, detail="The saved preorder total is inconsistent.")

        payment_method = reservation.preorder_payment_method
        payment_status = reservation.preorder_payment_status
        if payment_status is None:  # legacy reservation records
            legacy_paid = reservation.payment_method == "WALLET" and reservation.payment_status == "PAID"
            payment_method = "WALLET" if legacy_paid else "PAY_AT_DESK"
            payment_status = "PAID" if legacy_paid else "PENDING"
        if payment_status == "REFUNDED":
            raise HTTPException(status_code=409, detail="The preorder was refunded and cannot be sent to the kitchen.")
        if payment_status != "PAID":
            payment_method = "PAY_AT_DESK"

        preorder_offer = db.query(FoodOrderOffer).filter(FoodOrderOffer.id == reservation.preorder_offer_id).first() if reservation.preorder_offer_id else None
        order = FoodOrder(
            customer_id=customer.id,
            reservation_id=reservation.id,
            restaurant_id=restaurant.id,
            idempotency_key=idempotency_key,
            items=snapshots,
            subtotal=subtotal,
            offer_id=preorder_offer.id if preorder_offer else None,
            offer_code=reservation.preorder_offer_code,
            discount_amount=preorder_discount,
            total=(subtotal - preorder_discount).quantize(CENT, rounding=ROUND_HALF_UP),
            status="PLACED",
            payment_method=payment_method or "PAY_AT_DESK",
            payment_status=payment_status,
            paid_at=datetime.now(timezone.utc) if payment_status == "PAID" else None,
            customer_note="Reservation preorder",
        )
        db.add(order)
        db.flush()
        if preorder_offer is not None and preorder_discount > 0:
            db.add(OfferRedemption(offer_id=preorder_offer.id, order_id=order.id, customer_id=customer.id,
                discount_amount=preorder_discount, status="APPLIED"))
        record_order_event(db, order, actor=manager)
        db.commit()
        db.refresh(order)
        return serialize(order, db)
    except HTTPException:
        db.rollback()
        raise
    except IntegrityError:
        db.rollback()
        existing = db.query(FoodOrder).filter(
            FoodOrder.idempotency_key == uuid.uuid5(PREORDER_ORDER_NAMESPACE, str(reservation_id)),
        ).first()
        if existing is not None and existing.reservation_id == reservation_id:
            return serialize(existing, db)
        raise HTTPException(status_code=409, detail="Unable to queue this preorder safely. Please retry.")
    except Exception:
        db.rollback()
        raise


@chef_reservation_orders_router.get("/{reservation_id}/orders")
def chef_reservation_orders(reservation_id: uuid.UUID, chef: User = Depends(current_chef), db: Session = Depends(get_db)) -> list[dict]:
    restaurant = staff_restaurant(chef, db)
    reservation = db.query(Reservation).filter(Reservation.id == reservation_id, Reservation.restaurant_id == restaurant.id).first()
    if reservation is None:
        raise HTTPException(status_code=404, detail="Reservation not found for this restaurant.")
    rows = db.query(FoodOrder).filter(FoodOrder.reservation_id == reservation.id).order_by(FoodOrder.created_at.asc()).all()
    return [serialize(row, db) for row in rows]
    
@customer_router.get("")
def customer_orders(customer: User = Depends(current_customer), db: Session = Depends(get_db)) -> list[dict]:
    rows = db.query(FoodOrder).filter(FoodOrder.customer_id == customer.id).order_by(FoodOrder.created_at.desc()).all()
    return [serialize(row, db) for row in rows]


@customer_router.post("/{order_id}/cancel")
def cancel_customer_order(order_id: uuid.UUID, payload: CustomerOrderCancellation,
                          customer: User = Depends(current_customer), db: Session = Depends(get_db)) -> dict:
    row = db.query(FoodOrder).filter(FoodOrder.id == order_id, FoodOrder.customer_id == customer.id).with_for_update().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Order not found.")
    if row.status != "PLACED":
        raise HTTPException(status_code=409, detail="Only newly placed orders can be cancelled by the customer.")
    if row.payment_status == "PAID":
        account = db.query(WalletAccount).filter(WalletAccount.user_id == customer.id).with_for_update().first()
        if account is None:
            raise HTTPException(status_code=409, detail="Wallet refund could not be completed. Contact support.")
        account.balance = Decimal(account.balance) + Decimal(row.total)
        db.add(WalletTransaction(user_id=customer.id, amount=Decimal(row.total), transaction_type="REFUND",
            description="Wallet refund for cancelled food order", reference=f"food-order:{row.id}"))
        if row.payment_method == "WALLET":
            reverse_restaurant_payment(db, row.restaurant_id, Decimal(row.total),
                f"Customer wallet refund · ORD-{row.id.hex[:8].upper()}", f"customer-refund:order:{row.id}")
        row.payment_status = "REFUNDED"
    previous_status = row.status
    reverse_offer_redemption(db, row)
    row.status = "CANCELLED"
    row.status_reason = payload.reason
    record_order_event(db, row, previous_status, customer, payload.reason)
    db.commit()
    return serialize(row, db)


def list_for_staff(staff: User, db: Session) -> list[dict]:
    restaurant = staff_restaurant(staff, db)
    rows = db.query(FoodOrder).filter(FoodOrder.restaurant_id == restaurant.id).order_by(FoodOrder.created_at.desc()).all()
    return [serialize(row, db) for row in rows]


def update_status(order_id: uuid.UUID, next_status: str, allowed: dict, staff: User, db: Session,
                  reason: str | None = None) -> dict:
    restaurant = staff_restaurant(staff, db)
    row = db.query(FoodOrder).filter(FoodOrder.id == order_id, FoodOrder.restaurant_id == restaurant.id).with_for_update().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Order not found for this restaurant.")
    if next_status not in allowed.get(row.status, set()):
        raise HTTPException(status_code=409, detail=f"Order cannot move from {row.status} to {next_status}.")
    previous_status = row.status
    if next_status in {"CANCELLED", "REJECTED"}:
        reverse_offer_redemption(db, row)
    if next_status in {"CANCELLED", "REJECTED"} and row.payment_status == "PAID":
        is_preorder = row.reservation_id is not None and row.idempotency_key == uuid.uuid5(PREORDER_ORDER_NAMESPACE, str(row.reservation_id))
        if is_preorder:
            reservation = db.query(Reservation).filter(Reservation.id == row.reservation_id).with_for_update().first()
            if row.payment_method == "WALLET":
                account = db.query(WalletAccount).filter(WalletAccount.user_id == row.customer_id).with_for_update().first()
                if account is None:
                    raise HTTPException(status_code=409, detail="Wallet refund could not be completed. Contact support.")
                account.balance = Decimal(account.balance) + Decimal(row.total)
                db.add(WalletTransaction(user_id=row.customer_id, reservation_id=row.reservation_id,
                    amount=Decimal(row.total), transaction_type="REFUND",
                    description="Wallet refund for cancelled reservation preorder", reference=f"food-order:{row.id}"))
                reverse_restaurant_payment(db, row.restaurant_id, Decimal(row.total),
                    f"Customer wallet refund · preorder ORD-{row.id.hex[:8].upper()}", f"customer-refund:order:{row.id}")
            if reservation is not None:
                reservation.preorder_payment_status = "REFUNDED"
            row.payment_status = "REFUNDED"
        else:
            account = db.query(WalletAccount).filter(WalletAccount.user_id == row.customer_id).with_for_update().first()
            if account is None:
                raise HTTPException(status_code=409, detail="Wallet refund could not be completed. Contact support.")
            account.balance = Decimal(account.balance) + Decimal(row.total)
            row.payment_status = "REFUNDED"
            db.add(WalletTransaction(user_id=row.customer_id, amount=Decimal(row.total), transaction_type="REFUND",
                description="Wallet refund for cancelled food order", reference=f"food-order:{row.id}"))
            if row.payment_method == "WALLET":
                reverse_restaurant_payment(db, row.restaurant_id, Decimal(row.total),
                    f"Customer wallet refund · ORD-{row.id.hex[:8].upper()}", f"customer-refund:order:{row.id}")
    row.status = next_status
    now = datetime.now(timezone.utc)
    if next_status == "ACCEPTED":
        row.accepted_at = now
    elif next_status == "PREPARING":
        row.preparation_started_at = now
    elif next_status == "READY":
        row.ready_at = now
    elif next_status == "COMPLETED":
        row.completed_at = now
    if next_status in {"CANCELLED", "REJECTED"}:
        row.status_reason = reason.strip() if reason and reason.strip() else None
    record_order_event(db, row, previous_status, staff, reason)
    db.commit()
    db.refresh(row)
    return serialize(row, db)


@manager_router.get("")
def manager_orders(manager: User = Depends(current_manager), db: Session = Depends(get_db)) -> list[dict]:
    return list_for_staff(manager, db)


@manager_router.post("/spot", status_code=201)
def manager_create_spot_order(payload: SpotOrderInput, manager: User = Depends(current_manager), db: Session = Depends(get_db)) -> dict:
    restaurant = staff_restaurant(manager, db)
    try:
        previous = db.query(FoodOrder).filter(FoodOrder.idempotency_key == payload.idempotency_key).first()
        if previous:
            if previous.restaurant_id != restaurant.id or previous.table_id != payload.table_id:
                raise HTTPException(status_code=409, detail="This checkout key was already used for another order.")
            return serialize(previous, db)
        table = db.query(DiningTable).filter(
            DiningTable.id == payload.table_id, DiningTable.restaurant_id == restaurant.id,
        ).with_for_update().first()
        if table is None:
            raise HTTPException(status_code=404, detail="Table not found for this restaurant.")
        if table.status != "available":
            raise HTTPException(status_code=409, detail="This table is not available for a spot order.")
        quantities: dict[uuid.UUID, int] = {}
        for line in payload.items:
            quantities[line.variant_id] = quantities.get(line.variant_id, 0) + line.quantity
        if len(quantities) != len(payload.items):
            raise HTTPException(status_code=422, detail="Each menu option can only appear once in an order.")
        variants = db.query(FoodVariant).join(Food, FoodVariant.food_id == Food.id).join(Category, Food.category_id == Category.id).filter(
            FoodVariant.id.in_(quantities), Food.restaurant_id == restaurant.id,
            Food.is_available.is_(True), Food.deleted_at.is_(None), FoodVariant.is_available.is_(True), FoodVariant.deleted_at.is_(None),
            Category.is_active.is_(True), Category.deleted_at.is_(None),
        ).with_for_update().all()
        by_id = {variant.id: variant for variant in variants}
        if set(by_id) != set(quantities):
            raise HTTPException(status_code=409, detail="One or more menu items are unavailable at this restaurant.")
        snapshots = []
        subtotal = Decimal("0.00")
        for variant_id, quantity in quantities.items():
            variant = by_id[variant_id]
            price = Decimal(variant.price).quantize(CENT, rounding=ROUND_HALF_UP)
            line_total = (price * quantity).quantize(CENT, rounding=ROUND_HALF_UP)
            subtotal += line_total
            snapshots.append({"variant_id": str(variant.id), "food_name": variant.food.name,
                "description": variant.food.description, "image_url": variant.food.image.image_url if variant.food.image else None,
                "variant_name": variant.name, "quantity": quantity, "unit_price": float(price), "line_total": float(line_total)})
        subtotal = subtotal.quantize(CENT, rounding=ROUND_HALF_UP)
        order = FoodOrder(customer_id=None, guest_name=payload.guest_name, table_id=table.id,
            restaurant_id=restaurant.id, idempotency_key=payload.idempotency_key, items=snapshots,
            subtotal=subtotal, discount_amount=Decimal("0.00"), total=subtotal, status="PLACED",
            payment_method="PAY_AT_DESK", payment_status="PENDING", customer_note=payload.customer_note)
        table.status = "occupied"
        db.add(order)
        db.flush()
        record_order_event(db, order, actor=manager)
        db.commit()
        db.refresh(order)
        return serialize(order, db)
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise


@manager_router.patch("/{order_id}/status")
def manager_update_order(order_id: uuid.UUID, payload: StatusInput, manager: User = Depends(current_manager), db: Session = Depends(get_db)) -> dict:
    return update_status(order_id, payload.status, MANAGER_TRANSITIONS, manager, db, payload.reason)


@manager_router.post("/{order_id}/mark-paid")
def manager_mark_order_paid(order_id: uuid.UUID, manager: User = Depends(current_manager), db: Session = Depends(get_db)) -> dict:
    restaurant = staff_restaurant(manager, db)
    row = db.query(FoodOrder).filter(FoodOrder.id == order_id, FoodOrder.restaurant_id == restaurant.id).with_for_update().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Order not found for this restaurant.")
    if row.payment_method != "PAY_AT_DESK" or row.status in {"CANCELLED", "REJECTED"}:
        raise HTTPException(status_code=409, detail="This order cannot be marked paid.")
    row.payment_status = "PAID"
    row.paid_at = datetime.now(timezone.utc)
    if row.table_id is not None:
        table = db.query(DiningTable).filter(DiningTable.id == row.table_id).with_for_update().first()
        if table is not None and table.status == "occupied":
            table.status = "available"
    if row.reservation_id is not None and row.idempotency_key == uuid.uuid5(PREORDER_ORDER_NAMESPACE, str(row.reservation_id)):
        reservation = db.query(Reservation).filter(Reservation.id == row.reservation_id).with_for_update().first()
        if reservation is not None:
            reservation.preorder_payment_method = "PAY_AT_DESK"
            reservation.preorder_payment_status = "PAID"
            reservation.preorder_paid_at = row.paid_at
    record_order_event(db, row, actor=manager)
    db.commit()
    db.refresh(row)
    return serialize(row, db)


@manager_router.get("/offers")
def manager_offers(search: str | None = None, status: str | None = None, discount_type: str | None = Query(None, pattern="^(PERCENTAGE|FIXED)$"), applicability: str | None = Query(None, pattern="^(RESTAURANT|ITEMS|CATEGORIES)$"), valid_from: datetime | None = None, valid_to: datetime | None = None, sort: str = Query("created_at", pattern="^(created_at|discount_value|starts_at|ends_at)$"), direction: str = Query("desc", pattern="^(asc|desc)$"), offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200), manager: User = Depends(current_manager), db: Session = Depends(get_db)) -> dict:
    if (valid_from and valid_from.utcoffset() is None) or (valid_to and valid_to.utcoffset() is None):
        raise HTTPException(status_code=422, detail="Date filters must include a timezone.")
    restaurant = staff_restaurant(manager, db)
    query = db.query(FoodOrderOffer).filter(FoodOrderOffer.restaurant_id == restaurant.id)
    if search:
        query = query.filter(FoodOrderOffer.code.ilike(f"%{search.strip()}%") | FoodOrderOffer.title.ilike(f"%{search.strip()}%"))
    if discount_type: query = query.filter(FoodOrderOffer.discount_type == discount_type)
    if applicability: query = query.filter(FoodOrderOffer.applicability == applicability)
    if valid_from: query = query.filter(or_(FoodOrderOffer.ends_at.is_(None), FoodOrderOffer.ends_at >= valid_from))
    if valid_to: query = query.filter(or_(FoodOrderOffer.starts_at.is_(None), FoodOrderOffer.starts_at <= valid_to))
    rows = query.all()
    if status:
        rows = [row for row in rows if offer_data(row)["status"] == status.upper()]
    attr = {"discount_value": "discount_percent"}.get(sort, sort)
    rows.sort(key=lambda row: utc_instant(getattr(row, attr)) if attr in {"starts_at", "ends_at", "created_at"} and getattr(row, attr) else (Decimal(getattr(row, attr)) if attr == "discount_percent" else datetime.min.replace(tzinfo=timezone.utc)), reverse=direction == "desc")
    total = len(rows)
    rows = rows[offset:offset + limit]
    return {"items": [offer_data(row) for row in rows], "total": total, "offset": offset, "limit": limit}


@manager_router.get("/offers/summary")
def manager_offer_summary(manager: User = Depends(current_manager), db: Session = Depends(get_db)) -> dict:
    restaurant = staff_restaurant(manager, db)
    rows = db.query(FoodOrderOffer).filter(FoodOrderOffer.restaurant_id == restaurant.id).all()
    return {"offer_count": len(rows), "active_count": sum(offer_data(row)["status"] == "ACTIVE" for row in rows),
        "redemption_count": sum(row.uses for row in rows), "remaining_capacity": sum(max(0, row.max_uses-row.uses) for row in rows if row.max_uses is not None)}


@manager_router.get("/offers/{offer_id}")
def manager_offer_detail(offer_id: uuid.UUID, manager: User = Depends(current_manager), db: Session = Depends(get_db)) -> dict:
    restaurant = staff_restaurant(manager, db)
    row = db.query(FoodOrderOffer).filter(FoodOrderOffer.id == offer_id, FoodOrderOffer.restaurant_id == restaurant.id).first()
    if row is None: raise HTTPException(status_code=404, detail="Offer not found.")
    return offer_data(row)


def offer_data(row: FoodOrderOffer) -> dict:
    now = datetime.now(timezone.utc)
    start, end = utc_instant(row.starts_at), utc_instant(row.ends_at)
    state = "INACTIVE" if not row.is_active else "SCHEDULED" if start and start > now else "EXPIRED" if end and end < now else "EXHAUSTED" if row.max_uses is not None and row.uses >= row.max_uses else "ACTIVE"
    return {"id": str(row.id), "title": row.title, "description": row.description, "code": row.code,
        "discount_type": row.discount_type, "discount_percent": float(row.discount_percent), "discount_value": float(row.discount_percent),
        "max_discount": float(row.max_discount) if row.max_discount is not None else None,
        "minimum_subtotal": float(row.minimum_subtotal), "uses": row.uses, "max_uses": row.max_uses,
        "remaining_uses": max(0, row.max_uses-row.uses) if row.max_uses is not None else None,
        "per_customer_limit": row.per_customer_limit, "applicability": row.applicability,
        "eligible_food_ids": row.eligible_food_ids or [], "eligible_category_ids": row.eligible_category_ids or [], "status": state,
        "is_active": row.is_active, "starts_at": row.starts_at.isoformat() if row.starts_at else None,
        "ends_at": row.ends_at.isoformat() if row.ends_at else None, "created_at": row.created_at.isoformat(), "updated_at": row.updated_at.isoformat()}


def validate_offer_scope(db: Session, restaurant_id: uuid.UUID, payload: OfferInput) -> None:
    if payload.applicability == "ITEMS":
        count = db.query(Food.id).filter(Food.id.in_(payload.eligible_food_ids), Food.restaurant_id == restaurant_id, Food.deleted_at.is_(None)).count()
        if count != len(set(payload.eligible_food_ids)):
            raise HTTPException(status_code=422, detail="All selected items must belong to your restaurant.")
    if payload.applicability == "CATEGORIES":
        count = db.query(Category.id).filter(Category.id.in_(payload.eligible_category_ids), Category.restaurant_id == restaurant_id, Category.deleted_at.is_(None)).count()
        if count != len(set(payload.eligible_category_ids)):
            raise HTTPException(status_code=422, detail="All selected categories must belong to your restaurant.")


def apply_offer_input(row: FoodOrderOffer, payload: OfferInput) -> None:
    row.code = payload.code.strip().upper()
    row.title = payload.title.strip()
    row.description = payload.description.strip() if payload.description else None
    row.discount_type = payload.discount_type
    row.discount_percent = payload.discount_percent
    row.max_discount = payload.max_discount if payload.discount_type == "PERCENTAGE" else None
    row.minimum_subtotal = payload.minimum_subtotal
    row.max_uses = payload.max_uses
    row.per_customer_limit = payload.per_customer_limit
    row.applicability = payload.applicability
    row.eligible_food_ids = [str(value) for value in payload.eligible_food_ids] if payload.applicability == "ITEMS" else []
    row.eligible_category_ids = [str(value) for value in payload.eligible_category_ids] if payload.applicability == "CATEGORIES" else []
    row.starts_at, row.ends_at = payload.starts_at, payload.ends_at


@manager_router.post("/offers")
def create_offer(payload: OfferInput, manager: User = Depends(current_manager), db: Session = Depends(get_db)) -> dict:
    restaurant = staff_restaurant(manager, db)
    code = payload.code.strip().upper()
    if payload.ends_at and payload.starts_at and payload.ends_at <= payload.starts_at:
        raise HTTPException(status_code=422, detail="Offer end time must be after its start time.")
    validate_offer_scope(db, restaurant.id, payload)
    if db.query(FoodOrderOffer.id).filter(func.upper(FoodOrderOffer.code) == code).first():
        raise HTTPException(status_code=409, detail="That offer code already exists.")
    row = FoodOrderOffer(restaurant_id=restaurant.id)
    apply_offer_input(row, payload)
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="An offer with that code already exists.")
    db.refresh(row)
    return offer_data(row)


@manager_router.put("/offers/{offer_id}")
def update_offer(offer_id: uuid.UUID, payload: OfferInput, manager: User = Depends(current_manager), db: Session = Depends(get_db)) -> dict:
    restaurant = staff_restaurant(manager, db)
    row = db.query(FoodOrderOffer).filter(FoodOrderOffer.id == offer_id, FoodOrderOffer.restaurant_id == restaurant.id).with_for_update().first()
    if row is None: raise HTTPException(status_code=404, detail="Offer not found.")
    if payload.ends_at and payload.starts_at and payload.ends_at <= payload.starts_at:
        raise HTTPException(status_code=422, detail="Offer end time must be after its start time.")
    conflict = db.query(FoodOrderOffer.id).filter(func.upper(FoodOrderOffer.code) == payload.code.strip().upper(), FoodOrderOffer.id != row.id).first()
    if conflict: raise HTTPException(status_code=409, detail="That offer code already exists.")
    if payload.max_uses is not None and payload.max_uses < row.uses:
        raise HTTPException(status_code=409, detail="The redemption limit cannot be lower than the number already used.")
    validate_offer_scope(db, restaurant.id, payload)
    apply_offer_input(row, payload)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Offer changes conflict with an existing offer.")
    db.refresh(row)
    return offer_data(row)


@manager_router.patch("/offers/{offer_id}")
def toggle_offer(offer_id: uuid.UUID, is_active: bool = Query(...), manager: User = Depends(current_manager), db: Session = Depends(get_db)) -> dict:
    restaurant = staff_restaurant(manager, db)
    row = db.query(FoodOrderOffer).filter(FoodOrderOffer.id == offer_id, FoodOrderOffer.restaurant_id == restaurant.id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Offer not found.")
    row.is_active = is_active
    db.commit()
    return offer_data(row)


@manager_router.get("/offers/{offer_id}/redemptions")
def manager_offer_redemptions(offer_id: uuid.UUID, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200), manager: User = Depends(current_manager), db: Session = Depends(get_db)) -> dict:
    restaurant = staff_restaurant(manager, db)
    offer = db.query(FoodOrderOffer).filter(FoodOrderOffer.id == offer_id, FoodOrderOffer.restaurant_id == restaurant.id).first()
    if offer is None: raise HTTPException(status_code=404, detail="Offer not found.")
    query = db.query(OfferRedemption).join(FoodOrder, OfferRedemption.order_id == FoodOrder.id).filter(OfferRedemption.offer_id == offer_id, FoodOrder.restaurant_id == restaurant.id)
    return {"items": [{"id": str(r.id), "offer_id": str(r.offer_id), "order_id": str(r.order_id), "customer_id": str(r.customer_id) if r.customer_id else None, "discount_amount": float(r.discount_amount), "status": r.status, "created_at": r.created_at.isoformat()} for r in query.order_by(OfferRedemption.created_at.desc()).offset(offset).limit(limit).all()], "total": query.count()}


# Keep the UUID catch-all after static paths such as /offers, or FastAPI will
# treat "offers" as an order_id and return a UUID validation error.
@manager_router.get("/{order_id}")
def manager_order(order_id: uuid.UUID, manager: User = Depends(current_manager), db: Session = Depends(get_db)) -> dict:
    restaurant = staff_restaurant(manager, db)
    row = db.query(FoodOrder).filter(
        FoodOrder.id == order_id, FoodOrder.restaurant_id == restaurant.id,
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Order not found for this restaurant.")
    return serialize(row, db)


@customer_router.get("/offers")
def customer_offers(restaurant_id: uuid.UUID, customer: User = Depends(current_customer), db: Session = Depends(get_db)) -> list[dict]:
    rows = db.query(FoodOrderOffer).join(Restaurant, Restaurant.id == FoodOrderOffer.restaurant_id).filter(
        FoodOrderOffer.restaurant_id == restaurant_id, Restaurant.status == RestaurantStatus.APPROVED
    ).order_by(FoodOrderOffer.created_at.desc()).all()
    result = []
    for row in rows:
        data = offer_data(row)
        if not row.is_active:
            continue
        if row.applicability == "ITEMS":
            ids = [uuid.UUID(value) for value in (row.eligible_food_ids or [])]
            data["applicable_names"] = [name for (name,) in db.query(Food.name).filter(Food.id.in_(ids), Food.restaurant_id == restaurant_id).order_by(Food.name).all()] if ids else []
        elif row.applicability == "CATEGORIES":
            ids = [uuid.UUID(value) for value in (row.eligible_category_ids or [])]
            data["applicable_names"] = [name for (name,) in db.query(Category.name).filter(Category.id.in_(ids), Category.restaurant_id == restaurant_id).order_by(Category.name).all()] if ids else []
        else:
            data["applicable_names"] = []
        result.append(data)
    return result


@customer_router.post("/offers/validate")
def validate_customer_offer(payload: OfferValidationInput, customer: User = Depends(current_customer), db: Session = Depends(get_db)) -> dict:
    """Validate a proposed code against current database prices; never reserves capacity."""
    if not payload.reservation_id or not payload.offer_code:
        raise HTTPException(status_code=422, detail="A reservation and offer code are required.")
    reservation = db.query(Reservation).filter(Reservation.id == payload.reservation_id, Reservation.user_id == customer.id).first()
    if reservation is None: raise HTTPException(status_code=404, detail="Reservation not found.")
    q = {line.variant_id: line.quantity for line in payload.items}
    variants = db.query(FoodVariant).join(Food, FoodVariant.food_id == Food.id).join(Category, Food.category_id == Category.id).filter(FoodVariant.id.in_(q), Food.restaurant_id == reservation.restaurant_id, Food.is_available.is_(True), FoodVariant.is_available.is_(True), Food.deleted_at.is_(None), Category.is_active.is_(True), Category.deleted_at.is_(None)).all()
    if len(variants) != len(q): raise HTTPException(status_code=409, detail="One or more cart items are unavailable.")
    offer = db.query(FoodOrderOffer).filter(func.upper(FoodOrderOffer.code) == payload.offer_code.strip().upper(), FoodOrderOffer.restaurant_id == reservation.restaurant_id).first()
    if offer is None: raise HTTPException(status_code=422, detail="Offer code was not found.")
    items = []
    for v in variants:
        matches = offer.applicability == "RESTAURANT" or (offer.applicability == "ITEMS" and str(v.food.id) in (offer.eligible_food_ids or [])) or (offer.applicability == "CATEGORIES" and str(v.food.category_id) in (offer.eligible_category_ids or []))
        if matches: items.append(Decimal(v.price) * q[v.id])
    base = sum(items, Decimal("0.00")).quantize(CENT, rounding=ROUND_HALF_UP)
    uses = db.query(OfferRedemption.id).filter(OfferRedemption.offer_id == offer.id, OfferRedemption.customer_id == customer.id).count()
    reason = None
    now = datetime.now(timezone.utc)
    if not offer.is_active: reason = "Offer is inactive."
    elif offer.starts_at and utc_instant(offer.starts_at) > now: reason = "Offer is not active yet."
    elif offer.ends_at and utc_instant(offer.ends_at) < now: reason = "Offer has expired."
    elif offer.max_uses is not None and offer.uses >= offer.max_uses: reason = "Offer has been fully redeemed."
    elif offer.per_customer_limit is not None and uses >= offer.per_customer_limit: reason = "You have reached the redemption limit."
    elif base < Decimal(offer.minimum_subtotal): reason = "Eligible items do not meet the minimum subtotal."
    amount = calculate_offer_discount(offer, base)
    return {"valid": reason is None, "reason": reason, "offer": offer_data(offer), "eligible_subtotal": float(base), "discount_amount": float(amount if reason is None else Decimal("0.00"))}


@customer_router.post("/offers/preview")
def preview_customer_offer(payload: OfferPreviewInput, customer: User = Depends(current_customer), db: Session = Depends(get_db)) -> dict:
    """Validate an offer against preorder items before reservation creation."""
    restaurant = db.query(Restaurant).filter(Restaurant.id == payload.restaurant_id, Restaurant.status == RestaurantStatus.APPROVED).first()
    if restaurant is None:
        raise HTTPException(status_code=404, detail="Restaurant not found.")
    quantities = {line.variant_id: line.quantity for line in payload.items}
    if len(quantities) != len(payload.items):
        raise HTTPException(status_code=422, detail="Each menu option can only appear once in the preorder.")
    variants = db.query(FoodVariant).join(Food, FoodVariant.food_id == Food.id).join(Category, Food.category_id == Category.id).filter(
        FoodVariant.id.in_(quantities), Food.restaurant_id == restaurant.id, Food.is_available.is_(True),
        FoodVariant.is_available.is_(True), Food.deleted_at.is_(None), Category.is_active.is_(True), Category.deleted_at.is_(None),
    ).all()
    if len(variants) != len(quantities):
        raise HTTPException(status_code=409, detail="One or more selected menu options are no longer available.")
    offer = db.query(FoodOrderOffer).filter(func.upper(FoodOrderOffer.code) == payload.offer_code.strip().upper(), FoodOrderOffer.restaurant_id == restaurant.id).first()
    if offer is None:
        raise HTTPException(status_code=422, detail="Offer code was not found.")
    eligible = []
    for variant in variants:
        matches = offer.applicability == "RESTAURANT" or (offer.applicability == "ITEMS" and str(variant.food.id) in offer.eligible_food_ids) or (offer.applicability == "CATEGORIES" and str(variant.food.category_id) in offer.eligible_category_ids)
        if matches:
            eligible.append(Decimal(variant.price) * quantities[variant.id])
    base = sum(eligible, Decimal("0.00")).quantize(CENT, rounding=ROUND_HALF_UP)
    redemptions = db.query(OfferRedemption.id).filter(OfferRedemption.offer_id == offer.id, OfferRedemption.customer_id == customer.id, OfferRedemption.status == "APPLIED").count()
    claim_ids = [reservation_id for (reservation_id,) in db.query(Reservation.id).filter(Reservation.preorder_offer_id == offer.id, Reservation.user_id == customer.id, Reservation.status.in_(("PENDING_PAYMENT", "CONFIRMED", "SEATED"))).all()]
    claims = sum(1 for reservation_id in claim_ids if not db.query(FoodOrder.id).filter(FoodOrder.idempotency_key == uuid.uuid5(PREORDER_ORDER_NAMESPACE, str(reservation_id))).first())
    reason = None
    now = datetime.now(timezone.utc)
    if not offer.is_active: reason = "Offer is inactive."
    elif offer.starts_at and utc_instant(offer.starts_at) > now: reason = "Offer is not active yet."
    elif offer.ends_at and utc_instant(offer.ends_at) < now: reason = "Offer has expired."
    elif offer.max_uses is not None and offer.uses >= offer.max_uses: reason = "Offer has been fully redeemed."
    elif offer.per_customer_limit is not None and redemptions + claims >= offer.per_customer_limit: reason = "You have reached the redemption limit."
    elif base <= 0: reason = "This offer does not apply to the selected items."
    elif base < Decimal(offer.minimum_subtotal): reason = "Eligible items do not meet the minimum subtotal."
    discount = calculate_offer_discount(offer, base) if reason is None else Decimal("0.00")
    return {"valid": reason is None, "reason": reason, "offer": offer_data(offer), "eligible_subtotal": float(base), "discount_amount": float(discount)}


# Keep the UUID catch-all after static offer paths.
@customer_router.get("/{order_id}")
def customer_order(order_id: uuid.UUID, customer: User = Depends(current_customer), db: Session = Depends(get_db)) -> dict:
    row = db.query(FoodOrder).filter(FoodOrder.id == order_id, FoodOrder.customer_id == customer.id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Order not found.")
    return serialize(row, db)


@customer_router.post("/offers/preview")
def preview_customer_offer(payload: OfferPreviewInput, customer: User = Depends(current_customer), db: Session = Depends(get_db)) -> dict:
    """Preview a preorder offer before a reservation exists; creation revalidates it."""
    restaurant = db.query(Restaurant).filter(Restaurant.id == payload.restaurant_id, Restaurant.status == RestaurantStatus.APPROVED).first()
    if restaurant is None:
        raise HTTPException(status_code=404, detail="Restaurant not found.")
    quantities = {line.variant_id: line.quantity for line in payload.items}
    if len(quantities) != len(payload.items):
        raise HTTPException(status_code=422, detail="Each menu option can only appear once in the preorder.")
    variants = db.query(FoodVariant).join(Food, FoodVariant.food_id == Food.id).join(Category, Food.category_id == Category.id).filter(
        FoodVariant.id.in_(quantities), Food.restaurant_id == restaurant.id, Food.is_available.is_(True),
        FoodVariant.is_available.is_(True), Food.deleted_at.is_(None), Category.is_active.is_(True), Category.deleted_at.is_(None),
    ).all()
    if len(variants) != len(quantities):
        raise HTTPException(status_code=409, detail="One or more selected menu options are no longer available.")
    offer = db.query(FoodOrderOffer).filter(func.upper(FoodOrderOffer.code) == payload.offer_code.strip().upper(), FoodOrderOffer.restaurant_id == restaurant.id).first()
    if offer is None:
        raise HTTPException(status_code=422, detail="Offer code was not found.")
    eligible = []
    for variant in variants:
        if offer.applicability == "RESTAURANT" or (offer.applicability == "ITEMS" and str(variant.food.id) in offer.eligible_food_ids) or (offer.applicability == "CATEGORIES" and str(variant.food.category_id) in offer.eligible_category_ids):
            eligible.append(Decimal(variant.price) * quantities[variant.id])
    base = sum(eligible, Decimal("0.00")).quantize(CENT, rounding=ROUND_HALF_UP)
    offer_uses = db.query(OfferRedemption.id).filter(OfferRedemption.offer_id == offer.id, OfferRedemption.customer_id == customer.id, OfferRedemption.status == "APPLIED").count()
    reason = None
    now = datetime.now(timezone.utc)
    if not offer.is_active: reason = "Offer is inactive."
    elif offer.starts_at and utc_instant(offer.starts_at) > now: reason = "Offer is not active yet."
    elif offer.ends_at and utc_instant(offer.ends_at) < now: reason = "Offer has expired."
    elif offer.max_uses is not None and offer.uses >= offer.max_uses: reason = "Offer has been fully redeemed."
    elif offer.per_customer_limit is not None and offer_uses >= offer.per_customer_limit: reason = "You have reached the redemption limit."
    elif base == 0: reason = "This offer does not apply to the selected items."
    elif base < Decimal(offer.minimum_subtotal): reason = "Eligible items do not meet the minimum subtotal."
    discount = calculate_offer_discount(offer, base) if reason is None else Decimal("0.00")
    return {"valid": reason is None, "reason": reason, "offer": offer_data(offer), "eligible_subtotal": float(base), "discount_amount": float(discount)}


@admin_router.get("/offers")
def admin_offers(search: str | None = None, status: str | None = None, discount_type: str | None = Query(None, pattern="^(PERCENTAGE|FIXED)$"), valid_from: datetime | None = None, valid_to: datetime | None = None, restaurant_id: uuid.UUID | None = None, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200), admin: User = Depends(current_admin), db: Session = Depends(get_db)) -> dict:
    query = db.query(FoodOrderOffer)
    if restaurant_id: query = query.filter(FoodOrderOffer.restaurant_id == restaurant_id)
    if discount_type: query = query.filter(FoodOrderOffer.discount_type == discount_type)
    if valid_from and valid_from.utcoffset() is None or valid_to and valid_to.utcoffset() is None:
        raise HTTPException(status_code=422, detail="Date filters must include a timezone.")
    if valid_from: query = query.filter(or_(FoodOrderOffer.ends_at.is_(None), FoodOrderOffer.ends_at >= valid_from))
    if valid_to: query = query.filter(or_(FoodOrderOffer.starts_at.is_(None), FoodOrderOffer.starts_at <= valid_to))
    if search:
        query = query.outerjoin(Restaurant, Restaurant.id == FoodOrderOffer.restaurant_id).filter(FoodOrderOffer.title.ilike(f"%{search}%") | FoodOrderOffer.code.ilike(f"%{search}%") | Restaurant.name.ilike(f"%{search}%"))
    rows = query.order_by(FoodOrderOffer.created_at.desc()).all()
    if status:
        rows = [row for row in rows if offer_data(row)["status"] == status.upper()]
    total = len(rows)
    rows = rows[offset:offset + limit]
    return {"items": [dict(offer_data(r), restaurant_name=(db.query(Restaurant.name).filter(Restaurant.id == r.restaurant_id).scalar() or "Restaurant"), restaurant_id=str(r.restaurant_id)) for r in rows], "total": total, "offset": offset, "limit": limit}


@admin_router.get("/offers/statistics")
def admin_offer_statistics(admin: User = Depends(current_admin), db: Session = Depends(get_db)) -> dict:
    return {"offer_count": db.query(FoodOrderOffer.id).count(),
        "redemption_count": db.query(OfferRedemption.id).filter(OfferRedemption.status == "APPLIED").count(),
        "discount_total": float(db.query(func.coalesce(func.sum(OfferRedemption.discount_amount), 0)).filter(OfferRedemption.status == "APPLIED").scalar() or 0)}


@admin_router.get("/offers/{offer_id}")
def admin_offer_detail(offer_id: uuid.UUID, admin: User = Depends(current_admin), db: Session = Depends(get_db)) -> dict:
    row = db.query(FoodOrderOffer).filter(FoodOrderOffer.id == offer_id).first()
    if row is None: raise HTTPException(status_code=404, detail="Offer not found.")
    return dict(offer_data(row), restaurant_id=str(row.restaurant_id), restaurant_name=(db.query(Restaurant.name).filter(Restaurant.id == row.restaurant_id).scalar() or "Restaurant"))


@admin_router.get("/offer-redemptions")
def admin_offer_redemptions(search: str | None = None, restaurant_id: uuid.UUID | None = None, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200), admin: User = Depends(current_admin), db: Session = Depends(get_db)) -> dict:
    query = db.query(OfferRedemption, FoodOrderOffer, FoodOrder).join(FoodOrderOffer, OfferRedemption.offer_id == FoodOrderOffer.id).join(FoodOrder, OfferRedemption.order_id == FoodOrder.id)
    if restaurant_id: query = query.filter(FoodOrder.restaurant_id == restaurant_id)
    if search: query = query.filter(FoodOrderOffer.code.ilike(f"%{search}%") | FoodOrderOffer.title.ilike(f"%{search}%"))
    total = query.count()
    rows = query.order_by(OfferRedemption.created_at.desc()).offset(offset).limit(limit).all()
    return {"items": [{"id": str(r.id), "offer_title": o.title, "offer_code": o.code, "restaurant_id": str(order.restaurant_id), "order_id": str(order.id), "customer_id": str(r.customer_id) if r.customer_id else None, "discount_amount": float(r.discount_amount), "status": r.status, "created_at": r.created_at.isoformat()} for r,o,order in rows], "total": total}


@admin_router.get("/offer-redemptions/{redemption_id}")
def admin_offer_redemption_detail(redemption_id: uuid.UUID, admin: User = Depends(current_admin), db: Session = Depends(get_db)) -> dict:
    row = db.query(OfferRedemption).filter(OfferRedemption.id == redemption_id).first()
    if row is None: raise HTTPException(status_code=404, detail="Redemption not found.")
    offer = db.query(FoodOrderOffer).filter(FoodOrderOffer.id == row.offer_id).first()
    order = db.query(FoodOrder).filter(FoodOrder.id == row.order_id).first()
    return {"id": str(row.id), "offer": offer_data(offer) if offer else None,
        "restaurant_id": str(order.restaurant_id) if order else None, "order_id": str(row.order_id),
        "customer_id": str(row.customer_id) if row.customer_id else None,
        "discount_amount": float(row.discount_amount), "status": row.status, "created_at": row.created_at.isoformat()}


@chef_router.get("")
def chef_orders(chef: User = Depends(current_chef), db: Session = Depends(get_db)) -> list[dict]:
    return list_for_staff(chef, db)


@chef_router.patch("/{order_id}/status")
def chef_update_order(order_id: uuid.UUID, payload: StatusInput, chef: User = Depends(current_chef), db: Session = Depends(get_db)) -> dict:
    return update_status(order_id, payload.status, CHEF_TRANSITIONS, chef, db, payload.reason)


@admin_router.get("")
def admin_orders(restaurant_id: uuid.UUID | None = None, limit: int = Query(default=100, ge=1, le=500), admin: User = Depends(current_admin), db: Session = Depends(get_db)) -> list[dict]:
    query = db.query(FoodOrder)
    if restaurant_id:
        query = query.filter(FoodOrder.restaurant_id == restaurant_id)
    return [serialize(row, db) for row in query.order_by(FoodOrder.created_at.desc()).limit(limit).all()]


@admin_router.get("/{order_id}")
def admin_order(order_id: uuid.UUID, admin: User = Depends(current_admin), db: Session = Depends(get_db)) -> dict:
    row = db.query(FoodOrder).filter(FoodOrder.id == order_id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Order not found.")
    return serialize(row, db)
