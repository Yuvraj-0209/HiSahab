# Phase 30 — Flagged expenses, one day at a time

The full reasoning is in CLAUDE.md §11 Phase 30, §13.47 and the three guardrails at the end of
§14. This is the working record.

## What was built
- `GET /expenses/flagged?business_date=` and `GET /expenses/flagged/by-date` (manager floor).
- `expenses.awaiting_review_clause()`: one predicate for the lock, the queue and the counts.
- Lock refusal toast: "Review expenses" → `/expenses/flagged?date=<business_date>&shift=<id>`.
- `ui/DateStrip` (CSS-transition motion, no GSAP), `ui/ReviewExpense` (extracted `ReviewForm`).
- `screens/flagged_expenses.tsx` rebuilt: strip, day header, rows grouped by category, review
  sheet, "back to the shift" once the day is clear.

## Decisions made on the way
- **CSS transitions, not GSAP, for the strip.** Chips are keyed by date and carry their offset as
  `--o`; fourteen are rendered (four parked invisible) so a chip entering fades in. One engine per
  element.
- **No opacity on a visible chip.** The first version faded neighbours by distance; that fails
  contrast for enabled controls and stalls the smoke suite's settle check. Depth is scale plus an
  edge mask.
- **Chips narrower than their slot** so the magnified day does not sit on its neighbours' labels
  (found by looking at the rendered screen, not by a test).
- **`replace` navigation when choosing a day**, so ten days of stepping is not ten Back presses.

## Verification
- `pytest`: 1,755 passed (new: `tests/test_expense_flagged_queue.py`, 11 cases).
- `vitest`: 114 passed (new: `DateStrip`, `feedback`, `flagged_expenses`, `addDays`).
- `npm run budget`: within budget; `npm run e2e -g flagged`: light and dark, axe clean.

## Not verified by a machine
Whether the strip's glide *feels* right, at 390 px, on a real phone, and under reduced motion.
