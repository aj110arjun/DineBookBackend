import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

from sqlalchemy import create_engine
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
from app.models.user import AccountStatus, User, UserRole
from app.models.wallet import WalletTransaction
from app.schemas.admin_settings import AdminNotificationSettings
from app.schemas.customer_settings import GeneralSettings
from app.services.notifications import create_notification, notify_wallet_transaction

PATH = "/api/admin/settings"


class AdminSettingsTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        self.addCleanup(self.engine.dispose)
        with self.sessions() as db:
            self.admin = User(name="Admin", email="admin@example.test", password_hash="hash", role=UserRole.ADMIN)
            self.other = User(name="Other admin", email="other@example.test", password_hash="hash", role=UserRole.ADMIN)
            self.staff = [
                User(name=role.value, email=f"{role.value.lower()}@example.test", password_hash="hash", role=role)
                for role in (UserRole.CUSTOMER, UserRole.MANAGER, UserRole.CHEF)
            ]
            db.add_all([self.admin, self.other, *self.staff])
            db.commit()

        def override():
            with self.sessions() as db:
                yield db
        app.dependency_overrides[get_db] = override
        self.addCleanup(app.dependency_overrides.pop, get_db)
        guard = patch.object(settings, "rate_limit_enabled", False)
        guard.start()
        self.addCleanup(guard.stop)
        self.client = ASGIClient()
        self.token = create_access_token(str(self.admin.id))[0]

    def request(self, method="GET", section="", payload=None, token=None):
        return self.client.request(method, PATH + section, payload=payload, token=token or self.token)

    def emit(self, db, kind, category, admin_id=None):
        return create_notification(
            db, db.get(User, admin_id or self.admin.id), notification_type=kind, category=category,
            title="Admin update", message="Platform activity",
        )

    def test_defaults_initialize_once_and_responses_are_private(self):
        for _ in range(2):
            result = self.request()
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json, {
                "notifications": AdminNotificationSettings().model_dump(),
                "general": GeneralSettings().model_dump(),
            })
            self.assertEqual(result.headers["cache-control"], "no-store")
        with self.sessions() as db:
            self.assertEqual(db.query(CustomerSettings).count(), 1)
            self.assertEqual(db.query(NotificationPreference).count(), 5)
            self.assertIsNotNone(db.get(CustomerSettings, self.admin.id))

    def test_partial_preferences_persist_only_for_the_authenticated_admin(self):
        general = {
            "theme": "dark", "language": "en", "date_format": "YYYY-MM-DD",
            "time_format": "24h", "timezone": "America/New_York",
        }
        self.assertEqual(self.request("PATCH", "/general", general).json["general"], general)
        changed = self.request("PATCH", "/notifications", {"approval_updates": False}).json
        self.assertFalse(changed["notifications"]["approval_updates"])
        self.assertTrue(changed["notifications"]["review_updates"])
        self.assertEqual(self.request().json, changed)
        other_token = create_access_token(str(self.other.id))[0]
        other = self.request(token=other_token).json
        self.assertEqual(other["general"], GeneralSettings().model_dump())
        self.assertTrue(other["notifications"]["approval_updates"])
        self.assertIsNone(self.request("PATCH", "/general", {"timezone": None}).json["general"]["timezone"])
        with self.sessions() as db:
            for user in self.staff:
                self.assertIsNone(db.get(CustomerSettings, user.id))

    def test_existing_category_opt_outs_are_preserved_during_initialization(self):
        with self.sessions() as db:
            db.add(NotificationPreference(user_id=self.admin.id, category="APPROVAL", enabled=False))
            db.commit()
        values = self.request().json["notifications"]
        self.assertFalse(values["approval_updates"])
        self.assertTrue(values["review_updates"])
        self.assertTrue(self.request("PATCH", "/notifications", {"approval_updates": True}).json["notifications"]["approval_updates"])
        self.assertTrue(self.request().json["notifications"]["approval_updates"])

    def test_each_category_switch_controls_notifications_and_recipient_scoped_events(self):
        for field, kind, category in [
            ("approval_updates", "restaurant.approval_requested", "APPROVAL"),
            ("review_updates", "review.flagged", "REVIEW"),
            ("wallet_transactions", "wallet.transaction", "PAYMENT"),
            ("system_updates", "system.maintenance", "SYSTEM"),
        ]:
            with self.subTest(field=field):
                self.assertEqual(self.request("PATCH", "/notifications", {field: False}).status_code, 200)
                with self.sessions() as db:
                    self.assertIsNone(self.emit(db, kind, category))
                    self.assertEqual(db.query(Notification).count(), 0)
                    self.assertEqual(db.query(RealtimeEvent).count(), 0)
                    self.assertIsNotNone(self.emit(db, kind, category, self.other.id))
                self.request("PATCH", "/notifications", {field: True})
                with self.sessions() as db:
                    self.assertIsNotNone(self.emit(db, kind, category))
                    event = db.query(RealtimeEvent).one()
                    self.assertEqual(event.user_id, self.admin.id)
                    self.assertIsNone(event.restaurant_id)

    def test_global_opt_out_preserves_essential_notices_and_existing_history(self):
        with self.sessions() as db:
            existing_id = self.emit(db, "review.flagged", "REVIEW").id
            db.commit()
        self.request("PATCH", "/notifications", {field: False for field in AdminNotificationSettings.model_fields})
        with self.sessions() as db:
            for kind, category in [
                ("review.flagged", "REVIEW"), ("restaurant.approval_requested", "APPROVAL"),
                ("wallet.transaction", "PAYMENT"), ("system.maintenance", "SYSTEM"),
            ]:
                self.assertIsNone(self.emit(db, kind, category))
            self.assertIsNotNone(db.get(Notification, existing_id))
            for kind, category in [
                ("security.alert", "SYSTEM"), ("account.updated", "SYSTEM"),
                ("legal.receipt", "PAYMENT"), ("restaurant.suspended", "SYSTEM"),
                ("payment.failed", "PAYMENT"), ("wallet.topup.failed", "PAYMENT"), ("system.incident", "SYSTEM"),
            ]:
                self.assertIsNotNone(self.emit(db, kind, category))

    def test_wallet_producer_skips_alerts_without_dropping_financial_transactions(self):
        self.request("PATCH", "/notifications", {"wallet_transactions": False})
        with self.sessions() as db:
            for kind in ("CREDIT", "DEBIT", "REFUND"):
                transaction = WalletTransaction(
                    user_id=self.admin.id, amount=Decimal("10.00"),
                    transaction_type=kind, description="Commission wallet activity",
                )
                db.add(transaction)
                notify_wallet_transaction(db, transaction)
            db.commit()
            self.assertEqual(db.query(WalletTransaction).count(), 3)
            self.assertEqual(db.query(Notification).count(), 0)
            self.assertEqual(db.query(RealtimeEvent).count(), 0)
        self.request("PATCH", "/notifications", {"wallet_transactions": True})
        with self.sessions() as db:
            transaction = WalletTransaction(
                user_id=self.admin.id, amount=Decimal("10.00"),
                transaction_type="CREDIT", description="Commission received",
            )
            db.add(transaction)
            notify_wallet_transaction(db, transaction)
            self.assertEqual(db.query(Notification).one().action_url, "/admin/revenue")

    def test_legacy_preferences_and_admin_settings_stay_synchronized(self):
        # Exercise legacy PATCH both before and after granular initialization.
        for category, field in [("APPROVAL", "approval_updates"), ("PAYMENT", "wallet_transactions"),
                                ("REVIEW", "review_updates"), ("SYSTEM", "system_updates")]:
            result = self.client.request(
                "PATCH", "/api/notifications/preferences", token=self.token,
                payload={"preferences": {category: False}},
            )
            self.assertEqual(result.status_code, 200)
            self.assertFalse(self.request().json["notifications"][field])
            self.request("PATCH", "/notifications", {field: True})
            result = self.client.request("GET", "/api/notifications/preferences", token=self.token)
            self.assertTrue(result.json["preferences"][category])

    def test_unknown_admin_categories_retain_legacy_delivery_preferences(self):
        with self.sessions() as db:
            db.add(NotificationPreference(user_id=self.admin.id, category="STAFF", enabled=False))
            db.flush()
            self.assertIsNone(self.emit(db, "staff.updated", "STAFF"))
            self.assertIsNotNone(self.emit(db, "order.placed", "ORDER"))

    def test_unauthenticated_and_other_roles_cannot_access_any_admin_settings_endpoint(self):
        for method, section in [("GET", ""), ("PATCH", "/general"), ("PATCH", "/notifications")]:
            payload = {} if method == "PATCH" else None
            self.assertEqual(self.client.request(method, PATH + section, payload=payload).status_code, 401)
            for token in ["invalid", *[create_access_token(str(user.id))[0] for user in self.staff]]:
                self.assertEqual(self.request(method, section, payload, token=token).status_code, 401)

    def test_inactive_unverified_and_revoked_admin_sessions_cannot_change_preferences(self):
        for condition in ("inactive", "suspended", "unverified", "password_changed"):
            with self.subTest(condition=condition):
                with self.sessions() as db:
                    admin = db.get(User, self.admin.id)
                    admin.is_active = condition != "inactive"
                    admin.email_verified = condition != "unverified"
                    admin.status = AccountStatus.SUSPENDED if condition == "suspended" else AccountStatus.ACTIVE
                    admin.password_changed_at = datetime.now(timezone.utc) + timedelta(seconds=1) if condition == "password_changed" else None
                    db.commit()
                for method, section in [("GET", ""), ("PATCH", "/general"), ("PATCH", "/notifications")]:
                    self.assertEqual(self.request(method, section, {} if method == "PATCH" else None).status_code, 401)

    def test_strict_values_and_account_mass_assignment_are_rejected(self):
        for section, payload in [
            ("/general", {"theme": "invalid"}), ("/general", {"theme": None}),
            ("/general", {"language": "fr"}), ("/general", {"date_format": "D/M"}),
            ("/general", {"time_format": "25h"}), ("/general", {"timezone": "Mars/Olympus"}),
            ("/general", {"admin_id": str(self.other.id)}), ("/general", {"customer_id": str(self.other.id)}),
            ("/notifications", {"in_app_enabled": "false"}), ("/notifications", {"in_app_enabled": None}),
            ("/notifications", {"in_app_enabled": 0}), ("/notifications", {"security_enabled": False}),
            ("/notifications", {"email_enabled": False}), ("/notifications", {"user_id": str(self.other.id)}),
        ]:
            with self.subTest(payload=payload):
                self.assertEqual(self.request("PATCH", section, payload).status_code, 422)

    def test_failed_saves_roll_back_and_hide_database_details(self):
        initial = self.request().json
        for section, payload in [("/general", {"theme": "dark"}), ("/notifications", {"in_app_enabled": False})]:
            with patch.object(Session, "commit", side_effect=SQLAlchemyError("private database data")):
                result = self.request("PATCH", section, payload)
            self.assertEqual(result.status_code, 503)
            self.assertNotIn("private", result.json["detail"])
        self.assertEqual(self.request().json, initial)
