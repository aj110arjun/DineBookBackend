import uuid
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from app.db.database import get_db
from app.models.dining import DiningTable
from app.models.menu import Category, Food, FoodVariant
from app.models.orders import FoodOrder, FoodOrderOffer
from app.models.reservation import Reservation, ReservationTable
from app.models.restaurant import Restaurant, RestaurantStatus
from app.models.user import User
from app.models.wallet import WalletAccount, WalletTransaction
from app.models.realtime import RealtimeEvent
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
CHEF_TRANSITIONS = {"ACCEPTED": {"PREPARING"}, "PREPARING": {"READY"}}


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
    discount_percent: Decimal = Field(gt=0, le=100)
    minimum_subtotal: Decimal = Field(default=Decimal("0.00"), ge=0)
    max_uses: int | None = Field(default=None, gt=0)
    starts_at: datetime | None = None
    ends_at: datetime | None = None


def staff_restaurant(staff: User, db: Session) -> Restaurant:
    owner_id = staff.manager_id or staff.id
    restaurant = db.query(Restaurant).filter(Restaurant.manager_id == owner_id).first()
    if restaurant is None:
        raise HTTPException(status_code=404, detail="No restaurant is linked to this account.")
    return restaurant


def serialize(order: FoodOrder, db: Session) -> dict:
    restaurant = db.query(Restaurant).filter(Restaurant.id == order.restaurant_id).first()
    customer = db.query(User).filter(User.id == order.customer_id).first()
    reservation = db.query(Reservation).filter(Reservation.id == order.reservation_id).first() if order.reservation_id else None
    assigned_tables = []
    if reservation:
        assigned_tables = db.query(DiningTable).join(ReservationTable, ReservationTable.table_id == DiningTable.id).filter(
            ReservationTable.reservation_id == reservation.id
        ).order_by(DiningTable.table_number).all()
    is_preorder = order.idempotency_key == uuid.uuid5(PREORDER_ORDER_NAMESPACE, str(order.reservation_id)) if order.reservation_id else False
    return {
        "id": str(order.id), "reservation_id": str(order.reservation_id) if order.reservation_id else None,
        "is_preorder": is_preorder,
        "order_source": "PREORDER" if is_preorder else ("RESERVATION" if order.reservation_id else "WALK_IN"),
        "reservation": ({"id": str(reservation.id), "status": reservation.status,
            "reservation_date": reservation.reservation_date.isoformat(),
            "start_time": reservation.start_time.strftime("%H:%M"),
            "number_of_guests": reservation.number_of_guests,
            "special_request": reservation.special_request,
            "tables": [{"id": str(table.id), "table_number": table.table_number,
                        "floor_name": table.floor.floor_name if table.floor else None}
                       for table in assigned_tables]}
            if reservation else None),
        "restaurant_id": str(order.restaurant_id),
        "restaurant_name": restaurant.name if restaurant else "Restaurant",
        "customer_id": str(order.customer_id), "customer_name": customer.name if customer else "Customer",
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
                FoodOrderOffer.code == code, FoodOrderOffer.restaurant_id == reservation.restaurant_id,
                FoodOrderOffer.is_active.is_(True),
            ).with_for_update().first()
            now = datetime.now(timezone.utc)
            if (offer is None or (offer.starts_at and offer.starts_at > now)
                    or (offer.ends_at and offer.ends_at < now)
                    or (offer.max_uses is not None and offer.uses >= offer.max_uses)
                    or subtotal < Decimal(offer.minimum_subtotal)):
                raise HTTPException(status_code=422, detail="This offer is invalid, expired, or does not meet its minimum spend.")
            discount = (subtotal * Decimal(offer.discount_percent) / Decimal("100")).quantize(CENT, rounding=ROUND_HALF_UP)
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
            customer_note=payload.customer_note)
        db.add(order)
        db.flush()
        record_order_event(db, order, actor=actor or customer)
        if payload.payment_method == "WALLET":
            db.add(WalletTransaction(user_id=customer.id, amount=-total, transaction_type="DEBIT",
                description=f"Food order at {restaurant.name}", reference=f"food-order:{order.id}"))
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
        if subtotal != Decimal(reservation.preorder_total).quantize(CENT, rounding=ROUND_HALF_UP):
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

        order = FoodOrder(
            customer_id=customer.id,
            reservation_id=reservation.id,
            restaurant_id=restaurant.id,
            idempotency_key=idempotency_key,
            items=snapshots,
            subtotal=subtotal,
            discount_amount=Decimal("0.00"),
            total=subtotal,
            status="PLACED",
            payment_method=payment_method or "PAY_AT_DESK",
            payment_status=payment_status,
            customer_note="Reservation preorder",
        )
        db.add(order)
        db.flush()
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


