import importlib.util
import unittest
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from tests import sqlite_support  # noqa: F401
from tests.test_customer_profile import ASGIClient
from app.core.config import settings
from app.core.security import create_access_token
from app.db.database import Base, get_db
from app.main import app
from app.models.customer_settings import CustomerSettings
from app.models.notifications import Notification, NotificationPreference
from app.models.realtime import RealtimeEvent
from app.models.reservation import Reservation
from app.models.restaurant import Restaurant
from app.models.reviews import Review
from app.models.user import AccountStatus, User, UserRole
from app.routers.restaurants.reviews import ReplyPayload, reply_to_review
from app.routers.restaurants.reservations import reservation_data
from app.schemas.customer_settings import GeneralSettings, NotificationSettings
from app.services.customer_email import send_customer_notification_email
from app.services.notifications import create_notification
from app.services.reservation_email import send_reservation_confirmation

BASE_PATH = "/api/customer/settings"


class CustomerSettingsTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        with self.sessions() as db:
            self.customer = User(name="Asha Rao", email="asha@example.test", password_hash="hash", role=UserRole.CUSTOMER)
            self.other = User(name="Other Customer", email="other@example.test", password_hash="hash", role=UserRole.CUSTOMER)
            self.manager = User(name="Manager", email="manager@example.test", password_hash="hash", role=UserRole.MANAGER)
            db.add_all([self.customer, self.other, self.manager])
            db.flush()
            self.restaurant = Restaurant(manager_id=self.manager.id, name="Test Kitchen", address="1 Main", city="Pune")
            db.add(self.restaurant)
            db.commit()
        def override():
            with self.sessions() as db:
                yield db
        app.dependency_overrides[get_db] = override
        self.addCleanup(app.dependency_overrides.pop, get_db)
        self.addCleanup(self.engine.dispose)
        guard = patch.object(settings, "rate_limit_enabled", False)
        guard.start()
        self.addCleanup(guard.stop)
        self.client = ASGIClient()
        self.token = create_access_token(str(self.customer.id), auth_method="password")[0]

    def request(self, method="GET", section="", payload=None, token=None):
        return self.client.request(method, BASE_PATH + section, token=self.token if token is None else token, payload=payload)

    def test_defaults_are_created_once_for_existing_customer_and_persisted(self):
        expected = {"notifications": NotificationSettings().model_dump(), "general": GeneralSettings().model_dump()}
        for _ in range(2):
            result = self.request()
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json, expected)
            self.assertEqual(result.headers["cache-control"], "no-store")
        with self.sessions() as db:
            self.assertEqual(db.query(CustomerSettings).count(), 1)
            self.assertEqual(db.query(NotificationPreference).count(), len(expected["notifications"]))
            row = db.get(CustomerSettings, self.customer.id)
            self.assertIsNotNone(row.created_at)
            self.assertIsNotNone(row.updated_at)

    def test_legacy_category_opt_out_is_preserved_and_granular_preference_can_override(self):
        with self.sessions() as db:
            db.add(NotificationPreference(user_id=self.customer.id, category="RESERVATION", enabled=False))
            db.commit()
        prefs = self.request().json["notifications"]
        self.assertFalse(prefs["reservation_reminders"])
        self.assertFalse(prefs["reservation_confirmation"])
        result = self.request("PATCH", "/notifications", {"reservation_confirmation": True})
        self.assertTrue(result.json["notifications"]["reservation_confirmation"])
        self.assertFalse(result.json["notifications"]["reservation_reminders"])

    def test_notification_patch_updates_only_supplied_fields_and_current_customer(self):
        other_token = create_access_token(str(self.other.id))[0]
        self.request(token=other_token)
        changes = {"email_enabled": False, "reservation_reminders": False, "promotional_offers": True}
        result = self.request("PATCH", "/notifications", changes)
        self.assertEqual(result.status_code, 200)
        for field, value in changes.items():
            self.assertEqual(result.json["notifications"][field], value)
        self.assertTrue(result.json["notifications"]["in_app_enabled"])
        self.assertEqual(self.request().json, result.json)
        self.assertEqual(self.request(token=other_token).json["notifications"], NotificationSettings().model_dump())

    def test_general_patch_persists_and_browser_timezone_can_be_restored(self):
        changes = {"theme": "dark", "language": "en", "date_format": "YYYY-MM-DD", "time_format": "24h", "timezone": "Asia/Kolkata"}
        result = self.request("PATCH", "/general", changes)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json["general"], changes)
        self.assertEqual(self.request().json, result.json)
        self.assertIsNone(self.request("PATCH", "/general", {"timezone": None}).json["general"]["timezone"])
        with self.sessions() as db:
            self.assertEqual(db.get(CustomerSettings, self.customer.id).theme, "dark")
            self.assertIsNone(db.get(CustomerSettings, self.other.id))

    def test_all_supported_format_values_are_accepted(self):
        for field, values in (("theme", ["light", "dark", "system"]),
                              ("date_format", ["DD/MM/YYYY", "MM/DD/YYYY", "YYYY-MM-DD"]),
                              ("time_format", ["12h", "24h"]),
                              ("timezone", ["UTC", "Asia/Calcutta", "America/New_York", "Europe/London"])):
            for value in values:
                with self.subTest(field=field, value=value):
                    result = self.request("PATCH", "/general", {field: value})
                    self.assertEqual(result.status_code, 200)
                    self.assertEqual(result.json["general"][field], value)

    def test_unsupported_values_nulls_coercion_and_mass_assignment_are_rejected(self):
        for section, payloads in (("/general", [
            {"theme": "sepia"}, {"theme": None}, {"language": "fr"}, {"language": None},
            {"date_format": "D/M/YY"}, {"date_format": None}, {"time_format": "25h"}, {"time_format": None},
            {"timezone": "Mars/Olympus"}, {"timezone": "+05:30"}, {"timezone": "../etc/passwd"}, {"timezone": ""},
            {"timezone": 123}, {"timezone": "localtime"}, {"timezone": "A" * 101},
            {"customer_id": str(self.other.id)}, {"user_id": str(self.other.id)},
            {"theme": "dark", "is_active": False}, {"updated_at": "2000-01-01"},
        ]), ("/notifications", [
            {"email_enabled": None}, {"email_enabled": "false"}, {"in_app_enabled": 0},
            {"reservation_reminders": []}, {"security_enabled": False}, {"customer_id": str(self.other.id)},
        ])):
            for payload in payloads:
                with self.subTest(section=section, payload=payload):
                    self.assertEqual(self.request("PATCH", section, payload).status_code, 422)
        self.assertEqual(self.request().json["general"], GeneralSettings().model_dump())

    def test_endpoints_reject_unauthenticated_other_roles_inactive_unverified_and_revoked_sessions(self):
        paths = [("GET", ""), ("PATCH", "/general"), ("PATCH", "/notifications")]
        for method, section in paths:
            self.assertEqual(self.client.request(method, BASE_PATH + section, payload={} if method == "PATCH" else None).status_code, 401)
            self.assertEqual(self.request(method, section, {} if method == "PATCH" else None, token=self.token + "tampered").status_code, 401)
        for field, values in (("role", [UserRole.ADMIN, UserRole.CHEF, UserRole.MANAGER]),
                              ("is_active", [False]), ("email_verified", [False]),
                              ("status", [AccountStatus.SUSPENDED]),
                              ("password_changed_at", [datetime.now(timezone.utc) + timedelta(seconds=2)])):
            for value in values:
                with self.subTest(field=field, value=value):
                    with self.sessions() as db:
                        row = db.get(User, self.customer.id)
                        previous = getattr(row, field)
                        setattr(row, field, value)
                        db.commit()
                    for method, section in paths:
                        self.assertEqual(self.request(method, section, {} if method == "PATCH" else None).status_code, 401)
                    with self.sessions() as db:
                        setattr(db.get(User, self.customer.id), field, previous)
                        db.commit()

    def test_database_failure_returns_safe_error_and_rolls_back(self):
        self.request()
        with patch.object(Session, "commit", side_effect=SQLAlchemyError("private database details")):
            result = self.request("PATCH", "/general", {"theme": "dark"})
        self.assertEqual(result.status_code, 503)
        self.assertNotIn("private", result.json["detail"])
        self.assertEqual(self.request().json["general"]["theme"], "system")

    def emit(self, db, kind, category, **kwargs):
        return create_notification(db, db.get(User, self.customer.id), notification_type=kind, category=category,
                                   title="Test update", message="Test update", **kwargs)

    def test_optional_in_app_events_and_realtime_wakeups_respect_each_category(self):
        cases = [("reservation_confirmation", "reservation.confirmed", "RESERVATION"),
                 ("reservation_reminders", "reservation.reminder", "RESERVATION"),
                 ("reservation_updates", "reservation.cancelled", "RESERVATION"),
                 ("order_confirmation", "order.placed", "ORDER"),
                 ("order_status_updates", "order.preparing", "ORDER"),
                 ("order_completion", "order.completed", "ORDER"),
                 ("payment_confirmation", "payment.success", "PAYMENT"),
                 ("refund_updates", "payment.refunded", "PAYMENT"),
                 ("wallet_transactions", "wallet.transaction", "PAYMENT"),
                 ("review_updates", "review.moderated", "REVIEW")]
        for field, kind, category in cases:
            self.request("PATCH", "/notifications", {field: False})
            with self.sessions() as db:
                self.assertIsNone(self.emit(db, kind, category), kind)
                self.assertEqual(db.query(RealtimeEvent).count(), 0)
            self.request("PATCH", "/notifications", {field: True})
            with self.sessions() as db:
                self.assertIsNotNone(self.emit(db, kind, category), kind)
                self.assertEqual(db.query(Notification).count(), 1)
                # Session close rolls the test event back, as production owns the transaction.

    def test_global_channel_opt_out_and_security_bypass(self):
        self.request("PATCH", "/notifications", {"in_app_enabled": False, "email_enabled": False})
        with self.sessions() as db:
            self.assertIsNone(self.emit(db, "order.placed", "ORDER"))
            self.assertIsNotNone(self.emit(db, "security.password_changed", "SYSTEM"))
            self.assertIsNotNone(self.emit(db, "legal.receipt", "PAYMENT"))
            self.assertIsNotNone(self.emit(db, "payment.failed", "PAYMENT"))

    def test_promotions_require_opt_in_and_respect_both_channel_switches(self):
        with self.sessions() as db:
            self.assertIsNone(self.emit(db, "promotion.offer", "PROMOTION"))
        self.request("PATCH", "/notifications", {"promotional_offers": True})
        with self.sessions() as db:
            self.assertIsNotNone(self.emit(db, "promotion.offer", "PROMOTION"))
        self.request("PATCH", "/notifications", {"in_app_enabled": False})
        with self.sessions() as db:
            self.assertIsNone(self.emit(db, "promotion.offer", "PROMOTION"))

    def test_refund_transactions_use_refund_switch_not_wallet_switch(self):
        self.request("PATCH", "/notifications", {"wallet_transactions": False})
        with self.sessions() as db:
            self.assertIsNotNone(self.emit(db, "wallet.transaction", "PAYMENT", metadata={"transaction_type": "REFUND"}))
        self.request("PATCH", "/notifications", {"refund_updates": False, "wallet_transactions": True})
        with self.sessions() as db:
            self.assertIsNone(self.emit(db, "wallet.transaction", "PAYMENT", metadata={"transaction_type": "REFUND"}))

    def test_email_policy_skips_marketing_and_preserves_account_protection(self):
        with self.sessions() as db, patch("app.services.customer_email.send_branded_email") as smtp:
            customer = db.get(User, self.customer.id)
            self.assertFalse(send_customer_notification_email(db, customer, notification_type="marketing.offer", category="PROMOTION", subject="Offer"))
            smtp.assert_not_called()
        self.request("PATCH", "/notifications", {"promotional_offers": True})
        with self.sessions() as db, patch("app.services.customer_email.send_branded_email") as smtp:
            self.assertTrue(send_customer_notification_email(db, db.get(User, self.customer.id), notification_type="marketing.offer", category="PROMOTION", subject="Offer"))
            smtp.assert_called_once()
        self.request("PATCH", "/notifications", {"email_enabled": False})
        with self.sessions() as db, patch("app.services.customer_email.send_branded_email") as smtp:
            customer = db.get(User, self.customer.id)
            self.assertFalse(send_customer_notification_email(db, customer, notification_type="marketing.offer", category="PROMOTION", subject="Offer"))
            self.assertTrue(send_customer_notification_email(db, customer, notification_type="security.alert", category="SYSTEM", subject="Security"))
            smtp.assert_called_once()
        with patch("app.routers.auth.customer_auth.send_branded_email") as smtp:
            from app.routers.auth.customer_auth import send_password_reset_email
            send_password_reset_email(self.customer.email, "123456")
            smtp.assert_called_once()

    def test_existing_reservation_email_path_skips_attachments_and_smtp_when_opted_out(self):
        with self.sessions() as db:
            reservation = Reservation(user_id=self.customer.id, restaurant_id=self.restaurant.id,
                reservation_date=date(2026, 11, 20), start_time=time(20), end_time=time(21), number_of_guests=2,
                status="CONFIRMED", payment_status="PAID", fee_amount=100)
            db.add(reservation)
            db.commit()
            reservation_id = reservation.id
        for changes in ({"email_enabled": False}, {"email_enabled": True, "reservation_confirmation": False}):
            self.request("PATCH", "/notifications", changes)
            with self.sessions() as db, patch("app.services.reservation_email.build_reservation_invoice_pdf") as render, patch("app.services.customer_email.send_branded_email") as smtp:
                self.assertFalse(send_reservation_confirmation(db, reservation_id))
                render.assert_not_called()
                smtp.assert_not_called()
                self.assertIsNone(db.get(Reservation, reservation_id).confirmation_email_sent_at)

    def test_review_reply_delivery_respects_preference(self):
        with self.sessions() as db:
            review = Review(customer_id=self.customer.id, restaurant_id=self.restaurant.id, rating=5, content="Excellent service")
            db.add(review)
            db.commit()
            review_id = review.id
        self.request("PATCH", "/notifications", {"review_updates": False})
        with self.sessions() as db:
            reply_to_review(review_id, ReplyPayload(content="Thank you for visiting"), db.get(User, self.manager.id), db)
            self.assertEqual(db.query(Notification).count(), 0)
        self.request("PATCH", "/notifications", {"review_updates": True})
        with self.sessions() as db:
            reply_to_review(review_id, ReplyPayload(content="We hope to see you again"), db.get(User, self.manager.id), db)
            self.assertEqual(db.query(Notification).one().notification_type, "review.reply")

    def test_reservation_calendar_dates_are_preserved_and_activity_instants_are_available(self):
        with self.sessions() as db:
            reservation = Reservation(user_id=self.customer.id, restaurant_id=self.restaurant.id,
                reservation_date=date(2026, 11, 20), start_time=time(20, 15), end_time=time(21, 15),
                number_of_guests=2, status="CONFIRMED", payment_status="PAID", fee_amount=100,
                created_at=datetime(2026, 11, 13, 0, 30, tzinfo=timezone.utc))
            db.add(reservation)
            db.flush()
            data = reservation_data(reservation, db)
            self.assertEqual(data["reservation_date"], "20 November 2026")
            self.assertEqual(data["start_time"], "20:15")
            self.assertEqual(data["created_at"], "13 November 2026")
            self.assertEqual(data["timestamps"]["created_at"], "2026-11-13T00:30:00+00:00")

    def test_legacy_api_and_granular_settings_remain_consistent(self):
        self.request()
        result = self.client.request("PATCH", "/api/notifications/preferences", token=self.token, payload={"preferences": {"ORDER": False}})
        self.assertEqual(result.status_code, 200)
        self.assertFalse(result.json["preferences"]["ORDER"])
        self.assertFalse(self.request().json["notifications"]["order_completion"])
        self.request("PATCH", "/notifications", {"order_confirmation": True, "order_status_updates": True, "order_completion": True})
        result = self.client.request("GET", "/api/notifications/preferences", token=self.token)
        self.assertTrue(result.json["preferences"]["ORDER"])


