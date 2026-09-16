"""Importing bank statements and reconciling them (CLAUDE.md §5.3a, §8, §9).

Phase 20. Eight routes over four tables, and **exactly one of them writes a money row**:
`confirm-repayments`, which creates `credit_repayments` through the Phase 16 path that
already exists. Everything else stores statement lines, labels them, or reports a comparison.

## The rule that shapes this module

§14: no statement line may produce a business row except a confirmed repayment. That is
enforced structurally rather than by discipline -- there is no code path here that reaches
`expenses`, `collections`, `bank_deposits` or `daily_cash_summaries`, and
`tests/test_bank_no_side_effects.py` walks this module's AST to keep it that way.

The reason is §6.4: a `bank_deposits` row needs a `shift_id`, so inventing one would move a
closed day's expected cash on the strength of a file somebody uploaded. And §12 forbids
booking an IOCL payment as an expense, which would invent a daily cash shortage that never
happened.

## Role floors (§8)

Manager for everything except managing the accounts themselves, which is admin like every
other reference table. A statement is a *report* about money that has already moved; reading
and reconciling it is the same kind of act as reading the cash position, which §8 already puts
at the manager floor. Nothing here is an attendant's business -- §8 has never let an attendant
see a customer's balance, and a statement is a month of them.

## Idempotency

`POST /bank-statements/imports` and `POST /bank-transactions/confirm-repayments` both take an
`Idempotency-Key` (§6.10). The import is the more interesting case: line-level fingerprinting
already makes a *replayed* upload harmless, but the `bank_statement_imports` row is not
fingerprinted, so without a key a timed-out retry would leave a second import row claiming
zero lines -- a confusing artefact rather than a money error, but an avoidable one.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

import sqlalchemy as sa
from fastapi import APIRouter, Depends, File, Form, Header, Query, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy.orm import Session

from app.api.cursor import DEFAULT_LIMIT, MAX_LIMIT, decode_cursor, encode_cursor
from app.api.deps import Actor, require_role
from app.core import idempotency
from app.core.audit import AuditAction
from app.core.bank_statements import parse
from app.core.config import Settings, get_settings
from app.core.credit import CreditRepaymentMode
from app.core.errors import AppError
from app.core.roles import Role
from app.db.session import get_db
from app.models.bank import (
    BankAccount,
    BankStatementImport,
    BankTransaction,
)
from app.models.credit import CreditRepayment
from app.services import audit, bank as bank_service, credit as credit_service
from app.services import shifts as shift_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["bank statements"])

_MAX_ROWS = 200

# A settlement covers the previous trading day (T+1).
_ONE_DAY = timedelta(days=1)

LabelValue = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
BankNameValue = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)
]
Last4Value = Annotated[str, StringConstraints(pattern=r"^[0-9]{4}$")]


# --- schemas -------------------------------------------------------------------------


class BankAccountCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: LabelValue
    bank_name: BankNameValue
    # Deliberately only the last four. The full number reconciles nothing and storing it
    # makes this table worth stealing (§5.3a).
    account_number_last4: Last4Value | None = None


class BankAccountUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: LabelValue | None = None
    bank_name: BankNameValue | None = None
    account_number_last4: Last4Value | None = None
    is_active: bool | None = None


class BankAccountResponse(BaseModel):
    id: UUID
    label: str
    bank_name: str
    account_number_last4: str | None
    is_active: bool


class ImportResponse(BaseModel):
    id: UUID
    bank_account_id: UUID
    period_from: date
    period_to: date
    original_filename: str | None
    row_count: int
    imported_count: int
    skipped_count: int
    # Strings, not floats: §14 forbids money arithmetic in JavaScript, and a JSON number is
    # a float the moment a browser parses it.
    opening_balance: str | None
    closing_balance: str | None


class ImportPage(BaseModel):
    items: list[ImportResponse]
    next_cursor: str | None


class TransactionResponse(BaseModel):
    id: UUID
    txn_date: date
    narration: str
    amount: str
    direction: str
    running_balance: str | None
    classification: str
    is_expense: str
    suggested_expense: str
    matched_business_date: date | None
    credit_repayment_id: UUID | None


class TransactionPage(BaseModel):
    items: list[TransactionResponse]
    next_cursor: str | None


class TransactionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    classification: str | None = None
    is_expense: str | None = None


class ProposalResponse(BaseModel):
    credit_customer_id: UUID
    name: str
    reason: str
    confidence: str


class CreditReviewResponse(BaseModel):
    transaction_id: UUID
    txn_date: date
    narration: str
    amount: str
    verified_repayment_id: UUID | None
    ambiguous: bool
    proposals: list[ProposalResponse]


class SettlementResponse(BaseModel):
    business_date: date
    settled_on: date
    expected: str
    settled: str | None
    difference: str | None
    matches: bool
    source: str


class DepositResponse(BaseModel):
    kind: str
    txn_date: date
    amount: str
    transaction_id: UUID | None
    bank_deposit_id: UUID | None
    days_late: int | None


class ReconciliationResponse(BaseModel):
    date_from: date
    date_to: date
    settlements: list[SettlementResponse]
    deposits: list[DepositResponse]
    credits: list[CreditReviewResponse]
    # Money in transit -- the figure Phase 21's bridge adds to the closing bank balance.
    boundary_settlement: str | None
    boundary_settled_on: date | None


class ConfirmItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transaction_id: UUID
    credit_customer_id: UUID
    remember_sender: bool = False


class ConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[ConfirmItem] = Field(min_length=1, max_length=100)


class ConfirmFailure(BaseModel):
    transaction_id: UUID
    code: str
    detail: str


class ConfirmResponse(BaseModel):
    created: list[UUID]
    failed: list[ConfirmFailure]


# --- helpers -------------------------------------------------------------------------


def _account_or_404(db: Session, *, account_id: UUID, outlet_id: UUID) -> BankAccount:
    """Cross-outlet is a 404, matching `resolve_customer` and §7.3's posture on attachments:
    a caller learns nothing about whether an id exists somewhere they cannot see."""
    account = db.get(BankAccount, account_id)
    if account is None or account.outlet_id != outlet_id:
        raise AppError(
            status_code=404,
            code="BANK_ACCOUNT_NOT_FOUND",
            detail="No bank account with that id at this outlet.",
        )
    return account


def _account_response(account: BankAccount) -> BankAccountResponse:
    return BankAccountResponse(
        id=account.id,
        label=account.label,
        bank_name=account.bank_name,
        account_number_last4=account.account_number_last4,
        is_active=account.is_active,
    )


def _import_response(row: BankStatementImport) -> ImportResponse:
    return ImportResponse(
        id=row.id,
        bank_account_id=row.bank_account_id,
        period_from=row.period_from,
        period_to=row.period_to,
        original_filename=row.original_filename,
        row_count=row.row_count,
        imported_count=row.imported_count,
        skipped_count=row.skipped_count,
        opening_balance=None if row.opening_balance is None else str(row.opening_balance),
        closing_balance=None if row.closing_balance is None else str(row.closing_balance),
    )


def _transaction_response(row: BankTransaction) -> TransactionResponse:
    return TransactionResponse(
        id=row.id,
        txn_date=row.txn_date,
        narration=row.narration,
        amount=str(row.amount),
        direction=row.direction,
        running_balance=None if row.running_balance is None else str(row.running_balance),
        classification=row.classification,
        is_expense=row.is_expense,
        # A suggestion the screen may pre-select; the stored value stays `undecided` until a
        # human answers (§5.3a).
        suggested_expense=bank_service.suggested_expense_flag(row.classification),
        matched_business_date=row.matched_business_date,
        credit_repayment_id=row.credit_repayment_id,
    )


def _audit_account(account: BankAccount) -> dict[str, Any]:
    return {
        "label": account.label,
        "bank_name": account.bank_name,
        "account_number_last4": account.account_number_last4,
        "is_active": account.is_active,
    }


def _require_key(idempotency_key: str | None) -> str:
    if not idempotency_key:
        raise AppError(
            status_code=400,
            code="IDEMPOTENCY_KEY_REQUIRED",
            detail="This endpoint requires an Idempotency-Key header (§6.10).",
        )
    return idempotency_key


def _replayed(replay: idempotency.Replay) -> JSONResponse:
    return JSONResponse(status_code=replay.status_code, content=replay.body)


async def _read_bounded(file: UploadFile, *, max_bytes: int) -> bytes:
    """Bounded by the read itself, never by `Content-Length`, which a client can lie about.

    The same shape as `uploads.py::_read_bounded`, deliberately: one more byte than the limit
    so the validator can report a real observed size rather than validating a silently
    truncated -- and therefore corrupt -- file as though it were whole.
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(65536)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > max_bytes:
            break
    return b"".join(chunks)


