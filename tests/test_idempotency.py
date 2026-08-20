"""The §6.10 replay store.

"This is not optional." Attendants use phones on patchy rural connectivity, and a request
that times out on the way back is indistinguishable, from the phone, from one that never
arrived. Without this the retry creates a second ₹5,000 row and the day comes up over.

Phase 5 deferred this with a written reason: a reading is idempotent by construction
(`uq_nozzle_readings_shift_nozzle`), so building the store there would have been
scaffolding ahead of the phase that needs it (§11). A collection has no natural key --
§6.9 means one mode legitimately holds several rows over its lifetime -- so this is where
it lands.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import Engine, text

pytestmark = pytest.mark.anyio

DAY = date(2026, 4, 7)


def _post(client: AsyncClient, shift_id: UUID, headers: dict[str, str], key: str, **body):
    return client.post(
        f"/api/v1/shifts/{shift_id}/collections",
        json=body,
        headers={**headers, "Idempotency-Key": key},
    )


def _count(engine: Engine, shift_id: UUID) -> int:
    with engine.connect() as connection:
        return connection.execute(
            text("SELECT count(*) FROM collections WHERE shift_id = :s").bindparams(
                s=shift_id
            )
        ).scalar_one()


async def test_the_same_key_twice_creates_one_row_and_replays_the_response(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
    engine: Engine,
    clean_collections,
) -> None:
    """§10's required case, stated exactly as the policy words it.

    Note what is asserted: not merely that the second call succeeds, but that both
    responses are *identical*. A retry that returned a different id would leave the client
    holding a reference to a row that does not exist.
    """
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    headers = auth_headers(attendant)

    first = await _post(client, shift, headers, "retry-1", mode="cash", amount="5000.00")
    second = await _post(client, shift, headers, "retry-1", mode="cash", amount="5000.00")

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json() == second.json()
    assert _count(engine, shift) == 1


async def test_the_same_key_with_a_different_body_is_refused(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
    engine: Engine,
    clean_collections,
) -> None:
    """A key identifies one attempt at one action.

    Replaying the ₹5,000 answer for a ₹9,000 request would tell the client its ₹9,000
    landed when nothing of the sort happened -- a wrong number the caller has every reason
    to trust. Refusing is the only honest option.
    """
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    headers = auth_headers(attendant)

    await _post(client, shift, headers, "same-key", mode="cash", amount="5000.00")
    response = await _post(client, shift, headers, "same-key", mode="upi", amount="9000.00")

    assert response.status_code == 422
    assert response.json()["code"] == "IDEMPOTENCY_KEY_REUSED"
    assert _count(engine, shift) == 1


async def test_two_users_may_use_the_same_key(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
    engine: Engine,
    clean_collections,
) -> None:
    """§6.10's tuple is `(key, endpoint, user_id)`.

    Keys are chosen by clients, and two phones both sending "1" is not a collision anyone
    should have to prevent. Scoping by user also means one client can never replay
    another's response.
    """
    manager = make_user("manager")
    other = make_user("manager")
    shift = make_shift(manager, business_date=DAY, sequence=1)

    mine = await _post(client, shift, auth_headers(manager), "1", mode="cash", amount="5000.00")
    theirs = await _post(client, shift, auth_headers(other), "1", mode="upi", amount="900.00")

    assert mine.status_code == 201
    assert theirs.status_code == 201
    assert _count(engine, shift) == 2


async def test_the_same_key_on_a_different_endpoint_is_a_different_request(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
    clean_collections,
) -> None:
    """A client reusing "retry-1" across two different calls is careless, not malicious.

    Keeping the two apart is kinder than refusing one of them, and it cannot cause a wrong
    replay: the endpoint is part of the key.
    """
    manager = make_user("manager")
    shift = make_shift(manager, business_date=DAY, sequence=1)
    headers = auth_headers(manager)

    created = await _post(client, shift, headers, "shared", mode="cash", amount="5000.00")
    assert created.status_code == 201

    reversal = await client.post(
        f"/api/v1/shifts/{shift}/collections/{created.json()['id']}/reversals",
        json={"reason": "counted twice"},
        headers={**headers, "Idempotency-Key": "shared"},
    )

    assert reversal.status_code == 201


async def test_a_reversal_is_idempotent_too(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
    engine: Engine,
    clean_collections,
) -> None:
    """The route a unique constraint could never have protected.

    `uq_collections_reverses_id` stops a *second* reversal of the same row, so this case is
    belt and braces -- but the replayed body matters on its own: a retry must hand back the
    same reversal id rather than a 409 the client has no way to interpret.
    """
    manager = make_user("manager")
    shift = make_shift(manager, business_date=DAY, sequence=1)
    headers = auth_headers(manager)

    created = await _post(client, shift, headers, "c", mode="cash", amount="60000.00")
    url = f"/api/v1/shifts/{shift}/collections/{created.json()['id']}/reversals"
    body = {"reason": "cash miscounted at close"}

    first = await client.post(url, json=body, headers={**headers, "Idempotency-Key": "r"})
    second = await client.post(url, json=body, headers={**headers, "Idempotency-Key": "r"})

    assert first.status_code == 201
    assert first.json() == second.json()
    # original + one reversal, not two reversals.
    assert _count(engine, shift) == 2


async def test_a_post_without_the_header_is_refused(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
    engine: Engine,
) -> None:
    """Refusing beats defaulting to "no deduplication".

    A client that forgets the header is precisely the client whose retry will duplicate a
    ₹5,000 row, and a silent fallback would leave that to be discovered in a cash count
    weeks later.
    """
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)

    response = await client.post(
        f"/api/v1/shifts/{shift}/collections",
        json={"mode": "cash", "amount": "5000.00"},
        headers=auth_headers(attendant),
    )

    assert response.status_code == 400
    assert response.json()["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    assert _count(engine, shift) == 0


async def test_an_absurdly_long_key_is_refused(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
) -> None:
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)

    response = await _post(
        client, shift, auth_headers(attendant), "x" * 500, mode="cash", amount="1.00"
    )

    assert response.status_code == 400
    assert response.json()["code"] == "IDEMPOTENCY_KEY_TOO_LONG"


async def test_a_refused_request_releases_its_key(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
    clean_collections,
) -> None:
    """The reservation is committed before the handler runs, so a refusal must release it.

    Without `discard`, a business-rule 409 would leave a reservation carrying no response,
    and every retry for the next 24 hours would meet REQUEST_IN_PROGRESS -- locking the
    attendant out of an action that never happened. This is the failure mode that makes an
    idempotency store worse than none.
    """
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    headers = auth_headers(attendant)

    await _post(client, shift, headers, "taken", mode="cash", amount="5000.00")
    refused = await _post(client, shift, headers, "reused-after-failure", mode="cash", amount="1.00")
    assert refused.status_code == 409
    assert refused.json()["code"] == "COLLECTION_ALREADY_EXISTS"

    # The same key is free again, and now does real work on a mode that is still open.
    retry = await _post(
        client, shift, headers, "reused-after-failure", mode="upi", amount="1.00"
    )
    assert retry.status_code == 201


async def test_an_expired_key_is_not_replayed(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
    engine: Engine,
    clean_collections,
) -> None:
    """§6.10 stores a response for 24 hours, and this one is older.

    Backdating the stored row is the only honest way to test a TTL without freezing the
    clock: the store's own expiry check is what decides, not the test.
    """
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    headers = auth_headers(attendant)

    first = await _post(client, shift, headers, "old", mode="cash", amount="5000.00")
    assert first.status_code == 201

    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE idempotency_keys SET created_at = :t WHERE idempotency_key = 'old'"
            ).bindparams(t=datetime.now(tz=timezone.utc) - timedelta(hours=25))
        )

    # A day later the key is free, so this is treated as a genuinely new request -- and is
    # refused on its own merits by the one-live-row-per-mode rule rather than replayed.
    second = await _post(client, shift, headers, "old", mode="cash", amount="5000.00")
    assert second.status_code == 409
    assert second.json()["code"] == "COLLECTION_ALREADY_EXISTS"


async def test_an_in_flight_reservation_reports_itself(
    make_user: Callable[..., UUID],
    engine: Engine,
    clean_collections,
) -> None:
    """Two simultaneous retries race on the unique constraint; the loser is told to wait.

    Driven at the service level because two genuinely concurrent HTTP requests cannot be
    arranged in-process against a single test client -- and what is being asserted is the
    store's behaviour when it finds a reservation with no response, which is exactly the
    state the loser of that race observes.
    """
    from app.core import idempotency
    from app.core.errors import AppError
    from app.db.session import SessionLocal

    user = make_user("attendant")
    fingerprint = idempotency.fingerprint(path_params={"shift_id": "s"}, body={"a": 1})

    with SessionLocal() as session:
        # The winner reserves and has not yet stored a response.
        assert (
            idempotency.begin(
                session,
                key="racing",
                endpoint="POST /x",
                user_id=user,
                request_fingerprint=fingerprint,
            )
            is None
        )

        with pytest.raises(AppError) as caught:
            idempotency.begin(
                session,
                key="racing",
                endpoint="POST /x",
                user_id=user,
                request_fingerprint=fingerprint,
            )

    assert caught.value.status_code == 409
    assert caught.value.code == "REQUEST_IN_PROGRESS"


def test_the_cleanup_command_removes_only_expired_rows(
    make_user: Callable[..., UUID],
    engine: Engine,
    clean_collections,
) -> None:
    """§7.4's posture: a command run manually or from cron, not a job scheduler (§12)."""
    from app.core import idempotency
    from app.db.session import SessionLocal
    from app.jobs import cleanup_idempotency_keys

    user = make_user("attendant")
    fingerprint = idempotency.fingerprint(path_params={}, body={})

    with SessionLocal() as session:
        for key in ("fresh", "stale"):
            idempotency.begin(
                session,
                key=key,
                endpoint="POST /x",
                user_id=user,
                request_fingerprint=fingerprint,
            )

    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE idempotency_keys SET created_at = :t "
                "WHERE idempotency_key = 'stale'"
            ).bindparams(t=datetime.now(tz=timezone.utc) - timedelta(hours=30))
        )

    import sys

    argv = sys.argv
    sys.argv = ["cleanup_idempotency_keys"]
    try:
        assert cleanup_idempotency_keys.main() == 0
    finally:
        sys.argv = argv

    with engine.connect() as connection:
        remaining = [
            row[0]
            for row in connection.execute(
                text("SELECT idempotency_key FROM idempotency_keys")
            )
        ]

    assert "fresh" in remaining
    assert "stale" not in remaining


