"""The audit log's vocabulary (CLAUDE.md §5.3).

Built in Phase 4 rather than Phase 11. §11 invited the move ("consider building this at
step 4 instead if it feels cheap to do early; retrofitting is the usual regret") and §5.2
forces it: a backwards shift status transition must be audit-logged, and §6.8 lets an admin
reopen a shift. Phase 4 is the first phase that cannot be correct without this.
"""

from __future__ import annotations

from enum import StrEnum


class AuditAction(StrEnum):
    """What kind of change an audit row describes.

    `insert` / `update` -- an ordinary row creation or field change.
    `reversal` -- §6.9's correction mechanism: a new row negating an earlier one. Nothing
                  emits this until the financial tables exist; the label is here because
                  §5.3 names it and the enum's labels are fixed at migration time.
    `status_change` -- a lifecycle move, e.g. a shift going open -> closed -> locked, or
                  an admin reopening one. Distinguished from `update` because §5.2 singles
                  out backwards transitions as the thing that must be traceable.
    """

    insert = "insert"
    update = "update"
    reversal = "reversal"
    status_change = "status_change"
