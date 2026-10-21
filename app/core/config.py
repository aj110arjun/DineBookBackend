from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field
from decimal import Decimal
import os
from pathlib import Path


class Settings(BaseSettings):
    database_url: str = os.getenv("DATABASE_URL")
    frontend_url: str = "http://localhost:5173"
    frontend_public_url: str | None = None 

    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_from_email: str | None = None
    smtp_use_tls: bool = True

    # Keep the legacy development key for local session compatibility, but never
    # permit it in a production deployment (validated below).
    jwt_secret_key: str = "development-only-change-this-secret"
    app_env: str = "production"
    jwt_issuer: str = "dinebook-api"
    jwt_audience: str = "dinebook-web"
    jwt_expire_minutes: int = 60 * 24 * 7
    auth_cookie_secure: bool = False
    max_request_body_bytes: int = Field(default=20 * 1024 * 1024, ge=1024)
    max_json_body_bytes: int = Field(default=1024 * 1024, ge=1024)
    rate_limit_enabled: bool = True
    google_client_id: str | None = None
    google_client_secret: str | None = None
    cloudinary_cloud_name: str | None = None
    cloudinary_api_key: str | None = None
    cloudinary_api_secret: str | None = None
    razorpay_key_id: str | None = None
    razorpay_key_secret: str | None = None
    razorpay_webhook_secret: str | None = None
    razorpay_webhook_previous_secret: str | None = None
    order_tax_percent: Decimal = Field(default=Decimal("18.00"), ge=0, le=100)

    model_config = SettingsConfigDict(
        # Resolve from the backend source directory so config works whether
        # the app is launched from Backend/src or the repository root.
        env_file=Path(__file__).resolve().parents[2] / ".env",
        extra="ignore",
    )


settings = Settings()

if settings.app_env.lower() in {"production", "prod"}:
    if settings.jwt_secret_key == "development-only-change-this-secret" or len(settings.jwt_secret_key) < 32:
        raise RuntimeError("Production requires JWT_SECRET_KEY with at least 32 characters.")
    if not settings.auth_cookie_secure:
        raise RuntimeError("Production requires AUTH_COOKIE_SECURE=true.")