def test_discarding_a_completed_reservation_does_nothing(
    make_user: Callable[..., UUID],
    engine: Engine,
    clean_collections,
) -> None:
    """`discard` releases reservations, never stored answers.

    The distinction is the whole safety of the mechanism: releasing a key that already
    carries a response would let the next retry re-run work that has already been done,
    which is the exact duplicate §6.10 exists to prevent.
    """
    from app.core import idempotency
    from app.db.session import SessionLocal

    user = make_user("attendant")
    fingerprint = idempotency.fingerprint(path_params={}, body={})

    with SessionLocal() as session:
        idempotency.begin(
            session, key="done", endpoint="POST /x", user_id=user,
            request_fingerprint=fingerprint,
        )
        idempotency.store(
            session, key="done", endpoint="POST /x", user_id=user,
            status_code=201, body={"id": "abc"},
        )
        idempotency.discard(session, key="done", endpoint="POST /x", user_id=user)

        replay = idempotency.begin(
            session, key="done", endpoint="POST /x", user_id=user,
            request_fingerprint=fingerprint,
        )

    assert replay is not None
    assert replay.body == {"id": "abc"}


def test_the_cleanup_command_dry_run_deletes_nothing(
    make_user: Callable[..., UUID],
    engine: Engine,
    clean_collections,
) -> None:
    """A destructive command with no way to see what it would do is a command people run
    nervously or not at all."""
    from datetime import datetime, timedelta, timezone

    from app.core import idempotency
    from app.db.session import SessionLocal
    from app.jobs import cleanup_idempotency_keys

    user = make_user("attendant")
    with SessionLocal() as session:
        idempotency.begin(
            session, key="stale", endpoint="POST /x", user_id=user,
            request_fingerprint=idempotency.fingerprint(path_params={}, body={}),
        )
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE idempotency_keys SET created_at = :t "
                "WHERE idempotency_key = 'stale'"
            ).bindparams(t=datetime.now(tz=timezone.utc) - timedelta(hours=30))
        )

    import sys

    argv = sys.argv
    sys.argv = ["cleanup_idempotency_keys", "--dry-run"]
    try:
        assert cleanup_idempotency_keys.main() == 0
    finally:
        sys.argv = argv

    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT count(*) FROM idempotency_keys")
        ).scalar_one() == 1
