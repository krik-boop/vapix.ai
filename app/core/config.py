"""Application settings. All secrets come from environment variables / .env only."""

from functools import lru_cache
from urllib.parse import quote_plus

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ConfigError(RuntimeError):
    """Raised when a required setting is missing. Never contains secret values."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "dev"
    log_level: str = "INFO"

    # Database
    database_url: SecretStr | None = None
    postgres_user: str | None = None
    postgres_password: SecretStr | None = None
    postgres_db: str | None = None
    postgres_host: str = "db"
    postgres_port: int = 5432

    # amoCRM. The domain is treated as a secret too (it identifies the account).
    amo_domain: SecretStr | None = None
    amo_token: SecretStr | None = None

    # hh.ru
    hh_client_id: SecretStr | None = None
    hh_client_secret: SecretStr | None = None
    hh_access_token: SecretStr | None = None
    hh_refresh_token: SecretStr | None = None
    hh_user_agent: str | None = None
    hh_employer_id: str | None = None
    hh_api_base_url: str = "https://api.hh.ru"
    hh_vacancies_all_accessible: bool = False
    hh_token_auto_refresh: bool = False

    # HTTP
    http_rps: float = Field(default=5.0, gt=0, le=5.0)
    http_max_retries: int = Field(default=5, ge=0, le=10)
    http_backoff_base: float = Field(default=1.0, ge=0)
    http_backoff_max: float = Field(default=60.0, ge=0)
    http_timeout: float = Field(default=30.0, gt=0)

    # Sync
    sync_overlap_seconds: int = Field(default=300, ge=0)

    @field_validator(
        "database_url", "postgres_password", "amo_domain", "amo_token", "hh_client_id",
        "hh_client_secret", "hh_access_token", "hh_refresh_token", mode="before",
    )
    @classmethod
    def _empty_secret_is_none(cls, v):
        return None if v in ("", None) else v

    @field_validator("hh_employer_id", "hh_user_agent", "postgres_user", "postgres_db", mode="before")
    @classmethod
    def _empty_str_is_none(cls, v):
        return None if v == "" else v

    def get_database_url(self) -> str:
        if self.database_url is not None:
            return self.database_url.get_secret_value()
        missing = [n for n, v in (
            ("POSTGRES_USER", self.postgres_user),
            ("POSTGRES_PASSWORD", self.postgres_password),
            ("POSTGRES_DB", self.postgres_db),
        ) if not v]
        if missing:
            raise ConfigError(f"Database is not configured, missing: {', '.join(missing)}")
        password = quote_plus(self.postgres_password.get_secret_value())
        return (
            f"postgresql+psycopg://{quote_plus(self.postgres_user)}:{password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    def amo_base_url(self) -> str:
        require(self, "amo_domain", "amo_token")
        domain = self.amo_domain.get_secret_value().strip()
        domain = domain.removeprefix("https://").removeprefix("http://").strip("/")
        return f"https://{domain}"

    def secret_values(self) -> list[str]:
        """All configured secret values, used by the log redaction filter."""
        values = []
        for name, field in type(self).model_fields.items():
            value = getattr(self, name)
            if isinstance(value, SecretStr):
                raw = value.get_secret_value()
                if raw:
                    values.append(raw)
        return values


def require(settings: Settings, *names: str) -> None:
    missing = [n.upper() for n in names if not getattr(settings, n)]
    if missing:
        raise ConfigError(f"Missing required environment variables: {', '.join(missing)}")


@lru_cache
def get_settings() -> Settings:
    return Settings()
