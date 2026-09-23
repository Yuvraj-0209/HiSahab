"""Classifying statement lines and reconciling them against the books (CLAUDE.md §5.3a).

Phase 20. This module turns a parsed file into rows, labels each line, and then answers three
questions the outlet has never been able to ask:

1. Did Paytm settle what the meters said we took on card and UPI?
2. Did the cash we recorded depositing actually reach the bank?
3. Is a bank credit a customer settling udhaar -- one we already recorded, or one we missed?

**Only the third can write anything**, and only on an explicit human confirmation, which lives
in the router. The first two report agreement or a difference and stop. That is not politeness:
a deposit needs a `shift_id`, so inventing one would move §6.4's expected cash for a day that is
already closed, on the strength of a file somebody uploaded (§14).

## Classification is patterns, and it refuses to be clever

The rules below are drawn from one real month of one bank's export. An unfamiliar narration
falls through to `unclassified` and appears in the review list -- **never** to `other`, which
would read as a decision somebody made. §13.39: an unclassified line is visible work, a
misclassified one is invisible error.

## The T+1 boundary, and why the matcher looks past the period end

Paytm settles a whole trading day's card *and* UPI as one credit the next morning (~10am). So
a calendar-month statement does not contain a calendar month of settlements: the last day's
money arrives on the 1st of the next month, outside the file.

A matcher scoped to "lines inside this import" would therefore report the period's **last day
as unreconciled every single month**, forever. It looks a day past instead -- and the owner's
working practice is to download with a few days' overlap so the row is there.

The settlement is found **by narration and date**, never by position or by running balance. The
owner first described it as "the entry where the balance is lowest", which was true of the file
in front of him because Paytm posted first that morning -- but that describes where it landed
rather than what it is, and a day where a cash deposit posted first would silently match the
wrong row.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.core.bank_statements import ParsedLine, normalise_narration
from app.core.config import get_settings
from app.models.bank import (
    BankAccount,
    BankSenderAlias,
    BankStatementImport,
    BankTransaction,
)
from app.models.cash import BankDeposit, DailyCashSummary
from app.models.credit import CreditCustomer, CreditRepayment
from app.models.shift import Shift
from app.services import cash as cash_service

logger = logging.getLogger(__name__)


# --- classification ------------------------------------------------------------------
#
# Ordered: the first rule that matches wins. Drawn from the real July 2026 export, and every
# pattern here is a narration a human has actually looked at.

# Paytm's three observed forms. The IMPS variant really does arrive with the leading "P"
# truncated -- `IMPS/P2A/<ref>/AYTMPAYMENTSSER/...` -- because the bank's field is too short,
# so matching on "PAYTM" alone would miss it and report that day unsettled.
_PAYTM = re.compile(r"PAYTM\s*PAYMENTS|AYTMPAYMENTSSER", re.IGNORECASE)

# "BY CASH" is a self-deposit at the branch. "BY INST ... MICR CLG" is a cheque clearing,
# which is also money the pump physically banked.
_CASH_DEPOSIT = re.compile(r"^BY CASH|^BY INST\b.*MICR", re.IGNORECASE)

# IOCL: two products, two Credit Control Areas, and they must not be merged -- §12's three
# CCA balances are tracked separately and a payment feeds exactly one of them.
_IOCL_CBG = re.compile(r"IOCL\s*CBG|CBG\s*PAYMENT", re.IGNORECASE)
_IOCL_FUEL = re.compile(r"INDIAN\s*OIL\s*CORPORATION|\bIOCL\b", re.IGNORECASE)

_BANK_CHARGE = re.compile(
    r"CHARGES?\s+FOR|SMS\s+CHARGES|CHG\s+CASH\s+HANDLING|\bSERVICE\s+CHARGE",
    re.IGNORECASE,
)
_LOAN = re.compile(r"LOAN\s+RECOVERY|\bEMI\b", re.IGNORECASE)


def classify(line: ParsedLine) -> str:
    """Label one statement line. Returns a `bank_txn_classification` value.

    Anything unrecognised is `unclassified`, which is a request for a human rather than a
    conclusion (§13.39).
    """
    narration = line.narration

    if _PAYTM.search(narration):
        # Only a credit is a settlement. A debit to Paytm would be a refund or a chargeback,
        # which is a different event and must not silently satisfy a day's reconciliation.
        if line.direction == "credit":
            return "paytm_settlement"
        return "unclassified"

    if _CASH_DEPOSIT.search(narration) and line.direction == "credit":
        return "cash_deposit"

    # CBG is checked first: "IOCL CBG PAYMENT-STATE..." matches both IOCL patterns, and the
    # more specific one has to win or every CBG payment lands in the fuel bucket.
    if _IOCL_CBG.search(narration):
        return "iocl_cbg"
    if _IOCL_FUEL.search(narration):
        return "iocl_ms_hsd"

    if _BANK_CHARGE.search(narration):
        return "bank_charge"
    if _LOAN.search(narration):
        return "loan"

    # A credit that is none of the above is a candidate for udhaar -- deliberately the
    # catch-all on the credit side. Better to propose and be corrected than to file a
    # customer's payment as `other`, where nobody would look for it.
    if line.direction == "credit":
        return "udhaar_repayment"

    # A debit that is none of the above genuinely is unknown. It might be an expense, it might
    # be a transfer; only a human can say, and `is_expense` stays `undecided` until one does.
    return "unclassified"


def suggested_expense_flag(classification: str) -> str:
    """What `is_expense` should *default* to on the review screen -- a suggestion, never an
    answer (§5.3a).

    The row itself is stored `undecided` regardless; this only pre-selects a radio button. A
    rule that wrote the column would answer a question §6.8 says only a human may answer.
    """
    if classification == "bank_charge":
        return "yes"
    if classification in {"iocl_ms_hsd", "iocl_cbg", "self_transfer", "loan"}:
        return "no"
    return "undecided"


# --- reconciliation ------------------------------------------------------------------


@dataclass(frozen=True)
class SettlementCheck:
    """One trading day's card + UPI against what Paytm actually paid."""

    business_date: date
    settled_on: date
    expected: Decimal  # card_total + upi_total, from the books
    settled: Decimal | None  # what arrived; None = no settlement found
    source: str  # "snapshot" | "computed" -- §13.20's distinction
    transaction_ids: tuple[UUID, ...]
    tolerance: Decimal

    @property
    def difference(self) -> Decimal | None:
        if self.settled is None:
            return None
        return self.settled - self.expected

    @property
    def matches(self) -> bool:
        """Within tolerance, not exactly equal.

        The register carries a figure rounded to the rupee, so an exact match is the
        exception rather than the rule -- on real July data only 3 of 29 days matched to the
        paisa while 24 were within ₹2. The owner was explicit that the gap is **not** a
        payment-gateway fee: anything larger means somebody wrote the wrong amount, and those
        are the days he wants to chase.

        Flagging all 29 would be the failure §13.23 already names for variance alerts -- a
        list where almost every row is flagged is one nobody reads.
        """
        if self.settled is None:
            return False
        return abs(self.settled - self.expected) <= self.tolerance


