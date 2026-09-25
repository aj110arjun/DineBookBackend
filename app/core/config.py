from pydantic_settings import BaseSettings, SettingsConfigDict
import os


class Settings(BaseSettings):
    database_url: str = os.getenv("DATABASE_URL")
    frontend_url: str = "http://localhost:5173"
    smtp_host: str | None = os.getenv("SMTP_HOST")
    smtp_port: int = os.getenv("SMTP_PORT")
    smtp_username: str | None = os.getenv("SMTP_USERNAME")
    smtp_password: str | None = os.getenv("SMTP_PASSWORD")
    smtp_from_email: str | None = os.getenv("SMTP_FROM_EMAIL")
    smtp_use_tls: bool = os.getenv("SMTP_USE_TLS")

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


settings = Settings()
