import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from tests import sqlite_support  # noqa: F401
from tests.test_customer_profile import ASGIClient, multipart, picture
from app.core.config import settings
from app.core.security import create_access_token
from app.db.database import Base, get_db
from app.main import app
from app.models.restaurant import Restaurant, RestaurantStatus
from app.models.user import User, UserRole

PATH = "/api/chef/profile"


class ChefProfileTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        self.addCleanup(self.engine.dispose)
        with self.sessions() as db:
            self.manager = User(name="Manager", email="manager@example.com", password_hash="private", role=UserRole.MANAGER)
            self.customer = User(name="Customer", email="customer@example.com", password_hash="private", role=UserRole.CUSTOMER)
            db.add_all([self.manager, self.customer]); db.flush()
            self.chef = User(name="Chef Name", email="chef@example.com", password_hash="private", role=UserRole.CHEF, manager_id=self.manager.id,
                             phone="+91 9876543210", avatar_url="https://example.com/old.jpg", avatar_public_id="private-old")
            self.other = User(name="Other Chef", email="other@example.com", password_hash="private", role=UserRole.CHEF, manager_id=self.manager.id)
            self.restaurant = Restaurant(manager_id=self.manager.id, name="Assigned Kitchen", address="Main Street", city="Pune", cuisine_type="Indian", status=RestaurantStatus.APPROVED)
            db.add_all([self.chef, self.other, self.restaurant]); db.commit()

        def override():
            with self.sessions() as db:
                yield db
        app.dependency_overrides[get_db] = override
        self.addCleanup(app.dependency_overrides.pop, get_db)
        guard = patch.object(settings, "rate_limit_enabled", False); guard.start(); self.addCleanup(guard.stop)
        self.client = ASGIClient()
        self.token = create_access_token(str(self.chef.id))[0]

    def request(self, method="GET", suffix="", payload=None, token=None, **kwargs):
        return self.client.request(method, PATH + suffix, token=token or self.token, payload=payload, **kwargs)

    def upload(self, content=None, mime="image/png"):
        content, media = multipart(picture() if content is None else content, mime)
        return self.request("POST", "/avatar", content=content, content_type=media)

    def storage(self):
        return patch.multiple(settings, cloudinary_cloud_name="test", cloudinary_api_key="test", cloudinary_api_secret="test")

    def test_profile_returns_own_account_and_assigned_restaurant_without_secrets(self):
        result = self.request()
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.headers["cache-control"], "no-store")
        self.assertEqual(result.json["chef"]["id"], str(self.chef.id))
        self.assertEqual(result.json["restaurant"]["id"], str(self.restaurant.id))
        self.assertEqual(result.json["restaurant"]["name"], "Assigned Kitchen")
        for field in ("password_hash", "avatar_public_id", "manager_id", "must_change_password", "chef_invite_expires_at"):
            self.assertNotIn(field, result.json["chef"])
        for field in ("documents", "manager_id", "latitude", "longitude"):
            self.assertNotIn(field, result.json["restaurant"])

    def test_name_and_phone_persist_only_for_authenticated_chef(self):
        result = self.request("PATCH", payload={"name": "  Anne   O'Neil ", "phone": " +91 98765 43210 "})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json["name"], "Anne O'Neil")
        self.assertEqual(result.json["phone"], "+91 98765 43210")
        self.assertEqual(self.request().json["chef"]["name"], "Anne O'Neil")
        self.assertEqual(self.client.request("GET", "/api/chef/me", token=self.token).json["name"], "Anne O'Neil")
        with self.sessions() as db:
            self.assertEqual(db.get(User, self.other.id).name, "Other Chef")
            self.assertEqual(db.get(User, self.manager.id).name, "Manager")
            self.assertEqual(db.get(Restaurant, self.restaurant.id).name, "Assigned Kitchen")
        self.assertIsNone(self.request("PATCH", payload={"phone": ""}).json["phone"])
        self.assertEqual(self.request("PATCH", payload={}).json["name"], "Anne O'Neil")

    def test_privileged_identity_fields_and_restaurant_edits_are_rejected(self):
        for field in ("id", "chef_id", "email", "role", "status", "is_active", "manager_id", "password_hash", "avatar_url", "avatar_public_id", "created_at"):
            with self.subTest(field=field):
                self.assertEqual(self.request("PATCH", payload={field: str(self.other.id)}).status_code, 422)
        self.assertEqual(self.request("PATCH", "/restaurant", {"name": "Changed"}).status_code, 404)

    def test_invalid_names_and_phones_are_rejected(self):
        for payload in ({"name": None}, {"name": " "}, {"name": "A"}, {"name": "Chef123"}, {"name": "x" * 121},
                        {"phone": 1234567}, {"phone": "123"}, {"phone": "+91 0000000000"}, {"phone": "abc9876543210"}):
            with self.subTest(payload=payload):
                self.assertEqual(self.request("PATCH", payload=payload).status_code, 422)

    def test_authentication_and_wrong_roles_are_rejected_on_every_endpoint(self):
        for method, suffix in (("GET", ""), ("PATCH", ""), ("POST", "/avatar"), ("DELETE", "/avatar")):
            self.assertEqual(self.client.request(method, PATH + suffix, payload={} if method == "PATCH" else None).status_code, 401)
            for token in ("invalid", create_access_token(str(self.manager.id))[0], create_access_token(str(self.customer.id))[0]):
                self.assertEqual(self.request(method, suffix, {} if method == "PATCH" else None, token=token).status_code, 401)

    def test_temporary_password_suspension_inactivity_and_revocation_are_enforced(self):
        for condition in ("temporary", "suspended", "inactive", "revoked"):
            with self.sessions() as db:
                chef = db.get(User, self.chef.id)
                chef.must_change_password = condition == "temporary"
                chef.is_active = condition != "inactive"
                chef.password_changed_at = datetime.now(timezone.utc) + timedelta(minutes=1) if condition == "revoked" else None
                db.get(Restaurant, self.restaurant.id).status = RestaurantStatus.SUSPENDED if condition == "suspended" else RestaurantStatus.APPROVED
                db.commit()
            for method, suffix in (("GET", ""), ("PATCH", ""), ("POST", "/avatar"), ("DELETE", "/avatar")):
                with self.subTest(condition=condition, method=method):
                    self.assertEqual(self.request(method, suffix, {} if method == "PATCH" else None).status_code, 403 if condition in {"temporary", "suspended"} else 401)

    def test_avatar_upload_and_removal_update_session_identity_and_owned_storage(self):
        with self.storage(), patch("app.services.customer_profile.upload_file", return_value={"public_id": "new-avatar", "secure_url": "https://example.com/new.jpg"}) as upload, patch("app.services.customer_profile.delete_file", return_value={"result": "ok"}) as delete:
            result = self.upload()
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json["avatar_url"], "https://example.com/new.jpg")
            self.assertEqual(upload.call_args.kwargs["folder"], f"dinebook/chef-avatars/{self.chef.id}")
            delete.assert_called_once_with("private-old", resource_type="image")
            session = self.client.request("GET", "/api/chef/me", token=self.token).json
            self.assertEqual(session["avatar_url"], "https://example.com/new.jpg")
            self.assertEqual(session["phone"], "+91 9876543210")
            self.assertIsNone(self.request("DELETE", "/avatar").json["avatar_url"])
            delete.assert_called_with("new-avatar", resource_type="image")
            self.assertIsNone(self.request().json["chef"]["avatar_url"])

    def test_photo_validation_and_unconfigured_storage_preserve_existing_avatar(self):
        for content, mime, status in ((b"not an image", "image/png", 400), (b"<svg/>", "image/svg+xml", 400), (b"x" * (5 * 1024 * 1024 + 1), "image/png", 413)):
            self.assertEqual(self.upload(content, mime).status_code, status)
        with patch.multiple(settings, cloudinary_cloud_name=None, cloudinary_api_key=None, cloudinary_api_secret=None):
            self.assertEqual(self.upload().status_code, 503)
        self.assertEqual(self.request().json["chef"]["avatar_url"], self.chef.avatar_url)

    def test_database_failures_roll_back_details_and_new_avatar(self):
        with patch.object(Session, "commit", side_effect=SQLAlchemyError("private database information")):
            result = self.request("PATCH", payload={"name": "New Name"})
        self.assertEqual(result.status_code, 503)
        self.assertNotIn("private", result.json["detail"])
        with self.storage(), patch("app.services.customer_profile.upload_file", return_value={"public_id": "failed-avatar", "secure_url": "https://example.com/new.jpg"}), patch("app.services.customer_profile.delete_file", return_value={"result": "ok"}) as delete, patch.object(Session, "commit", side_effect=SQLAlchemyError("private")):
            self.assertEqual(self.upload().status_code, 502)
            delete.assert_called_once_with("failed-avatar", resource_type="image")
        result = self.request().json["chef"]
        self.assertEqual(result["name"], "Chef Name")
        self.assertEqual(result["avatar_url"], self.chef.avatar_url)

    def test_unassigned_chef_and_legacy_email_are_displayable(self):
        with self.sessions() as db:
            chef = db.get(User, self.chef.id)
            chef.manager_id = None
            chef.email = "chef@localhost"
            db.commit()
        result = self.request()
        self.assertEqual(result.status_code, 200)
        self.assertIsNone(result.json["restaurant"])
        self.assertEqual(result.json["chef"]["email"], "chef@localhost")
