import unittest
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from unittest.mock import patch

from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from tests import sqlite_support  # noqa: F401
from tests.test_customer_profile import ASGIClient
from app.core.config import settings
from app.core.security import create_access_token, decode_access_token, hash_password, validate_new_password, verify_secret
from app.db.database import Base, get_db
from app.main import app
from app.models.restaurant import Restaurant, RestaurantStatus
from app.models.user import AccountStatus, User, UserRole
from app.routers.auth.chef_auth import ChefChangePasswordRequest
from app.routers.auth.customer_auth import CustomerPasswordResetRequest
from app.routers.auth.password_recovery import RecoveryResetRequest
from app.schemas.customer import CustomerRegisterRequest
from app.schemas.manager import ManagerRegisterRequest
from app.schemas.password_change import PasswordChangeRequest

OLD = "Original123!"
NEW = "Fresh8!x"
ROLES = ("admin", "manager", "chef", "customer")


class PasswordLengthTests(unittest.TestCase):
    def test_shared_policy_accepts_eight_characters_and_rejects_seven(self):
        self.assertEqual(validate_new_password(NEW), NEW)
        with self.assertRaises(ValueError):
            validate_new_password("Valid7!")

    def test_registration_change_and_reset_schemas_share_the_length_limits(self):
        for password in ("Valid7!", NEW, "Fresh8!xy", "a1" * 64, "a1" * 64 + "a"):
            cases = [
                (CustomerRegisterRequest, {"name": "Asha Rao", "email": "asha@example.com",
                    "password": password, "confirm_password": password}),
                (ManagerRegisterRequest, {"name": "Manager", "email": "manager@example.com",
                    "password": password, "restaurant_name": "Kitchen", "cuisine_type": "Indian",
                    "restaurant_contact": "9876543210", "address": "Market Road", "city": "Pune",
                    "state": "Maharashtra", "pin_code": "411001"}),
                (ChefChangePasswordRequest, {"password": password, "confirm_password": password}),
                (PasswordChangeRequest, {"current_password": OLD, "new_password": password,
                    "confirm_password": password}),
                (CustomerPasswordResetRequest, {"email": "asha@example.com", "code": "123456",
                    "new_password": password}),
                (RecoveryResetRequest, {"email": "manager@example.com", "code": "123456",
                    "new_password": password}),
            ]
            for model, payload in cases:
                with self.subTest(model=model.__name__, length=len(password)):
                    if 8 <= len(password) <= 128:
                        model(**payload)
                    else:
                        with self.assertRaises(ValidationError):
                            model(**payload)


class PasswordChangeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original_hash = hash_password(OLD)

    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        self.addCleanup(self.engine.dispose)
        self.accounts = {}
        with self.sessions() as db:
            for role in ROLES:
                self.accounts[role] = User(
                    name=role.title(), email=f"{role}@example.com", password_hash=self.original_hash,
                    role=UserRole(role.upper()),
                )
            db.add_all(self.accounts.values())
            db.flush()
            self.accounts["chef"].manager_id = self.accounts["manager"].id
            self.restaurant = Restaurant(
                manager_id=self.accounts["manager"].id, name="Password test kitchen",
                address="Street", city="Pune", status=RestaurantStatus.APPROVED,
            )
            db.add(self.restaurant)
            db.commit()
        self.tokens = {role: create_access_token(str(account.id))[0] for role, account in self.accounts.items()}

        def override():
            with self.sessions() as db:
                yield db
        app.dependency_overrides[get_db] = override
        self.addCleanup(app.dependency_overrides.pop, get_db)
        guard = patch.object(settings, "rate_limit_enabled", False)
        guard.start()
        self.addCleanup(guard.stop)
        self.client = ASGIClient()

    def payload(self, role, **changes):
        values = {"current_password": OLD, "new_password": NEW, "confirm_password": NEW, **changes}
        if role == "chef":
            values["password"] = values.pop("new_password")
        return values

    def change(self, role, payload=None, token=None, headers=None):
        return self.client.request(
            "POST", f"/api/auth/{role}/change-password", token=token or self.tokens[role],
            payload=self.payload(role) if payload is None else payload, headers=headers,
        )

    def settings(self, role, token):
        return self.client.request("GET", f"/api/{role}/settings", token=token)

    def renewed_token(self, response):
        cookie = SimpleCookie()
        cookie.load(response.headers["set-cookie"])
        self.assertTrue(cookie["dinebook_access_token"]["httponly"])
        self.assertEqual(cookie["dinebook_access_token"]["samesite"].lower(), "lax")
        self.assertEqual(cookie["dinebook_access_token"]["path"], "/")
        return cookie["dinebook_access_token"].value

    def test_every_role_can_change_to_eight_characters_and_continue_with_a_renewed_session(self):
        for role in ROLES:
            with self.subTest(role=role):
                original_settings = self.settings(role, self.tokens[role]).json
                result = self.change(role)
                self.assertEqual(result.status_code, 200)
                self.assertEqual(result.headers["cache-control"], "no-store")
                renewed = self.renewed_token(result)
                self.assertNotEqual(renewed, self.tokens[role])
                self.assertEqual(self.settings(role, self.tokens[role]).status_code, 401)
                continued = self.settings(role, renewed)
                self.assertEqual(continued.status_code, 200)
                self.assertEqual(continued.json, original_settings)
                with self.sessions() as db:
                    account = db.get(User, self.accounts[role].id)
                    self.assertTrue(verify_secret(NEW, account.password_hash))
                    self.assertFalse(verify_secret(OLD, account.password_hash))
                    self.assertIsNotNone(account.password_changed_at)
                    for other in ROLES:
                        if other not in ROLES[:ROLES.index(role) + 1]:
                            self.assertEqual(db.get(User, self.accounts[other].id).password_hash, self.original_hash)
                for password, expected in [(OLD, 401), (NEW, 200)]:
                    login = self.client.request(
                        "POST", f"/api/auth/{role}/login",
                        payload={"email": self.accounts[role].email, "password": password},
                    )
                    self.assertEqual(login.status_code, expected)
                    if role == "customer" and expected == 200:
                        self.assertTrue(login.json["can_change_password"])

    def test_wrong_current_password_leaves_credentials_and_sessions_unchanged(self):
        for role in ROLES:
            result = self.change(role, self.payload(role, current_password="Incorrect123!"))
            self.assertEqual(result.status_code, 400)
            self.assertNotIn("set-cookie", result.headers)
            self.assertEqual(self.settings(role, self.tokens[role]).status_code, 200)
            with self.sessions() as db:
                self.assertEqual(db.get(User, self.accounts[role].id).password_hash, self.original_hash)

    def test_password_policy_confirmation_and_account_mass_assignment_are_validated(self):
        for role in ROLES:
            for changes in [
                {"current_password": ""}, {"new_password": "Valid7!", "confirm_password": "Valid7!"},
                {"new_password": "letterswithoutnumbers", "confirm_password": "letterswithoutnumbers"},
                {"new_password": "123456789012", "confirm_password": "123456789012"},
                {"new_password": " " * 12, "confirm_password": " " * 12},
                {"new_password": "a1" * 65, "confirm_password": "a1" * 65},
                {"confirm_password": "Different789!"}, {"new_password": OLD, "confirm_password": OLD},
                {"new_password": None}, {"current_password": 123},
                {"user_id": str(self.accounts["customer"].id)},
            ]:
                with self.subTest(role=role, fields=list(changes)):
                    self.assertEqual(self.change(role, self.payload(role, **changes)).status_code, 422)

    def test_missing_authentication_and_wrong_portals_cannot_change_passwords(self):
        for role in ROLES:
            path = f"/api/auth/{role}/change-password"
            self.assertEqual(self.client.request("POST", path, payload=self.payload(role)).status_code, 401)
            for other in ROLES:
                if other != role:
                    self.assertEqual(self.change(role, token=self.tokens[other]).status_code, 401)

    def test_inactive_accounts_and_revoked_sessions_are_rejected(self):
        for role in ROLES:
            for condition in ("inactive", "suspended", "unverified", "revoked"):
                with self.subTest(role=role, condition=condition):
                    with self.sessions() as db:
                        account = db.get(User, self.accounts[role].id)
                        account.is_active = condition != "inactive"
                        account.email_verified = condition != "unverified"
                        account.status = AccountStatus.SUSPENDED if condition == "suspended" else AccountStatus.ACTIVE
                        account.password_changed_at = datetime.now(timezone.utc) + timedelta(seconds=1) if condition == "revoked" else None
                        db.commit()
                    self.assertEqual(self.change(role).status_code, 401)

    def test_suspended_restaurants_cannot_change_manager_or_chef_passwords(self):
        with self.sessions() as db:
            db.get(Restaurant, self.restaurant.id).status = RestaurantStatus.SUSPENDED
            db.commit()
        for role in ("manager", "chef"):
            self.assertEqual(self.change(role).status_code, 403)

    def test_failed_updates_roll_back_without_rotating_the_session_or_exposing_database_details(self):
        for role in ROLES:
            with patch.object(Session, "commit", side_effect=SQLAlchemyError("private database details")):
                result = self.change(role)
            self.assertEqual(result.status_code, 503)
            self.assertNotIn("private", result.json["detail"])
            self.assertNotIn("set-cookie", result.headers)
            with self.sessions() as db:
                account = db.get(User, self.accounts[role].id)
                self.assertEqual(account.password_hash, self.original_hash)
                self.assertIsNone(account.password_changed_at)
            self.assertEqual(self.settings(role, self.tokens[role]).status_code, 200)

    def test_existing_chef_initial_setup_payload_still_works_without_current_password(self):
        with self.sessions() as db:
            db.get(User, self.accounts["chef"].id).must_change_password = True
            db.commit()
        result = self.change("chef", {"password": NEW})
        self.assertEqual(result.status_code, 200)
        renewed = self.renewed_token(result)
        self.assertEqual(self.settings("chef", renewed).status_code, 200)
        with self.sessions() as db:
            self.assertFalse(db.get(User, self.accounts["chef"].id).must_change_password)

    def test_normal_chef_change_requires_current_password(self):
        self.assertEqual(self.change("chef", {"password": NEW, "confirm_password": NEW}).status_code, 400)

    def test_customer_password_sign_in_method_is_preserved_when_renewing_the_cookie(self):
        token = create_access_token(str(self.accounts["customer"].id), auth_method="password")[0]
        result = self.change("customer", token=token)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(decode_access_token(self.renewed_token(result))["auth_method"], "password")

    def test_customer_session_and_profile_expose_password_change_availability(self):
        for method in ("google", "password", None):
            token = create_access_token(str(self.accounts["customer"].id), auth_method=method)[0]
            for path in ("/api/customer/me", "/api/customer/profile"):
                with self.subTest(method=method, path=path):
                    result = self.client.request("GET", path, token=token)
                    self.assertEqual(result.status_code, 200)
                    self.assertEqual(result.json["can_change_password"], method != "google")
                    self.assertNotIn("auth_method", result.json)

    def test_google_sessions_cannot_change_passwords_even_with_the_correct_current_password(self):
        token = create_access_token(str(self.accounts["customer"].id), auth_method="google")[0]
        result = self.change("customer", token=token)
        self.assertEqual(result.status_code, 403)
        self.assertIn("Google account", result.json["detail"])
        self.assertNotIn("set-cookie", result.headers)
        self.assertEqual(self.settings("customer", token).status_code, 200)
        with self.sessions() as db:
            account = db.get(User, self.accounts["customer"].id)
            self.assertEqual(account.password_hash, self.original_hash)
            self.assertIsNone(account.password_changed_at)

    def test_untrusted_origins_cannot_submit_password_changes(self):
        for role in ROLES:
            result = self.change(role, headers={"origin": "https://untrusted.example"})
            self.assertEqual(result.status_code, 403)
            with self.sessions() as db:
                self.assertEqual(db.get(User, self.accounts[role].id).password_hash, self.original_hash)
