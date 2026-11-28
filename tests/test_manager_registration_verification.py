import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from tests import sqlite_support  # noqa: F401
from tests.test_customer_profile import ASGIClient
from app.core.config import settings
from app.core.security import hash_password
from app.db.database import Base, get_db
from app.main import app
from app.models.restaurant import Restaurant
from app.models.restaurant_document import RestaurantDocument
from app.models.user import EmailVerificationCode, User
from app.services.manager_registration_verification import require_registration_verification


EMAIL = "manager@example.com"
PREFIX = "/api/auth/manager"


def application_body(email, token):
    boundary = "dinebook-manager-registration-test"
    fields = {
        "name": "Test Manager", "email": email, "password": "Fresh8!x",
        "restaurant_name": "Test Kitchen", "restaurant_description": "Local food",
        "cuisine_type": "Indian", "restaurant_contact": "+91 9876543210",
        "address": "12 Market Road", "city": "Pune", "state": "Maharashtra", "pin_code": "411001",
    }
    if token is not None:
        fields["verification_token"] = token
    parts = []
    for name, value in fields.items():
        parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n').encode())
    for name in ("fssai_license", "business_registration", "owner_identity", "branding_images"):
        image = name == "branding_images"
        mime, extension, content = ("image/png", "png", b"\x89PNG\r\n\x1a\nimage") if image else (
            "application/pdf", "pdf", b"%PDF-1.4\n%%EOF",
        )
        parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{name}.{extension}"\r\n'
                      f'Content-Type: {mime}\r\n\r\n').encode() + content + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


class ManagerRegistrationVerificationTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        self.addCleanup(self.engine.dispose)

        def override():
            with self.sessions() as db:
                yield db
        app.dependency_overrides[get_db] = override
        self.addCleanup(app.dependency_overrides.pop, get_db)
        for target, options in (
            ("app.routers.auth.manager_auth.send_manager_verification_email", {}),
            ("app.routers.auth.manager_auth.upload_file", {"return_value": {
                "secure_url": "https://example.com/document", "public_id": "registration-test",
            }}),
            ("app.routers.auth.manager_auth.delete_file", {}),
        ):
            mock = patch(target, **options)
            setattr(self, target.rsplit(".", 1)[-1], mock.start())
            self.addCleanup(mock.stop)
        rate_limit = patch.object(settings, "rate_limit_enabled", False)
        rate_limit.start()
        self.addCleanup(rate_limit.stop)
        self.client = ASGIClient()

    def send_code(self, email=EMAIL):
        result = self.client.request("POST", f"{PREFIX}/send-verification", payload={"email": email})
        self.assertEqual(result.status_code, 200, result.json)
        return self.send_manager_verification_email.call_args.args[1]

    def verify(self, email=EMAIL):
        code = self.send_code(email)
        result = self.client.request("POST", f"{PREFIX}/verify-email", payload={"email": email, "code": code})
        self.assertEqual(result.status_code, 200, result.json)
        return result.json

    def register(self, token=None, email=EMAIL):
        content, content_type = application_body(email, token)
        return self.client.request("POST", f"{PREFIX}/register", content=content, content_type=content_type)

    def assert_no_application(self):
        self.upload_file.assert_not_called()
        with self.sessions() as db:
            self.assertEqual(db.query(User).count(), 0)
            self.assertEqual(db.query(Restaurant).count(), 0)

    def test_successful_otp_issues_email_bound_proof_with_a_separate_two_hour_lifetime(self):
        code = self.send_code("Manager@Example.com")
        with self.sessions() as db:
            otp_expiry = db.get(EmailVerificationCode, EMAIL).expires_at.replace(tzinfo=timezone.utc)
        result = self.client.request("POST", f"{PREFIX}/verify-email", payload={"email": "Manager@Example.com", "code": code})
        self.assertEqual(result.status_code, 200)
        proof = result.json
        self.assertEqual(proof["email"], EMAIL)
        self.assertGreaterEqual(len(proof["verification_token"]), 40)
        expiry = datetime.fromisoformat(proof["expires_at"].replace("Z", "+00:00"))
        self.assertGreater(expiry, otp_expiry + timedelta(minutes=110))
        self.assertLessEqual(expiry, datetime.now(timezone.utc) + timedelta(hours=2))
        with self.sessions() as db:
            stored = db.get(EmailVerificationCode, EMAIL)
            self.assertNotEqual(stored.code_hash, proof["verification_token"])
            self.assertTrue(stored.code_hash.startswith("manager-registration:"))
        repeated = self.client.request("POST", f"{PREFIX}/verify-email", payload={"email": EMAIL, "code": code})
        self.assertEqual(repeated.status_code, 400)
        self.assertNotIn("verification_token", repeated.json)

    def test_wrong_and_expired_otps_do_not_issue_a_token(self):
        code = self.send_code()
        wrong = "000000" if code != "000000" else "111111"
        result = self.client.request("POST", f"{PREFIX}/verify-email", payload={"email": EMAIL, "code": wrong})
        self.assertEqual(result.status_code, 400)
        self.assertNotIn("verification_token", result.json)
        with self.sessions() as db:
            db.get(EmailVerificationCode, EMAIL).expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            db.commit()
        result = self.client.request("POST", f"{PREFIX}/verify-email", payload={"email": EMAIL, "code": code})
        self.assertEqual(result.status_code, 400)
        self.assertNotIn("verification_token", result.json)

    def test_missing_tampered_and_different_email_tokens_cannot_register(self):
        token = self.verify()["verification_token"]
        self.assertEqual(self.register().status_code, 422)
        for email, supplied in ((EMAIL, token + "x"), ("another@example.com", token), (EMAIL, "manager-email-verified")):
            with self.subTest(email=email, token=supplied):
                result = self.register(supplied, email)
                self.assertEqual(result.status_code, 403)
                self.assertEqual(result.json["detail"]["code"], "email_verification_required")
        self.assert_no_application()

    def test_expired_verification_cannot_register(self):
        token = self.verify()["verification_token"]
        with self.sessions() as db:
            db.get(EmailVerificationCode, EMAIL).expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            db.commit()
        self.assertEqual(self.register(token).status_code, 403)
        self.assert_no_application()

    def test_an_otp_or_the_previous_shared_verification_marker_is_not_registration_proof(self):
        code = self.send_code()
        self.assertEqual(self.register(code).status_code, 403)
        with self.sessions() as db:
            record = db.get(EmailVerificationCode, EMAIL)
            record.code_hash = hash_password("manager-email-verified")
            db.commit()
        self.assertEqual(self.register("manager-email-verified").status_code, 403)
        self.assert_no_application()

    def test_resending_an_otp_revokes_the_previous_registration_token(self):
        previous = self.verify()["verification_token"]
        replacement_code = self.send_code()
        self.assertEqual(self.register(previous).status_code, 403)
        result = self.client.request("POST", f"{PREFIX}/verify-email", payload={"email": EMAIL, "code": replacement_code})
        self.assertEqual(result.status_code, 200)
        replacement = result.json["verification_token"]
        self.assertNotEqual(previous, replacement)
        self.assertEqual(self.register(previous).status_code, 403)
        self.assertEqual(self.register(replacement).status_code, 201)

    def test_a_long_application_can_finish_after_the_otp_expires_and_consumes_the_token(self):
        proof = self.verify()
        with patch("app.core.otp.datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime.now(timezone.utc) + timedelta(minutes=30)
            result = self.register(proof["verification_token"])
        self.assertEqual(result.status_code, 201, result.json)
        self.assertEqual(self.upload_file.call_count, 4)
        with self.sessions() as db:
            self.assertTrue(db.query(User).one().email_verified)
            self.assertEqual(db.query(RestaurantDocument).count(), 4)
            self.assertIsNone(db.get(EmailVerificationCode, EMAIL))
            with self.assertRaises(HTTPException) as rejected:
                require_registration_verification(db, EMAIL, proof["verification_token"])
            self.assertEqual(rejected.exception.status_code, 403)
        self.assertEqual(self.register(proof["verification_token"]).status_code, 409)
        self.assertEqual(self.client.request("GET", "/api/customer/me", token=proof["verification_token"]).status_code, 401)

    def test_failed_uploads_preserve_the_token_for_retry(self):
        proof = self.verify()
        self.upload_file.side_effect = RuntimeError("Storage unavailable")
        self.assertEqual(self.register(proof["verification_token"]).status_code, 500)
        with self.sessions() as db:
            self.assertEqual(db.query(User).count(), 0)
            self.assertEqual(db.query(Restaurant).count(), 0)
            self.assertIsNotNone(db.get(EmailVerificationCode, EMAIL))
        self.upload_file.side_effect = None
        result = self.register(proof["verification_token"])
        self.assertEqual(result.status_code, 201, result.json)
