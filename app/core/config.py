"""Application configuration, read from environment variables.

Every tunable in CLAUDE.md §16 lives here. Nothing reads os.environ directly
anywhere else in the codebase -- one source of truth.
"""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, PostgresDsn, field_validator, model_validator
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
    # Optional at the type level so local development works without a Supabase project
    # (tests mint their own tokens). Made mandatory in production by
    # _supabase_auth_must_be_configured_in_prod below.
    #
    # SUPABASE_SERVICE_KEY has two consumers and neither is token verification, which uses
    # the JWT secret alone: Storage since Phase 8 (app/services/storage.py) and the Auth
    # admin API since Phase 14 (app/services/supabase_auth.py). Both fall back to an offline
    # double when it is absent, which is right for dev and silently wrong for production --
    # hence its promotion into the prod validator below.
    SUPABASE_URL: str | None = None
    SUPABASE_SERVICE_KEY: str | None = None
    # Phase 12. PUBLIC BY DESIGN, and the only Supabase secret-shaped value in this class
    # that is meant to leave the server: GET /api/v1/auth-config serves it unauthenticated
    # so a browser can reach Supabase Auth to log in at all (§7 of the Phase 12 plan).
    #
    # Note how differently it reads from SUPABASE_SERVICE_KEY two lines up, which grants
    # full database access and must never reach a client. Same provider, same shape,
    # opposite rule -- which is exactly why the distinction is written here rather than
    # left to whoever next copies a value out of the Supabase dashboard.
    SUPABASE_ANON_KEY: str | None = None
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
    # §6.11 -- a DIFFERENT dial from the line above, on purpose: "a manager should look at
    # this" and "this needs paper proof" are different questions. Never fold them into one.
    EXPENSE_RECEIPT_THRESHOLD: Decimal = Decimal("5000.00")
    # §13.23 (Phase 13) -- above this, a day's cash variance is worth a manager's attention.
    #
    # A THIRD dial, and separate from the two above for the reason those two are separate from
    # each other: they ask about a single expense, this asks whether a whole day reconciled.
    # Folding it into either would weld two unrelated questions to one number forever.
    #
    # The figure is a guess (§14's open questions). Too low and every day is flagged, which
    # teaches a manager to dismiss the list unread; too high and the ₹500 gap §5.2 describes --
    # the one booked as udhaar against a salesman's own name -- never surfaces at all.
    VARIANCE_ALERT_THRESHOLD: Decimal = Decimal("100.00")
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

    @field_validator(
        "EXPENSE_REVIEW_THRESHOLD",
        "EXPENSE_RECEIPT_THRESHOLD",
        "VARIANCE_ALERT_THRESHOLD",
        mode="after",
    )
    @classmethod
    def _threshold_must_be_positive(cls, value: Decimal) -> Decimal:
        """A threshold of zero or less is never what somebody meant.

        `VARIANCE_ALERT_THRESHOLD` joins the two expense dials here in Phase 13 rather than
        getting a validator of its own. Zero would flag every day whose variance is anything
        other than exactly nil -- including the ₹0.01 rounding noise a real count produces --
        and a negative value would flag nothing at all, since §13.23 compares an absolute
        value. Both are silent misconfigurations rather than errors, which is exactly the kind
        this class refuses at load time rather than at the first surprising report.
        """
        if value <= 0:
            raise ValueError("Thresholds must be greater than zero.")
        return value

    @model_validator(mode="after")
    def _supabase_auth_must_be_configured_in_prod(self) -> Settings:
        """A production deployment with no JWT secret must refuse to boot.

        Without this it would start happily and then reject every authenticated request
        with a 500 -- or, far worse, a future refactor could make an absent secret mean
        "skip verification". Failing at startup is the only safe direction.

        SUPABASE_URL is required alongside it because it supplies the expected `iss` claim;
        without it, a token minted by any other Supabase project would pass.

        SUPABASE_ANON_KEY joins them in Phase 12, for the same "fail at startup" reason one
        step earlier in the flow. The frontend is served by this application, and its login
        screen reaches Supabase Auth with that key; without it nobody can obtain a token at
        all. The failure would otherwise surface as a login page that simply does not work
        in production, which is a far more expensive way to learn about a missing variable
        than refusing to boot.

        SUPABASE_SERVICE_KEY joins them in Phase 14, and it closes a hole rather than adding
        a requirement. It has had a consumer since Phase 8 -- `build_storage` -- which
        **falls back to a local temp directory when it is absent**, so a production that
        forgot it has been writing receipts to /tmp with nothing complaining, and every
        signed URL it issued was a `file://` URI. Phase 14 adds a second consumer,
        `build_auth`, whose fallback cannot create a real account at all. Two silent
        degradations is one more than this deserved; the rule above -- fail at startup --
        was always the right one for this key too.
        """
        if self.ENV != "prod":
            return self

        missing = [
            name
            for name in (
                "SUPABASE_JWT_SECRET",
                "SUPABASE_URL",
                "SUPABASE_ANON_KEY",
                "SUPABASE_SERVICE_KEY",
            )
            if not (getattr(self, name) or "").strip()
        ]
        if missing:
            raise ValueError(
                f"{', '.join(missing)} must be set when ENV=prod (CLAUDE.md §8, §16)."
            )
        return self

    @property
    def supabase_jwks_uri(self) -> str | None:
        """Where Supabase publishes the public keys for asymmetric access tokens.

        Projects created since Supabase moved to JWT signing keys issue ES256 tokens, whose
        signatures are checked against a public key rather than the shared secret. Derived
        from SUPABASE_URL rather than configured separately: it is a fixed path on the same
        host, and a second env var would only be an opportunity for the two to disagree.
        """
        if not (self.SUPABASE_URL or "").strip():
            return None
        return f"{self.SUPABASE_URL.rstrip('/')}/auth/v1/.well-known/jwks.json"

    @property
    def supabase_issuer(self) -> str | None:
        """The `iss` claim Supabase stamps on its access tokens.

        Built in one place so the "/auth/v1" suffix cannot drift between the verifier and
        anything else that needs it. None when SUPABASE_URL is unset, which tells
        decode_access_token() to skip the issuer check.
        """
        if not (self.SUPABASE_URL or "").strip():
            return None
        return f"{self.SUPABASE_URL.rstrip('/')}/auth/v1"


@lru_cache
def get_settings() -> Settings:
    """Cached so the .env file is parsed once per process.

    Tests clear the cache (get_settings.cache_clear()) when they need to build a
    Settings object from a different environment.
    """
    return Settings()  # type: ignore[call-arg]