def _card_and_upi_for(
    db: Session, *, outlet_id: UUID, business_date: date
) -> tuple[Decimal, str] | None:
    """A day's card + UPI, read from the snapshot where one exists (§13.20, §14).

    **Never recomputes a reconciled day.** §5.2 stores the components so a reader can see what
    the manager was told, and §14 forbids recomputing them. A day with no summary is computed
    live, and the caller is told which it was -- the two are different kinds of claim.

    Returns `None` when the date has no shifts at all, which is not a discrepancy: the outlet
    was shut, and a settlement on the following day would be the real anomaly.
    """
    summary = db.execute(
        sa.select(DailyCashSummary).where(
            DailyCashSummary.outlet_id == outlet_id,
            DailyCashSummary.business_date == business_date,
        )
    ).scalar_one_or_none()

    if summary is not None:
        return summary.card_total + summary.upi_total, "snapshot"

    if not cash_service.shifts_on(db, outlet_id=outlet_id, business_date=business_date):
        return None

    totals = cash_service.day_totals(
        db, outlet_id=outlet_id, business_date=business_date
    )
    return totals.card_total + totals.upi_total, "computed"


def settlement_checks(
    db: Session,
    *,
    outlet_id: UUID,
    bank_account_id: UUID,
    date_from: date,
    date_to: date,
    tolerance: Decimal | None = None,
) -> list[SettlementCheck]:
    """Reconcile every trading day in the window against its T+1 settlement.

    `date_to` is a **trading** date, so the last day's settlement is looked for on
    `date_to + 1` -- outside a calendar-month import unless the owner downloaded with overlap.
    A missing settlement reports `settled=None` rather than zero: "Paytm has not paid yet, or
    the row is not in this file" is a different fact from "Paytm paid nothing" (§6.8).
    """
    if tolerance is None:
        tolerance = get_settings().SETTLEMENT_TOLERANCE

    settlements = _settlements_by_date(
        db,
        bank_account_id=bank_account_id,
        # One day past the window: that is where the last trading day's money lands.
        date_from=date_from + timedelta(days=1),
        date_to=date_to + timedelta(days=1),
    )

    checks: list[SettlementCheck] = []
    day = date_from
    while day <= date_to:
        expected = _card_and_upi_for(db, outlet_id=outlet_id, business_date=day)
        if expected is None:
            day += timedelta(days=1)
            continue

        amount, source = expected
        settled_on = day + timedelta(days=1)
        rows = settlements.get(settled_on, [])

        checks.append(
            SettlementCheck(
                business_date=day,
                settled_on=settled_on,
                expected=amount,
                # **Summed, never "the one row".** 14 July 2026 settled as two credits -- an
                # IMPS and an RTGS -- and assuming a single row would have invented a
                # discrepancy the size of the smaller one on a perfectly correct day.
                settled=(
                    sum((row.amount for row in rows), Decimal("0.00")) if rows else None
                ),
                source=source,
                transaction_ids=tuple(row.id for row in rows),
                tolerance=tolerance,
            )
        )
        day += timedelta(days=1)

    return checks