class SettingsMigrationTests(unittest.TestCase):
    def test_upgrade_and_downgrade_preserve_existing_customers_and_notification_opt_outs(self):
        engine = create_engine("sqlite://")
        self.addCleanup(engine.dispose)
        User.__table__.create(engine)
        NotificationPreference.__table__.create(engine)
        with Session(engine) as db:
            customer = User(name="Existing Customer", email="existing@example.test", password_hash="hash", role=UserRole.CUSTOMER)
            db.add(customer)
            db.flush()
            db.add(NotificationPreference(user_id=customer.id, category="ORDER", enabled=False))
            db.commit()
        path = Path(__file__).parents[1] / "alembic/versions/0053_customer_settings.py"
        spec = importlib.util.spec_from_file_location("settings_migration", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with engine.begin() as connection, patch.object(module, "op", Operations(MigrationContext.configure(connection))):
            module.upgrade()
            self.assertIn("customer_settings", inspect(connection).get_table_names())
            self.assertEqual(inspect(connection).get_pk_constraint("customer_settings")["constrained_columns"], ["customer_id"])
            module.downgrade()
            self.assertNotIn("customer_settings", inspect(connection).get_table_names())
        with Session(engine) as db:
            self.assertEqual(db.query(User).count(), 1)
            self.assertFalse(db.query(NotificationPreference).one().enabled)
