"""§6.6's outstanding balance: the arithmetic, against a real database.

The number this whole phase exists to produce, and the one §14 says will be checked first by
the owner -- so it is tested directly against `app/services/credit.py` rather than only
through whatever an endpoint chooses to return. The routes that expose it land in Step 9 and
have their own tests; these hold the definition still underneath them.

`SessionLocal` rather than a mock, following `tests/test_audit.py`: the whole question here
is what the database sums, and a mocked session would be testing the mock.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from decimal import Decimal
from uuid import UUID

import pytest

pytestmark = pytest.mark.anyio

DAY = date(2026, 9, 4)


def _outstanding(customer_id: UUID) -> Decimal:
    from app.db.session import SessionLocal
    from app.services import credit as credit_service

    with SessionLocal() as session:
        return credit_service.outstanding(session, customer_id=customer_id)


def _outstanding_by_customer(outlet_id: UUID) -> dict[UUID, Decimal]:
    from app.db.session import SessionLocal
    from app.services import credit as credit_service

    with SessionLocal() as session:
        return credit_service.outstanding_by_customer(session, outlet_id=outlet_id)


# --- the basic shape ---------------------------------------------------------


async def test_a_customer_with_no_rows_owes_nothing(
    make_credit_customer: Callable[..., UUID],
) -> None:
    """₹0.00, not None and not an error. A new customer is a real answer."""
    customer = make_credit_customer(name="Fresh")

    assert _outstanding(customer) == Decimal("0.00")


async def test_one_sale_is_the_whole_balance(
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_sale: Callable[..., UUID],
) -> None:
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    customer = make_credit_customer(name="Ramesh")
    make_credit_sale(
        shift, customer, make_attachment(attendant), amount="2500.00"
    )

    assert _outstanding(customer) == Decimal("2500.00")


async def test_outstanding_is_correct_after_a_partial_repayment(
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_sale: Callable[..., UUID],
    make_credit_repayment: Callable[..., UUID],
) -> None:
    """§10's named case."""
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    customer = make_credit_customer(name="Partial")
    make_credit_sale(shift, customer, make_attachment(attendant), amount="5000.00")
    make_credit_repayment(shift, customer, amount="1800.00")

    assert _outstanding(customer) == Decimal("3200.00")


async def test_paise_survive_the_round_trip(
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_sale: Callable[..., UUID],
    make_credit_repayment: Callable[..., UUID],
) -> None:
    """§3 rule 1. In binary floating point 0.1 + 0.2 != 0.3, and in a cash system that
    compounds into unexplainable variance -- so the assertion is on the exact Decimal."""
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    customer = make_credit_customer(name="Paise")
    make_credit_sale(shift, customer, make_attachment(attendant), amount="0.10")
    make_credit_sale(shift, customer, make_attachment(attendant), amount="0.20")
    make_credit_repayment(shift, customer, amount="0.30")

    balance = _outstanding(customer)
    assert balance == Decimal("0.00")
    assert isinstance(balance, Decimal)


# --- reversals net out (§6.6, §14) -------------------------------------------


async def test_a_reversed_sale_leaves_no_debt_behind(
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_sale: Callable[..., UUID],
) -> None:
    """The negative row cancels the positive one. §14 forbids filtering reversals out of
    this sum precisely so that a cancelled udhaar cannot reappear as debt."""
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    customer = make_credit_customer(name="Cancelled")
    attachment = make_attachment(attendant)
    original = make_credit_sale(shift, customer, attachment, amount="3000.00")
    make_credit_sale(
        shift,
        customer,
        attachment,
        amount="-3000.00",
        reverses_id=original,
        reversal_reason="wrong customer",
    )

    assert _outstanding(customer) == Decimal("0.00")


async def test_a_reversal_with_a_replacement_leaves_the_corrected_figure(
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_sale: Callable[..., UUID],
) -> None:
    """₹3,000 reversed and replaced with ₹2,800 leaves ₹2,800 -- not ₹5,800, and not ₹0."""
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    customer = make_credit_customer(name="Corrected")
    attachment = make_attachment(attendant)
    original = make_credit_sale(shift, customer, attachment, amount="3000.00")
    make_credit_sale(
        shift,
        customer,
        attachment,
        amount="-3000.00",
        reverses_id=original,
        reversal_reason="mistyped the amount",
    )
    make_credit_sale(shift, customer, attachment, amount="2800.00")

    assert _outstanding(customer) == Decimal("2800.00")


