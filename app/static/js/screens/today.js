/* Today: the shift spine (CLAUDE.md §4.7, §5.2, §6.8).
 *
 * §5.2 calls `shifts` "the spine -- everything hangs off this", and this screen is that spine
 * made visible: which shift is open, what it is worth so far, and the lifecycle actions
 * available on it.
 *
 * ## The status transitions, and why each button says what it says
 *
 * open -> closed -> locked, and never backwards without an admin action that is itself
 * audit-logged (§5.2). The buttons are role-gated for politeness only -- §8's floors are
 * enforced server-side, and every action here handles a real 403.
 *
 * **Closing can legitimately fail, and those failures are the feature.** §6.8's three
 * preconditions -- MISSING_NOZZLE_READINGS, MISSING_COLLECTIONS, CREDIT_SALE_MISSING_RECEIPT
 * -- fire on *absence*, never on a mismatch. A shift whose collections do not equal its sales
 * closes normally, because that gap is §6.4's variance and §6.6's udhaar. §14 is explicit
 * that blocking on it "teaches staff to type figures that balance", so this screen never
 * pre-checks the numbers and never disables Close on the strength of them. It sends the
 * request and reports what the server said.
 *
 * ## Profit is labelled, every time it appears
 *
 * §13.7: what this reports is *gross fuel margin on quantity sold*, not business profit. It
 * excludes stock revaluation entirely -- holding 12 kL when the rate rises ₹1 is a real
 * ₹12,000 gain this system will never see. §13.7 requires the label wherever the figure is
 * displayed, because an unlabelled "profit" here is exactly the plausible-but-wrong number
 * this project exists to prevent.
 */

import { el, empty, pill, render, row } from "../dom.js";
import { api, explain, ApiError } from "../api.js";
import { format, quantity } from "../money.js";
import { businessDate, businessDateWeekday, timeOnly, todayAtOutlet } from "../time.js";
import { field, Form, select } from "../ui/field.js";
import { openSheet } from "../ui/sheet.js";
import { notify } from "../ui/toast.js";
import { satisfies } from "../ui/nav.js";

const STATUS_PILL = { open: "open", closed: "closed", locked: "locked" };

export async function renderToday(container, { session, navigate }) {
  const { shell, me } = session;
  shell.setTab("today");
  shell.setTitle("Today");
  shell.setActions();

  render(container, el("div", { className: "t-caption", text: "Loading…" }));

  let shift = null;
  try {
    shift = await api.get("/shifts/current");
  } catch (error) {
    if (!(error instanceof ApiError) || error.code !== "NO_OPEN_SHIFT") {
      // NOT_YOUR_SHIFT reaches an attendant when somebody else's shift is the open one --
      // a real state, not an error to hide behind a generic message.
      render(container, errorCard(error, () => renderToday(container, { session, navigate })));
      return;
    }
  }

  if (!shift) {
    renderNoShift(container, { session, navigate });
    return;
  }

  shell.setTitle(
    "Today",
    `${businessDateWeekday(shift.business_date)} ${businessDate(shift.business_date)} · shift ${shift.sequence}`,
  );

  // Both of these are manager-floor reads, so an attendant simply does not make them --
  // §8 for sales (a report, not a data-entry sheet) and for the roster (an attendant has
  // no foreign name to resolve, because they cannot read anybody else's shift).
  let sales = null;
  const roster = new Map();
  if (satisfies(me.role, "manager")) {
    try {
      for (const person of await api.get("/users", { include_inactive: true })) {
        roster.set(person.id, person.full_name);
      }
    } catch {
      // Left empty on purpose. `shiftCard` falls back to the pre-Phase-14 wording rather
      // than failing the screen over a label.
    }
    try {
      sales = await api.get(`/shifts/${shift.id}/sales`);
    } catch (error) {
      // A missing price refuses valuation outright (§6.3) and that is correct behaviour --
      // a day valued at zero would reconcile to a surplus nobody can explain. Reported as a
      // note on the card rather than as a failure of the whole screen.
      sales = { error };
    }
  }

  render(
    container,
    el("div", { className: "stack" }, [
      // A grid rather than a column: on a laptop these three read as a dashboard, and on a
      // phone auto-fit collapses them back to one column with no breakpoint to maintain.
      el("div", { className: "grid" }, [
        shiftCard(shift, me, roster),
        sales ? salesCard(sales) : null,
        actionsCard(shift, { session, container, navigate }),
      ]),
      el("div", { className: "section-label t-micro", text: "Entry" }),
      el("div", { className: "list" }, [
        linkRow("Nozzle readings", "Meters, testing, and the carried opening", () =>
          navigate(`#/shifts/${shift.id}/readings`),
        ),
        linkRow("Collections", "Cash, card, UPI and wallet", () =>
          navigate(`#/shifts/${shift.id}/collections`),
        ),
        linkRow("Expenses", "What was paid out of the drawer", () =>
          navigate(`#/shifts/${shift.id}/expenses`),
        ),
        linkRow("Credit & repayments", "Udhaar issued and settled", () =>
          navigate(`#/shifts/${shift.id}/credit`),
        ),
      ]),
    ]),
  );
}

