"""Parsing a bank statement export (CLAUDE.md §5.3a, §13.36).

Pure-function tests: no database, no server, no fixtures beyond a CSV on disk. That is the
point of keeping `app/core/bank_statements.py` I/O-free -- the module that decides whether a
line is a credit or a debit is the one whose mistakes are least visible downstream, so it gets
tested most cheaply and most thoroughly.

The fixture at `tests/fixtures/bob_statement_sample.csv` mirrors the real Bank of Baroda
export structurally -- nine preamble rows, an address with embedded newlines, trailer rows,
separate DR/CR columns, `Cr` suffixes, Indian digit grouping -- with invented amounts and
names. The real statement is gitignored: it carries an account number and a month of
counterparty names.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.core.bank_statements import ParsedStatement, fingerprint, normalise_narration, parse
from app.core.errors import AppError

_FIXTURE = Path(__file__).parent / "fixtures" / "bob_statement_sample.csv"
_ACCOUNT = "11111111-1111-1111-1111-111111111111"


def _parse_fixture() -> ParsedStatement:
    return parse(_FIXTURE.read_bytes(), account_id=_ACCOUNT)


def test_the_real_export_structure_parses() -> None:
    """Seven transactions out of a file that is mostly not transactions.

    Three share a date, deliberately -- see `test_the_endpoints_are_found_by_file_position`.
    """
    statement = _parse_fixture()

    assert len(statement.lines) == 7
    assert statement.period_from == date(2026, 7, 1)
    assert statement.period_to == date(2026, 7, 5)


def test_preamble_and_trailer_rows_are_skipped() -> None:
    """Nine rows of account header and two of footer are structural, not data.

    They are skipped on the strength of having no parseable date, rather than by counting
    rows -- the preamble's length is a property of one export (a longer address moves it),
    not of the bank.
    """
    statement = _parse_fixture()

    narrations = [line.narration for line in statement.lines]
    assert not any("AUTHORISED SIGNATORY" in n for n in narrations)
    assert not any("computer-generated" in n for n in narrations)
    assert not any("Contact-Us" in n for n in narrations)


def test_the_debit_column_and_the_credit_column_are_not_confused() -> None:
    """§13.36's whole reason for existing, and §14's named guardrail.

    Swap these and every repayment enters backwards: nothing crashes, the ledger still
    balances, and the only symptom is customers who paid appearing to owe more.
    """
    statement = _parse_fixture()
    by_date = {line.txn_date: line for line in statement.lines}

    # An IOCL payment leaves the account.
    assert by_date[date(2026, 7, 4)].direction == "debit"
    assert by_date[date(2026, 7, 4)].amount == Decimal("10000.00")

    # A customer's NEFT arrives in it.
    assert by_date[date(2026, 7, 3)].direction == "credit"
    assert by_date[date(2026, 7, 3)].amount == Decimal("1234.56")


def test_amounts_are_decimal_never_float() -> None:
    """§3 rule 1 does not stop at the database boundary."""
    statement = _parse_fixture()

    for line in statement.lines:
        assert isinstance(line.amount, Decimal)
        if line.running_balance is not None:
            assert isinstance(line.running_balance, Decimal)


def test_indian_digit_grouping_parses_exactly() -> None:
    """`1,23,456.78` is lakh-grouped, not thousand-grouped, and is quoted in the CSV.

    Exactness is the assertion: `Decimal("123456.78")`, never a float that happens to print
    the same.
    """
    statement = _parse_fixture()
    cash_deposit = next(line for line in statement.lines if line.narration == "BY CASH")

    assert cash_deposit.amount == Decimal("123456.78")
    assert cash_deposit.direction == "credit"


def test_the_balance_suffix_is_stripped() -> None:
    """The column reads ` 16000.00Cr`. The `Cr` is a label, not part of the number."""
    statement = _parse_fixture()
    newest = statement.lines[0]

    assert newest.running_balance == Decimal("116000.00")


def test_quoted_fields_with_embedded_newlines_do_not_break_parsing() -> None:
    """The account holder's address spans three lines inside one cell.

    Splitting on commas returns confident garbage here -- which is why §14 forbids it by name.
    If this test passes, the `csv` module is doing its job.
    """
    statement = _parse_fixture()

    assert len(statement.lines) == 7


class TestEndpoints:
    """The opening and closing balance -- the figures Phase 21's profit bridge stands on.

    A bank balance exists nowhere else in this schema, so if these are wrong the bridge is
    wrong, and wrong in the plausible direction: a number of about the right size that nothing
    else can contradict.
    """

    def test_closing_balance_is_the_newest_line_in_file_order(self) -> None:
        statement = _parse_fixture()

        assert statement.closing_balance == Decimal("116000.00")

    def test_opening_balance_reverses_the_oldest_line_out_of_its_own_balance(self) -> None:
        """The balance *before* the first transaction is not printed anywhere.

        The oldest line is a ₹58 debit leaving ₹-103,691.34, so the period opened ₹58 higher.
        """
        statement = _parse_fixture()

        assert statement.opening_balance == Decimal("-103633.34")

    def test_the_endpoints_are_found_by_file_position_not_by_date(self) -> None:
        """**This is the bug this class was written to catch.**

        The export is newest-first and the balance column is the balance *after* each
        transaction in file order. Four lines can share one date -- the real July statement
        has four dated 1 July, whose balances run *downwards* through the file even though
        every one of them is a credit. Picking the endpoint by `min(date)` therefore selects
        an arbitrary row among the ties and reverses the wrong amount out of the wrong
        balance.

        The invariant below is what makes it self-checking, and it is the only assertion here
        that does not depend on the fixture's specific numbers: **opening + credits − debits
        must equal closing.** It held on the real file only once the endpoints came from file
        position.
        """
        statement = _parse_fixture()

        credits = sum(
            line.amount for line in statement.lines if line.direction == "credit"
        )
        debits = sum(line.amount for line in statement.lines if line.direction == "debit")

        assert statement.opening_balance is not None
        assert statement.opening_balance + credits - debits == statement.closing_balance


class TestRefusingWhatItCannotRead:
    """§13.36: an unfamiliar layout raises rather than doing its best.

    Every case here would otherwise be silent. A parser that guessed would produce rows that
    look exactly like correctly-parsed rows.
    """

    def test_an_unknown_header_is_refused_by_name(self) -> None:
        data = b"Date,Details,Amount\n01/07/2026,SOMETHING,100.00\n"

        with pytest.raises(AppError) as caught:
            parse(data, account_id=_ACCOUNT)

        assert caught.value.code == "UNRECOGNISED_STATEMENT_FORMAT"
        assert caught.value.status_code == 422

    def test_a_header_missing_the_credit_column_is_refused(self) -> None:
        """Half a format is more dangerous than none: every line would read as a debit."""
        data = (
            b"TRAN DATE,NARRATION,WITHDRAWAL(DR),BALANCE(INR)\n"
            b"01/07/2026,SOMETHING,100.00,900.00\n"
        )

        with pytest.raises(AppError) as caught:
            parse(data, account_id=_ACCOUNT)

        assert caught.value.code == "UNRECOGNISED_STATEMENT_FORMAT"
        assert "credit" in caught.value.detail

    def test_the_refusal_names_the_headers_it_did_find(self) -> None:
        """So the reader can see *why* it was refused without opening the file."""
        data = b"Date,Details,Amount\n01/07/2026,SOMETHING,100.00\n"

        with pytest.raises(AppError) as caught:
            parse(data, account_id=_ACCOUNT)

        assert "another bank's format needs its own parser" in caught.value.detail

    def test_a_line_filling_both_amount_columns_is_refused(self) -> None:
        """Exactly one side must be filled. Both means the columns are not what we think."""
        data = (
            b"TRAN DATE,NARRATION,WITHDRAWAL(DR),DEPOSIT(CR),BALANCE(INR)\n"
            b"01/07/2026,SOMETHING,100.00,200.00,900.00\n"
        )

        with pytest.raises(AppError) as caught:
            parse(data, account_id=_ACCOUNT)

        assert caught.value.code == "UNRECOGNISED_STATEMENT_FORMAT"

    def test_a_file_with_a_header_but_no_transactions_is_refused(self) -> None:
        data = b"TRAN DATE,NARRATION,WITHDRAWAL(DR),DEPOSIT(CR),BALANCE(INR)\n"

        with pytest.raises(AppError) as caught:
            parse(data, account_id=_ACCOUNT)

        assert caught.value.code == "EMPTY_STATEMENT"

    def test_a_negative_amount_is_refused(self) -> None:
        """Direction is carried by the column. A sign would put it in two places at once."""
        data = (
            b"TRAN DATE,NARRATION,WITHDRAWAL(DR),DEPOSIT(CR),BALANCE(INR)\n"
            b"01/07/2026,SOMETHING,,-200.00,900.00\n"
        )

        with pytest.raises(AppError) as caught:
            parse(data, account_id=_ACCOUNT)

        assert caught.value.code == "UNPARSEABLE_AMOUNT"


class TestFingerprints:
    """What makes re-uploading an overlapping month safe (§5.3a)."""

    def test_the_same_line_fingerprints_identically_across_reformatting(self) -> None:
        """A re-export with different spacing or casing is the same transaction."""
        first = fingerprint(
            account_id=_ACCOUNT,
            txn_date=date(2026, 7, 3),
            amount=Decimal("1234.56"),
            direction="credit",
            narration="NEFT-HDFCH01093378250-GUPTA OVERSEAS",
        )
        second = fingerprint(
            account_id=_ACCOUNT,
            txn_date=date(2026, 7, 3),
            amount=Decimal("1234.56"),
            direction="credit",
            narration="  neft-hdfch01093378250-gupta   overseas  ",
        )

        assert first == second

    def test_a_different_account_fingerprints_differently(self) -> None:
        """Account-scoped: two accounts can genuinely carry the same date, amount and text,
        and a global identity would refuse the second one's real transaction."""
        shared = {
            "txn_date": date(2026, 7, 3),
            "amount": Decimal("1234.56"),
            "direction": "credit",
            "narration": "NEFT-SOMEBODY",
        }

        assert fingerprint(account_id=_ACCOUNT, **shared) != fingerprint(
            account_id="22222222-2222-2222-2222-222222222222", **shared
        )

    def test_direction_is_part_of_the_identity(self) -> None:
        """₹1,000 in and ₹1,000 out on one day are different events."""
        shared = {
            "account_id": _ACCOUNT,
            "txn_date": date(2026, 7, 3),
            "amount": Decimal("1000.00"),
            "narration": "SOMETHING",
        }

        assert fingerprint(direction="credit", **shared) != fingerprint(
            direction="debit", **shared
        )

    def test_every_line_in_the_fixture_has_a_distinct_fingerprint(self) -> None:
        statement = _parse_fixture()

        assert len({line.fingerprint for line in statement.lines}) == len(statement.lines)


