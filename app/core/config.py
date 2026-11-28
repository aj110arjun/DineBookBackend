import os

from decimal import Decimal
from pathlib import Path

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = ""

    @field_validator("database_url", mode="before")
    @classmethod
    def require_database_url(cls, value):
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Set DATABASE_URL in DineBookBackend/.env to your PostgreSQL connection URL.")
        return value

    frontend_url: str
    frontend_public_url: str | None = None 

    smtp_host: str | None = None
    smtp_port: int | None = None
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_from_email: str | None = None
    smtp_use_tls: bool = True
    max_image_upload_size_mb: int = 5

    # Keep the legacy development key for local session compatibility, but never
    # permit it in a production deployment (validated below).
    jwt_secret_key: str = "development-only-change-this-secret"
    app_env: str = "development"
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

    ai_support_enabled: bool = Field(default=True, validation_alias=AliasChoices("AI_SUPPORT_ENABLED", "AI_CHAT_ENABLED"))
    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen3:1.7b"
    ollama_timeout_seconds: float = Field(default=90, ge=1, le=180, validation_alias=AliasChoices("OLLAMA_TIMEOUT_SECONDS", "OLLAMA_TIMEOUT"))
    ollama_num_ctx: int = Field(default=4096, ge=2048, le=8192)
    ollama_num_predict: int = Field(default=256, ge=64, le=512)
    ollama_keep_alive: str = "5m"
    ollama_api_key: SecretStr | None = None
    ollama_ca_bundle: str | None = None
    ai_support_max_history_messages: int = Field(default=6, ge=0, le=12)
    ai_support_turn_timeout_seconds: int = Field(default=120, ge=10, le=240)
    ai_support_retention_days: int = Field(default=30, ge=1, le=365)
    ai_support_contact_url: str = ""

    @field_validator("ollama_base_url")
    @classmethod
    def validate_ollama_url(cls, value: str) -> str:
        from urllib.parse import urlsplit
        url = urlsplit(value)
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or url.query or url.fragment or url.path not in {"", "/"}:
            raise ValueError("OLLAMA_BASE_URL must be an HTTP(S) origin without credentials or a path.")
        return value.rstrip("/")

    @field_validator("ai_support_contact_url")
    @classmethod
    def validate_support_url(cls, value: str) -> str:
        from urllib.parse import urlsplit
        if value and (urlsplit(value).scheme != "https" or not urlsplit(value).hostname or urlsplit(value).username or urlsplit(value).password):
            raise ValueError("AI_SUPPORT_CONTACT_URL must be a verified HTTPS support URL.")
        return value

    model_config = SettingsConfigDict(
        # Resolve from the backend source directory so config works whether
        # the app is launched from Backend/src or the repository root.
        env_file=Path(__file__).resolve().parents[2] / ".env",
        extra="ignore",
    )


settings = Settings()

if settings.app_env.lower() in {"production", "prod"}:
    from urllib.parse import urlsplit
    inference_url = urlsplit(settings.ollama_base_url)
    if inference_url.scheme != "https" and inference_url.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("Remote production inference requires HTTPS.")
    if settings.jwt_secret_key == "development-only-change-this-secret" or len(settings.jwt_secret_key) < 32:
        raise RuntimeError("Production requires JWT_SECRET_KEY with at least 32 characters.")
    if not settings.auth_cookie_secure:
        raise RuntimeError("Production requires AUTH_COOKIE_SECURE=true.")