function linkRow(label, hint, onClick) {
  return el(
    "button",
    {
      className: "list-row",
      attrs: { type: "button" },
      on: { click: onClick },
      style: { width: "100%", background: "none", border: 0, textAlign: "left", font: "inherit", color: "inherit", cursor: "pointer" },
    },
    [
      el("div", { className: "list-row-main" }, [
        el("div", { className: "t-body", text: label }),
        el("div", { className: "t-caption", text: hint }),
      ]),
      el("div", { className: "t-body", text: "›", attrs: { "aria-hidden": "true" } }),
    ],
  );
}

function shiftCard(shift, me, roster) {
  // Phase 14. This row used to read "another attendant" for anybody but the caller, because
  // nothing in the app could turn a user id into a person. `roster` is a Map, empty for an
  // attendant (who never sees a foreign shift anyway) and empty if the fetch failed -- in
  // both cases the old string is still the fallback, because a roster that will not load
  // must not blank the card.
  const attendant =
    shift.attendant_id === me.id
      ? `${me.full_name} (you)`
      : (roster.get(shift.attendant_id) ?? "another attendant");

  return el("div", { className: "card stack" }, [
    el("div", { className: "row-between" }, [
      el("div", {}, [
        el("div", { className: "t-micro", text: "Shift" }),
        el("div", {
          className: "t-title",
          text: `${businessDate(shift.business_date)} · ${shift.sequence}`,
        }),
      ]),
      pill(shift.status, STATUS_PILL[shift.status] ?? "neutral"),
    ]),
    el("div", { className: "list" }, [
      row("Started", timeOnly(shift.started_at)),
      row("Ends", timeOnly(shift.ended_at, { absent: "not set" })),
      row("Attendant", attendant),
    ]),
  ]);
}

function salesCard(sales) {
  if (sales.error) {
    return el("div", { className: "card stack" }, [
      el("div", { className: "t-micro", text: "Metered sales" }),
      el("p", { className: "t-body", text: explain(sales.error) }),
      el("p", {
        className: "t-caption",
        text: "Sales cannot be valued until a price exists for every fuel sold. Nothing is wrong with the readings.",
      }),
    ]);
  }

  const quantities = Object.entries(sales.quantity_by_unit ?? {});

  return el("div", { className: "card stack" }, [
    el("div", { className: "t-micro", text: "Metered sales" }),
    el("div", { className: "t-amount", text: format(sales.total_sale_value) }),

    // §4.5: quantities are keyed by unit and NEVER summed across them. A litre of petrol and
    // a kilogram of CBG are not addable, and one "total quantity" would be a number with no
    // meaning. Rendered as separate figures for the same reason the API returns them that way.
    quantities.length
      ? el(
          "div",
          { className: "row" },
          quantities.map(([unit, amount]) =>
            el("span", {
              className: "pill pill-neutral t-numeric",
              text: quantity(amount, unit),
            }),
          ),
        )
      : null,

    el("div", { className: "list" }, [
      row("Gross fuel margin", format(sales.total_gross_fuel_margin, { absent: "no margin entered" })),
    ]),

    // §13.7, and this label is not optional. Petrol and diesel margins have never been
    // entered at this outlet (§14's open questions), so this figure covers CBG alone today.
    el("p", {
      className: "t-caption",
      text:
        "Gross fuel margin on quantity sold — not business profit. It excludes stock revaluation, " +
        "so a price move against fuel already in the tank is invisible here.",
    }),

    sales.incomplete
      ? el("p", {
          className: "t-caption",
          text: "Some nozzles have no closing reading yet, so this total is partial.",
        })
      : null,
  ]);
}

