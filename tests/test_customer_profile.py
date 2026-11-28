import asyncio
import importlib.util
import io
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from alembic.migration import MigrationContext
from alembic.operations import Operations
from PIL import Image
from sqlalchemy import Column, MetaData, String, Table, create_engine, inspect, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from tests import sqlite_support  # noqa: F401
from app.core.config import settings
from app.core.security import create_access_token
from app.db.database import get_db
from app.main import app
from app.models.user import AccountStatus, User, UserRole
from app.routers.auth.customer_auth import ACCESS_COOKIE
from app.services.customer_profile import MAX_AVATAR_BYTES


class ASGIClient:
    """Full-app requests, using the project's ASGI test convention without httpx."""
    def request(self, method, path, *, token=None, payload=None, content=b"", content_type="application/json", headers=None):
        if payload is not None:
            content = json.dumps(payload).encode()
        request_headers = {"content-type": content_type, **(headers or {})}
        if token:
            request_headers["cookie"] = f"{ACCESS_COOKIE}={token}"
        scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
                 "http_version": "1.1", "method": method, "path": path, "raw_path": path.encode(),
                 "query_string": b"", "root_path": "", "scheme": "http", "server": ("test", 80),
                 "client": ("127.0.0.1", 12345),
                 "headers": [(key.encode(), value.encode()) for key, value in request_headers.items()]}
        messages = []
        consumed = False

        async def receive():
            nonlocal consumed
            if consumed:
                await asyncio.Event().wait()
            consumed = True
            return {"type": "http.request", "body": content, "more_body": False}

        async def send(message):
            messages.append(message)

        async def run():
            await asyncio.wait_for(app(scope, receive, send), timeout=5)
        asyncio.run(run())
        start = next(message for message in messages if message["type"] == "http.response.start")
        body = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
        return SimpleNamespace(status_code=start["status"], json=json.loads(body),
                               headers={key.decode(): value.decode() for key, value in start["headers"]})


def picture(format="PNG"):
    output = io.BytesIO()
    Image.new("RGB", (8, 8), "navy").save(output, format=format)
    return output.getvalue()


def multipart(content, mime):
    boundary = "dinebook-profile-test"
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="photo"\r\n'
            f'Content-Type: {mime}\r\n\r\n').encode() + content + f"\r\n--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


class CustomerProfileTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        User.__table__.create(self.engine)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        with self.sessions() as db:
            self.customer = User(name="Asha Rao", email="asha@example.com", password_hash="secret hash", role=UserRole.CUSTOMER,
                                 avatar_url="https://res.cloudinary.com/demo/old", avatar_public_id="customer/old")
            self.other = User(name="Other Customer", email="other@example.com", password_hash="other hash", role=UserRole.CUSTOMER,
                              avatar_url="https://res.cloudinary.com/demo/other", avatar_public_id="customer/other")
            db.add_all([self.customer, self.other])
            db.commit()
        def session_override():
            with self.sessions() as db:
                yield db
        app.dependency_overrides[get_db] = session_override
        self.addCleanup(app.dependency_overrides.pop, get_db)
        self.addCleanup(self.engine.dispose)
        self.rate_limit = patch.object(settings, "rate_limit_enabled", False)
        self.rate_limit.start()
        self.addCleanup(self.rate_limit.stop)
        self.client = ASGIClient()
        self.token = create_access_token(str(self.customer.id), auth_method="password")[0]

    def request(self, method="GET", path="/api/customer/profile", **kwargs):
        return self.client.request(method, path, token=self.token, **kwargs)

    def upload(self, content=None, mime="image/png"):
        body, content_type = multipart(picture() if content is None else content, mime)
        return self.request("POST", "/api/customer/profile/avatar", content=body, content_type=content_type)

    def test_profile_and_existing_me_return_safe_customer_information(self):
        for path in ("/api/customer/profile", "/api/customer/me"):
            result = self.request(path=path)
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json["name"], "Asha Rao")
            self.assertIsNone(result.json["phone"])
            self.assertTrue(result.json["created_at"])
            self.assertFalse(result.json["can_change_email"])
            self.assertTrue(result.json["can_change_password"])
            self.assertTrue({"password_hash", "avatar_public_id", "auth_method", "password_changed_at"}.isdisjoint(result.json))
            self.assertEqual(result.headers["cache-control"], "no-store")

    def test_every_endpoint_requires_authentication(self):
        for method, path in (("GET", "/api/customer/profile"), ("PATCH", "/api/customer/profile"),
                             ("POST", "/api/customer/profile/avatar"), ("DELETE", "/api/customer/profile/avatar")):
            with self.subTest(method=method):
                self.assertEqual(self.client.request(method, path).status_code, 401)
        self.assertEqual(self.client.request("GET", "/api/customer/profile", token=self.token + "tampered").status_code, 401)

    def test_other_roles_inactive_unverified_and_revoked_sessions_are_rejected(self):
        for field, values in (("role", [UserRole.ADMIN, UserRole.MANAGER, UserRole.CHEF]),
                              ("status", [AccountStatus.SUSPENDED]), ("is_active", [False]),
                              ("email_verified", [False]),
                              ("password_changed_at", [datetime.now(timezone.utc) + timedelta(seconds=1)])):
            for value in values:
                with self.subTest(field=field, value=value):
                    with self.sessions() as db:
                        user = db.get(User, self.customer.id)
                        original = getattr(user, field)
                        setattr(user, field, value)
                        db.commit()
                    for method, path in (("GET", "/api/customer/profile"), ("PATCH", "/api/customer/profile"),
                                         ("POST", "/api/customer/profile/avatar"), ("DELETE", "/api/customer/profile/avatar")):
                        self.assertEqual(self.request(method, path).status_code, 401)
                    with self.sessions() as db:
                        setattr(db.get(User, self.customer.id), field, original)
                        db.commit()

    def test_update_trims_and_persists_only_current_customer_then_clears_phone(self):
        response = self.request("PATCH", payload={"name": "  Anne   O'Neil  ", "phone": "  +91 98765 43210  "})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["name"], "Anne O'Neil")
        with self.sessions() as db:
            self.assertEqual(db.get(User, self.customer.id).phone, "+91 98765 43210")
            self.assertEqual(db.get(User, self.other.id).name, "Other Customer")
        for phone in ("", None):
            self.assertIsNone(self.request("PATCH", payload={"phone": phone}).json["phone"])
        self.assertEqual(self.request("PATCH", payload={}).status_code, 200)

    def test_invalid_fields_and_mass_assignment_leave_account_unchanged(self):
        for payload in ({"name": "  "}, {"name": None}, {"name": "x"}, {"name": "A" * 121}, {"name": "123"},
                        {"phone": "+91 0000000000"}, {"phone": "123"}, {"phone": 9876543210},
                        {"phone": "1" * 21}, {"name": "Updated Name", "email": "changed@example.com"},
                        {"id": str(self.other.id)}, {"role": "ADMIN"}, {"is_active": False},
                        {"password_hash": "changed"}, {"avatar_url": "https://example.com/image"},
                        {"avatar_public_id": "customer/other"}, {"status": "INACTIVE"}, {"created_at": "2000-01-01"}):
            with self.subTest(payload=payload):
                self.assertEqual(self.request("PATCH", payload=payload).status_code, 422)
        with self.sessions() as db:
            user = db.get(User, self.customer.id)
            self.assertEqual((user.name, user.email, user.password_hash), ("Asha Rao", "asha@example.com", "secret hash"))

    def test_avatar_rejects_size_mime_and_forged_or_truncated_image_content(self):
        with patch("app.services.customer_profile.upload_file") as upload:
            for content, mime, status in ((picture(), "image/gif", 400), (b"", "image/png", 400),
                    (b"x" * (MAX_AVATAR_BYTES + 1), "image/png", 413), (bytes.fromhex("89504e470d0a1a0a") + b"forged", "image/png", 400),
                    (picture("JPEG"), "image/png", 400), (picture("JPEG")[:-20], "image/jpeg", 400)):
                with self.subTest(mime=mime, size=len(content)):
                    self.assertEqual(self.upload(content, mime).status_code, status)
            upload.assert_not_called()

    def test_supported_avatars_persist_and_cleanup_previous_image(self):
        with patch.object(settings, "cloudinary_cloud_name", "test"), patch.object(settings, "cloudinary_api_key", "test"), \
             patch.object(settings, "cloudinary_api_secret", "test"), \
             patch("app.services.customer_profile.upload_file") as upload, patch("app.services.customer_profile.delete_file", return_value={"result": "ok"}) as delete:
            previous = "customer/old"
            for format, mime in (("JPEG", "image/jpeg"), ("PNG", "image/png"), ("WEBP", "image/webp")):
                public_id = f"customer/new-{format}"
                upload.return_value = {"public_id": public_id, "secure_url": f"https://res.cloudinary.com/demo/{format}"}
                result = self.upload(picture(format), mime)
                self.assertEqual(result.status_code, 200)
                delete.assert_called_with(previous, resource_type="image")
                self.assertEqual(self.request().json["avatar_url"], upload.return_value["secure_url"])
                self.assertIn(str(self.customer.id), upload.call_args.kwargs["folder"])
                previous = public_id
            with self.sessions() as db:
                self.assertEqual(db.get(User, self.customer.id).avatar_public_id, previous)
                self.assertEqual(db.get(User, self.other.id).avatar_public_id, "customer/other")

    def test_avatar_delete_is_owned_idempotent_and_tolerates_storage_cleanup_failure(self):
        with patch("app.services.customer_profile.delete_file", side_effect=RuntimeError("provider down")) as delete:
            result = self.request("DELETE", "/api/customer/profile/avatar")
            self.assertEqual(result.status_code, 200)
            self.assertIsNone(result.json["avatar_url"])
            self.assertEqual(self.request("DELETE", "/api/customer/profile/avatar").status_code, 200)
            delete.assert_called_once_with("customer/old", resource_type="image")
        with self.sessions() as db:
            self.assertIsNone(db.get(User, self.customer.id).avatar_public_id)
            self.assertEqual(db.get(User, self.other.id).avatar_public_id, "customer/other")

    def test_upload_configuration_provider_and_database_failure_preserve_current_photo(self):
        with patch.object(settings, "cloudinary_api_key", None):
            self.assertEqual(self.upload().status_code, 503)
        config = {"cloudinary_cloud_name": "test", "cloudinary_api_key": "test", "cloudinary_api_secret": "test"}
        with patch.multiple(settings, **config), patch("app.services.customer_profile.upload_file", side_effect=RuntimeError("provider secret")):
            response = self.upload()
            self.assertEqual(response.status_code, 502)
            self.assertNotIn("secret", str(response.json))
        from sqlalchemy.exc import SQLAlchemyError
        with patch.multiple(settings, **config), \
             patch("app.services.customer_profile.upload_file", return_value={"public_id": "customer/new", "secure_url": "https://res.cloudinary.com/demo/new"}), \
             patch.object(Session, "commit", side_effect=SQLAlchemyError("database failure")), \
             patch("app.services.customer_profile.delete_file", return_value={"result": "ok"}) as delete:
            self.assertEqual(self.upload().status_code, 502)
            delete.assert_called_once_with("customer/new", resource_type="image")
        self.assertEqual(self.request().json["avatar_url"], self.customer.avatar_url)

    def test_full_app_origin_guard_covers_profile_mutations(self):
        result = self.request("PATCH", payload={"name": "Changed"}, headers={"origin": "https://untrusted.example"})
        self.assertEqual(result.status_code, 403)
        self.assertEqual(self.request().json["name"], "Asha Rao")


