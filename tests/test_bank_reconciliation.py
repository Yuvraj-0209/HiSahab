"""Reconciling statement lines against the books (CLAUDE.md §5.3a, §6.4, §14).

Three reconciliations, and the rule that governs all of them: **only the credit-matching one
may ever write, and only on a human's confirmation.** These tests assert the *absence* of
writes as carefully as they assert the figures, because a reconciliation that quietly created
a `bank_deposits` row would move §6.4's expected cash for a day already closed -- on the
strength of a file somebody uploaded (§14).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, text

from app.services import bank as bank_service

JULY = date(2026, 7, 1)


def _outlet() -> UUID:
    from app.core.config import get_settings

    return get_settings().DEFAULT_OUTLET_ID


def _call(fn, **kwargs):
    """Run a service function in its own session, as `test_reporting_service.py` does."""
    from app.db.session import SessionLocal

    with SessionLocal() as session:
        return fn(session, **kwargs)


@pytest.fixture
def bank_account(engine: Engine) -> Iterator[UUID]:
    account_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO bank_accounts (id, outlet_id, label, bank_name) "
                "VALUES (:id, :outlet, :label, 'Bank of Baroda')"
            ).bindparams(id=account_id, outlet=_outlet(), label=f"Acct {account_id.hex[:8]}")
        )
    try:
        yield account_id
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM bank_transactions WHERE bank_account_id = :id").bindparams(
                    id=account_id
                )
            )
            connection.execute(
                text(
                    "DELETE FROM bank_statement_imports WHERE bank_account_id = :id"
                ).bindparams(id=account_id)
            )
            connection.execute(
                text("DELETE FROM bank_accounts WHERE id = :id").bindparams(id=account_id)
            )


@pytest.fixture
def make_bank_txn(engine: Engine, bank_account: UUID) -> Iterator[Callable[..., UUID]]:
    """Insert a statement line directly, bypassing the importer."""
    import_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO bank_statement_imports (id, bank_account_id, period_from, "
                "period_to, row_count, imported_count, skipped_count) "
                "VALUES (:id, :account, '2026-07-01', '2026-08-01', 0, 0, 0)"
            ).bindparams(id=import_id, account=bank_account)
        )

    def _make(
        txn_date: date,
        amount: str,
        *,
        direction: str = "credit",
        narration: str = "SOMETHING",
        classification: str = "unclassified",
        credit_repayment_id: UUID | None = None,
    ) -> UUID:
        txn_id = uuid4()
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO bank_transactions (id, bank_account_id, import_id, "
                    "txn_date, narration, amount, direction, classification, fingerprint, "
                    "credit_repayment_id) VALUES (:id, :account, :import_id, :d, :narration, "
                    "CAST(:amount AS numeric), CAST(:direction AS bank_txn_direction), "
                    "CAST(:classification AS bank_txn_classification), :fp, :repayment)"
                ).bindparams(
                    id=txn_id,
                    account=bank_account,
                    import_id=import_id,
                    d=txn_date,
                    narration=narration,
                    amount=amount,
                    direction=direction,
                    classification=classification,
                    fp=txn_id.hex,
                    repayment=credit_repayment_id,
                )
            )
        return txn_id

    yield _make


def _shift_with_collections(
    engine: Engine,
    make_shift,
    make_collection,
    make_user,
    *,
    business_date: date,
    card: str,
    upi: str,
) -> UUID:
    attendant = make_user("attendant")
    shift = make_shift(
        business_date=business_date,
        attendant_id=attendant,
        started_at=datetime.combine(business_date, time(6, 0), tzinfo=timezone.utc),
    )
    make_collection(shift, mode="card", amount=card)
    make_collection(shift, mode="upi", amount=upi)
    return shift


class TestPaytmSettlements:
    """§6.4's card + UPI against what Paytm actually paid, T+1."""

    def test_a_day_that_settles_exactly_reports_a_match(
        self, engine, bank_account, make_bank_txn, make_shift, make_collection, make_user
    ) -> None:
        _shift_with_collections(
            engine,
            make_shift,
            make_collection,
            make_user,
            business_date=JULY,
            card="20000.00",
            upi="10000.00",
        )
        make_bank_txn(
            JULY + timedelta(days=1),
            "30000.00",
            narration="RTGS-YESBR1-PAYTM PAYMENTS SERVICE",
            classification="paytm_settlement",
        )

        checks = _call(
            bank_service.settlement_checks,
            outlet_id=_outlet(),
            bank_account_id=bank_account,
            date_from=JULY,
            date_to=JULY,
        )

        assert len(checks) == 1
        assert checks[0].expected == Decimal("30000.00")
        assert checks[0].settled == Decimal("30000.00")
        assert checks[0].matches
        assert checks[0].difference == Decimal("0.00")

    def test_a_settlement_split_across_two_rows_is_summed(
        self, engine, bank_account, make_bank_txn, make_shift, make_collection, make_user
    ) -> None:
        """**14 July 2026 really did this**, and it is why the matcher must not take one row.

        The day settled as an IMPS of ₹61,376.94 plus an RTGS of ₹295,819.86. Assuming a
        single row would have reported a shortfall the size of the smaller one on a day that
        was perfectly correct -- and a phantom discrepancy trains a manager to ignore the list.
        """
        _shift_with_collections(
            engine,
            make_shift,
            make_collection,
            make_user,
            business_date=JULY,
            card="295819.86",
            upi="61376.94",
        )
        settled_on = JULY + timedelta(days=1)
        make_bank_txn(
            settled_on,
            "61376.94",
            narration="IMPS/P2A/619515552408/AYTMPAYMENTSSER/IMPS",
            classification="paytm_settlement",
        )
        make_bank_txn(
            settled_on,
            "295819.86",
            narration="RTGS-YESBR1-PAYTM PAYMENTS SERVICE",
            classification="paytm_settlement",
        )

        checks = _call(
            bank_service.settlement_checks,
            outlet_id=_outlet(),
            bank_account_id=bank_account,
            date_from=JULY,
            date_to=JULY,
        )

        assert checks[0].settled == Decimal("357196.80")
        assert checks[0].matches
        assert len(checks[0].transaction_ids) == 2

    def test_the_last_day_of_a_window_is_settled_from_the_day_after(
        self, engine, bank_account, make_bank_txn, make_shift, make_collection, make_user
    ) -> None:
        """**The structural bug this design exists to avoid.**

        A matcher scoped to "lines inside this import" reports the period's last day as
        unreconciled *every single month*, forever, because T+1 money lands outside the file.
        Here the window ends on the trading day and the settlement is a day later.
        """
        _shift_with_collections(
            engine,
            make_shift,
            make_collection,
            make_user,
            business_date=JULY,
            card="15000.00",
            upi="5000.00",
        )
        make_bank_txn(
            JULY + timedelta(days=1),
            "20000.00",
            narration="RTGS-PAYTM PAYMENTS SERVICE",
            classification="paytm_settlement",
        )

        checks = _call(
            bank_service.settlement_checks,
            outlet_id=_outlet(),
            bank_account_id=bank_account,
            date_from=JULY,
            date_to=JULY,
        )

        assert checks[0].settled_on == JULY + timedelta(days=1)
        assert checks[0].matches

    def test_a_missing_settlement_reports_none_not_zero(
        self, engine, bank_account, make_bank_txn, make_shift, make_collection, make_user
    ) -> None:
        """§6.8's rule: "Paytm has not paid yet" is a different fact from "Paytm paid nothing".

        Zero would read as a discrepancy the size of the whole day; `None` reads as what it
        is, which is that the row is not in this file.
        """
        _shift_with_collections(
            engine,
            make_shift,
            make_collection,
            make_user,
            business_date=JULY,
            card="20000.00",
            upi="10000.00",
        )

        checks = _call(
            bank_service.settlement_checks,
            outlet_id=_outlet(),
            bank_account_id=bank_account,
            date_from=JULY,
            date_to=JULY,
        )

        assert checks[0].settled is None
        assert checks[0].difference is None
        assert not checks[0].matches

    def test_a_day_with_no_shifts_is_not_a_discrepancy(
        self, engine, bank_account, make_bank_txn
    ) -> None:
        """The outlet was shut. Reporting it would be noise, and §13.20 draws the same line."""
        checks = _call(
            bank_service.settlement_checks,
            outlet_id=_outlet(),
            bank_account_id=bank_account,
            date_from=JULY,
            date_to=JULY,
        )

        assert checks == []

    def test_a_reconciled_day_is_read_from_the_snapshot_not_recomputed(
        self,
        engine,
        bank_account,
        make_bank_txn,
        make_shift,
        make_collection,
        make_user,
        make_daily_summary,
    ) -> None:
        """§14 forbids recomputing a reconciled day's stored components.

        The summary here deliberately disagrees with the live collections. The check must use
        the *stored* figure -- what the manager was told on the day -- and say so via `source`.
        """
        _shift_with_collections(
            engine,
            make_shift,
            make_collection,
            make_user,
            business_date=JULY,
            card="20000.00",
            upi="10000.00",
        )
        make_daily_summary(
            business_date=JULY, card_total="18000.00", upi_total="9000.00"
        )

        checks = _call(
            bank_service.settlement_checks,
            outlet_id=_outlet(),
            bank_account_id=bank_account,
            date_from=JULY,
            date_to=JULY,
        )

        assert checks[0].source == "snapshot"
        assert checks[0].expected == Decimal("27000.00")

    def test_an_unreconciled_day_is_computed_live_and_says_so(
        self, engine, bank_account, make_bank_txn, make_shift, make_collection, make_user
    ) -> None:
        """§13.20's distinction: a computed figure is an estimate of a day still in motion."""
        _shift_with_collections(
            engine,
            make_shift,
            make_collection,
            make_user,
            business_date=JULY,
            card="20000.00",
            upi="10000.00",
        )

        checks = _call(
            bank_service.settlement_checks,
            outlet_id=_outlet(),
            bank_account_id=bank_account,
            date_from=JULY,
            date_to=JULY,
        )

        assert checks[0].source == "computed"
        assert checks[0].expected == Decimal("30000.00")