function actionsCard(shift, { session, container, navigate }) {
  const { me } = session;
  const actions = [];

  if (shift.status === "open" && satisfies(me.role, "manager")) {
    actions.push(
      el("button", {
        className: "btn btn-primary btn-block",
        text: "Close shift",
        attrs: { type: "button" },
        on: { click: () => closeShift(shift, { session, container, navigate }) },
      }),
    );
  }

  if (shift.status === "closed" && satisfies(me.role, "admin")) {
    actions.push(
      el("button", {
        className: "btn btn-primary btn-block",
        text: "Lock shift",
        attrs: { type: "button" },
        on: { click: () => lockShift(shift, { session, container, navigate }) },
      }),
      el("button", {
        className: "btn btn-block",
        text: "Reopen shift",
        attrs: { type: "button" },
        on: { click: () => reopenShift(shift, { session, container, navigate }) },
      }),
    );
  }

  if (!actions.length) {
    // Never a bare empty region (§16: every screen answers "what's here?"). Saying why there
    // is nothing to do is more useful than showing nothing.
    const reason =
      shift.status === "locked"
        ? "This shift is locked. Locked is terminal — corrections happen as reversals."
        : shift.status === "closed"
          ? "This shift is closed. An admin can lock or reopen it."
          : "Only a manager or admin can close a shift.";
    return el("div", { className: "card" }, [el("p", { className: "t-caption", text: reason })]);
  }

  return el("div", { className: "card stack" }, actions);
}

/* --- lifecycle --------------------------------------------------------------- */

async function closeShift(shift, context) {
  // No client-side pre-check of collections against sales, deliberately. §6.8 and §14: that
  // gap is the variance, and refusing to close on it leaves the salesman in front of a form
  // with one freely adjustable field. The server decides; we report.
  try {
    await api.patch(`/shifts/${shift.id}/close`, {});
    notify.success("Shift closed.");
    renderToday(context.container, context);
  } catch (error) {
    notify.error(explain(error), { requestId: error.requestId });
  }
}

async function lockShift(shift, context) {
  try {
    await api.patch(`/shifts/${shift.id}/lock`, {});
    notify.success("Shift locked.");
    renderToday(context.container, context);
  } catch (error) {
    notify.error(explain(error), { requestId: error.requestId });
  }
}

function reopenShift(shift, context) {
  // §6.8: reopening takes a MANDATORY reason and is audit-logged. A sheet rather than a
  // confirm dialog, because the reason is the point -- and §5.2 requires it to be a real
  // sentence, not an acknowledgement.
  const reason = field({
    name: "reason",
    label: "Why is this being reopened?",
    required: true,
    hint: "Recorded in the audit trail against your name. 3–500 characters.",
  });
  const form = new Form({ reason });

  const submit = el("button", {
    className: "btn btn-primary btn-block",
    text: "Reopen shift",
    attrs: { type: "button" },
  });

  submit.addEventListener("click", async () => {
    form.clearErrors();
    submit.disabled = true;
    try {
      await api.patch(`/shifts/${shift.id}/reopen`, { reason: form.values().reason });
      sheet.close();
      notify.success("Shift reopened.");
      // §13.10: a mid-chain reopen FLAGS the next shift's reading for review rather than
      // recomputing it, and flags a finalised day's summary (§13.16). Said out loud, because
      // the consequence is invisible on this screen and somebody has to act on it.
      notify.info(
        "Any following shift's opening reading is now flagged for review. Nothing was recomputed.",
      );
      renderToday(context.container, context);
    } catch (error) {
      submit.disabled = false;
      const unmatched = error.isValidation ? form.showErrors(error.detail) : [];
      if (!error.isValidation || unmatched.length) {
        notify.error(explain(error), { requestId: error.requestId });
      }
    }
  });

  const sheet = openSheet({
    title: "Reopen shift",
    body: el("div", { className: "stack" }, [
      el("p", {
        className: "t-caption",
        text: "Reopening moves this shift back to open. The reason is stored in the audit trail.",
      }),
      reason,
    ]),
    footer: el("div", { style: { padding: "0 1rem 1rem" } }, [submit]),
  });
}

/* --- no open shift ----------------------------------------------------------- */

function renderNoShift(container, context) {
  const { session } = context;
  session.shell.setTitle("Today", "No open shift");

  render(
    container,
    el("div", { className: "stack" }, [
      el("div", { className: "card stack" }, [
        el("p", { className: "t-body", text: "There is no open shift at this outlet." }),
        el("p", {
          className: "t-caption",
          text: "Only one shift may be open at a time — that is what makes the carried-forward meter reading unambiguous.",
        }),
        el("button", {
          className: "btn btn-primary btn-block",
          text: "Open a shift",
          attrs: { type: "button" },
          // `container` is passed explicitly rather than read off `context`, and that is a
          // bug fix rather than a style preference. This function used to call
          // `openShiftSheet(context)`, and `renderNoShift` is invoked with
          // `{ session, navigate }` -- no `container` key -- so the refresh after a
          // successful POST reached `render(undefined, ...)` and threw. The shift was
          // created and the screen never showed it.
          //
          // Found in Phase 14 while adding the attendant picker below. §13.18 names this
          // exact class: everything below the browser is tested to 100% and a broken form
          // fails no suite. A positional argument is the version that cannot be forgotten.
          on: { click: () => openShiftSheet(container, context) },
        }),
      ]),
    ]),
  );
}

