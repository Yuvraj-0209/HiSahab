"""§6.6's credit limit: the rule, the boundary, and the null case.

Tested against `app/services/credit.py::check_credit_limit` directly. The HTTP layer adds the
admin-override permission tests (Step 5); these pin the comparison itself, which is the half
that goes silently wrong -- a `>=` here would refuse a sale that lands exactly on the limit,
and coercing a NULL limit to zero would refuse every sale to the customers trusted most.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from decimal import Decimal
from uuid import UUID

import pytest

from app.core.errors import AppError

pytestmark = pytest.mark.anyio

DAY = date(2026, 9, 11)


def _check(customer_id: UUID, amount: str) -> None:
    """Raises AppError if §6.6 refuses the sale, returns None if it allows it."""
    from app.db.session import SessionLocal
    from app.models.credit import CreditCustomer
    from app.services import credit as credit_service

    with SessionLocal() as session:
        customer = session.get(CreditCustomer, customer_id)
        credit_service.check_credit_limit(
            session, customer=customer, amount=Decimal(amount)
        )


async def test_a_customer_with_no_limit_is_unlimited(
    make_credit_customer: Callable[..., UUID],
) -> None:
    """`credit_limit IS NULL` means no limit and must never be read as zero (§14)."""
    customer = make_credit_customer(name="Unlimited", credit_limit=None)

    _check(customer, "500000.00")


async def test_a_sale_under_the_limit_is_allowed(
    make_credit_customer: Callable[..., UUID],
) -> None:
    customer = make_credit_customer(name="Under", credit_limit="10000.00")

    _check(customer, "4000.00")


async def test_a_sale_over_the_limit_is_refused(
    make_credit_customer: Callable[..., UUID],
) -> None:
    customer = make_credit_customer(name="Over", credit_limit="10000.00")

    with pytest.raises(AppError) as caught:
        _check(customer, "10001.00")

    assert caught.value.status_code == 409
    assert caught.value.code == "CREDIT_LIMIT_EXCEEDED"


@pytest.mark.parametrize(
    "amount,allowed",
    [("10000.00", True), ("10000.01", False)],
)
async def test_the_boundary_is_strictly_greater_than(
    make_credit_customer: Callable[..., UUID], amount: str, allowed: bool
) -> None:
    """§10: landing exactly on the limit is allowed, one paisa over is not.

    Every threshold in this codebase compares the same way -- §6.7's review threshold and
    §6.11's receipt threshold are both strictly `>` -- deliberately, so nobody has to
    remember which is which.
    """
    customer = make_credit_customer(name="Boundary", credit_limit="10000.00")

    if allowed:
        _check(customer, amount)
    else:
        with pytest.raises(AppError) as caught:
            _check(customer, amount)
        assert caught.value.code == "CREDIT_LIMIT_EXCEEDED"


async def test_the_limit_is_measured_against_the_existing_balance_not_the_sale_alone(
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_sale: Callable[..., UUID],
) -> None:
    """The rule is `outstanding + amount > limit`, not `amount > limit`.

    A ₹6,000 sale is comfortably under a ₹10,000 limit on its own; against ₹7,000 already
    owed it is not. Getting this wrong lets a customer walk past their limit in small steps,
    which is the same structuring §6.7's aggregate rule exists to catch elsewhere.
    """
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    customer = make_credit_customer(name="Accumulating", credit_limit="10000.00")
    make_credit_sale(shift, customer, make_attachment(attendant), amount="7000.00")

    with pytest.raises(AppError) as caught:
        _check(customer, "6000.00")

    assert caught.value.code == "CREDIT_LIMIT_EXCEEDED"


async def test_a_repayment_frees_the_limit_up_again(
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_sale: Callable[..., UUID],
    make_credit_repayment: Callable[..., UUID],
) -> None:
    """The limit is on the *balance*, not on lifetime turnover -- a customer who settles up
    can buy again."""
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    customer = make_credit_customer(name="Settled Up", credit_limit="10000.00")
    make_credit_sale(shift, customer, make_attachment(attendant), amount="9500.00")
    make_credit_repayment(shift, customer, amount="9500.00")

    _check(customer, "9000.00")


async def test_a_reversed_sale_frees_the_limit_up_too(
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_sale: Callable[..., UUID],
) -> None:
    """Because `outstanding` sums reversals in rather than filtering them out (§6.6), a
    cancelled udhaar stops counting against the limit -- which is the behaviour anyone would
    expect and the reason §14 forbids filtering them."""
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    customer = make_credit_customer(name="Reversed", credit_limit="10000.00")
    attachment = make_attachment(attendant)
    original = make_credit_sale(shift, customer, attachment, amount="9500.00")

    with pytest.raises(AppError):
        _check(customer, "1000.00")

    make_credit_sale(
        shift,
        customer,
        attachment,
        amount="-9500.00",
        reverses_id=original,
        reversal_reason="wrong customer",
    )

    _check(customer, "1000.00")


async def test_a_zero_limit_refuses_everything(
    make_credit_customer: Callable[..., UUID],
) -> None:
    """Zero is a real answer, distinct from NULL: a customer on the list but allowed no
    further udhaar at all. `ck_credit_customers_limit_not_negative` permits it for exactly
    this reason."""
    customer = make_credit_customer(name="Frozen", credit_limit="0.00")

    with pytest.raises(AppError) as caught:
        _check(customer, "0.01")

    assert caught.value.code == "CREDIT_LIMIT_EXCEEDED"
