import base64
import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta, timezone

from pwdlib import PasswordHash
from app.core.config import settings

_password_hash = PasswordHash.recommended()


def hash_password(password: str) -> str:
    return _password_hash.hash(password)


def verify_secret(secret: str, secret_hash: str) -> bool:
    try:
        return _password_hash.verify(secret, secret_hash)
    except Exception:
        return False


def create_access_token(subject: str) -> tuple[str, datetime]:
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_expire_minutes)
    header = _base64url(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = _base64url(json.dumps({
        "sub": subject, "iat": int(time.time()), "exp": int(expires_at.timestamp()),
        "iss": settings.jwt_issuer, "aud": settings.jwt_audience, "typ": "access",
    }, separators=(",", ":")).encode())
    signing_input = f"{header}.{payload}"
    signature = hmac.new(settings.jwt_secret_key.encode(), signing_input.encode(), hashlib.sha256).digest()
    return f"{signing_input}.{_base64url(signature)}", expires_at


def decode_access_token(token: str) -> dict:
    try:
        header_part, payload_part, signature_part = token.split(".")
        header = json.loads(_base64url_decode(header_part))
        payload = json.loads(_base64url_decode(payload_part))
        if header.get("alg") != "HS256" or not isinstance(payload.get("sub"), str):
            raise ValueError("Invalid token")
        if payload.get("typ", "access") != "access":
            raise ValueError("Invalid token type")
        if payload.get("iss", settings.jwt_issuer) != settings.jwt_issuer:
            raise ValueError("Invalid token issuer")
        if payload.get("aud", settings.jwt_audience) != settings.jwt_audience:
            raise ValueError("Invalid token audience")
        signing_input = f"{header_part}.{payload_part}"
        expected = hmac.new(settings.jwt_secret_key.encode(), signing_input.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _base64url_decode(signature_part)):
            raise ValueError("Invalid token signature")
        if (isinstance(payload.get("exp"), bool) or not isinstance(payload.get("exp"), (int, float))
                or payload["exp"] <= time.time()):
            raise ValueError("Expired token")
        issued_at = payload.get("iat")
        if issued_at is not None and (isinstance(issued_at, bool) or not isinstance(issued_at, (int, float))
                                      or issued_at > time.time() + 60):
            raise ValueError("Invalid token issue time")
        not_before = payload.get("nbf")
        if not_before is not None and (isinstance(not_before, bool) or not isinstance(not_before, (int, float))
                                       or not_before > time.time()):
            raise ValueError("Token is not active")
        return payload
    except (KeyError, TypeError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("Invalid token") from exc


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _base64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