async function openShiftSheet(container, context) {
  const { me } = context.session;

  // Defaults come from the outlet's shift template (§5.1), which is exactly what it is for:
  // "nobody should retype 06:00 and 22:00 every morning". The values are materialised onto
  // the shift row by the server -- the template is never read back afterwards (§14).
  let templates = [];
  try {
    templates = await api.get("/shift-templates");
  } catch {
    // Not fatal: started_at is optional and the server falls back to the template itself.
    templates = [];
  }

  // Phase 14. A manager may open a shift in a salesman's name -- `POST /shifts` has
  // accepted `attendant_id` since Phase 4 -- but until the roster endpoint existed there
  // was no way to offer the choice, so through the app it was impossible.
  //
  // Gated on the manager floor for two reasons that agree: `GET /users` refuses an
  // attendant (§8), and `POST /shifts` refuses an attendant a foreign `attendant_id` with
  // 403 NOT_YOUR_SHIFT anyway -- so a picker shown to one would offer choices the server
  // would reject. Not hidden as a permission control (§8: hiding a button is UX); the
  // server enforces both halves regardless.
  let roster = [];
  if (satisfies(me.role, "manager")) {
    try {
      roster = await api.get("/users");
    } catch {
      // Not fatal either: with no roster the field is simply absent and the server
      // defaults `attendant_id` to the caller, which is the pre-Phase-14 behaviour.
      roster = [];
    }
  }

  const businessDateField = field({
    name: "business_date",
    label: "Business date",
    type: "date",
    // §6.1: a future business date is always a data-entry error, and "future" is evaluated
    // in the outlet's zone rather than the browser's.
    value: todayAtOutlet(),
    max: todayAtOutlet(),
    required: true,
    hint: "The trading day this shift belongs to — not necessarily the day it is typed in.",
  });

  const fields = { business_date: businessDateField };

  if (templates.length > 1) {
    fields.sequence_hint = select({
      name: "sequence_hint",
      label: "Shift",
      options: templates.map((template) => ({
        value: String(template.sequence),
        label: `${template.label} (${template.starts_at_local}–${template.ends_at_local})`,
      })),
      hint: "The sequence is assigned by the server; this only picks the default times.",
    });
  }

  if (roster.length) {
    fields.attendant_id = select({
      name: "attendant_id",
      label: "Attendant",
      value: me.id,
      options: roster.map((person) => ({
        value: person.id,
        label: person.id === me.id ? `${person.full_name} (you)` : person.full_name,
      })),
      hint: "The one person accountable for this shift's cash. A shortfall is booked against this name.",
    });
  }

  const form = new Form(fields);

  const submit = el("button", {
    className: "btn btn-primary btn-block",
    text: "Open shift",
    attrs: { type: "button" },
  });

  submit.addEventListener("click", async () => {
    form.clearErrors();
    submit.disabled = true;
    try {
      // `sequence` is deliberately never sent: it is server-assigned, and ShiftCreate's
      // extra="forbid" makes sending it a 422 (§14).
      const values = form.values();
      const body = { business_date: values.business_date };
      // Omitted entirely rather than sent as the caller's own id, so an attendant's request
      // is byte-identical to what it was before Phase 14 and the server's own default
      // (`payload.attendant_id or actor.user.id`) stays the single place that decision is
      // made.
      if (values.attendant_id) body.attendant_id = values.attendant_id;
      await api.post("/shifts", body);
      sheet.close();
      notify.success("Shift opened.");
      renderToday(container, context);
    } catch (error) {
      submit.disabled = false;
      const unmatched = error.isValidation ? form.showErrors(error.detail) : [];
      if (!error.isValidation || unmatched.length) {
        notify.error(explain(error), { requestId: error.requestId });
      }
    }
  });

  const sheet = openSheet({
    title: "Open a shift",
    body: el("div", { className: "stack" }, form.nodes()),
    footer: el("div", { style: { padding: "0 1rem 1rem" } }, [submit]),
  });
}

/* --- shared ------------------------------------------------------------------ */

export function errorCard(error, onRetry) {
  return el("div", { className: "card stack" }, [
    el("p", { className: "t-body", text: explain(error) }),
    error?.requestId
      ? el("p", { className: "t-micro toast-request-id", text: `Reference ${error.requestId}` })
      : null,
    onRetry
      ? el("button", {
          className: "btn",
          text: "Try again",
          attrs: { type: "button" },
          on: { click: onRetry },
        })
      : null,
  ]);
}
