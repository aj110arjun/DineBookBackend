import unittest
import uuid
from decimal import Decimal
from types import SimpleNamespace
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
from app.models.restaurant import Restaurant, RestaurantStatus
from app.models.user import User, UserRole
from app.models.wallet import WalletTransaction
from app.schemas.chef_settings import ChefNotificationSettings
from app.schemas.customer_settings import GeneralSettings
from app.services.notifications import create_notification, notify_order_status, notify_wallet_transaction

PATH = "/api/chef/settings"


class ChefSettingsTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        self.addCleanup(self.engine.dispose)
        with self.sessions() as db:
            self.manager = User(name="Manager", email="manager@example.test", password_hash="hash", role=UserRole.MANAGER)
            self.customer = User(name="Customer", email="customer@example.test", password_hash="hash", role=UserRole.CUSTOMER)
            db.add_all([self.manager, self.customer])
            db.flush()
            self.chef = User(name="Chef", email="chef@example.test", password_hash="hash", role=UserRole.CHEF, manager_id=self.manager.id)
            self.other = User(name="Other chef", email="other@example.test", password_hash="hash", role=UserRole.CHEF, manager_id=self.manager.id)
            self.restaurant = Restaurant(manager_id=self.manager.id, name="Kitchen", address="Street", city="Pune", status=RestaurantStatus.APPROVED)
            db.add_all([self.chef, self.other, self.restaurant])
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
        self.token = create_access_token(str(self.chef.id))[0]

    def request(self, method="GET", section="", payload=None, token=None):
        return self.client.request(method, PATH + section, payload=payload, token=token or self.token)

    def emit(self, db, kind, category, metadata=None):
        return create_notification(db, db.get(User, self.chef.id), notification_type=kind, category=category,
                                   title="Chef update", message="Kitchen activity", metadata=metadata)

    def test_defaults_initialize_once_and_responses_are_private(self):
        for _ in range(2):
            result = self.request()
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json, {"notifications": ChefNotificationSettings().model_dump(), "general": GeneralSettings().model_dump()})
            self.assertEqual(result.headers["cache-control"], "no-store")
        with self.sessions() as db:
            self.assertEqual(db.query(CustomerSettings).count(), 1)
            self.assertEqual(db.query(NotificationPreference).count(), 7)
            self.assertIsNotNone(db.get(CustomerSettings, self.chef.id))

    def test_preferences_persist_per_chef_and_do_not_change_other_portals(self):
        general = {"theme": "dark", "language": "en", "date_format": "YYYY-MM-DD", "time_format": "24h", "timezone": "America/New_York"}
        self.assertEqual(self.request("PATCH", "/general", general).json["general"], general)
        self.assertEqual(self.request().json["general"], general)
        self.request("PATCH", "/notifications", {"order_status_updates": False})
        other = create_access_token(str(self.other.id))[0]
        other_settings = self.request(token=other).json
        self.assertEqual(other_settings["general"], GeneralSettings().model_dump())
        self.assertTrue(other_settings["notifications"]["order_status_updates"])
        self.assertIsNone(self.request("PATCH", "/general", {"timezone": None}).json["general"]["timezone"])
        with self.sessions() as db:
            self.assertIsNone(db.get(CustomerSettings, self.manager.id))
            self.assertIsNone(db.get(CustomerSettings, self.customer.id))

    def test_partial_updates_preserve_legacy_category_opt_outs(self):
        with self.sessions() as db:
            db.add(NotificationPreference(user_id=self.chef.id, category="ORDER", enabled=False))
            db.commit()
        values = self.request().json["notifications"]
        self.assertFalse(values["order_confirmation"])
        self.assertFalse(values["order_completion"])
        changed = self.request("PATCH", "/notifications", {"order_confirmation": True}).json["notifications"]
        self.assertTrue(changed["order_confirmation"])
        self.assertFalse(changed["order_status_updates"])
        self.assertTrue(changed["wallet_transactions"])
        self.assertEqual(self.request().json["notifications"], changed)

    def test_each_switch_gates_notification_and_realtime_delivery(self):
        cases = [("order_confirmation", "order.placed", "ORDER"),
                 ("order_status_updates", "order.preparing", "ORDER"),
                 ("order_completion", "order.completed", "ORDER"),
                 ("payment_confirmation", "payment.success", "PAYMENT"),
                 ("refund_updates", "payment.refunded", "PAYMENT"),
                 ("wallet_transactions", "wallet.transaction", "PAYMENT")]
        for field, kind, category in cases:
            with self.subTest(field=field):
                self.assertEqual(self.request("PATCH", "/notifications", {field: False}).status_code, 200)
                with self.sessions() as db:
                    self.assertIsNone(self.emit(db, kind, category))
                    self.assertEqual(db.query(Notification).count(), 0)
                    self.assertEqual(db.query(RealtimeEvent).count(), 0)
                self.request("PATCH", "/notifications", {field: True})
                with self.sessions() as db:
                    self.assertIsNotNone(self.emit(db, kind, category))
                    event = db.query(RealtimeEvent).one()
                    self.assertEqual(event.user_id, self.chef.id)
                    self.assertIsNone(event.restaurant_id)

    def test_global_opt_out_keeps_essential_notices_and_existing_history(self):
        with self.sessions() as db:
            existing = self.emit(db, "order.placed", "ORDER")
            existing_id = existing.id
            db.commit()
        self.request("PATCH", "/notifications", {"in_app_enabled": False})
        with self.sessions() as db:
            self.assertIsNone(self.emit(db, "order.preparing", "ORDER"))
            self.assertIsNone(self.emit(db, "wallet.transaction", "PAYMENT"))
            self.assertIsNotNone(db.get(Notification, existing_id))
            for kind, category in [("security.alert", "SYSTEM"), ("account.updated", "SYSTEM"),
                                   ("legal.receipt", "PAYMENT"), ("restaurant.suspended", "SYSTEM"),
                                   ("payment.failed", "PAYMENT"), ("wallet.topup.failed", "PAYMENT"), ("system.incident", "SYSTEM")]:
                self.assertIsNotNone(self.emit(db, kind, category))

    def test_actual_order_producer_respects_one_chefs_preferences_only(self):
        self.request("PATCH", "/notifications", {"order_status_updates": False})
        order = SimpleNamespace(id=uuid.uuid4(), restaurant_id=self.restaurant.id, customer_id=self.customer.id, status="PREPARING")
        with self.sessions() as db:
            notify_order_status(db, order)
            recipients = {item.recipient_id for item in db.query(Notification).all()}
            self.assertEqual(recipients, {self.other.id, self.manager.id, self.customer.id})
            self.assertEqual({event.user_id for event in db.query(RealtimeEvent).all()}, recipients)

    def test_wallet_producer_distinguishes_refunds_and_credits(self):
        self.request("PATCH", "/notifications", {"refund_updates": False, "wallet_transactions": True})
        with self.sessions() as db:
            for kind in ("REFUND", "CREDIT"):
                transaction = WalletTransaction(user_id=self.chef.id, amount=Decimal("10.00"), transaction_type=kind, description="Test transfer")
                db.add(transaction)
                notify_wallet_transaction(db, transaction)
            items = db.query(Notification).all()
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0].metadata_json["transaction_type"], "CREDIT")
            self.assertEqual(items[0].action_url, "/chef/revenue")

    def test_legacy_preferences_api_and_granular_settings_stay_consistent(self):
        self.request()
        result = self.client.request("PATCH", "/api/notifications/preferences", token=self.token, payload={"preferences": {"ORDER": False, "PAYMENT": False}})
        self.assertEqual(result.status_code, 200)
        values = self.request().json["notifications"]
        self.assertFalse(values["order_completion"])
        self.assertFalse(values["wallet_transactions"])
        self.request("PATCH", "/notifications", {"order_confirmation": True, "order_status_updates": True, "order_completion": True})
        result = self.client.request("GET", "/api/notifications/preferences", token=self.token)
        self.assertTrue(result.json["preferences"]["ORDER"])
        self.assertFalse(result.json["preferences"]["PAYMENT"])

    def test_authentication_and_wrong_roles_are_rejected_for_all_endpoints(self):
        for method, section in [("GET", ""), ("PATCH", "/general"), ("PATCH", "/notifications")]:
            payload = {} if method == "PATCH" else None
            self.assertEqual(self.client.request(method, PATH + section, payload=payload).status_code, 401)
            for token in ("invalid", create_access_token(str(self.customer.id))[0], create_access_token(str(self.manager.id))[0]):
                self.assertEqual(self.request(method, section, payload, token=token).status_code, 401)

    def test_temporary_password_inactive_accounts_and_suspended_restaurants_are_blocked(self):
        for condition in ("must_change_password", "inactive", "suspended"):
            with self.subTest(condition=condition):
                with self.sessions() as db:
                    chef = db.get(User, self.chef.id)
                    chef.must_change_password = condition == "must_change_password"
                    chef.is_active = condition != "inactive"
                    db.get(Restaurant, self.restaurant.id).status = RestaurantStatus.SUSPENDED if condition == "suspended" else RestaurantStatus.APPROVED
                    db.commit()
                for method, section in [("GET", ""), ("PATCH", "/general"), ("PATCH", "/notifications")]:
                    self.assertEqual(self.request(method, section, {} if method == "PATCH" else None).status_code, 401 if condition == "inactive" else 403)

    def test_strict_values_and_account_mass_assignment(self):
        for section, payload in [("/general", {"theme": "invalid"}), ("/general", {"theme": None}), ("/general", {"language": "fr"}),
                ("/general", {"date_format": "D/M"}), ("/general", {"time_format": "25h"}), ("/general", {"timezone": "Mars/Olympus"}),
                ("/general", {"chef_id": str(self.other.id)}), ("/general", {"customer_id": str(self.other.id)}),
                ("/notifications", {"in_app_enabled": "false"}), ("/notifications", {"in_app_enabled": None}),
                ("/notifications", {"in_app_enabled": 0}), ("/notifications", {"security_enabled": False}),
                ("/notifications", {"email_enabled": False}), ("/notifications", {"user_id": str(self.other.id)})]:
            with self.subTest(payload=payload):
                self.assertEqual(self.request("PATCH", section, payload).status_code, 422)

    def test_failed_saves_roll_back_and_hide_database_details(self):
        self.request()
        for section, payload in [("/general", {"theme": "dark"}), ("/notifications", {"in_app_enabled": False})]:
            with patch.object(Session, "commit", side_effect=SQLAlchemyError("private database data")):
                result = self.request("PATCH", section, payload)
            self.assertEqual(result.status_code, 503)
            self.assertNotIn("private", result.json["detail"])
        self.assertEqual(self.request().json, {"notifications": ChefNotificationSettings().model_dump(), "general": GeneralSettings().model_dump()})