def _settlements_by_date(
    db: Session, *, bank_account_id: UUID, date_from: date, date_to: date
) -> dict[date, list[BankTransaction]]:
    rows = (
        db.execute(
            sa.select(BankTransaction).where(
                BankTransaction.bank_account_id == bank_account_id,
                BankTransaction.classification == "paytm_settlement",
                BankTransaction.txn_date >= date_from,
                BankTransaction.txn_date <= date_to,
            )
        )
        .scalars()
        .all()
    )

    grouped: dict[date, list[BankTransaction]] = {}
    for row in rows:
        grouped.setdefault(row.txn_date, []).append(row)
    return grouped


def boundary_settlement(
    db: Session, *, bank_account_id: UUID, period_to: date
) -> BankTransaction | None:
    """The settlement that lands the morning after a period ends -- money in transit.

    Phase 21's bridge needs this: a period's closing position is the bank balance *plus* this,
    because the last day's card takings are in the profit but not yet in the account. The
    opening position gets the same treatment at the other end, so consecutive periods chain
    with nothing double-counted.

    **Found by classification and date, not by balance or position** -- see the module
    docstring. Returns the single largest settlement if several landed, since the day's
    settlement is one payment even when the bank split it across two rows; callers wanting the
    full figure should sum `settlement_checks`.
    """
    rows = _settlements_by_date(
        db,
        bank_account_id=bank_account_id,
        date_from=period_to + timedelta(days=1),
        date_to=period_to + timedelta(days=1),
    ).get(period_to + timedelta(days=1), [])

    if not rows:
        return None
    return max(rows, key=lambda row: row.amount)


@dataclass(frozen=True)
class DepositCheck:
    """A cash deposit, as the bank saw it and as the books recorded it."""

    kind: str  # "matched" | "missing_from_books" | "missing_from_bank"
    txn_date: date
    amount: Decimal
    transaction_id: UUID | None
    bank_deposit_id: UUID | None
    # How many days after the books recorded it the bank actually saw it. Shown rather than
    # hidden: a deposit that took three days is matched but is still worth a glance.
    days_late: int | None = None