@customer_router.get("/{order_id}")
def customer_order(order_id: uuid.UUID, customer: User = Depends(current_customer), db: Session = Depends(get_db)) -> dict:
    row = db.query(FoodOrder).filter(FoodOrder.id == order_id, FoodOrder.customer_id == customer.id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Order not found.")
    return serialize(row, db)


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
        row.payment_status = "REFUNDED"
    previous_status = row.status
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


@manager_router.get("/{order_id}")
def manager_order(order_id: uuid.UUID, manager: User = Depends(current_manager), db: Session = Depends(get_db)) -> dict:
    restaurant = staff_restaurant(manager, db)
    row = db.query(FoodOrder).filter(
        FoodOrder.id == order_id, FoodOrder.restaurant_id == restaurant.id,
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Order not found for this restaurant.")
    return serialize(row, db)


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
    if row.reservation_id is not None and row.idempotency_key == uuid.uuid5(PREORDER_ORDER_NAMESPACE, str(row.reservation_id)):
        reservation = db.query(Reservation).filter(Reservation.id == row.reservation_id).with_for_update().first()
        if reservation is not None:
            reservation.preorder_payment_method = "PAY_AT_DESK"
            reservation.preorder_payment_status = "PAID"
    record_order_event(db, row, actor=manager)
    db.commit()
    db.refresh(row)
    return serialize(row, db)


@manager_router.get("/offers")
def manager_offers(manager: User = Depends(current_manager), db: Session = Depends(get_db)) -> list[dict]:
    restaurant = staff_restaurant(manager, db)
    return [offer_data(row) for row in db.query(FoodOrderOffer).filter(FoodOrderOffer.restaurant_id == restaurant.id).order_by(FoodOrderOffer.created_at.desc()).all()]


def offer_data(row: FoodOrderOffer) -> dict:
    return {"id": str(row.id), "code": row.code, "discount_percent": float(row.discount_percent),
        "minimum_subtotal": float(row.minimum_subtotal), "uses": row.uses, "max_uses": row.max_uses,
        "is_active": row.is_active, "starts_at": row.starts_at.isoformat() if row.starts_at else None,
        "ends_at": row.ends_at.isoformat() if row.ends_at else None}


@manager_router.post("/offers")
def create_offer(payload: OfferInput, manager: User = Depends(current_manager), db: Session = Depends(get_db)) -> dict:
    restaurant = staff_restaurant(manager, db)
    code = payload.code.strip().upper()
    if payload.ends_at and payload.starts_at and payload.ends_at <= payload.starts_at:
        raise HTTPException(status_code=422, detail="Offer end time must be after its start time.")
    if db.query(FoodOrderOffer.id).filter(FoodOrderOffer.code == code).first():
        raise HTTPException(status_code=409, detail="That offer code already exists.")
    row = FoodOrderOffer(restaurant_id=restaurant.id, code=code, discount_percent=payload.discount_percent,
        minimum_subtotal=payload.minimum_subtotal, max_uses=payload.max_uses,
        starts_at=payload.starts_at, ends_at=payload.ends_at)
    db.add(row)
    db.commit()
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
