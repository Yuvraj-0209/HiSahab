"""Expense categories -- what an expense can be filed under (CLAUDE.md §5.1, §6.11).

Mirrors `app/api/v1/fuel_types.py` almost line for line, and for the same reason: adding
something you actually spend on should be data entry, not a schema change requiring a
developer and a deploy. Reads sit at the attendant floor -- everyone filling in an expense
form needs the list -- while writes are admin-only, the new "Manage expense categories" row
in §8's permission table.

**Two differences from `fuel_types`, both deliberate.**

*Outlet-scoped.* `PETROL` means the same thing at every outlet, so `fuel_types` is global.
"Tea" does not, and `requires_receipt` is a control decision an outlet's own admin makes --
so every read filters on the caller's outlet and `PATCH` resolves the outlet from the row
being edited, via `resolve_outlet_from_expense_category` below (the §8 pattern established by
`nozzles.py::resolve_outlet_from_nozzle`).

*`code` is normalised, then validated against the same regex the database enforces.* A code
arrives as anything and is stripped and upper-cased, so `tea` and ` Tea ` both become `TEA`
and cannot become two categories meaning one thing -- which is the specific failure §5.2's
"not free text" warning is about, because two spellings silently split one category's total
and §6.7's aggregate rule then never fires.
"""

from __future__ import annotations

import logging
import re
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import Actor, require_role
from app.core.errors import AppError
from app.core.roles import Role
from app.db.session import get_db
from app.models.expense_category import CODE_PATTERN, ExpenseCategory

logger = logging.getLogger(__name__)

router = APIRouter(tags=["reference data"])

# Same considered exception as fuel_types': §9 mandates cursor pagination on list endpoints,
# but categories are bounded reference data -- a couple of dozen rows a station edits a few
# times a year -- and a cursor would be ceremony with no reader. The cap makes the bound
# structural rather than assumed.
_MAX_ROWS = 500

_CODE_RE = re.compile(CODE_PATTERN)


class ExpenseCategoryResponse(BaseModel):
    id: UUID
    code: str
    display_name: str
    # Always returned. §6.11's rule is evaluated server-side and snapshotted onto the
    # expense, but a client that knows this can tell the user a receipt will be needed
    # *before* they fill in the form rather than after a 422.
    requires_receipt: bool
    is_active: bool


class ExpenseCategoryCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Stripped and upper-cased in the handler, then checked against the database's own
    # regex. Not a Pydantic `pattern=` constraint, because that would run *before*
    # normalisation and reject a perfectly good `tea`.
    code: str = Field(min_length=1, max_length=50)
    display_name: str = Field(min_length=1, max_length=200)
    requires_receipt: bool = False


class ExpenseCategoryUpdate(BaseModel):
    """What may change after creation -- and, by omission, what may not.

    `code` is absent on purpose, and `extra="forbid"` turns an attempt to send it into a 422
    rather than a silent no-op, exactly as `FuelTypeUpdate` does for `unit_of_measure`. The
    silent version is the dangerous one: an admin who "renamed" a category and got a 200
    back would reasonably believe it worked.

    Changing a code would retroactively relabel every expense ever filed under it, and
    history would stop meaning what it said. A category that is genuinely different is a new
    row; one that is merely misspelled gets a new `display_name`.

    `requires_receipt` *must* stay editable -- it is the knob §6.11 exists to give the admin.
    Editing it is safe only because the answer is snapshotted onto each expense at insert, so
    flipping it never rewrites whether history complied.
    """

    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    requires_receipt: bool | None = None
    is_active: bool | None = None


def resolve_outlet_from_expense_category(
    category_id: UUID, db: Session = Depends(get_db)
) -> UUID:
    """The outlet that owns this category, for `require_role` to authorise against.

    Mirrors `nozzles.py::resolve_outlet_from_nozzle`, including the 404 living in the
    dependency rather than the handler: this runs first, and without it a request against a
    nonexistent category would report a permission failure instead of a missing row.
    """
    outlet_id = db.execute(
        select(ExpenseCategory.outlet_id).where(ExpenseCategory.id == category_id)
    ).scalar_one_or_none()
    if outlet_id is None:
        raise AppError(
            status_code=404,
            code="CATEGORY_NOT_FOUND",
            detail="No expense category with that id.",
        )
    return outlet_id


def _to_response(row: ExpenseCategory) -> ExpenseCategoryResponse:
    return ExpenseCategoryResponse(
        id=row.id,
        code=row.code,
        display_name=row.display_name,
        requires_receipt=row.requires_receipt,
        is_active=row.is_active,
    )


