import hashlib
import asyncio
import hmac
import json
import unittest
import uuid
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException, Response
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from tests import sqlite_support
from app.core.config import settings
from app.core.security import (create_access_token, decode_access_token, hash_password,
                               verify_secret, _base64url)
from app.db.database import Base, get_db
from app.models.dining import DiningTable, RestaurantFloor
from app.models.orders import FoodOrder
from app.models.restaurant import Restaurant, RestaurantStatus
from app.models.reservation import Reservation
from app.models.revenue import PlatformCommission
from app.models.user import AccountStatus, User, UserRole
from app.models.wallet import WalletAccount, WalletTopUp, WalletTransaction
from app.models.realtime import WebhookReceipt
from app.routers.admin_auth import current_admin
from app.routers.chef_auth import current_chef, ChefChangePasswordRequest, change_chef_password
from app.routers.customer_auth import (current_customer, CustomerPasswordChangeRequest,
    change_customer_password, router as customer_auth_router)
from app.routers.manager_auth import current_manager
from app.routers.dining import FloorUpdate, TableUpdate
from app.routers.manager_menu import VariantPayload
from app.routers.operating_hours import Period
from app.routers.orders import OfferInput, manager_mark_order_paid, update_status, MANAGER_TRANSITIONS
from app.routers.reservations import (ReservationCreate, within_operating_hours,
    manager_update_reservation_status, ManagerReservationStatus)
from app.routers.revenue import gross_revenue, sync_commission_records
from app.routers.webhooks import router as webhook_router
from app.services.notifications import _safe_action_url


class ASGIClient:
    """Exercise real FastAPI routing without an optional HTTP client dependency."""
    def __init__(self, app):
        self.app = app
        self.cookies = {}

    def post(self, path, **kwargs):
        body = kwargs.get("content", json.dumps(kwargs.get("json", {})).encode())
        headers = {"content-type": "application/json", **kwargs.get("headers", {})}
        if self.cookies:
            headers["cookie"] = "; ".join(f"{key}={value}" for key, value in self.cookies.items())
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                 "method": "POST", "path": path, "query_string": b"", "root_path": "",
                 "scheme": "http", "server": ("test", 80), "client": ("127.0.0.1", 1),
                 "headers": [(key.encode(), value.encode()) for key, value in headers.items()]}
        messages = []

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(message):
            messages.append(message)

        asyncio.run(self.app(scope, receive, send))
        start = next(message for message in messages if message["type"] == "http.response.start")
        text = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body").decode()
        return SimpleNamespace(status_code=start["status"], text=text,
            headers={key.decode(): value.decode() for key, value in start["headers"]},
            json=lambda: json.loads(text))


