from pydantic_settings import BaseSettings, SettingsConfigDict
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

    jwt_secret_key: str = "development-only-change-this-secret"
    jwt_expire_minutes: int = 60 * 24 * 7
    auth_cookie_secure: bool = False

    google_client_id: str | None = None
    google_client_secret: str | None = None
    google_redirect_uri: str = "http://localhost:8000/api/auth/customer/google/callback"

    cloudinary_cloud_name: str | None = None
    cloudinary_api_key: str | None = None
    cloudinary_api_secret: str | None = None

    model_config = SettingsConfigDict(
        # Resolve from the backend source directory so config works whether
        # the app is launched from Backend/src or the repository root.
        env_file=Path(__file__).resolve().parents[2] / ".env",
        extra="ignore",
    )


settings = Settings()