@router.get("/expense-categories", response_model=list[ExpenseCategoryResponse])
def list_expense_categories(
    include_inactive: bool = Query(default=False),
    actor: Actor = Depends(require_role(Role.attendant)),
    db: Session = Depends(get_db),
) -> list[ExpenseCategoryResponse]:
    """Every category this outlet files expenses under. Attendant floor -- it fills a form."""
    statement = (
        select(ExpenseCategory)
        .where(ExpenseCategory.outlet_id == actor.outlet_id)
        .order_by(ExpenseCategory.code)
    )
    if not include_inactive:
        statement = statement.where(ExpenseCategory.is_active.is_(True))

    rows = db.execute(statement.limit(_MAX_ROWS)).scalars().all()
    return [_to_response(row) for row in rows]


@router.post("/expense-categories", response_model=ExpenseCategoryResponse, status_code=201)
def create_expense_category(
    payload: ExpenseCategoryCreate,
    actor: Actor = Depends(require_role(Role.admin)),
    db: Session = Depends(get_db),
) -> ExpenseCategoryResponse:
    """Add a category the outlet has started spending on. Admin only (§8).

    **Do not create a fuel-purchase, tanker, IOCL or PAD category here.** That money leaves
    the bank, never the drawer, and §6.4 would invent a daily cash shortage that never
    happened. Until Phase 8 the `expense_category` enum made it impossible; now only §14's
    guardrail does, so it is restated at the one endpoint that could do it.
    """
    code = payload.code.strip().upper()

    if not _CODE_RE.match(code):
        # 422 rather than 409: the payload itself is malformed. Checked here rather than as
        # a Pydantic `pattern=` so that normalisation runs first -- `tea` is a fine thing to
        # send, `TEA CHAI` is not.
        raise AppError(
            status_code=422,
            code="CATEGORY_CODE_INVALID",
            detail=(
                "A category code must start with a letter and contain only letters, "
                "digits and underscores (it is upper-cased for you). One category cannot "
                "be allowed to exist under two spellings -- see CLAUDE.md §5.1."
            ),
        )

    existing = db.execute(
        select(ExpenseCategory).where(
            ExpenseCategory.outlet_id == actor.outlet_id,
            ExpenseCategory.code == code,
        )
    ).scalar_one_or_none()
    if existing is not None:
        # 409 rather than 422: the payload is well-formed, it conflicts with the world.
        raise AppError(
            status_code=409,
            code="CATEGORY_CODE_EXISTS",
            detail=f"An expense category with code {code} already exists.",
        )

    row = ExpenseCategory(
        outlet_id=actor.outlet_id,
        code=code,
        display_name=payload.display_name.strip(),
        requires_receipt=payload.requires_receipt,
        created_by=actor.user.id,
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    logger.info(
        "expense category created",
        extra={
            "expense_category_id": str(row.id),
            "code": row.code,
            "requires_receipt": row.requires_receipt,
        },
    )
    return _to_response(row)


@router.patch(
    "/expense-categories/{category_id}", response_model=ExpenseCategoryResponse
)
def update_expense_category(
    category_id: UUID,
    payload: ExpenseCategoryUpdate,
    actor: Actor = Depends(
        require_role(Role.admin, resolve_outlet_from_expense_category)
    ),
    db: Session = Depends(get_db),
) -> ExpenseCategoryResponse:
    """Edit the mutable parts of a category. Admin only (§8).

    There is no DELETE, here or anywhere (§3 rule 6). Set `is_active` false instead: the
    category stops appearing in the default list while every historical expense that
    references it stays intact, readable and reportable.
    """
    # Not None: resolve_outlet_from_expense_category already 404'd inside the dependency.
    row = db.get(ExpenseCategory, category_id)

    # exclude_unset so that "not mentioned" and "explicitly set to null" stay distinct --
    # a PATCH that omits is_active must not clear it.
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise AppError(
            status_code=422,
            code="NO_FIELDS_TO_UPDATE",
            detail="Provide at least one field to change.",
        )

    for field, value in changes.items():
        if value is None:
            continue
        setattr(row, field, value.strip() if isinstance(value, str) else value)

    db.commit()
    db.refresh(row)

    logger.info(
        "expense category updated",
        extra={"expense_category_id": str(row.id), "fields": sorted(changes)},
    )
    return _to_response(row)