def deposit_checks(
    db: Session,
    *,
    outlet_id: UUID,
    bank_account_id: UUID,
    date_from: date,
    date_to: date,
    window_days: int | None = None,
) -> list[DepositCheck]:
    """Reconcile `BY CASH` lines against `bank_deposits` (§5.3a).

    **Creates nothing and modifies nothing.** A `bank_deposits` row needs a `shift_id`, so
    inventing one would move §6.4's expected cash for a day already closed (§14). The three
    outcomes are reported; a human decides what to do.

    Matching is on date and amount. `bank_reference` is not used as a key here because the
    bank's own narration for a branch deposit is just "BY CASH" -- there is no reference to
    match against.
    """
    if window_days is None:
        window_days = get_settings().DEPOSIT_MATCH_WINDOW_DAYS

    statement_rows = (
        db.execute(
            sa.select(BankTransaction).where(
                BankTransaction.bank_account_id == bank_account_id,
                BankTransaction.classification == "cash_deposit",
                BankTransaction.txn_date >= date_from,
                # Widened by the window: cash recorded on the last day of the period reaches
                # the bank after it, and bounding both sides identically would report that
                # deposit as missing from the bank every single month.
                BankTransaction.txn_date <= date_to + timedelta(days=window_days),
            )
        )
        .scalars()
        .all()
    )

    book_rows = (
        db.execute(
            sa.select(BankDeposit)
            .join(Shift, Shift.id == BankDeposit.shift_id)
            .where(
                Shift.outlet_id == outlet_id,
                BankDeposit.business_date >= date_from,
                BankDeposit.business_date <= date_to,
            )
        )
        .scalars()
        .all()
    )

    # **Matched on amount within a few days, not on an exact date**, because cash leaves the
    # locker on the trading day and reaches the branch the next morning. Found on real data:
    # exact-date matching reported 21 bank-only and 16 app-only deposits for July, of which
    # 14 were one-day pairs of identical amounts -- 28 false discrepancies in a list of 37,
    # which is a list nobody would read twice.
    #
    # Greedy, and ordered nearest-first so a deposit pairs with the closest candidate rather
    # than the first one found. Two identical amounts a day apart are correct whichever way
    # round they pair, and there is no third field that could distinguish them anyway.
    unmatched_books = list(book_rows)
    checks: list[DepositCheck] = []

    # Statement rows in date order, so the earliest bank line claims the earliest book row it
    # can. Pairing "nearest first" instead looks smarter and is worse: with books on the 1st
    # and 2nd and bank lines on the 2nd and 3rd, the 2nd-to-2nd pair is nearest, which strands
    # the 1st with the 3rd and reports `days_late` of 0 and 2 for what is plainly 1 and 1.
    # Everything still matches either way -- but `days_late` is the whole reason that column
    # exists, so a wrong one is worse than none.
    statement_rows = sorted(statement_rows, key=lambda row: row.txn_date)

    for row in statement_rows:
        candidates = [
            book
            for book in unmatched_books
            # The bank can only see it on or after the day the books say it left the locker.
            if book.amount == row.amount
            and 0 <= (row.txn_date - book.business_date).days <= window_days
        ]
        partner = min(
            candidates,
            key=lambda book: book.business_date,
            default=None,
        )
        if partner is not None:
            unmatched_books.remove(partner)
            checks.append(
                DepositCheck(
                    kind="matched",
                    txn_date=row.txn_date,
                    amount=row.amount,
                    transaction_id=row.id,
                    bank_deposit_id=partner.id,
                    days_late=(row.txn_date - partner.business_date).days,
                )
            )
        else:
            checks.append(
                DepositCheck(
                    kind="missing_from_books",
                    txn_date=row.txn_date,
                    amount=row.amount,
                    transaction_id=row.id,
                    bank_deposit_id=None,
                )
            )

    for book in unmatched_books:
        # Recorded as deposited, never seen by the bank. The more alarming direction of the
        # two, and the reason this reconciliation is worth running at all.
        checks.append(
            DepositCheck(
                kind="missing_from_bank",
                txn_date=book.business_date,
                amount=book.amount,
                transaction_id=None,
                bank_deposit_id=book.id,
            )
        )

    return sorted(checks, key=lambda check: (check.txn_date, check.amount))


# --- matching a credit to a customer -------------------------------------------------


@dataclass(frozen=True)
class CustomerProposal:
    """One candidate customer for an unmatched bank credit, with why."""

    credit_customer_id: UUID
    name: str
    reason: str
    confidence: str  # "high" | "medium" | "low"


@dataclass(frozen=True)
class CreditReview:
    """A bank credit that may be udhaar: already recorded, or awaiting a decision."""

    transaction_id: UUID
    txn_date: date
    narration: str
    amount: Decimal
    verified_repayment_id: UUID | None
    ambiguous: bool
    proposals: tuple[CustomerProposal, ...]


