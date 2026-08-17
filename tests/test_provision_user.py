"""The bootstrap provisioning command (app/jobs/provision_user.py).

Calls main() directly with an argv list rather than shelling out, so failures surface as
Python tracebacks and the test does not depend on which interpreter is on PATH.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import Engine, text

from app.jobs.provision_user import main


@pytest.fixture
def provisioned_ids(engine: Engine) -> Iterator[list[UUID]]:
    """Collects ids the command creates so the test can clean up after itself."""
    created: list[UUID] = []

    yield created

    if created:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "DELETE FROM outlet_memberships WHERE user_id = ANY(:ids)"
                ).bindparams(ids=created)
            )
            connection.execute(
                text("DELETE FROM user_profiles WHERE id = ANY(:ids)").bindparams(
                    ids=created
                )
            )


def _argv(user_id: UUID, role: str = "admin", *extra: str) -> list[str]:
    return [
        "--user-id",
        str(user_id),
        "--full-name",
        "Bootstrap Admin",
        "--role",
        role,
        *extra,
    ]


def test_provisions_a_profile_and_a_membership(
    engine: Engine, provisioned_ids: list[UUID]
) -> None:
    user_id = uuid4()
    provisioned_ids.append(user_id)

    exit_code = main(_argv(user_id, "admin"))

    assert exit_code == 0
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT p.full_name, m.role::text AS role, m.is_active "
                "FROM user_profiles p JOIN outlet_memberships m ON m.user_id = p.id "
                "WHERE p.id = :id"
            ).bindparams(id=user_id)
        ).one()

    assert row.full_name == "Bootstrap Admin"
    assert row.role == "admin"
    assert row.is_active is True


def test_created_by_is_null_for_a_bootstrapped_user(
    engine: Engine, provisioned_ids: list[UUID]
) -> None:
    """NULL means "provisioned by the system" -- the first admin has no creator."""
    user_id = uuid4()
    provisioned_ids.append(user_id)

    main(_argv(user_id))

    with engine.connect() as connection:
        created_by = connection.execute(
            text("SELECT created_by FROM user_profiles WHERE id = :id").bindparams(
                id=user_id
            )
        ).scalar_one()

    assert created_by is None


def test_rerunning_with_the_same_role_is_a_no_op(
    engine: Engine, provisioned_ids: list[UUID]
) -> None:
    """Idempotent: a second run must not create a duplicate membership."""
    user_id = uuid4()
    provisioned_ids.append(user_id)

    assert main(_argv(user_id, "manager")) == 0
    assert main(_argv(user_id, "manager")) == 0

    with engine.connect() as connection:
        count = connection.execute(
            text(
                "SELECT count(*) FROM outlet_memberships WHERE user_id = :id"
            ).bindparams(id=user_id)
        ).scalar_one()

    assert count == 1


def test_changing_role_is_refused_without_force(
    engine: Engine, provisioned_ids: list[UUID]
) -> None:
    """A careless re-run must not silently demote the only admin."""
    user_id = uuid4()
    provisioned_ids.append(user_id)
    main(_argv(user_id, "admin"))

    exit_code = main(_argv(user_id, "attendant"))

    assert exit_code == 1
    with engine.connect() as connection:
        role = connection.execute(
            text(
                "SELECT role::text FROM outlet_memberships WHERE user_id = :id"
            ).bindparams(id=user_id)
        ).scalar_one()
    assert role == "admin"


def test_force_changes_an_existing_role(
    engine: Engine, provisioned_ids: list[UUID]
) -> None:
    user_id = uuid4()
    provisioned_ids.append(user_id)
    main(_argv(user_id, "attendant"))

    exit_code = main(_argv(user_id, "manager", "--force"))

    assert exit_code == 0
    with engine.connect() as connection:
        role = connection.execute(
            text(
                "SELECT role::text FROM outlet_memberships WHERE user_id = :id"
            ).bindparams(id=user_id)
        ).scalar_one()
    assert role == "manager"


def test_unknown_outlet_is_refused_and_writes_nothing(engine: Engine) -> None:
    """Checked before the profile insert, so a typo leaves no half-provisioned user."""
    user_id = uuid4()

    exit_code = main(_argv(user_id, "admin", "--outlet-id", str(uuid4())))

    assert exit_code == 1
    with engine.connect() as connection:
        count = connection.execute(
            text("SELECT count(*) FROM user_profiles WHERE id = :id").bindparams(
                id=user_id
            )
        ).scalar_one()
    assert count == 0


def test_an_invalid_role_is_rejected_by_the_parser() -> None:
    """argparse choices, so a typo fails before touching the database."""
    with pytest.raises(SystemExit):
        main(_argv(uuid4(), "superuser"))


async def test_provisioned_user_can_then_authenticate(
    provisioned_ids: list[UUID],
    client: AsyncClient,
    make_token: Callable[..., str],
) -> None:
    """The command and the auth stack must agree on what a user is.

    This is the join between the two halves of Phase 2: if the command wrote a row that
    get_current_user() cannot resolve, every test above still passes and yet nobody can
    log in.
    """
    user_id = uuid4()
    provisioned_ids.append(user_id)
    main(_argv(user_id, "manager"))

    response = await client.get(
        "/api/v1/me", headers={"Authorization": f"Bearer {make_token(user_id)}"}
    )

    assert response.status_code == 200
    assert response.json()["role"] == "manager"