def test_narration_normalisation_collapses_whitespace_and_upper_cases() -> None:
    assert normalise_narration("  neft  -  gupta   overseas ") == "NEFT - GUPTA OVERSEAS"


def test_an_oldest_first_export_is_read_correctly_too() -> None:
    """The direction of travel is detected, not assumed.

    The endpoints come from file *position*, so a bank that exports oldest-first would have
    its opening and closing balances exactly swapped if the order were hardcoded. Same seven
    transactions as the fixture, emitted the other way up; the invariant must still hold.
    """
    header = b"TRAN DATE,NARRATION,WITHDRAWAL(DR),DEPOSIT(CR),BALANCE(INR)\n"
    body = (
        b"01/07/2026,CHARGES,58.00,, -103691.34Cr\n"
        b"01/07/2026,SHREEJI COTFABS,,40000.00, -63691.34Cr\n"
        b"01/07/2026,V V ENTERPRISES,,60000.00, -3691.34Cr\n"
        b"02/07/2026,BY CASH,,123456.78, 119765.44Cr\n"
        b"05/07/2026,PAYTM PAYMENTS SERVICE,,5000.00, 124765.44Cr\n"
    )

    statement = parse(header + body, account_id=_ACCOUNT)

    credits = sum(line.amount for line in statement.lines if line.direction == "credit")
    debits = sum(line.amount for line in statement.lines if line.direction == "debit")

    assert statement.opening_balance == Decimal("-103633.34")
    assert statement.closing_balance == Decimal("124765.44")
    assert statement.opening_balance + credits - debits == statement.closing_balance


def test_a_single_line_statement_has_coherent_endpoints() -> None:
    """One transaction: opening reverses it out, closing is its own balance."""
    data = (
        b"TRAN DATE,NARRATION,WITHDRAWAL(DR),DEPOSIT(CR),BALANCE(INR)\n"
        b"01/07/2026,ONLY LINE,,1000.00, 5000.00Cr\n"
    )

    statement = parse(data, account_id=_ACCOUNT)

    assert statement.opening_balance == Decimal("4000.00")
    assert statement.closing_balance == Decimal("5000.00")


def test_a_statement_with_no_balance_column_still_imports() -> None:
    """§5.3a makes the endpoints nullable for exactly this case.

    The lines are the point; the balances are a bonus that Phase 21 happens to need. Refusing
    a file for lacking them would refuse a perfectly good month of transactions.
    """
    data = (
        b"TRAN DATE,NARRATION,WITHDRAWAL(DR),DEPOSIT(CR)\n"
        b"01/07/2026,SOMETHING,,1000.00\n"
    )

    statement = parse(data, account_id=_ACCOUNT)

    assert len(statement.lines) == 1
    assert statement.opening_balance is None
    assert statement.closing_balance is None