def recorded_candidates(
    db: Session, *, outlet_id: UUID, row: BankTransaction
) -> list[CreditRepayment]:
    """Every typed-in repayment this statement credit could be (§13.37).

    Bank-transfer repayments with no shift, on the line's date, for the line's amount, not
    already claimed by a *different* statement line -- a repayment verifies exactly once.

    Split out of `verify_against_recorded` in Phase 21 so the credit statement can mark every
    candidate of a tie as `ambiguous`, rather than only learning that *some* tie happened. The
    query is unchanged; the Bank screen and the statement ask it identically (§13.42).
    """
    candidates = (
        db.execute(
            sa.select(CreditRepayment)
            .join(
                CreditCustomer, CreditCustomer.id == CreditRepayment.credit_customer_id
            )
            .where(
                CreditCustomer.outlet_id == outlet_id,
                CreditRepayment.shift_id.is_(None),
                CreditRepayment.mode == "bank_transfer",
                CreditRepayment.business_date == row.txn_date,
                CreditRepayment.amount == row.amount,
                CreditRepayment.reverses_id.is_(None),
            )
        )
        .scalars()
        .all()
    )

    # Already claimed by another statement line -- a repayment verifies exactly once.
    claimed = set(
        db.execute(
            sa.select(BankTransaction.credit_repayment_id).where(
                BankTransaction.credit_repayment_id.is_not(None),
                BankTransaction.id != row.id,
            )
        )
        .scalars()
        .all()
    )
    return [row_ for row_ in candidates if row_.id not in claimed]


def resolve_candidates(
    candidates: list[CreditRepayment], *, row: BankTransaction
) -> tuple[UUID | None, bool]:
    """Pick the one candidate this line verifies, or report a tie. Pure; no queries.

    **An unresolvable tie links nothing.** Two customers paying the same amount on one day are
    genuinely indistinguishable, and an arbitrary pick would put a verification tick against
    the wrong person's ledger. A wrong tick is worse than a missing one, because it stops
    anybody looking again.
    """
    if not candidates:
        return None, False
    if len(candidates) == 1:
        return candidates[0].id, False

    # Several. A reference on both sides can still separate them.
    narration = normalise_narration(row.narration)
    referenced = [
        candidate
        for candidate in candidates
        if candidate.bank_reference
        and normalise_narration(candidate.bank_reference) in narration
    ]
    if len(referenced) == 1:
        return referenced[0].id, False

    return None, True


def verify_against_recorded(
    db: Session, *, outlet_id: UUID, row: BankTransaction
) -> tuple[UUID | None, bool]:
    """Is this statement credit a repayment somebody already typed in? (§13.37)

    Matches on `business_date` + `amount` among bank-transfer repayments with no shift, using
    `bank_reference` to break a tie when both sides carry one. Returns
    `(verified_repayment_id, ambiguous)`.
    """
    return resolve_candidates(
        recorded_candidates(db, outlet_id=outlet_id, row=row), row=row
    )


def propose_customers(
    db: Session, *, outlet_id: UUID, row: BankTransaction
) -> list[CustomerProposal]:
    """Rank customers who might have sent this credit. **A list with reasons, never an answer.**

    Three signals, in descending trust:

    * a **remembered sender** -- a fragment a human confirmed previously (§4.7's shape: the
      system predicts, a human confirms, and the prediction never writes)
    * the customer's **name inside the narration**
    * **amount equal to their outstanding balance**, as a secondary boost only -- never a match
      on its own, because two customers can owe the same round figure
    """
    narration = normalise_narration(row.narration)

    aliases = db.execute(
        sa.select(BankSenderAlias, CreditCustomer)
        .join(CreditCustomer, CreditCustomer.id == BankSenderAlias.credit_customer_id)
        .where(CreditCustomer.outlet_id == outlet_id)
    ).all()

    proposals: dict[UUID, CustomerProposal] = {}

    for alias, customer in aliases:
        if alias.fragment in narration:
            proposals[customer.id] = CustomerProposal(
                credit_customer_id=customer.id,
                name=customer.name,
                reason=f"Remembered sender “{alias.fragment}”",
                confidence="high",
            )

    customers = (
        db.execute(sa.select(CreditCustomer).where(CreditCustomer.outlet_id == outlet_id))
        .scalars()
        .all()
    )

    for customer in customers:
        if customer.id in proposals:
            continue
        name = normalise_narration(customer.name)
        # Two characters would match half the list; a real trading name is longer than that.
        if len(name) >= 4 and name in narration:
            proposals[customer.id] = CustomerProposal(
                credit_customer_id=customer.id,
                name=customer.name,
                reason="Name appears in the narration",
                confidence="medium",
            )

    return sorted(
        proposals.values(),
        key=lambda proposal: ({"high": 0, "medium": 1, "low": 2}[proposal.confidence], proposal.name),
    )


