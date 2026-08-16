"""Application configuration, read from environment variables.

Every tunable in CLAUDE.md §16 lives here. Nothing reads os.environ directly
anywhere else in the codebase -- one source of truth.
"""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, PostgresDsn, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    ENV: Literal["dev", "test", "prod"] = "dev"

    # --- Database -----------------------------------------------------------
    # No default. If this is unset the app must refuse to boot: silently
    # connecting to somewhere unexpected is worse than a crash on startup.
    DATABASE_URL: PostgresDsn
    TEST_DATABASE_URL: PostgresDsn | None = None

    # --- Supabase -----------------------------------------------------------
    # Declared now because §16 presents the environment surface as a whole.
    # Nothing reads these in Phase 1; auth arrives in Phase 2 and storage in Phase 8.
    SUPABASE_URL: str | None = None
    SUPABASE_SERVICE_KEY: str | None = None
    SUPABASE_JWT_SECRET: str | None = None
    SUPABASE_STORAGE_BUCKET: str = "receipts"

    # --- Multi-tenancy ------------------------------------------------------
    # V1 serves one outlet. The schema is multi-outlet ready (see CLAUDE.md §5.0)
    # but there is no switching UI and no RLS yet, so every scoped row is stamped
    # with this well-known id. Fixed rather than random so that dev, test and
    # production all agree and fixtures stay deterministic.
    DEFAULT_OUTLET_ID: UUID = UUID("00000000-0000-0000-0000-000000000001")
    DEFAULT_OUTLET_NAME: str = "Main Outlet"

    # --- Business rules -----------------------------------------------------
    # Decimal, never float. A float here would poison every threshold comparison
    # in Phase 7 -- and 1000.00 is not exactly representable in binary floating
    # point, so the boundary tests in CLAUDE.md §10 would fail unpredictably.
    EXPENSE_REVIEW_THRESHOLD: Decimal = Decimal("1000.00")
    MAX_UPLOAD_BYTES: int = 5_242_880
    MAX_FLOW_RATE_LPM: int = 60
    SIGNED_URL_TTL_SECONDS: int = 300

    # --- Web ----------------------------------------------------------------
    # NoDecode stops pydantic-settings from trying to JSON-parse the env value,
    # so a plain comma-separated string reaches the validator below.
    CORS_ALLOWED_ORIGINS: Annotated[list[str], NoDecode] = Field(default_factory=list)
    TZ_DISPLAY: str = "Asia/Kolkata"

    @field_validator("CORS_ALLOWED_ORIGINS", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        """Accept a comma-separated string from the environment."""
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("CORS_ALLOWED_ORIGINS", mode="after")
    @classmethod
    def _reject_wildcard(cls, value: list[str]) -> list[str]:
        """CLAUDE.md §9: never allow_origins=["*"].

        Enforced at config load rather than trusted to a code review, so the rule
        cannot be broken by an environment variable in production.
        """
        if any(origin.strip() == "*" for origin in value):
            raise ValueError(
                'CORS_ALLOWED_ORIGINS must not contain "*" -- list explicit origins '
                "(CLAUDE.md §9)."
            )
        return value

    @field_validator("EXPENSE_REVIEW_THRESHOLD", mode="after")
    @classmethod
    def _threshold_must_be_positive(cls, value: Decimal) -> Decimal:
        if value <= 0:
            raise ValueError("EXPENSE_REVIEW_THRESHOLD must be greater than zero.")
        return value


@lru_cache
def get_settings() -> Settings:
    """Cached so the .env file is parsed once per process.

    Tests clear the cache (get_settings.cache_clear()) when they need to build a
    Settings object from a different environment.
    """
    return Settings()  # type: ignore[call-arg]
