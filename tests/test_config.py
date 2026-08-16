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