def credit_reviews(
    db: Session, *, outlet_id: UUID, bank_account_id: UUID, date_from: date, date_to: date
) -> list[CreditReview]:
    """Every bank credit that looks like udhaar, verified or awaiting a decision."""
    rows = (
        db.execute(
            sa.select(BankTransaction)
            .where(
                BankTransaction.bank_account_id == bank_account_id,
                BankTransaction.classification == "udhaar_repayment",
                BankTransaction.txn_date >= date_from,
                BankTransaction.txn_date <= date_to,
            )
            .order_by(BankTransaction.txn_date, BankTransaction.amount)
        )
        .scalars()
        .all()
    )

    reviews: list[CreditReview] = []
    for row in rows:
        if row.credit_repayment_id is not None:
            verified_id, ambiguous = row.credit_repayment_id, False
        else:
            verified_id, ambiguous = verify_against_recorded(
                db, outlet_id=outlet_id, row=row
            )

        reviews.append(
            CreditReview(
                transaction_id=row.id,
                txn_date=row.txn_date,
                narration=row.narration,
                amount=row.amount,
                verified_repayment_id=verified_id,
                ambiguous=ambiguous,
                proposals=(
                    ()
                    if verified_id is not None
                    else tuple(propose_customers(db, outlet_id=outlet_id, row=row))
                ),
            )
        )

    return reviews


# --- the credit statement's bank tick (§13.42, Phase 21) ---------------------


class RepaymentBankStatus:
    """What the bank says about one bank-transfer repayment. Strings, so they serialise as-is.

    `ambiguous` is §13.37's tie: this repayment is one of several identical candidates for one
    statement line, and nothing is ticked. `not_on_statement` and `no_statement` differ in
    whether anybody has uploaded a statement covering the date -- "the bank did not see it" and
    "we have not looked" are different facts, and collapsing them would read an un-uploaded
    month as a list of missing payments.
    """

    verified = "verified"
    ambiguous = "ambiguous"
    not_on_statement = "not_on_statement"
    no_statement = "no_statement"


def repayment_bank_status(
    db: Session,
    *,
    outlet_id: UUID,
    repayments: list[tuple[UUID, date]],
    date_from: date,
    date_to: date,
) -> dict[UUID, str]:
    """The bank's verdict on each `(repayment_id, business_date)` given. Reads only.

    A repayment is `verified` when a statement line is **linked** to it (it was created from
    that line) **or** when the Bank screen's live match resolves to it. The live half runs
    `recorded_candidates` / `resolve_candidates` -- the functions `credit_reviews` uses --
    over every unlinked `udhaar_repayment` line dated in `[date_from, date_to]`, so this screen
    and the Bank screen give the same answer by construction rather than by agreement (§13.42).

    The caller passes only original (non-reversal) `bank_transfer` rows. A reversal is not a
    bank credit, and cash, card and UPI have no single line that could verify them -- Paytm
    settles card and UPI together as one daily credit.
    """
    if not repayments:
        return {}

    ids = [repayment_id for repayment_id, _ in repayments]

    verified: set[UUID] = set(
        db.execute(
            sa.select(BankTransaction.credit_repayment_id)
            .join(BankAccount, BankAccount.id == BankTransaction.bank_account_id)
            .where(
                BankAccount.outlet_id == outlet_id,
                BankTransaction.credit_repayment_id.in_(ids),
            )
        )
        .scalars()
        .all()
    )
    ambiguous: set[UUID] = set()

    unlinked = (
        db.execute(
            sa.select(BankTransaction)
            .join(BankAccount, BankAccount.id == BankTransaction.bank_account_id)
            .where(
                BankAccount.outlet_id == outlet_id,
                BankTransaction.classification == "udhaar_repayment",
                BankTransaction.credit_repayment_id.is_(None),
                BankTransaction.txn_date >= date_from,
                BankTransaction.txn_date <= date_to,
            )
        )
        .scalars()
        .all()
    )
    for line in unlinked:
        candidates = recorded_candidates(db, outlet_id=outlet_id, row=line)
        verified_id, is_tie = resolve_candidates(candidates, row=line)
        if verified_id is not None:
            verified.add(verified_id)
        elif is_tie:
            ambiguous.update(candidate.id for candidate in candidates)

    periods = db.execute(
        sa.select(BankStatementImport.period_from, BankStatementImport.period_to)
        .join(BankAccount, BankAccount.id == BankStatementImport.bank_account_id)
        .where(BankAccount.outlet_id == outlet_id)
    ).all()

    def _covered(day: date) -> bool:
        return any(start <= day <= end for start, end in periods)

    statuses: dict[UUID, str] = {}
    for repayment_id, business_date in repayments:
        if repayment_id in verified:
            statuses[repayment_id] = RepaymentBankStatus.verified
        elif repayment_id in ambiguous:
            statuses[repayment_id] = RepaymentBankStatus.ambiguous
        elif _covered(business_date):
            statuses[repayment_id] = RepaymentBankStatus.not_on_statement
        else:
            statuses[repayment_id] = RepaymentBankStatus.no_statement
    return statuses


