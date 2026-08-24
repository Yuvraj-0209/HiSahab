"""Configuration rules that must hold before anything else works."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.core.config import Settings

_REQUIRED = {"DATABASE_URL": "postgresql+psycopg://u:p@localhost:5433/db"}


def _settings(**overrides: object) -> Settings:
    # _env_file=None so a developer's real .env cannot influence the result.
    return Settings(_env_file=None, **{**_REQUIRED, **overrides})  # type: ignore[arg-type]


def test_cors_wildcard_is_rejected() -> None:
    """CLAUDE.md §9/§14: never allow_origins=["*"], enforced at config load."""
    with pytest.raises(ValidationError, match=r'must not contain "\*"'):
        _settings(CORS_ALLOWED_ORIGINS="*")


def test_cors_wildcard_rejected_even_when_mixed_with_real_origins() -> None:
    with pytest.raises(ValidationError):
        _settings(CORS_ALLOWED_ORIGINS="https://example.com,*")


def test_cors_origins_parse_from_a_comma_separated_string() -> None:
    settings = _settings(
        CORS_ALLOWED_ORIGINS="https://a.example.com, https://b.example.com"
    )

    assert settings.CORS_ALLOWED_ORIGINS == [
        "https://a.example.com",
        "https://b.example.com",
    ]


def test_expense_threshold_is_decimal_not_float() -> None:
    """CLAUDE.md §14: no float anywhere near money, including in config."""
    settings = _settings()

    assert isinstance(settings.EXPENSE_REVIEW_THRESHOLD, Decimal)
    assert not isinstance(settings.EXPENSE_REVIEW_THRESHOLD, float)
    assert settings.EXPENSE_REVIEW_THRESHOLD == Decimal("1000.00")


def test_expense_threshold_from_env_stays_exact() -> None:
    """A string from the environment must not round-trip through float."""
    settings = _settings(EXPENSE_REVIEW_THRESHOLD="1000.01")

    assert settings.EXPENSE_REVIEW_THRESHOLD == Decimal("1000.01")


# --- §13.23's variance dial (Phase 13) ----------------------------------------


def test_variance_alert_threshold_is_decimal_not_float() -> None:
    """§14 again, for the third dial. Same rule, and it does not get an exemption for
    being a comparison threshold rather than a stored amount."""
    settings = _settings()

    assert isinstance(settings.VARIANCE_ALERT_THRESHOLD, Decimal)
    assert not isinstance(settings.VARIANCE_ALERT_THRESHOLD, float)
    assert settings.VARIANCE_ALERT_THRESHOLD == Decimal("100.00")


def test_variance_alert_threshold_from_env_stays_exact() -> None:
    settings = _settings(VARIANCE_ALERT_THRESHOLD="100.01")

    assert settings.VARIANCE_ALERT_THRESHOLD == Decimal("100.01")


def test_the_three_thresholds_are_independent_dials() -> None:
    """§16: "Never fold them into one value."

    The two expense dials already had this guarantee against each other. §13.23's dial asks a
    third, unrelated question -- whether a whole day's cash reconciled, not whether one expense
    deserves a look or a receipt -- so it needs the same protection. Every figure here is
    deliberately a non-default, because all three defaults being correct is exactly what would
    let one setting hide behind three names.
    """
    settings = _settings(
        EXPENSE_REVIEW_THRESHOLD="250.00",
        EXPENSE_RECEIPT_THRESHOLD="7500.00",
        VARIANCE_ALERT_THRESHOLD="42.00",
    )

    assert settings.EXPENSE_REVIEW_THRESHOLD == Decimal("250.00")
    assert settings.EXPENSE_RECEIPT_THRESHOLD == Decimal("7500.00")
    assert settings.VARIANCE_ALERT_THRESHOLD == Decimal("42.00")


@pytest.mark.parametrize("bad", ["0", "0.00", "-1.00"])
def test_a_non_positive_variance_threshold_refuses_to_load(bad: str) -> None:
    """Both bad directions are *silent* misconfigurations, which is why they are refused here
    rather than left to surface as a strange report.

    Zero flags every day whose variance is anything but exactly nil, including the ₹0.01 a
    real count produces -- a list that is always full is a list nobody reads. A negative value
    flags nothing at all, since §13.23 compares an absolute value, so the alerts screen would
    sit permanently empty and look like it was working.
    """
    with pytest.raises(ValidationError, match="greater than zero"):
        _settings(VARIANCE_ALERT_THRESHOLD=bad)


def test_the_expense_thresholds_still_refuse_a_non_positive_value() -> None:
    """The validator gained a third field in Phase 13; this pins that it did not *lose* the
    two it already had. Widening a shared validator is exactly where that goes unnoticed."""
    for name in ("EXPENSE_REVIEW_THRESHOLD", "EXPENSE_RECEIPT_THRESHOLD"):
        with pytest.raises(ValidationError, match="greater than zero"):
            _settings(**{name: "0.00"})


def test_database_url_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refusing to boot beats silently connecting somewhere unexpected.

    DATABASE_URL is deleted from the environment for this test because conftest
    exports it for the whole session.
    """
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(ValidationError, match="DATABASE_URL"):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_default_outlet_id_is_the_well_known_seed() -> None:
    settings = _settings()

    assert str(settings.DEFAULT_OUTLET_ID) == "00000000-0000-0000-0000-000000000001"