class CustomerProfileMigrationTests(unittest.TestCase):
    def test_upgrade_preserves_existing_rows_and_adds_nullable_fields_only(self):
        engine = create_engine("sqlite://")
        self.addCleanup(engine.dispose)
        metadata = MetaData()
        users = Table("users", metadata, Column("id", String, primary_key=True), Column("name", String), Column("email", String))
        unrelated = Table("unrelated", metadata, Column("value", String))
        metadata.create_all(engine)
        filename = Path(__file__).resolve().parents[1] / "alembic/versions/0052_customer_profile.py"
        spec = importlib.util.spec_from_file_location("profile_migration", filename)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        with engine.begin() as connection:
            connection.execute(users.insert().values(id="retained", name="Existing Customer", email="existing@example.com"))
            connection.execute(unrelated.insert().values(value="retained"))
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
            columns = {column["name"]: column for column in inspect(connection).get_columns("users")}
            for name in ("phone", "avatar_url", "avatar_public_id"):
                self.assertTrue(columns[name]["nullable"])
            self.assertEqual(connection.execute(select(users.c.name)).scalar_one(), "Existing Customer")
            self.assertEqual(connection.execute(select(unrelated)).scalar_one(), "retained")
            self.assertIsNone(connection.exec_driver_sql("SELECT phone FROM users").scalar_one())
            with Operations.context(MigrationContext.configure(connection)):
                migration.downgrade()
            self.assertEqual(connection.execute(select(users.c.name)).scalar_one(), "Existing Customer")
