"""The seeded outlet (CLAUDE.md §5.0, multi-tenancy contract).

V1 serves one outlet. The schema is multi-outlet ready but there is no switching UI
and no RLS yet, so every scoped table from Phase 2 onward references this row.
"""

from __future__ import annotations

from sqlalchemy import Engine, text

from app.core.config import get_settings


def test_exactly_one_outlet_is_seeded(engine: Engine) -> None:
    with engine.connect() as connection:
        count = connection.execute(text("SELECT count(*) FROM outlets")).scalar_one()

    assert count == 1


def test_seeded_outlet_matches_configured_default(engine: Engine) -> None:
    """Config and schema must agree, or Phase 2 will stamp rows with a dangling id."""
    settings = get_settings()

    with engine.connect() as connection:
        row = connection.execute(
            text("SELECT id, name, is_active FROM outlets")
        ).one()

    assert row.id == settings.DEFAULT_OUTLET_ID
    assert row.name == settings.DEFAULT_OUTLET_NAME
    assert row.is_active is True


def test_outlet_id_defaults_to_a_generated_uuid(engine: Engine) -> None:
    """A second outlet must not need its id supplied -- §5's PK convention."""
    with engine.begin() as connection:
        new_id = connection.execute(
            text("INSERT INTO outlets (name) VALUES ('Temp Outlet') RETURNING id")
        ).scalar_one()
        assert new_id is not None
        connection.execute(
            text("DELETE FROM outlets WHERE id = :id").bindparams(id=new_id)
        )