class TestBoundarySettlement:
    """Money in transit -- the term that makes Phase 21's bridge close."""

    def test_it_finds_the_settlement_landing_the_morning_after(
        self, bank_account, make_bank_txn
    ) -> None:
        make_bank_txn(
            date(2026, 8, 1),
            "233680.26",
            narration="RTGS-YESBR12026080100014644-PAYTM PAYMENTS SERVICE",
            classification="paytm_settlement",
        )

        row = _call(
            bank_service.boundary_settlement,
            bank_account_id=bank_account,
            period_to=date(2026, 7, 31),
        )

        assert row is not None
        assert row.amount == Decimal("233680.26")

    def test_it_ignores_everything_else_on_that_morning(
        self, bank_account, make_bank_txn
    ) -> None:
        """**The owner's explicit instruction**, and the two exclusions have different reasons.

        On 1 Aug 2026 the real file carries the settlement plus two cash deposits and two
        udhaar repayments. The deposits are July's cash but are already counted in the
        cash-in-hand bucket; the repayments are the fortnightly bill for 15-31 July, already
        counted as July credit sales. Taking either here would double-count it.

        Found by classification, never by position or balance -- the owner first described it
        as "the entry where the balance is lowest", which was true of his file only because
        Paytm happened to post first that morning.
        """
        settled_on = date(2026, 8, 1)
        make_bank_txn(
            settled_on,
            "233680.26",
            narration="RTGS-PAYTM PAYMENTS SERVICE",
            classification="paytm_settlement",
        )
        make_bank_txn(
            settled_on, "250000.00", narration="BY CASH", classification="cash_deposit"
        )
        make_bank_txn(
            settled_on,
            "55961.00",
            narration="NEFT-HDFCH01163020800-GUPTA OVERSEAS",
            classification="udhaar_repayment",
        )

        row = _call(
            bank_service.boundary_settlement,
            bank_account_id=bank_account,
            period_to=date(2026, 7, 31),
        )

        assert row is not None
        assert row.amount == Decimal("233680.26")

    def test_it_returns_none_when_the_statement_stops_at_the_period_end(
        self, bank_account, make_bank_txn
    ) -> None:
        """A 1-31 July download has no 1 August row. The caller must handle the absence
        rather than being handed a zero it would treat as a real figure."""
        row = _call(
            bank_service.boundary_settlement,
            bank_account_id=bank_account,
            period_to=date(2026, 7, 31),
        )

        assert row is None


