"""Classifying statement lines and extracting who sent the money (CLAUDE.md §5.3a, §13.39).

Pure-function tests over `app/services/bank.py`'s labelling half -- no database needed. The
narrations here are real ones from the July 2026 export with amounts and reference numbers
left as they were; they are the bank's own text, which is the only thing that makes a test of
a pattern-matcher worth anything.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.core.bank_statements import ParsedLine
from app.services.bank import _sender_fragment, classify, suggested_expense_flag


def _line(narration: str, direction: str = "credit") -> ParsedLine:
    return ParsedLine(
        txn_date=date(2026, 7, 15),
        narration=narration,
        amount=Decimal("1000.00"),
        direction=direction,
        running_balance=None,
        fingerprint="x",
    )


class TestPaytm:
    """The settlement is the largest single category in the file and drives §6.4's check."""

    @pytest.mark.parametrize(
        "narration",
        [
            "RTGS-YESBR12026073100012383-PAYTM PAYMENTS SERVICE",
            "NEFT-YESAP62115537142-PAYTM PAYMENTS SERVICES LIMI",
            "IMPS/P2A/619515552408/AYTMPAYMENTSSER/IMPSAXB9199",
        ],
    )
    def test_all_three_observed_forms_are_recognised(self, narration: str) -> None:
        """The IMPS form arrives with the leading "P" truncated -- `AYTMPAYMENTSSER`.

        The bank's field is too short for the full name. Matching on "PAYTM" alone would miss
        it, and 14 July 2026 -- which settled as two rows, an IMPS and an RTGS -- would report
        a shortfall the size of the IMPS on a day that was perfectly correct.
        """
        assert classify(_line(narration)) == "paytm_settlement"

    def test_a_debit_to_paytm_is_not_a_settlement(self) -> None:
        """A refund or chargeback is a different event and must not satisfy a day's check."""
        assert (
            classify(_line("RTGS-PAYTM PAYMENTS SERVICE", direction="debit"))
            != "paytm_settlement"
        )


class TestIocl:
    """Two products, two Credit Control Areas, and §12 tracks their balances separately."""

    def test_cbg_wins_over_the_generic_iocl_pattern(self) -> None:
        """`IOCL CBG PAYMENT-STATE...` matches both patterns; the specific one must win.

        Otherwise every CBG payment lands in the MS/HSD bucket and two of §12's three CCA
        balances are wrong in opposite directions.
        """
        narration = "RTGS-BARBR52026070400952174-IOCL CBG PAYMENT-STATE"

        assert classify(_line(narration, direction="debit")) == "iocl_cbg"

    def test_a_fuel_payment_is_ms_hsd(self) -> None:
        narration = "RTGS-BARBR52026072800840004-INDIAN OIL CORPORATION"

        assert classify(_line(narration, direction="debit")) == "iocl_ms_hsd"


@pytest.mark.parametrize(
    ("narration", "expected"),
    [
        ("BY CASH", "cash_deposit"),
        ("BY INST 000527 - MICR CLG (CTS)", "cash_deposit"),
        ("Charges for PORD Customer Payment :003835771281", "bank_charge"),
        ("SMS Charges for MAY 26", "bank_charge"),
        ("Chg Cash handling for:29-06-2026", "bank_charge"),
    ],
)
def test_the_remaining_recognised_forms(narration: str, expected: str) -> None:
    direction = "credit" if expected == "cash_deposit" else "debit"

    assert classify(_line(narration, direction=direction)) == expected


def test_loan_recovery_is_its_own_category(self=None) -> None:
    """Repaying principal is money leaving the bank but not a cost of trading."""
    assert classify(_line("Loan Recovery For38100600001500", direction="debit")) == "loan"


