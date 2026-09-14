"""Parsing a bank statement export (CLAUDE.md §5.3a, §13.36).

Pure functions over `bytes` -- no I/O, no FastAPI, no database, no storage client. The same
shape as `app/core/uploads.py` and for the same reason: every case here is testable with no
server, no database and no file on disk, which is what lets the module that turns a file into
money rows be the most heavily tested part of the phase.

## Why this refuses an unfamiliar layout instead of doing its best

A tolerant parser that guesses at unrecognised headers was rejected outright, and the reason
is the failure mode rather than tidiness. The export carries **separate `WITHDRAWAL(DR)` and
`DEPOSIT(CR)` columns**; mistake one for the other and every repayment enters backwards.
Nothing crashes. The ledger still balances. Every total still looks like money. The only
symptom is that customers who paid appear to owe more, months later, with no way to trace it
back to the import that did it.

So: the header row is *located* by scanning for a known cell, the required columns are
*identified* by name, and anything else raises `UNRECOGNISED_STATEMENT_FORMAT` naming the
headers it actually found. **A second bank means writing a second parser**, deliberately --
that is cheaper than one parser quietly misreading both (§13.36).

## Why the header is found by scanning rather than by row index

The sample has nine preamble rows -- account holder, an address containing embedded newlines,
branch, MICR, IFSC, a title line. That count is a property of *one export on one day*, not of
the bank: a longer address or an extra nominee line moves it. Scanning for `TRAN DATE` is
stable against everything except a genuine format change, which is the thing we want to fail
on anyway.

## Money

`Decimal`, straight from the string, per §3 rule 1. Never `float`, not even transiently --
`float("1,23,456.78".replace(",", ""))` looks harmless and is exactly the bug §3 rule 1
exists to prevent. Indian digit grouping (`1,23,456.78`) is handled by removing separators
rather than by a locale, and the `Cr`/`Dr` suffix on the balance column is stripped here
rather than being carried into the database.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from app.core.errors import AppError

# The cell that marks the header row. Searched for case-insensitively with whitespace
# collapsed, because a bank that changes 'TRAN DATE' to 'Tran Date' has not changed its
# format in any way that matters.
_HEADER_ANCHOR = "tran date"

# Header cell -> the field it supplies. Matched after normalisation (lower-cased, whitespace
# collapsed), so 'WITHDRAWAL(DR)' and 'Withdrawal (Dr)' are the same key.
_COLUMN_ALIASES: dict[str, str] = {
    "tran date": "txn_date",
    "transaction date": "txn_date",
    "value date": "value_date",
    "narration": "narration",
    "description": "narration",
    "particulars": "narration",
    "withdrawal(dr)": "debit",
    "withdrawal (dr)": "debit",
    "withdrawal": "debit",
    "debit": "debit",
    "deposit(cr)": "credit",
    "deposit (cr)": "credit",
    "deposit": "credit",
    "credit": "credit",
    "balance(inr)": "balance",
    "balance (inr)": "balance",
    "balance": "balance",
    "chq.no.": "cheque_no",
    "chq no": "cheque_no",
    "cheque no": "cheque_no",
}

# Without a date, a narration and *both* amount columns there is nothing to import safely.
# `balance` is deliberately not required: a statement with no running balance is still worth
# importing -- the lines are the point, and §5.3a makes the endpoints nullable for this reason.
_REQUIRED_FIELDS = frozenset({"txn_date", "narration", "debit", "credit"})

# dd/mm/yyyy in the sample. Listed explicitly rather than guessed, because 01/07/2026 is a
# valid date under both dd/mm and mm/dd and picking the wrong one silently moves every
# transaction by up to eleven months.
_DATE_FORMATS = ("%d/%m/%Y", "%d-%m-%Y")

_AMOUNT_STRIP = re.compile(r"[,\s₹]")
_BALANCE_SUFFIX = re.compile(r"(cr|dr)\.?$", re.IGNORECASE)
_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class ParsedLine:
    """One statement line, after parsing and before it becomes a database row."""

    txn_date: date
    narration: str
    amount: Decimal
    direction: str  # "credit" | "debit" -- mirrors the bank_txn_direction enum
    running_balance: Decimal | None
    fingerprint: str


@dataclass(frozen=True)
class ParsedStatement:
    """Everything a file yields: its lines, its period, and its endpoints."""

    lines: tuple[ParsedLine, ...]
    period_from: date
    period_to: date
    opening_balance: Decimal | None
    closing_balance: Decimal | None


def _normalise_header(cell: str) -> str:
    return _WHITESPACE.sub(" ", cell.strip().lower())


def normalise_narration(narration: str) -> str:
    """Whitespace collapsed and upper-cased -- the form used for fingerprinting and matching.

    The stored `narration` column keeps the bank's own text verbatim; this is a derived copy.
    Normalising makes a reformatted re-export of the same month fingerprint identically, which
    is what stops a duplicate import writing the month twice.
    """
    return _WHITESPACE.sub(" ", narration.strip()).upper()


def fingerprint(
    *, account_id: str, txn_date: date, amount: Decimal, direction: str, narration: str
) -> str:
    """A stable identity for one statement line (§5.3a).

    Account-scoped rather than global: two accounts genuinely can carry the same date, amount
    and narration, and a global identity would refuse the second one's real transaction.
    """
    payload = "|".join(
        (
            account_id,
            txn_date.isoformat(),
            # `str` on a Decimal is exact; a float would make this identity depend on binary
            # rounding, so a line could fingerprint differently on different machines.
            str(amount),
            direction,
            normalise_narration(narration),
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _decode(data: bytes) -> str:
    """Bank exports are not reliably UTF-8. Try the likely encodings, then give up loudly."""
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise AppError(
        status_code=422,
        code="UNREADABLE_STATEMENT_FILE",
        detail="The file could not be decoded as text. Is it a CSV rather than a PDF or XLS?",
    )


def _parse_amount(raw: str) -> Decimal | None:
    """`None` for an empty cell; a `Decimal` otherwise. Never a float (§3 rule 1)."""
    cleaned = _AMOUNT_STRIP.sub("", raw or "")
    cleaned = _BALANCE_SUFFIX.sub("", cleaned)
    if not cleaned:
        return None
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        raise AppError(
            status_code=422,
            code="UNPARSEABLE_AMOUNT",
            detail=f"Could not read {raw!r} as an amount.",
        ) from None


def _parse_date(raw: str) -> date | None:
    text = (raw or "").strip()
    if not text:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _locate_header(rows: list[list[str]]) -> tuple[int, dict[str, int]]:
    """Find the header row and map each required field to its column index.

    Raises `UNRECOGNISED_STATEMENT_FORMAT` rather than guessing -- see the module docstring.
    """
    for index, row in enumerate(rows):
        normalised = [_normalise_header(cell) for cell in row]
        if _HEADER_ANCHOR not in normalised:
            continue

        columns: dict[str, int] = {}
        for position, cell in enumerate(normalised):
            field = _COLUMN_ALIASES.get(cell)
            # First occurrence wins: a trailing blank or repeated header must not shadow the
            # real column.
            if field is not None and field not in columns:
                columns[field] = position

        missing = _REQUIRED_FIELDS - columns.keys()
        if missing:
            found = ", ".join(cell for cell in normalised if cell) or "(none)"
            raise AppError(
                status_code=422,
                code="UNRECOGNISED_STATEMENT_FORMAT",
                detail=(
                    "The statement's header row is missing required columns: "
                    f"{', '.join(sorted(missing))}. Found: {found}. This parser reads Bank "
                    "of Baroda's CSV export; another bank's format needs its own parser."
                ),
            )
        return index, columns

    raise AppError(
        status_code=422,
        code="UNRECOGNISED_STATEMENT_FORMAT",
        detail=(
            "No header row was found -- expected a row containing 'TRAN DATE'. This parser "
            "reads Bank of Baroda's CSV export; another bank's format needs its own parser."
        ),
    )


def _is_newest_first(lines: list[ParsedLine]) -> bool:
    """Which end of the file is the newest transaction?

    Decided by the dates rather than assumed, so a bank exporting oldest-first works too. Ties
    are ignored entirely -- the question is only which *direction* the file runs, and a run of
    same-day lines says nothing about that. Equal endpoints (a single line, or a file entirely
    within one day) fall back to newest-first, matching the export this parser was written
    for; with one date the choice cannot change any arithmetic anyway.
    """
    first, last = lines[0].txn_date, lines[-1].txn_date
    if first != last:
        return first > last
    return True


def parse(data: bytes, *, account_id: str) -> ParsedStatement:
    """Read a statement export into lines, a period and its endpoints.

    `account_id` is used only to build fingerprints -- nothing here touches the database.
    """
    text = _decode(data)
    rows = list(csv.reader(io.StringIO(text)))

    header_index, columns = _locate_header(rows)

    def cell(row: list[str], field: str) -> str:
        position = columns.get(field)
        if position is None or position >= len(row):
            return ""
        return row[position]

    lines: list[ParsedLine] = []
    for row in rows[header_index + 1 :]:
        txn_date = _parse_date(cell(row, "txn_date"))
        # A row with no parseable date is preamble, a trailer ("This is a computer-generated
        # statement"), or a page break. Skipping quietly is right here: these are structural
        # artefacts of the export, not data somebody entered.
        if txn_date is None:
            continue

        debit = _parse_amount(cell(row, "debit"))
        credit = _parse_amount(cell(row, "credit"))

        # Exactly one side must be filled. Both, or neither, means the columns were not what
        # this parser thinks they are -- which is precisely the case it must not paper over.
        if (debit is None) == (credit is None):
            raise AppError(
                status_code=422,
                code="UNRECOGNISED_STATEMENT_FORMAT",
                detail=(
                    f"The line dated {txn_date.isoformat()} has "
                    + ("both a debit and a credit" if debit is not None else "neither")
                    + ". Each statement line must fill exactly one of the two columns."
                ),
            )

        direction = "debit" if debit is not None else "credit"
        amount = debit if debit is not None else credit
        assert amount is not None  # narrowing for the type checker; the branch above proves it

        if amount <= 0:
            raise AppError(
                status_code=422,
                code="UNPARSEABLE_AMOUNT",
                detail=(
                    f"The line dated {txn_date.isoformat()} has a non-positive amount "
                    f"({amount}). Direction is carried by the column, never by a sign."
                ),
            )

        narration = (cell(row, "narration") or "").strip()
        if not narration:
            raise AppError(
                status_code=422,
                code="UNRECOGNISED_STATEMENT_FORMAT",
                detail=f"The line dated {txn_date.isoformat()} has an empty narration.",
            )

        lines.append(
            ParsedLine(
                txn_date=txn_date,
                narration=narration,
                amount=amount,
                direction=direction,
                running_balance=_parse_amount(cell(row, "balance")),
                fingerprint=fingerprint(
                    account_id=account_id,
                    txn_date=txn_date,
                    amount=amount,
                    direction=direction,
                    narration=narration,
                ),
            )
        )

    if not lines:
        raise AppError(
            status_code=422,
            code="EMPTY_STATEMENT",
            detail="The statement's header was found but it contains no transaction lines.",
        )

    dates = [line.txn_date for line in lines]

    # **The endpoints come from file position, never from the date.** The running balance is
    # the balance *after* each transaction **in the order the file lists them**, so the two
    # ends of the file are the two ends of the period -- whatever the dates say.
    #
    # This is not a nicety. The real July export has four lines dated 1 July whose balances
    # run *downwards* through the file even though all four are credits, because the file is
    # newest-first. Choosing an endpoint with `min(txn_date)` therefore picks an arbitrary row
    # among the ties and reverses the wrong amount out of the wrong balance. The first version
    # of this function did exactly that and produced an opening balance ₹349,962 too high --
    # a plausible figure, in the right order of magnitude, that nothing else in the system
    # could contradict, feeding Phase 21's profit bridge as the one term with no second
    # source. Caught by the invariant asserted in the tests: opening + credits − debits must
    # equal closing.
    #
    # Direction of travel is detected rather than assumed: a bank that exports oldest-first is
    # equally valid, and the endpoints simply swap.
    newest_first = _is_newest_first(lines)
    oldest = lines[-1] if newest_first else lines[0]
    newest = lines[0] if newest_first else lines[-1]

    return ParsedStatement(
        lines=tuple(lines),
        period_from=min(dates),
        period_to=max(dates),
        # The balance *before* the oldest line is not printed anywhere, so the opening figure
        # is that line's own running balance reversed out of it. Only possible when the file
        # carries a balance column at all -- hence the nullability in §5.3a.
        opening_balance=(
            None
            if oldest.running_balance is None
            else (
                oldest.running_balance - oldest.amount
                if oldest.direction == "credit"
                else oldest.running_balance + oldest.amount
            )
        ),
        closing_balance=newest.running_balance,
    )