class TestDepositReconciliation:
    """`BY CASH` against `bank_deposits` -- and the writes that must not happen."""

    def test_a_matching_deposit_is_paired(
        self, engine, bank_account, make_bank_txn, make_shift, make_user, make_bank_deposit
    ) -> None:
        attendant = make_user("attendant")
        shift = make_shift(business_date=JULY, attendant_id=attendant)
        make_bank_deposit(shift, amount="50000.00", business_date=JULY)
        make_bank_txn(
            JULY, "50000.00", narration="BY CASH", classification="cash_deposit"
        )

        checks = _call(
            bank_service.deposit_checks,
            outlet_id=_outlet(),
            bank_account_id=bank_account,
            date_from=JULY,
            date_to=JULY,
        )

        assert len(checks) == 1
        assert checks[0].kind == "matched"

    def test_a_deposit_the_books_never_recorded_is_surfaced(
        self, bank_account, make_bank_txn
    ) -> None:
        make_bank_txn(
            JULY, "50000.00", narration="BY CASH", classification="cash_deposit"
        )

        checks = _call(
            bank_service.deposit_checks,
            outlet_id=_outlet(),
            bank_account_id=bank_account,
            date_from=JULY,
            date_to=JULY,
        )

        assert [check.kind for check in checks] == ["missing_from_books"]

    def test_a_deposit_that_never_reached_the_bank_is_surfaced(
        self, engine, bank_account, make_shift, make_user, make_bank_deposit
    ) -> None:
        """The more alarming direction, and the reason this reconciliation is worth running."""
        attendant = make_user("attendant")
        shift = make_shift(business_date=JULY, attendant_id=attendant)
        make_bank_deposit(shift, amount="50000.00", business_date=JULY)

        checks = _call(
            bank_service.deposit_checks,
            outlet_id=_outlet(),
            bank_account_id=bank_account,
            date_from=JULY,
            date_to=JULY,
        )

        assert [check.kind for check in checks] == ["missing_from_bank"]

    def test_reconciling_creates_no_bank_deposit_row(
        self, engine, bank_account, make_bank_txn
    ) -> None:
        """**§14's rule, asserted rather than trusted.**

        A `bank_deposits` row needs a `shift_id`, so inventing one would move §6.4's expected
        cash for a day already closed. The statement line stays a statement line.
        """
        with engine.connect() as connection:
            before = connection.execute(
                text("SELECT count(*) FROM bank_deposits")
            ).scalar_one()

        make_bank_txn(
            JULY, "50000.00", narration="BY CASH", classification="cash_deposit"
        )
        _call(
            bank_service.deposit_checks,
            outlet_id=_outlet(),
            bank_account_id=bank_account,
            date_from=JULY,
            date_to=JULY,
        )

        with engine.connect() as connection:
            after = connection.execute(
                text("SELECT count(*) FROM bank_deposits")
            ).scalar_one()

        assert after == before


