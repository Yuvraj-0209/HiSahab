"""The whole pipeline against the outlet's real July 2026 statement (CLAUDE.md §13.18).

Every other test in this phase uses invented figures. This one runs the real export through
upload, parse, classify and reconcile, and asserts the numbers the owner and I checked by
hand during Phase 20's design.

**It skips when the file is absent**, which is the normal case: `bank_statements/` is
gitignored because the real file carries an account number and a month of counterparty names.
So this is a test that runs on the owner's machine and in nobody else's CI -- deliberately,
and §13.18 already draws that line for the frontend. The synthetic fixture in
`test_bank_statement_parsing.py` covers the same shapes for everyone else.

What it protects that a synthetic fixture cannot: the real file is the only place the
*combination* occurs -- 108 lines, four of them sharing a date, a settlement split across two
rows on 14 July, a truncated payer name, a masked IMPS account, and Indian digit grouping --
and it was the real file that exposed the endpoint bug the parser shipped with.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from app.core.bank_statements import parse
from app.services.bank import classify

_STATEMENTS = Path(__file__).resolve().parents[1] / "bank_statements"

pytestmark = pytest.mark.asyncio


def _real_statement() -> bytes:
    """The 1 July - 1 Aug export, or a skip. Newest file wins if several are present."""
    if not _STATEMENTS.is_dir():
        pytest.skip("bank_statements/ is gitignored and absent -- see this module's docstring")
    candidates = sorted(_STATEMENTS.glob("*.csv"), key=lambda p: p.stat().st_size)
    if not candidates:
        pytest.skip("no real statement CSV present")
    return candidates[-1].read_bytes()


async def test_the_real_statement_parses_with_a_closing_balance_that_checks_out() -> None:
    """**The invariant that caught the endpoint bug**: opening + credits − debits = closing.

    It is the only assertion here that depends on no hand-entered figure. The first version
    of the parser chose endpoints by `min(txn_date)` and this failed by ₹349,962 -- a
    plausible number, in the right order of magnitude, with nothing in the system able to
    contradict it. Phase 21's bridge would have stood on it.
    """
    statement = parse(_real_statement(), account_id=str(uuid4()))

    credits = sum(line.amount for line in statement.lines if line.direction == "credit")
    debits = sum(line.amount for line in statement.lines if line.direction == "debit")

    assert statement.opening_balance is not None
    assert statement.opening_balance + credits - debits == statement.closing_balance


async def test_the_figures_the_owner_and_i_checked_by_hand() -> None:
    """July 2026, from the 1 Jul - 1 Aug export. Each of these was verified in conversation."""
    statement = parse(_real_statement(), account_id=str(uuid4()))

    july = [line for line in statement.lines if line.txn_date.month == 7]
    credits = [line for line in july if line.direction == "credit"]
    debits = [line for line in july if line.direction == "debit"]

    assert len(july) == 101
    assert len(credits) == 73
    assert len(debits) == 28
    assert sum(line.amount for line in credits) == Decimal("10954162.71")
    assert sum(line.amount for line in debits) == Decimal("11373613.06")
    # The 1 July morning bank balance, which is where §6.5's chain would start.
    assert statement.opening_balance == Decimal("1314258.08")


async def test_every_paytm_settlement_in_july_is_recognised() -> None:
    """One settlement per day, 1-31 July with no gaps, plus the 1 August boundary row.

    The gaplessness is the assertion worth having: a single missed narration form would show
    up here as a hole, and the *shape* of the miss -- one day, every month -- is exactly what
    a manager would learn to ignore rather than investigate.
    """
    statement = parse(_real_statement(), account_id=str(uuid4()))

    settlements = [
        line for line in statement.lines if classify(line) == "paytm_settlement"
    ]
    july_days = {line.txn_date.day for line in settlements if line.txn_date.month == 7}

    assert july_days == set(range(1, 32))
    # 14 July settled as two rows -- an IMPS and an RTGS -- so 32 rows cover 31 days.
    assert len([line for line in settlements if line.txn_date.month == 7]) == 32
    assert any(line.txn_date.month == 8 for line in settlements)


async def test_the_fourteenth_of_july_settled_across_two_rows() -> None:
    """The day that would have produced a phantom discrepancy under a one-row assumption."""
    statement = parse(_real_statement(), account_id=str(uuid4()))

    rows = [
        line
        for line in statement.lines
        if classify(line) == "paytm_settlement" and line.txn_date.day == 14
    ]

    assert len(rows) == 2
    assert sum(line.amount for line in rows) == Decimal("357196.80")


async def test_the_boundary_settlement_is_the_one_the_owner_named() -> None:
    """31 July's trading, landing 1 August -- the money-in-transit term for Phase 21.

    The same morning also carries two cash deposits and two udhaar repayments, and the owner
    was explicit that none of them belong here: the deposits are already in the cash-in-hand
    bucket, and the repayments are the fortnightly bill for 15-31 July, already counted as
    July credit sales. Taking either would double-count it.
    """
    statement = parse(_real_statement(), account_id=str(uuid4()))

    august = [line for line in statement.lines if line.txn_date.month == 8]
    settlements = [line for line in august if classify(line) == "paytm_settlement"]

    assert len(settlements) == 1
    assert settlements[0].amount == Decimal("233680.26")
    # Everything else that morning is correctly something other than a settlement.
    assert {classify(line) for line in august if line is not settlements[0]} == {
        "cash_deposit",
        "udhaar_repayment",
        "iocl_cbg",
        "bank_charge",
    }


async def test_the_two_statements_agree_about_31_july() -> None:
    """A cross-check between exports rather than against a figure I typed.

    The 1-31 July download closed at ₹894,807.73. In the 1 Jul - 1 Aug download, the balance
    immediately before the 1 August settlement is the same figure, reached by a different
    route through a different file.
    """
    statement = parse(_real_statement(), account_id=str(uuid4()))

    settlement = next(
        line
        for line in statement.lines
        if line.txn_date.month == 8 and classify(line) == "paytm_settlement"
    )

    assert settlement.running_balance is not None
    assert settlement.running_balance - settlement.amount == Decimal("894807.73")


async def test_classification_leaves_only_genuinely_ambiguous_debits_unclassified() -> None:
    """105 of 108 lines classified; the three that are not are debits nobody can label.

    §13.39: an unclassified line is visible work, a misclassified one is invisible error. The
    assertion is that the *residue* is small and is entirely debits -- a credit falling
    through would mean a customer's payment filed where nobody looks.
    """
    statement = parse(_real_statement(), account_id=str(uuid4()))

    unclassified = [line for line in statement.lines if classify(line) == "unclassified"]

    assert len(unclassified) == 3
    assert all(line.direction == "debit" for line in unclassified)


async def test_the_udhaar_candidates_are_the_companies_the_owner_named() -> None:
    """The owner confirmed these are credit customers settling up (§11 phase 20)."""
    from app.services.bank import _sender_fragment

    statement = parse(_real_statement(), account_id=str(uuid4()))

    payers = {
        _sender_fragment(line.narration)
        for line in statement.lines
        if classify(line) == "udhaar_repayment"
    }

    for expected in (
        "GUPTA OVERSEAS",
        "V V ENTERPRISES",
        "SHREEJI COTFABS",
        "GRM FOODKRAFT PVT LTD",
        "STAR INTERNATIONAL",
        "KRISHNA CONSTRU",
        "PANIPAT HANDLOOM",
    ):
        assert expected in payers

    # The masked IMPS line yields nothing, correctly -- there is no payer in its text.
    assert None in payers


async def test_the_real_statement_imports_through_the_api(
    client, make_user, auth_headers
) -> None:
    """End to end: upload the real file and confirm every line lands exactly once."""
    admin = make_user("admin")
    headers = auth_headers(admin)
    account = await client.post(
        "/api/v1/bank-accounts",
        json={"label": f"Real {uuid4().hex[:6]}", "bank_name": "Bank of Baroda"},
        headers=headers,
    )
    account_id = account.json()["id"]
    data = _real_statement()

    first = await client.post(
        "/api/v1/bank-statements/imports",
        files={"file": ("july.csv", data, "text/csv")},
        data={"bank_account_id": account_id},
        headers={**headers, "Idempotency-Key": str(uuid4())},
    )
    # The owner's actual working practice: download with overlap, upload again.
    second = await client.post(
        "/api/v1/bank-statements/imports",
        files={"file": ("july.csv", data, "text/csv")},
        data={"bank_account_id": account_id},
        headers={**headers, "Idempotency-Key": str(uuid4())},
    )

    assert first.status_code == 201, first.text
    assert first.json()["row_count"] == 108
    assert first.json()["imported_count"] == 108
    assert second.json()["imported_count"] == 0
    assert second.json()["skipped_count"] == 108