class FieldValidationTests(unittest.TestCase):
    def test_required_floor_and_table_fields_cannot_be_cleared(self):
        for model, fields in ((FloorUpdate, ("floor_name", "floor_number", "is_active")),
                              (TableUpdate, ("floor_id", "table_number", "capacity", "reservation_fee", "table_type", "status"))):
            for field in fields:
                with self.subTest(model=model.__name__, field=field), self.assertRaises(ValidationError):
                    model(**{field: None})
        self.assertIsNone(FloorUpdate(description=None).description)
        self.assertEqual(TableUpdate().model_fields_set, set())

    def test_menu_prices_reject_sub_cent_and_non_finite_values(self):
        for price in ("1.001", "NaN", "Infinity", "-1", "100000000"):
            with self.subTest(price=price), self.assertRaises(ValidationError):
                VariantPayload(name="Regular", price=price)
        self.assertEqual(VariantPayload(name="Regular", price="12.50").price, Decimal("12.50"))

    def test_offers_reject_blank_titles_mixed_scope_and_invalid_dates(self):
        start = datetime.now(timezone.utc)
        invalid = ({"title": "   "}, {"starts_at": start, "ends_at": start},
                   {"applicability": "ITEMS", "eligible_food_ids": [uuid.uuid4()], "eligible_category_ids": [uuid.uuid4()]},
                   {"discount_percent": 101}, {"starts_at": datetime.now()})
        for extra in invalid:
            with self.subTest(extra=extra), self.assertRaises(ValidationError):
                OfferInput(**{"code": "SAVE", "discount_percent": 10, **extra})

    def test_schedule_times_require_local_minute_precision(self):
        for opens in ("09:00:01", "09:00+05:30"):
            with self.subTest(opens=opens), self.assertRaises(ValidationError):
                Period(opens=opens, closes="18:00")
        self.assertEqual(Period(opens="09:15", closes="18:00").opens, time(9, 15))

    def test_booking_times_and_table_selections_are_validated(self):
        data = {"restaurant_id": uuid.uuid4(), "reservation_date": date(2030, 1, 1),
                "start_time": "09:15", "number_of_guests": 2}
        table_id = uuid.uuid4()
        for extra in ({"start_time": "09:00+05:30"}, {"start_time": "09:00:01"},
                      {"table_ids": [table_id, table_id]},
                      {"table_id": table_id, "table_ids": [uuid.uuid4()]}):
            with self.subTest(extra=extra), self.assertRaises(ValidationError):
                ReservationCreate(**{**data, **extra})
        ReservationCreate(**data)

    def test_booking_slots_follow_the_actual_opening_time(self):
        with patch("app.routers.reservations.effective_periods", return_value=([{"opens": "09:15", "closes": "18:00"}], None)):
            self.assertTrue(within_operating_hours(None, uuid.uuid4(), date(2030, 1, 1), time(9, 45), time(11, 15)))
            self.assertFalse(within_operating_hours(None, uuid.uuid4(), date(2030, 1, 1), time(10), time(11, 30)))

    def test_wallet_notification_links_are_preserved(self):
        for role in ("chef", "manager", "admin"):
            self.assertEqual(_safe_action_url(f"/{role}/revenue"), f"/{role}/revenue")
        self.assertIsNone(_safe_action_url("https://example.com/chef/revenue"))

    def test_signed_non_finite_expiry_and_non_object_tokens_are_rejected(self):
        for payload in ({"sub": "x", "exp": float("nan")}, [], "text"):
            header = _base64url(b'{"alg":"HS256"}')
            body = _base64url(json.dumps(payload).encode())
            signature = _base64url(hmac.new(settings.jwt_secret_key.encode(), f"{header}.{body}".encode(), hashlib.sha256).digest())
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                decode_access_token(f"{header}.{body}.{signature}")


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine, expire_on_commit=False)
        self.manager = User(name="Manager", email="manager@example.com", password_hash="unused", role=UserRole.MANAGER)
        self.customer = User(name="Customer", email="customer@example.com", password_hash=hash_password("Original123!"), role=UserRole.CUSTOMER)
        self.db.add_all((self.manager, self.customer)); self.db.flush()
        self.restaurant = Restaurant(manager_id=self.manager.id, name="Test", address="Street", city="Pune", status=RestaurantStatus.APPROVED)
        self.db.add(self.restaurant); self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def client_for(self, router):
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_db] = lambda: self.db
        return ASGIClient(app)

    def test_old_sessions_are_rejected_by_every_portal_after_password_change(self):
        for role, authorize in ((UserRole.CUSTOMER, current_customer), (UserRole.MANAGER, current_manager),
                                (UserRole.CHEF, current_chef), (UserRole.ADMIN, current_admin)):
            self.customer.role = role
            self.customer.manager_id = self.manager.id if role == UserRole.CHEF else None
            old, _ = create_access_token(str(self.customer.id))
            self.customer.password_changed_at = datetime.now(timezone.utc)
            self.db.commit()
            with self.subTest(role=role), self.assertRaises(HTTPException) as caught:
                authorize(token=old, db=self.db)
            self.assertEqual(caught.exception.status_code, 401)
            new, _ = create_access_token(str(self.customer.id))
            self.assertEqual(authorize(token=new, db=self.db).id, self.customer.id)

    def test_customer_password_change_endpoint_reauthenticates_and_rotates_session(self):
        client = self.client_for(customer_auth_router)
        old, _ = create_access_token(str(self.customer.id))
        client.cookies["dinebook_access_token"] = old
        data = {"current_password": "wrong", "new_password": "Replacement123!", "confirm_password": "Replacement123!"}
        self.assertEqual(client.post("/api/auth/customer/change-password", json=data).status_code, 400)
        data["current_password"] = "Original123!"
        response = client.post("/api/auth/customer/change-password", json=data)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(verify_secret(data["new_password"], self.customer.password_hash))
        self.assertIn("dinebook_access_token=", response.headers["set-cookie"])
        with self.assertRaises(HTTPException):
            current_customer(token=old, db=self.db)

    def test_password_change_rejects_blank_and_mismatching_passwords(self):
        for new, confirm in (("          ", "          "), ("Replacement123!", "OtherPassword123!")):
            with self.subTest(new=new), self.assertRaises(ValidationError):
                CustomerPasswordChangeRequest(current_password="Original123!", new_password=new, confirm_password=confirm)

    def test_initial_chef_password_cannot_access_operational_routes(self):
        self.customer.role = UserRole.CHEF
        self.customer.manager_id = self.manager.id
        self.customer.must_change_password = True
        self.db.commit()
        token, _ = create_access_token(str(self.customer.id))
        request = Request({"type": "http", "path": "/api/chef/orders", "headers": []})
        with self.assertRaises(HTTPException) as caught:
            current_chef(token=token, db=self.db, request=request)
        self.assertEqual(caught.exception.status_code, 403)
        request = Request({"type": "http", "path": "/api/chef/me", "headers": []})
        self.assertEqual(current_chef(token=token, db=self.db, request=request).id, self.customer.id)
        change_chef_password(ChefChangePasswordRequest(password="Replacement123!"), Response(), self.customer, self.db)
        self.assertFalse(self.customer.must_change_password)

    def test_paid_order_retries_preserve_timestamp_and_table_until_completion(self):
        floor = RestaurantFloor(restaurant_id=self.restaurant.id, floor_number=1, floor_name="Main")
        self.db.add(floor); self.db.flush()
        table = DiningTable(restaurant_id=self.restaurant.id, floor_id=floor.id, table_number="1", capacity=2, status="occupied")
        self.db.add(table); self.db.flush()
        order = FoodOrder(restaurant_id=self.restaurant.id, table_id=table.id, items=[], subtotal=100, total=118,
                          status="READY", payment_method="PAY_AT_DESK", payment_status="PENDING")
        self.db.add(order); self.db.commit()
        manager_mark_order_paid(order.id, self.manager, self.db)
        paid_at = order.paid_at
        self.assertEqual(table.status, "occupied")
        manager_mark_order_paid(order.id, self.manager, self.db)
        self.assertEqual(order.paid_at, paid_at)
        update_status(order.id, "COMPLETED", MANAGER_TRANSITIONS, self.manager, self.db)
        self.assertEqual(table.status, "available")

    def paid_reservation(self):
        now = datetime.now(timezone.utc)
        reservation = Reservation(user_id=self.customer.id, restaurant_id=self.restaurant.id,
            reservation_date=now.date(), start_time=time(10), end_time=time(11, 30), number_of_guests=2,
            status="SEATED", fee_amount=Decimal("50.00"), payment_method="WALLET",
            payment_status="PAID", paid_at=now, fulfillment_type="PREORDER",
            preorder_total=Decimal("100.00"), preorder_payment_method="WALLET",
            preorder_payment_status="PAID", preorder_paid_at=now)
        self.db.add(reservation); self.db.commit()
        return reservation

    def test_preorder_revenue_and_commission_survive_kitchen_order_creation(self):
        reservation = self.paid_reservation()
        self.assertEqual(gross_revenue(self.db, [self.manager.id], "month"), Decimal("150.00"))
        order = FoodOrder(customer_id=self.customer.id, restaurant_id=self.restaurant.id,
            reservation_id=reservation.id, idempotency_key=uuid.uuid5(
                uuid.UUID("89dd0385-bbad-4aeb-b600-bf4377641e10"), str(reservation.id)),
            items=[], subtotal=100, total=100, payment_status="PAID", payment_method="WALLET")
        self.db.add(order); self.db.commit()
        self.assertEqual(gross_revenue(self.db, [self.manager.id], "month"), Decimal("150.00"))
        sync_commission_records(self.db, [self.manager.id], Decimal("10.00"))
        self.db.commit()
        sync_commission_records(self.db, [self.manager.id], Decimal("10.00"))
        self.db.commit()
        records = self.db.query(PlatformCommission).all()
        self.assertEqual(len(records), 2)
        self.assertEqual(sum(row.commission_amount for row in records), Decimal("15.00"))

    def test_cancelled_orders_do_not_block_reservation_completion(self):
        reservation = self.paid_reservation()
        self.db.add(FoodOrder(customer_id=self.customer.id, restaurant_id=self.restaurant.id,
            reservation_id=reservation.id, items=[], subtotal=100, total=100,
            status="CANCELLED", payment_status="REFUNDED"))
        self.db.commit()
        result = manager_update_reservation_status(reservation.id,
            ManagerReservationStatus(status="COMPLETED"), self.manager, self.db)
        self.assertEqual(result["status"], "COMPLETED")

    def test_webhook_signatures_duplicates_and_out_of_order_events(self):
        topup = WalletTopUp(user_id=self.customer.id, razorpay_order_id="order_test", amount=Decimal("100.00"))
        self.db.add(topup); self.db.commit()
        client = self.client_for(webhook_router)

        def send(event_id, event_type, amount=10000, valid_signature=True):
            body = json.dumps({"event": event_type, "payload": {"payment": {"entity": {
                "order_id": "order_test", "id": "pay_test", "amount": amount,
                "currency": "INR", "status": "captured"}}}}).encode()
            signature = hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()
            return client.post("/api/webhooks/razorpay", content=body, headers={
                "content-type": "application/json", "x-razorpay-event-id": event_id,
                "x-razorpay-signature": signature if valid_signature else "invalid"})

        with patch.object(settings, "razorpay_webhook_secret", "test-secret"):
            self.assertEqual(send("bad-signature", "payment.captured", valid_signature=False).status_code, 401)
            self.assertEqual(send("bad-amount", "payment.captured", amount=1).status_code, 400)
            self.assertEqual(send("failed-first", "payment.failed").status_code, 200)
            self.assertEqual(send("captured", "payment.captured").status_code, 200)
            self.assertEqual(send("captured", "payment.captured").json()["status"], "duplicate")
            self.assertEqual(send("late-failure", "payment.failed").status_code, 200)
            self.assertEqual(send("captured-again", "payment.captured").status_code, 200)
        self.assertEqual(self.db.get(WalletAccount, self.customer.id).balance, Decimal("100.00"))
        self.assertEqual(topup.status, "PAID")
        self.assertEqual(self.db.query(WalletTransaction).count(), 1)
        self.assertEqual(self.db.query(WebhookReceipt).count(), 4)


if __name__ == "__main__":
    unittest.main()