# --- bank accounts -------------------------------------------------------------------


@router.get("/bank-accounts", response_model=list[BankAccountResponse])
def list_bank_accounts(
    actor: Actor = Depends(require_role(Role.manager)),
    db: Session = Depends(get_db),
) -> Any:
    """Not cursor-paginated, like `/nozzles` and `/fuel-types`: an outlet has a handful of
    accounts and a cursor would be ceremony over a list that fits on a phone screen (§9)."""
    rows = (
        db.execute(
            sa.select(BankAccount)
            .where(BankAccount.outlet_id == actor.outlet_id)
            .order_by(BankAccount.label)
        )
        .scalars()
        .all()
    )
    return [_account_response(row) for row in rows]


@router.post("/bank-accounts", response_model=BankAccountResponse, status_code=201)
def create_bank_account(
    payload: BankAccountCreate,
    actor: Actor = Depends(require_role(Role.admin)),
    db: Session = Depends(get_db),
) -> Any:
    """Admin-managed reference data, like every other table somebody configures once."""
    existing = db.execute(
        sa.select(BankAccount).where(
            BankAccount.outlet_id == actor.outlet_id,
            BankAccount.label == payload.label,
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise AppError(
            status_code=409,
            code="BANK_ACCOUNT_LABEL_EXISTS",
            detail="A bank account with that label already exists at this outlet.",
        )

    account = BankAccount(
        # §5.0: from the actor, never from the payload. A client-supplied outlet is how
        # somebody grants themselves a row at a pump they do not work at (§14).
        outlet_id=actor.outlet_id,
        label=payload.label,
        bank_name=payload.bank_name,
        account_number_last4=payload.account_number_last4,
        created_by=actor.user.id,
    )
    db.add(account)
    db.flush()

    audit.record(
        db,
        outlet_id=actor.outlet_id,
        table_name="bank_accounts",
        record_id=account.id,
        action=AuditAction.insert,
        changed_by=actor.user.id,
        new_values=_audit_account(account),
    )
    db.commit()
    db.refresh(account)
    return _account_response(account)


@router.patch("/bank-accounts/{account_id}", response_model=BankAccountResponse)
def update_bank_account(
    account_id: UUID,
    payload: BankAccountUpdate,
    actor: Actor = Depends(require_role(Role.admin)),
    db: Session = Depends(get_db),
) -> Any:
    account = _account_or_404(db, account_id=account_id, outlet_id=actor.outlet_id)
    before = _audit_account(account)

    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        raise AppError(
            status_code=422,
            code="NO_FIELDS_TO_UPDATE",
            detail="Supply at least one field to change.",
        )

    if "label" in fields and fields["label"] != account.label:
        clash = db.execute(
            sa.select(BankAccount).where(
                BankAccount.outlet_id == actor.outlet_id,
                BankAccount.label == fields["label"],
                BankAccount.id != account.id,
            )
        ).scalar_one_or_none()
        if clash is not None:
            raise AppError(
                status_code=409,
                code="BANK_ACCOUNT_LABEL_EXISTS",
                detail="A bank account with that label already exists at this outlet.",
            )

    for field, value in fields.items():
        setattr(account, field, value)
    db.flush()

    audit.record(
        db,
        outlet_id=actor.outlet_id,
        table_name="bank_accounts",
        record_id=account.id,
        # Deactivation is an ordinary update, never `status_change` -- §14 reserves that
        # label for a *shift* lifecycle move.
        action=AuditAction.update,
        changed_by=actor.user.id,
        old_values=before,
        new_values=_audit_account(account),
    )
    db.commit()
    db.refresh(account)
    return _account_response(account)


# --- importing -----------------------------------------------------------------------


@router.post("/bank-statements/imports", response_model=ImportResponse, status_code=201)
async def import_statement(
    file: UploadFile = File(...),
    bank_account_id: UUID = Form(...),
    actor: Actor = Depends(require_role(Role.manager)),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Any:
    """Parse a statement, store every line, and classify each.

    **Re-uploading an overlapping period is the normal way to work**, not an error: §4.7's
    "typed in after the fact" applies to statements too, and the T+1 boundary means the owner
    downloads with a few days' overlap on purpose. Lines already seen are skipped by
    fingerprint and counted, so the same file can be uploaded any number of times.

    The file is parsed and **discarded** (§13.38). The rows are the record, and they carry
    more than the file does -- the classification of each line.
    """
    key = _require_key(idempotency_key)
    endpoint = "POST /bank-statements/imports"

    account = _account_or_404(
        db, account_id=bank_account_id, outlet_id=actor.outlet_id
    )

    data = await _read_bounded(file, max_bytes=settings.MAX_STATEMENT_BYTES)
    if len(data) > settings.MAX_STATEMENT_BYTES:
        raise AppError(
            status_code=413,
            code="FILE_TOO_LARGE",
            detail=(
                f"The file is over the {settings.MAX_STATEMENT_BYTES}-byte limit "
                "(MAX_STATEMENT_BYTES)."
            ),
        )
    if not data:
        raise AppError(
            status_code=422, code="EMPTY_FILE", detail="The uploaded file is empty."
        )

    replay = idempotency.begin(
        db,
        key=key,
        endpoint=endpoint,
        user_id=actor.user.id,
        request_fingerprint=idempotency.fingerprint(
            path_params={}, body={"bank_account_id": str(bank_account_id), "size": len(data)}
        ),
    )
    if replay is not None:
        return _replayed(replay)

    try:
        statement = parse(data, account_id=str(account.id))

        seen = set(
            db.execute(
                sa.select(BankTransaction.fingerprint).where(
                    BankTransaction.bank_account_id == account.id
                )
            )
            .scalars()
            .all()
        )

        record = BankStatementImport(
            bank_account_id=account.id,
            period_from=statement.period_from,
            period_to=statement.period_to,
            # Display label only -- never used to build a path, never trusted (§7.2).
            original_filename=file.filename,
            row_count=len(statement.lines),
            imported_count=0,
            skipped_count=0,
            opening_balance=statement.opening_balance,
            closing_balance=statement.closing_balance,
        )
        record.created_by = actor.user.id
        db.add(record)
        db.flush()

        imported = 0
        skipped = 0
        for line in statement.lines:
            if line.fingerprint in seen:
                skipped += 1
                continue
            seen.add(line.fingerprint)

            classification = bank_service.classify(line)
            db.add(
                BankTransaction(
                    bank_account_id=account.id,
                    import_id=record.id,
                    txn_date=line.txn_date,
                    narration=line.narration,
                    amount=line.amount,
                    direction=line.direction,
                    running_balance=line.running_balance,
                    classification=classification,
                    # For a settlement, the trading day it covers (T-1). Stored so the rule
                    # is a fact on the row rather than re-derived on every read.
                    matched_business_date=(
                        line.txn_date - _ONE_DAY
                        if classification == "paytm_settlement"
                        else None
                    ),
                    classified_by=actor.user.id,
                    classified_at=sa.func.now(),
                    fingerprint=line.fingerprint,
                    created_by=actor.user.id,
                )
            )
            imported += 1

        record.imported_count = imported
        record.skipped_count = skipped

        audit.record(
            db,
            outlet_id=actor.outlet_id,
            table_name="bank_statement_imports",
            record_id=record.id,
            action=AuditAction.insert,
            changed_by=actor.user.id,
            new_values={
                "bank_account_id": str(account.id),
                "period_from": record.period_from.isoformat(),
                "period_to": record.period_to.isoformat(),
                "row_count": record.row_count,
                "imported_count": imported,
                "skipped_count": skipped,
            },
        )
        db.commit()
        db.refresh(record)
    except Exception:
        db.rollback()
        idempotency.discard(db, key=key, endpoint=endpoint, user_id=actor.user.id)
        raise

    response = _import_response(record)
    body = jsonable_encoder(response)
    idempotency.store(
        db, key=key, endpoint=endpoint, user_id=actor.user.id, status_code=201, body=body
    )

    logger.info(
        "bank statement imported",
        extra={
            "bank_account_id": str(account.id),
            "import_id": str(record.id),
            "row_count": record.row_count,
            "imported": imported,
            "skipped": skipped,
        },
    )
    return JSONResponse(status_code=201, content=body)


@router.get("/bank-statements/imports", response_model=ImportPage)
def list_imports(
    actor: Actor = Depends(require_role(Role.manager)),
    db: Session = Depends(get_db),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: str | None = Query(default=None),
) -> Any:
    statement = (
        sa.select(BankStatementImport)
        .join(BankAccount, BankAccount.id == BankStatementImport.bank_account_id)
        .where(BankAccount.outlet_id == actor.outlet_id)
        .order_by(BankStatementImport.created_at.desc(), BankStatementImport.id.desc())
        .limit(limit + 1)
    )
    if cursor:
        created_at, row_id = decode_cursor(cursor)
        statement = statement.where(
            sa.tuple_(BankStatementImport.created_at, BankStatementImport.id)
            < sa.tuple_(created_at, row_id)
        )

    rows = list(db.execute(statement).scalars().all())
    has_more = len(rows) > limit
    rows = rows[:limit]

    return ImportPage(
        items=[_import_response(row) for row in rows],
        next_cursor=(
            encode_cursor(rows[-1].created_at, rows[-1].id) if has_more and rows else None
        ),
    )


# --- transactions --------------------------------------------------------------------
#
# NOTE the declaration order below: `/bank-transactions/confirm-repayments` is a **static**
# path and must be declared before `/bank-transactions/{transaction_id}`, or the
# parameterised route swallows it and FastAPI tries to parse "confirm-repayments" as a UUID.
# The same trap `/credit-customers/outstanding` and `/fuel-prices/current` already carry, and
# `app/api/v1/router.py` documents it.


@router.get("/bank-transactions", response_model=TransactionPage)
def list_transactions(
    actor: Actor = Depends(require_role(Role.manager)),
    db: Session = Depends(get_db),
    bank_account_id: UUID | None = Query(default=None),
    classification: str | None = Query(default=None),
    direction: str | None = Query(default=None),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: str | None = Query(default=None),
) -> Any:
    statement = (
        sa.select(BankTransaction)
        .join(BankAccount, BankAccount.id == BankTransaction.bank_account_id)
        .where(BankAccount.outlet_id == actor.outlet_id)
        .order_by(BankTransaction.created_at.desc(), BankTransaction.id.desc())
        .limit(limit + 1)
    )
    if bank_account_id is not None:
        statement = statement.where(BankTransaction.bank_account_id == bank_account_id)
    if classification is not None:
        statement = statement.where(BankTransaction.classification == classification)
    if direction is not None:
        statement = statement.where(BankTransaction.direction == direction)
    if date_from is not None:
        statement = statement.where(BankTransaction.txn_date >= date_from)
    if date_to is not None:
        statement = statement.where(BankTransaction.txn_date <= date_to)
    if cursor:
        created_at, row_id = decode_cursor(cursor)
        statement = statement.where(
            sa.tuple_(BankTransaction.created_at, BankTransaction.id)
            < sa.tuple_(created_at, row_id)
        )

    rows = list(db.execute(statement).scalars().all())
    has_more = len(rows) > limit
    rows = rows[:limit]

    return TransactionPage(
        items=[_transaction_response(row) for row in rows],
        next_cursor=(
            encode_cursor(rows[-1].created_at, rows[-1].id) if has_more and rows else None
        ),
    )


@router.get("/bank-statements/reconciliation", response_model=ReconciliationResponse)
def reconciliation(
    bank_account_id: UUID,
    date_from: date,
    date_to: date,
    actor: Actor = Depends(require_role(Role.manager)),
    db: Session = Depends(get_db),
) -> Any:
    """The three reconciliations for a window. **Writes nothing** (§8, §14).

    `date_to` is a *trading* date: settlements are looked for on the day after it, which is
    where T+1 money lands and is outside a calendar-month import unless the caller downloaded
    with overlap.
    """
    _account_or_404(db, account_id=bank_account_id, outlet_id=actor.outlet_id)

    if date_to < date_from:
        raise AppError(
            status_code=422,
            code="INVALID_DATE_RANGE",
            detail="`date_to` must not be before `date_from`.",
        )

    settlements = bank_service.settlement_checks(
        db,
        outlet_id=actor.outlet_id,
        bank_account_id=bank_account_id,
        date_from=date_from,
        date_to=date_to,
    )
    deposits = bank_service.deposit_checks(
        db,
        outlet_id=actor.outlet_id,
        bank_account_id=bank_account_id,
        date_from=date_from,
        date_to=date_to,
    )
    credits = bank_service.credit_reviews(
        db,
        outlet_id=actor.outlet_id,
        bank_account_id=bank_account_id,
        date_from=date_from,
        date_to=date_to,
    )
    boundary = bank_service.boundary_settlement(
        db, bank_account_id=bank_account_id, period_to=date_to
    )

    return ReconciliationResponse(
        date_from=date_from,
        date_to=date_to,
        settlements=[
            SettlementResponse(
                business_date=check.business_date,
                settled_on=check.settled_on,
                expected=str(check.expected),
                # `None`, never "0.00": "Paytm has not paid yet" is a different fact from
                # "Paytm paid nothing", and §14 forbids the client coalescing it (§6.8).
                settled=None if check.settled is None else str(check.settled),
                difference=None if check.difference is None else str(check.difference),
                matches=check.matches,
                source=check.source,
            )
            for check in settlements
        ],
        deposits=[
            DepositResponse(
                kind=check.kind,
                txn_date=check.txn_date,
                amount=str(check.amount),
                transaction_id=check.transaction_id,
                bank_deposit_id=check.bank_deposit_id,
                days_late=check.days_late,
            )
            for check in deposits
        ],
        credits=[
            CreditReviewResponse(
                transaction_id=review.transaction_id,
                txn_date=review.txn_date,
                narration=review.narration,
                amount=str(review.amount),
                verified_repayment_id=review.verified_repayment_id,
                ambiguous=review.ambiguous,
                proposals=[
                    ProposalResponse(
                        credit_customer_id=proposal.credit_customer_id,
                        name=proposal.name,
                        reason=proposal.reason,
                        confidence=proposal.confidence,
                    )
                    for proposal in review.proposals
                ],
            )
            for review in credits
        ],
        boundary_settlement=None if boundary is None else str(boundary.amount),
        boundary_settled_on=None if boundary is None else boundary.txn_date,
    )


@router.post(
    "/bank-transactions/confirm-repayments",
    response_model=ConfirmResponse,
    status_code=201,
)
def confirm_repayments(
    payload: ConfirmRequest,
    actor: Actor = Depends(require_role(Role.manager)),
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Any:
    """**The only endpoint in this module that writes money.**

    Each confirmed line creates one `credit_repayments` row through the same checks
    `create_dated_repayment` applies -- `resolve_customer(for_sale=False)`, the future-date
    guard, `refuse_entry_before_opening_balance` -- and stamps `credit_repayment_id` on the
    statement line so it can never be confirmed twice.

    `mode` is **always `bank_transfer`** and is never taken from the client: a statement line
    by definition did not arrive in the drawer, and `ck_credit_repayments_cash_needs_shift`
    would refuse `cash` anyway.

    **Partial success is reported, not refused.** If three of forty lines fail -- a
    deactivated customer, a date before an opening balance -- the other thirty-seven land and
    the response names the three with their codes. Refusing the whole batch for one bad line
    would leave the owner with no way to proceed, which is the friction §6.8 warns teaches
    people to work around the system.
    """
    key = _require_key(idempotency_key)
    endpoint = "POST /bank-transactions/confirm-repayments"

    replay = idempotency.begin(
        db,
        key=key,
        endpoint=endpoint,
        user_id=actor.user.id,
        request_fingerprint=idempotency.fingerprint(
            path_params={}, body=jsonable_encoder(payload)
        ),
    )
    if replay is not None:
        return _replayed(replay)

    created: list[UUID] = []
    failed: list[ConfirmFailure] = []

    try:
        today = shift_service.outlet_today(get_settings().TZ_DISPLAY)

        for item in payload.items:
            try:
                row = db.get(BankTransaction, item.transaction_id)
                if row is None:
                    raise AppError(
                        status_code=404,
                        code="BANK_TRANSACTION_NOT_FOUND",
                        detail="No statement line with that id.",
                    )
                _account_or_404(
                    db, account_id=row.bank_account_id, outlet_id=actor.outlet_id
                )
                if row.direction != "credit":
                    raise AppError(
                        status_code=422,
                        code="NOT_A_CREDIT",
                        detail="Only an incoming credit can be a repayment.",
                    )
                if row.credit_repayment_id is not None:
                    raise AppError(
                        status_code=409,
                        code="ALREADY_CONFIRMED",
                        detail="That statement line already has a repayment against it.",
                    )

                customer = credit_service.resolve_customer(
                    db,
                    customer_id=item.credit_customer_id,
                    outlet_id=actor.outlet_id,
                    # §5.1's asymmetry: a deactivated customer may still pay off what they
                    # owe. Refusing their money would strand a balance nothing can clear.
                    for_sale=False,
                )

                if row.txn_date > today:
                    raise AppError(
                        status_code=422,
                        code="BUSINESS_DATE_IN_FUTURE",
                        detail="That statement line is dated in the future.",
                    )

                credit_service.refuse_entry_before_opening_balance(
                    db, customer_id=customer.id, business_date=row.txn_date
                )

                repayment = CreditRepayment(
                    credit_customer_id=customer.id,
                    # No shift: this money reached a bank account, not the pump (§5.2), so it
                    # is invisible to every §6.4 sum by construction rather than by a filter.
                    shift_id=None,
                    business_date=row.txn_date,
                    amount=row.amount,
                    mode=CreditRepaymentMode.bank_transfer.value,
                    bank_reference=row.narration[:200],
                    created_by=actor.user.id,
                )
                db.add(repayment)
                db.flush()

                row.credit_repayment_id = repayment.id
                row.classification = "udhaar_repayment"

                if item.remember_sender:
                    bank_service.remember_sender(
                        db,
                        customer_id=customer.id,
                        narration=row.narration,
                        actor_id=actor.user.id,
                    )

                audit.record(
                    db,
                    outlet_id=actor.outlet_id,
                    table_name="credit_repayments",
                    record_id=repayment.id,
                    action=AuditAction.insert,
                    changed_by=actor.user.id,
                    new_values={
                        "credit_customer_id": str(customer.id),
                        "business_date": repayment.business_date.isoformat(),
                        "amount": str(repayment.amount),
                        "mode": repayment.mode,
                        "bank_transaction_id": str(row.id),
                    },
                )
                db.flush()
                created.append(repayment.id)
            except AppError as error:
                # One bad line must not cost the other thirty-nine. Rolling back to a
                # savepoint keeps the successful rows and discards only this one.
                db.rollback()
                failed.append(
                    ConfirmFailure(
                        transaction_id=item.transaction_id,
                        code=error.code,
                        detail=error.detail,
                    )
                )

        db.commit()
    except Exception:
        db.rollback()
        idempotency.discard(db, key=key, endpoint=endpoint, user_id=actor.user.id)
        raise

    response = ConfirmResponse(created=created, failed=failed)
    body = jsonable_encoder(response)
    idempotency.store(
        db, key=key, endpoint=endpoint, user_id=actor.user.id, status_code=201, body=body
    )

    # NOT `created` / `failed` as keys: `created` is a reserved `LogRecord` attribute, and
    # `logging` raises `KeyError: Attempt to overwrite 'created'` rather than ignoring it.
    # That crashed the request *after* the repayments were committed -- a 500 handed back for
    # work that had actually succeeded, which is the worst shape a failure can take here.
    # Caught by the API tests; prefixed keys make the collision impossible to reintroduce.
    logger.info(
        "bank repayments confirmed",
        extra={"repayments_created": len(created), "repayments_failed": len(failed)},
    )
    return JSONResponse(status_code=201, content=body)


@router.patch("/bank-transactions/{transaction_id}", response_model=TransactionResponse)
def update_transaction(
    transaction_id: UUID,
    payload: TransactionUpdate,
    actor: Actor = Depends(require_role(Role.manager)),
    db: Session = Depends(get_db),
) -> Any:
    """Correct a classification, or answer the expense question.

    **An ordinary `UPDATE`, and §6.9 is not breached by it** -- see `app/models/bank.py`. A
    statement line copies what the bank did rather than asserting anything, so correcting our
    *opinion* of a line moves no money: the amount, date and narration are untouched.
    """
    row = db.get(BankTransaction, transaction_id)
    if row is None:
        raise AppError(
            status_code=404,
            code="BANK_TRANSACTION_NOT_FOUND",
            detail="No statement line with that id.",
        )
    _account_or_404(db, account_id=row.bank_account_id, outlet_id=actor.outlet_id)

    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        raise AppError(
            status_code=422,
            code="NO_FIELDS_TO_UPDATE",
            detail="Supply at least one field to change.",
        )

    before = {
        "classification": row.classification,
        "is_expense": row.is_expense,
    }

    if "classification" in fields:
        if fields["classification"] not in _CLASSIFICATIONS:
            raise AppError(
                status_code=422,
                code="INVALID_CLASSIFICATION",
                detail=f"Unknown classification. One of: {', '.join(sorted(_CLASSIFICATIONS))}.",
            )
        row.classification = fields["classification"]
        row.classified_by = actor.user.id
        row.classified_at = sa.func.now()

    if "is_expense" in fields:
        if fields["is_expense"] not in _EXPENSE_FLAGS:
            raise AppError(
                status_code=422,
                code="INVALID_EXPENSE_FLAG",
                detail="`is_expense` must be one of: yes, no, undecided.",
            )
        if fields["is_expense"] != "undecided" and row.direction != "debit":
            # The database says this too; catching it here gives a named code instead of an
            # opaque constraint violation.
            raise AppError(
                status_code=422,
                code="ONLY_DEBITS_ARE_EXPENSES",
                detail=(
                    "Only a debit can be an expense -- a credit marked as one would be "
                    "incoming money counted as a cost."
                ),
            )
        row.is_expense = fields["is_expense"]
        row.expense_decided_by = actor.user.id
        row.expense_decided_at = sa.func.now()

    db.flush()
    audit.record(
        db,
        outlet_id=actor.outlet_id,
        table_name="bank_transactions",
        record_id=row.id,
        action=AuditAction.update,
        changed_by=actor.user.id,
        old_values=before,
        new_values={"classification": row.classification, "is_expense": row.is_expense},
    )
    db.commit()
    db.refresh(row)
    return _transaction_response(row)


_CLASSIFICATIONS = frozenset(
    {
        "udhaar_repayment",
        "cash_deposit",
        "paytm_settlement",
        "iocl_ms_hsd",
        "iocl_cbg",
        "bank_charge",
        "loan",
        "self_transfer",
        "other",
        "unclassified",
    }
)

_EXPENSE_FLAGS = frozenset({"yes", "no", "undecided"})