async def test_a_reversed_repayment_puts_the_debt_back(
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_sale: Callable[..., UUID],
    make_credit_repayment: Callable[..., UUID],
) -> None:
    """A bounced cheque. The repayment is cancelled, so the customer owes the money again."""
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    customer = make_credit_customer(name="Bounced")
    make_credit_sale(shift, customer, make_attachment(attendant), amount="4000.00")
    repayment = make_credit_repayment(shift, customer, amount="4000.00")
    assert _outstanding(customer) == Decimal("0.00")

    make_credit_repayment(
        shift,
        customer,
        amount="-4000.00",
        reverses_id=repayment,
        reversal_reason="cheque bounced",
    )

    assert _outstanding(customer) == Decimal("4000.00")


# --- the negative case (§6.6) ------------------------------------------------


async def test_a_repayment_larger_than_the_balance_goes_negative(
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_sale: Callable[..., UUID],
    make_credit_repayment: Callable[..., UUID],
) -> None:
    """An advance, or a customer rounding up. §6.6 records this as a decision rather than
    leaving a future reader to wonder whether the minus sign is a bug."""
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    customer = make_credit_customer(name="Advance")
    make_credit_sale(shift, customer, make_attachment(attendant), amount="1000.00")
    make_credit_repayment(shift, customer, amount="1500.00")

    assert _outstanding(customer) == Decimal("-500.00")


# --- balances span shifts and days -------------------------------------------


async def test_a_balance_spans_shifts_and_business_dates(
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_sale: Callable[..., UUID],
    make_credit_repayment: Callable[..., UUID],
) -> None:
    """Udhaar issued in March and settled in April is the entire point of the feature, so
    nothing in this sum may be scoped to a shift or a business date."""
    attendant = make_user("attendant")
    monday = make_shift(attendant, business_date=DAY, sequence=1)
    tuesday = make_shift(
        attendant, business_date=date(2026, 9, 5), sequence=1, status="closed"
    )
    customer = make_credit_customer(name="Across Days")
    make_credit_sale(monday, customer, make_attachment(attendant), amount="900.00")
    make_credit_repayment(tuesday, customer, amount="400.00")

    assert _outstanding(customer) == Decimal("500.00")


async def test_one_customers_balance_is_not_another_customers(
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_sale: Callable[..., UUID],
) -> None:
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    one = make_credit_customer(name="One")
    two = make_credit_customer(name="Two")
    make_credit_sale(shift, one, make_attachment(attendant), amount="700.00")

    assert _outstanding(one) == Decimal("700.00")
    assert _outstanding(two) == Decimal("0.00")


# --- the report agrees with the definition -----------------------------------


async def test_the_bulk_report_agrees_with_the_single_customer_figure(
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_sale: Callable[..., UUID],
    make_credit_repayment: Callable[..., UUID],
) -> None:
    """`outstanding_by_customer` is the report's version of `outstanding`, written as two
    grouped aggregates rather than N subtractions. The two must never disagree -- this is
    the test that keeps the optimisation honest.

    Deliberately covers all three shapes the join has to survive: sales only, repayments
    only (a customer in credit), and both.
    """
    from app.core.config import get_settings

    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    sales_only = make_credit_customer(name="Sales Only")
    repaid_only = make_credit_customer(name="Repaid Only")
    both = make_credit_customer(name="Both")
    neither = make_credit_customer(name="Neither")

    make_credit_sale(shift, sales_only, make_attachment(attendant), amount="1200.00")
    make_credit_repayment(shift, repaid_only, amount="300.00")
    make_credit_sale(shift, both, make_attachment(attendant), amount="2000.00")
    make_credit_repayment(shift, both, amount="750.00")

    report = _outstanding_by_customer(get_settings().DEFAULT_OUTLET_ID)

    for customer_id in (sales_only, repaid_only, both, neither):
        assert report[customer_id] == _outstanding(customer_id), customer_id
    assert report[sales_only] == Decimal("1200.00")
    assert report[repaid_only] == Decimal("-300.00")
    assert report[both] == Decimal("1250.00")
    assert report[neither] == Decimal("0.00")


async def test_the_bulk_report_is_scoped_to_one_outlet(
    make_user: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    engine,
) -> None:
    """§5.0: `credit_customers` carries its own outlet_id, and a report that ignored it
    would show one dealer another dealer's debtors."""
    from uuid import uuid4

    from sqlalchemy import text

    from app.core.config import get_settings

    other_outlet = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO outlets (id, name) VALUES (:id, 'Second Outlet')"
            ).bindparams(id=other_outlet)
        )

    try:
        here = make_credit_customer(name="Ours")
        elsewhere = make_credit_customer(name="Theirs", outlet_id=other_outlet)

        report = _outstanding_by_customer(get_settings().DEFAULT_OUTLET_ID)

        assert here in report
        assert elsewhere not in report
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM credit_customers WHERE outlet_id = :id").bindparams(
                    id=other_outlet
                )
            )
            connection.execute(
                text("DELETE FROM outlets WHERE id = :id").bindparams(id=other_outlet)
            )
