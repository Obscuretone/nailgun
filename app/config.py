from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    api_key: SecretStr = Field(min_length=32)
    mailgun_send_keys_file: Path | None = None
    state_directory: Path = Path("data")
    mariadb_database: str
    mariadb_user: str
    mariadb_password_file: Path
    mariadb_socket: Path = Path("/run/mysqld/mysqld.sock")
    smtp_host: str = "smtp-relay.gmail.com"
    smtp_port: int = 587
    smtp_mode: Literal["starttls", "ssl"] = "starttls"
    smtp_username: str = ""
    smtp_password: SecretStr = SecretStr("")
    smtp_source_ip: str | None = None
    smtp_helo: str = "mail.example.com"
    smtp_timeout: int = Field(default=30, ge=1, le=120)
    public_host: str = "mail.example.com"
    daily_recipient_limit: int = Field(default=400, ge=1)
    max_pending: int = Field(default=1000, ge=1)
    max_storage_bytes: int = Field(default=268435456, ge=1048576)
    max_attempts: int = Field(default=5, ge=1, le=10)
    send_interval: float = Field(default=2, ge=0)
    body_retention_days: int = Field(default=7, ge=1, le=365)
    additional_domains: list[str] = Field(default_factory=list)
    worker_enabled: bool = True

    @field_validator("api_key")
    @classmethod
    def reject_example_key(cls, value):
        if value.get_secret_value().startswith("replace-with"):
            raise ValueError("Generate an independent random API key")
        return value

    @model_validator(mode="after")
    def validate_auth(self):
        if bool(self.smtp_username) != bool(self.smtp_password.get_secret_value()):
            raise ValueError("SMTP_USERNAME and SMTP_PASSWORD must be set together")
        return self