class TestTheCatchAlls:
    """What happens to a narration nobody anticipated -- §13.39's whole subject."""

    def test_an_unrecognised_credit_becomes_an_udhaar_candidate(self) -> None:
        """Deliberately the catch-all on the credit side.

        Better to propose a customer and be corrected than to file somebody's payment as
        `other`, where nobody would ever look for it.
        """
        assert (
            classify(_line("NEFT-HDFCH01131905980-GUPTA OVERSEAS"))
            == "udhaar_repayment"
        )

    def test_an_unrecognised_debit_is_unclassified_never_other(self) -> None:
        """`other` would read as a decision somebody made. `unclassified` asks for a human.

        An unclassified line is visible work; a misclassified one is invisible error.
        """
        narration = "EBANK:1518329928/SBCOLLECT/DBBRFRL1QLSN86/BILLDES"

        assert classify(_line(narration, direction="debit")) == "unclassified"


class TestTheExpenseSuggestion:
    """`is_expense` is only ever *suggested* here -- the row stores `undecided` regardless."""

    def test_a_bank_charge_suggests_yes(self) -> None:
        assert suggested_expense_flag("bank_charge") == "yes"

    @pytest.mark.parametrize(
        "classification", ["iocl_ms_hsd", "iocl_cbg", "self_transfer", "loan"]
    )
    def test_money_moving_between_the_owners_own_pockets_suggests_no(
        self, classification: str
    ) -> None:
        """An IOCL top-up is a bucket movement, not a cost (§12).

        Counting it as an expense would understate profit by the whole advance -- and §6.4
        would invent a daily cash shortage that never happened.
        """
        assert suggested_expense_flag(classification) == "no"

    def test_anything_else_suggests_nothing(self) -> None:
        """No suggestion is itself the right answer when the system genuinely cannot tell."""
        assert suggested_expense_flag("unclassified") == "undecided"


class TestSenderFragments:
    """What gets remembered against a customer when a human confirms a match.

    A fragment must identify a *payer*, never a payment. Remembering a transaction reference
    would make a memory that can never hit again; remembering a payment *mode* would make one
    that hits everything.
    """

    @pytest.mark.parametrize(
        ("narration", "expected"),
        [
            ("NEFT-HDFCH01131905980-GUPTA OVERSEAS", "GUPTA OVERSEAS"),
            ("NEFT-HDFCH01093378250-V V ENTERPRISES", "V V ENTERPRISES"),
            ("NEFT-AXODH20238804661-SHREEJI COTFABS", "SHREEJI COTFABS"),
            ("NEFT-SBIN326201241754-GRM FOODKRAFT PVT LTD", "GRM FOODKRAFT PVT LTD"),
            ("SB20260720763000/STAR INTERNATIONAL/", "STAR INTERNATIONAL"),
            ("IMPS/P2A/621114290912/KRISHNA CONSTRU/Jattal dies", "KRISHNA CONSTRU"),
        ],
    )
    def test_the_payer_is_extracted_from_a_real_narration(
        self, narration: str, expected: str
    ) -> None:
        assert _sender_fragment(narration) == expected

    def test_the_same_payer_extracts_identically_across_months(self) -> None:
        """The whole value of the memory: July's fragment must match August's.

        These two are real -- Gupta Overseas paid on 16 July and again on 1 August, through
        different transaction references.
        """
        july = _sender_fragment("NEFT-HDFCH01131905980-GUPTA OVERSEAS")
        august = _sender_fragment("NEFT-HDFCH01163020800-GUPTA OVERSEAS")

        assert july == august == "GUPTA OVERSEAS"

    def test_a_masked_account_yields_no_fragment(self) -> None:
        """**The bug this class was written to catch.**

        `IMPS/P2A/619515238400/XXXXXXXXXX1925/0` carries no payer at all -- the bank has
        masked the account number and there is no name anywhere in it. The first version
        returned `'IMPS'`, the payment *mode*, which as a remembered fragment would match
        every IMPS transfer from anybody and attach them all to one customer.

        `None` is the honest answer: nothing here identifies who sent the money.
        """
        assert _sender_fragment("IMPS/P2A/619515238400/XXXXXXXXXX1925/0") is None

    def test_a_pure_reference_narration_yields_no_fragment(self) -> None:
        assert _sender_fragment("RTGS-BARBR52026072800840004-") is None