def remember_sender(
    db: Session, *, customer_id: UUID, narration: str, actor_id: UUID | None
) -> BankSenderAlias | None:
    """Store the identifying part of a narration against a customer a human just confirmed.

    Returns `None` when nothing usable could be extracted, or when the fragment is already
    remembered -- both are ordinary outcomes, not errors. Never called except from a
    confirmation.
    """
    fragment = _sender_fragment(narration)
    if fragment is None:
        return None

    existing = db.execute(
        sa.select(BankSenderAlias).where(
            BankSenderAlias.credit_customer_id == customer_id,
            BankSenderAlias.fragment == fragment,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    alias = BankSenderAlias(
        credit_customer_id=customer_id, fragment=fragment, created_by=actor_id
    )
    db.add(alias)
    return alias


# A transaction reference: long runs of digits, or letter-digit soup like YESAP62115537142.
# These identify *one payment*, never a payer, so remembering one would make a memory that can
# never hit again.
_REFERENCE_LIKE = re.compile(r"^[A-Z]*\d[A-Z0-9]*$")

# A masked account number -- `XXXXXXXXXX1925`. The bank has anonymised the payer, so there is
# no name in the narration at all and the honest answer is that we do not know who sent it.
_MASKED_LIKE = re.compile(r"^X{3,}")

# Payment rails and scheme codes. These appear in *every* narration of their kind, so
# remembering one would attach every IMPS (or NEFT, or RTGS) transfer from anybody to a single
# customer -- a memory that hits everything is worse than no memory at all.
_NOT_A_PAYER = frozenset(
    {
        "NEFT",
        "IMPS",
        "RTGS",
        "UPI",
        "P2A",
        "P2P",
        "EBANK",
        "SBCOLLECT",
        "CTS",
        "MICR",
        "CLG",
        "INST",
        "BY CASH",
    }
)


def _sender_fragment(narration: str) -> str | None:
    """The part of a narration that names who sent the money, or `None` when nothing does.

    `NEFT-HDFCH01131905980-GUPTA OVERSEAS` -> `GUPTA OVERSEAS`. The bank puts the payer last,
    after a transaction reference, so the approach is to split on the separators and keep the
    longest part that could plausibly be somebody's name.

    **Returning `None` is a real outcome, not a failure.** `IMPS/P2A/619515238400/
    XXXXXXXXXX1925/0` carries no payer -- the account is masked and there is no name anywhere
    in it. An earlier version returned `'IMPS'` here, which as a remembered fragment would
    have matched every IMPS transfer from anybody and attached them all to one customer. A
    memory that hits everything is worse than none.
    """
    normalised = normalise_narration(narration)
    parts = [part.strip() for part in re.split(r"[-/:]", normalised) if part.strip()]

    candidates = [
        part
        for part in parts
        # Four characters filters initials without excluding a short trading name.
        if len(part) >= 4
        and part not in _NOT_A_PAYER
        and not _REFERENCE_LIKE.match(part.replace(" ", ""))
        and not _MASKED_LIKE.match(part)
    ]
    if not candidates:
        return None

    return max(candidates, key=len)