def test_prod_requires_the_jwt_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """CLAUDE.md §8: a production deployment with no JWT secret must not boot.

    The alternative is an app that starts fine and then 500s on every authenticated
    request -- or, worse, one where a future refactor lets an absent secret mean
    "skip verification".

    Deleted from the environment because conftest exports a test secret for the whole
    session, the same way test_database_url_is_required does.
    """
    monkeypatch.delenv("SUPABASE_JWT_SECRET", raising=False)

    with pytest.raises(ValidationError, match="SUPABASE_JWT_SECRET"):
        _settings(ENV="prod", SUPABASE_URL="https://project.supabase.co")


def test_prod_requires_the_supabase_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """It supplies the expected `iss`; without it, any project's token would pass."""
    monkeypatch.delenv("SUPABASE_URL", raising=False)

    with pytest.raises(ValidationError, match="SUPABASE_URL"):
        _settings(ENV="prod", SUPABASE_JWT_SECRET="a-secret-of-at-least-32-bytes-here")


def test_prod_requires_the_anon_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Phase 12: the frontend is served by this app, and its login screen needs the key.

    Without it a production deployment starts happily and presents a login page that
    cannot reach Supabase at all -- a far more expensive way to discover a missing
    environment variable than refusing to boot. Same argument as the JWT secret above,
    one step earlier in the flow.
    """
    monkeypatch.delenv("SUPABASE_ANON_KEY", raising=False)

    with pytest.raises(ValidationError, match="SUPABASE_ANON_KEY"):
        _settings(
            ENV="prod",
            SUPABASE_URL="https://project.supabase.co",
            SUPABASE_JWT_SECRET="a-secret-of-at-least-32-bytes-here",
        )


def test_prod_requires_the_service_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Phase 14, and this one closes a hole rather than adding a requirement.

    The key has had a consumer since Phase 8 -- `build_storage` -- and its absence has
    always been **silent**: the factory falls back to `LocalStorage`, so a production that
    forgot it wrote every receipt to a machine-local temp directory and handed out
    `file://` URIs as signed URLs, with nothing anywhere complaining. Phase 14 adds a second
    consumer whose fallback cannot mint a real account at all.

    Two silent degradations is one more than this deserved, and the rule the three tests
    above apply -- refuse to boot -- was always the right one for this key too. Note the
    consequence for an existing deployment: it is a **new** required variable, not merely a
    documented one.
    """
    monkeypatch.delenv("SUPABASE_SERVICE_KEY", raising=False)

    with pytest.raises(ValidationError, match="SUPABASE_SERVICE_KEY"):
        _settings(
            ENV="prod",
            SUPABASE_URL="https://project.supabase.co",
            SUPABASE_JWT_SECRET="a-secret-of-at-least-32-bytes-here",
            SUPABASE_ANON_KEY="a-public-anon-key",
        )


def test_prod_boots_when_supabase_is_fully_configured() -> None:
    """"Fully configured" gained a third value in Phase 12 and a fourth in Phase 14."""
    settings = _settings(
        ENV="prod",
        SUPABASE_URL="https://project.supabase.co",
        SUPABASE_JWT_SECRET="a-secret-of-at-least-32-bytes-here",
        SUPABASE_ANON_KEY="a-public-anon-key",
        SUPABASE_SERVICE_KEY="a-service-key-that-never-leaves-the-server",
    )

    assert settings.ENV == "prod"


def test_the_anon_key_and_the_service_key_are_separate_settings() -> None:
    """The distinction this project cannot afford to blur (§16).

    Both are long opaque strings from the same Supabase dashboard; one is designed to ship
    in a browser and the other grants full database access. A refactor that collapsed them
    into one field would pass every other test in this file.
    """
    settings = _settings(
        SUPABASE_ANON_KEY="public-anon", SUPABASE_SERVICE_KEY="secret-service"
    )

    assert settings.SUPABASE_ANON_KEY == "public-anon"
    assert settings.SUPABASE_SERVICE_KEY == "secret-service"


def test_dev_does_not_require_supabase_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Local development has no Supabase project; tests mint their own tokens."""
    monkeypatch.delenv("SUPABASE_JWT_SECRET", raising=False)
    monkeypatch.delenv("SUPABASE_URL", raising=False)

    settings = _settings(ENV="dev")

    assert settings.SUPABASE_JWT_SECRET is None


def test_supabase_issuer_is_derived_from_the_url() -> None:
    settings = _settings(SUPABASE_URL="https://project.supabase.co")

    assert settings.supabase_issuer == "https://project.supabase.co/auth/v1"


def test_supabase_issuer_tolerates_a_trailing_slash() -> None:
    """Otherwise a stray slash in .env yields a doubled one and every token fails."""
    settings = _settings(SUPABASE_URL="https://project.supabase.co/")

    assert settings.supabase_issuer == "https://project.supabase.co/auth/v1"


def test_supabase_issuer_is_none_when_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """None tells decode_access_token() to skip the issuer check."""
    monkeypatch.delenv("SUPABASE_URL", raising=False)

    settings = _settings()

    assert settings.supabase_issuer is None