class TestVerifyingRecordedRepayments:
    """The owner's headline request: did the transfer I typed in actually arrive? (§13.37)"""

    def test_a_unique_date_and_amount_verifies_an_existing_repayment(
        self, bank_account, make_bank_txn, make_credit_customer, make_credit_repayment
    ) -> None:
        customer = make_credit_customer(name="Gupta Overseas")
        repayment = make_credit_repayment(
            None,
            customer,
            amount="34599.00",
            mode="bank_transfer",
            business_date=date(2026, 7, 16),
        )
        make_bank_txn(
            date(2026, 7, 16),
            "34599.00",
            narration="NEFT-HDFCH01131905980-GUPTA OVERSEAS",
            classification="udhaar_repayment",
        )

        reviews = _call(
            bank_service.credit_reviews,
            outlet_id=_outlet(),
            bank_account_id=bank_account,
            date_from=date(2026, 7, 16),
            date_to=date(2026, 7, 16),
        )

        assert len(reviews) == 1
        assert reviews[0].verified_repayment_id == repayment
        assert not reviews[0].ambiguous

    def test_two_identical_repayments_on_one_day_verify_nothing(
        self, bank_account, make_bank_txn, make_credit_customer, make_credit_repayment
    ) -> None:
        """**A wrong tick is worse than a missing one**, because it stops anybody looking.

        Two customers paying the same amount on the same day are genuinely indistinguishable
        without a reference, so neither is linked and both are shown to a human (§13.37).
        """
        first = make_credit_customer(name="Alpha Traders")
        second = make_credit_customer(name="Beta Traders")
        make_credit_repayment(
            None,
            first,
            amount="10000.00",
            mode="bank_transfer",
            business_date=date(2026, 7, 16),
        )
        make_credit_repayment(
            None,
            second,
            amount="10000.00",
            mode="bank_transfer",
            business_date=date(2026, 7, 16),
        )
        make_bank_txn(
            date(2026, 7, 16),
            "10000.00",
            narration="NEFT-SOMEBODY",
            classification="udhaar_repayment",
        )

        reviews = _call(
            bank_service.credit_reviews,
            outlet_id=_outlet(),
            bank_account_id=bank_account,
            date_from=date(2026, 7, 16),
            date_to=date(2026, 7, 16),
        )

        assert reviews[0].verified_repayment_id is None
        assert reviews[0].ambiguous

    def test_a_cash_repayment_is_never_verified_against_a_bank_line(
        self, bank_account, make_bank_txn, make_credit_customer, make_credit_repayment,
        make_shift, make_user
    ) -> None:
        """Cash lands in a drawer, not in the bank. Matching one here would claim the bank
        confirmed money it never saw."""
        attendant = make_user("attendant")
        shift = make_shift(business_date=date(2026, 7, 16), attendant_id=attendant)
        customer = make_credit_customer(name="Cash Payer")
        make_credit_repayment(shift, customer, amount="5000.00", mode="cash")
        make_bank_txn(
            date(2026, 7, 16),
            "5000.00",
            narration="NEFT-SOMEBODY",
            classification="udhaar_repayment",
        )

        reviews = _call(
            bank_service.credit_reviews,
            outlet_id=_outlet(),
            bank_account_id=bank_account,
            date_from=date(2026, 7, 16),
            date_to=date(2026, 7, 16),
        )

        assert reviews[0].verified_repayment_id is None


