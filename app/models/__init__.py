"""ORM models.

Importing this package registers every model on `Base.metadata`, which is what
`alembic revision --autogenerate` compares the live database against. alembic/env.py
imports this package for exactly that reason, so a new model file must be added here or
autogenerate will not see it.
"""

from __future__ import annotations

from app.models.attachment import Attachment
from app.models.audit import AuditLog
from app.models.cash import BankDeposit, DailyCashSummary, NonFuelSale
from app.models.collection import Collection
from app.models.credit import CreditCustomer, CreditRepayment, CreditSale
from app.models.expense import Expense
from app.models.expense_category import ExpenseCategory
from app.models.fuel import FuelMargin, FuelPrice, FuelType
from app.models.idempotency import IdempotencyKey
from app.models.nozzle import Nozzle
from app.models.outlet import Outlet
from app.models.reading import NozzleReading
from app.models.shift import OutletShiftTemplate, Shift
from app.models.shortfall import SalesmanShortfall, SalesmanShortfallSettlement
from app.models.user import OutletMembership, UserProfile

__all__ = [
    "Attachment",
    "AuditLog",
    "BankDeposit",
    "Collection",
    "CreditCustomer",
    "CreditRepayment",
    "CreditSale",
    "DailyCashSummary",
    "Expense",
    "ExpenseCategory",
    "FuelMargin",
    "FuelPrice",
    "FuelType",
    "IdempotencyKey",
    "NonFuelSale",
    "Nozzle",
    "NozzleReading",
    "Outlet",
    "OutletMembership",
    "OutletShiftTemplate",
    "SalesmanShortfall",
    "SalesmanShortfallSettlement",
    "Shift",
    "UserProfile",
]