class TestProposingCustomers:
    """A ranked list with reasons, never an answer (§4.7)."""

    def test_a_name_in_the_narration_proposes_that_customer(
        self, bank_account, make_bank_txn, make_credit_customer
    ) -> None:
        customer = make_credit_customer(name="Gupta Overseas")
        make_bank_txn(
            date(2026, 7, 16),
            "34599.00",
            narration="NEFT-HDFCH01131905980-GUPTA OVERSEAS",
            classification="udhaar_repayment",
        )

        reviews = _call(
            bank_service.credit_reviews,
            outlet_id=_outlet(),
            bank_account_id=bank_account,
            date_from=date(2026, 7, 16),
            date_to=date(2026, 7, 16),
        )

        assert [p.credit_customer_id for p in reviews[0].proposals] == [customer]
        assert reviews[0].proposals[0].confidence == "medium"

    def test_an_unrecognisable_narration_proposes_nobody(
        self, bank_account, make_bank_txn, make_credit_customer
    ) -> None:
        """Proposing a wrong customer is worse than proposing none -- a tick is easy to give."""
        make_credit_customer(name="Gupta Overseas")
        make_bank_txn(
            date(2026, 7, 16),
            "1000.00",
            narration="IMPS/P2A/619515238400/XXXXXXXXXX1925/0",
            classification="udhaar_repayment",
        )

        reviews = _call(
            bank_service.credit_reviews,
            outlet_id=_outlet(),
            bank_account_id=bank_account,
            date_from=date(2026, 7, 16),
            date_to=date(2026, 7, 16),
        )

        assert reviews[0].proposals == ()

    def test_a_remembered_sender_outranks_a_name_match(
        self, engine, bank_account, make_bank_txn, make_credit_customer
    ) -> None:
        """§4.7's shape: the system predicts from what a human confirmed before, and the
        prediction still does not write."""
        remembered = make_credit_customer(name="Some Other Name")
        make_credit_customer(name="Gupta Overseas")
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO bank_sender_aliases (credit_customer_id, fragment) "
                    "VALUES (:customer, 'GUPTA OVERSEAS')"
                ).bindparams(customer=remembered)
            )

        make_bank_txn(
            date(2026, 7, 16),
            "34599.00",
            narration="NEFT-HDFCH01131905980-GUPTA OVERSEAS",
            classification="udhaar_repayment",
        )

        reviews = _call(
            bank_service.credit_reviews,
            outlet_id=_outlet(),
            bank_account_id=bank_account,
            date_from=date(2026, 7, 16),
            date_to=date(2026, 7, 16),
        )

        assert reviews[0].proposals[0].credit_customer_id == remembered
        assert reviews[0].proposals[0].confidence == "high"

    def test_a_truncated_payer_name_still_matches_the_full_narration(
        self, engine, bank_account, make_bank_txn, make_credit_customer
    ) -> None:
        """The bank truncates the payer field at ~50 characters, so one customer can produce
        two fragments -- `SANT INDERMANI ENTERPR` and `SANT INDERMANI ENTERPRISES`.

        Substring containment resolves it: the shorter alias matches the longer narration, so
        confirming either form covers both.
        """
        customer = make_credit_customer(name="Sant Indermani Enterprises")
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO bank_sender_aliases (credit_customer_id, fragment) "
                    "VALUES (:customer, 'SANT INDERMANI ENTERPR')"
                ).bindparams(customer=customer)
            )

        make_bank_txn(
            date(2026, 7, 14),
            "100000.00",
            narration="NEFT-HDFCH01127105101-SANT INDERMANI ENTERPRISES",
            classification="udhaar_repayment",
        )

        reviews = _call(
            bank_service.credit_reviews,
            outlet_id=_outlet(),
            bank_account_id=bank_account,
            date_from=date(2026, 7, 14),
            date_to=date(2026, 7, 14),
        )

        assert reviews[0].proposals[0].credit_customer_id == customer
        assert reviews[0].proposals[0].confidence == "high"
