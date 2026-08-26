/* The reading worksheet -- the screen §4.7 was written about.
 *
 * §4.7 is the longest argument in CLAUDE.md and its conclusion is a *user interface*
 * requirement, not a data one:
 *
 *   "So: pre-filled, not typeable, and confirmed against the physical meter, with a mismatch
 *    path that captures the real reading and raises it for review before anybody is blamed.
 *    Zero typing on a normal day; the abnormal day becomes visible instead of reassigned."
 *
 * The cost of getting this wrong is spelled out there too, and it is why the confirm control
 * below is a deliberate physical act rather than a pre-ticked box:
 *
 *   "Fuel siphoned through a nozzle between shifts still moves the totalizer -- but an assumed
 *    opening says it did not, so the missing quantity is absorbed into the next shift as sales
 *    that produced no cash. The shift then comes up short, and this outlet books a shortfall
 *    as udhaar against the salesman's own name. An assumed opening therefore converts theft
 *    into a debt owed by someone who did nothing wrong."
 *
 * So: `opening_confirmed` is a required boolean with no server-side default, and this screen
 * must not pre-satisfy it. The confirm button starts unconfirmed every time.
 *
 * ## Three states per line, and the second one is the point
 *
 *   matches      the meter reads what the chain predicted. One tap. Zero typing.
 *   mismatch     the meter reads something else. The real value is captured, a reason is
 *                required, and the row is flagged for review -- visible, not absorbed.
 *   anchor       no predecessor exists (a new nozzle, or the first shift ever). The opening
 *                becomes a required field and the caller must be an admin (§4.7).
 *
 * ## testing_quantity is a question, never a pre-filled zero
 *
 * §4.2: omitting it produces "a small, permanent, daily cash shortfall that is extremely hard
 * to diagnose", and §14 lists reading it as an omission among the failure modes. 0 is a real
 * answer -- CBG is not calibration-tested here (§4.5) -- but it has to be *an answer*. The
 * field is therefore empty until touched, with the unit named so nobody guesses litres.
 *
 * ## Two later additions, same rule
 *
 * Nozzles are grouped by fuel type into tappable tiles (a label and a status pill) rather
 * than always-expanded cards, because a full shift's worksheet is long and scrolling it
 * repeatedly is the thing this screen was slowest at. Tapping a tile opens the exact same
 * sheets described above -- nothing about what gets confirmed, or how, changed.
 *
 * "No sale" pre-fills a guess (closing = opening, testing = 0) for a nozzle that stayed dry.
 * It is a convenience, never a default: the sheet still opens, the attendant still sees both
 * figures, and Save is still a tap they have to make. See the comments at buildEntryForm and
 * closingSheet's noSaleBtn for exactly how each sheet guarantees the values are actually sent.
 */

import { el, empty, pill, render } from "../dom.js";
import { api, explain } from "../api.js";
import { quantity, reading as formatReading } from "../money.js";
import { field, Form } from "../ui/field.js";
import { openSheet } from "../ui/sheet.js";
import { notify } from "../ui/toast.js";
import { satisfies } from "../ui/nav.js";
import { errorCard } from "./today.js";

export async function renderReadings(container, { session, navigate, shiftId }, { silent = false } = {}) {
  const { shell } = session;
  shell.setTab("entry");
  shell.setTitle("Readings");
  shell.setActions();

  // A submit handler calls this again to show fresh data. The worksheet still on screen is
  // correct enough to look at for the half-second the request takes -- collapsing it to a
  // one-line "Loading…" node first (as the very first mount below still does) shrinks the
  // document just long enough for the browser to clamp window.scrollY toward 0, and nothing
  // afterwards restores it. Skipping the skeleton on a refresh removes the clamp instead of
  // working around it: the container goes stale-height straight to fresh-height, with no
  // near-empty state in between for the scroll position to be clamped against.
  if (!silent) {
    render(container, el("div", { className: "t-caption", text: "Loading…" }));
  }

  let worksheet;
  try {
    worksheet = await api.get(`/shifts/${shiftId}/readings`);
  } catch (error) {
    render(
      container,
      errorCard(error, () => renderReadings(container, { session, navigate, shiftId })),
    );
    return;
  }

  const editable = worksheet.shift_status === "open";
  const context = { session, container, navigate, shiftId, editable };
  const groups = groupByFuelType(worksheet.lines);

  render(
    container,
    el("div", { className: "stack" }, [
      el("div", { className: "card stack" }, [
        el("div", { className: "row-between" }, [
          el("span", { className: "t-micro", text: "Shift" }),
          pill(worksheet.shift_status, worksheet.shift_status),
        ]),
        el("p", {
          className: "t-caption",
          text: editable
            ? "Each opening is carried forward from that nozzle's last closing reading. Confirm it against the meter — do not assume it."
            : "This shift is no longer open, so readings cannot be changed. Corrections happen through an admin reopen.",
        }),
      ]),

      groups.length
        ? el(
            "div",
            { className: "stack" },
            groups.flatMap(({ code, lines }) => [
              el("div", { className: "section-label t-micro", text: code }),
              el(
                "div",
                { className: "tile-grid" },
                lines.map((line) => tile(line, context)),
              ),
            ]),
          )
        : empty("No active nozzles at this outlet."),
    ]),
  );
}

/** Groups worksheet lines by fuel_type_code, in first-seen order. fuel_type_code is
 * admin-managed data (§5.1) with no fixed set of values -- this outlet happens to sell
 * petrol, diesel and CBG, but the grouping is driven entirely by whatever distinct codes are
 * actually present, never a hardcoded list. First-seen order (rather than alphabetical) is
 * deliberate: it matches whatever order the admin entered the nozzles/dispensers in, which is
 * more meaningful to an attendant walking the forecourt than an alphabetical accident. */
function groupByFuelType(lines) {
  const order = [];
  const byCode = new Map();
  for (const line of lines) {
    if (!byCode.has(line.fuel_type_code)) {
      byCode.set(line.fuel_type_code, []);
      order.push(line.fuel_type_code);
    }
    byCode.get(line.fuel_type_code).push(line);
  }
  return order.map((code) => ({ code, lines: byCode.get(code) }));
}

/* --- one nozzle, as a compact tile --------------------------------------------- */

function tile(line, context) {
  const saved = line.reading;
  const done = saved !== null;
  const flagged = done && saved.requires_review;
  const status = tileStatus(line, saved, done, flagged);

  return el(
    "button",
    {
      className: flagged ? "tile flagged" : "tile",
      attrs: { type: "button" },
      on: { click: () => openTile(line, context, { saved, done }) },
    },
    [
      el("div", { className: "tile-label t-body", text: line.nozzle_label }),
      pill(status.text, status.kind),
      done ? tileFigures(line, saved) : null,
    ],
  );
}

/** A compact one-line glance at what's already recorded, using the same formatters (never a
 * client-side rounding -- §3 rule 1) as the full detail one tap away in savedRows(). Absent
 * values fall back to money.js's own short "—", not savedRows()'s longer "awaiting closing" --
 * that fuller explanation still lives one tap away; this row is a glance, not the detail. */
function tileFigures(line, saved) {
  return el("div", { className: "tile-figures t-caption t-numeric" }, [
    tileFigure("O", formatReading(saved.opening_reading)),
    tileFigure("C", formatReading(saved.closing_reading)),
    tileFigure("S", quantity(saved.quantity_sold, line.unit_of_measure)),
  ]);
}

function tileFigure(tag, value) {
  return el("span", { className: "tile-figure" }, [
    el("span", { className: "tile-figure-tag", text: tag }),
    ` ${value}`,
  ]);
}

/** Mirrors the pill vocabulary this screen has always shown -- only where it's shown moved,
 * from an inline card to a tile. */
function tileStatus(line, saved, done, flagged) {
  if (!done) {
    return line.requires_anchor ? { text: "needs anchor", kind: "review" } : { text: "not started", kind: "neutral" };
  }
  if (flagged) return { text: "flagged", kind: "review" };
  return saved.closing_reading === null ? { text: "opening only", kind: "neutral" } : { text: "recorded", kind: "open" };
}

/** Decides which sheet a tap opens. Every branch is a relocation of an existing flow, not new
 * business logic: what happens for each state is unchanged from before tiles existed. */
function openTile(line, context, { saved, done }) {
  if (!context.editable) return readOnlySheet(line, saved, done);
  if (!done) {
    return line.requires_anchor ? anchorSheet(line, context) : entrySheet(line, context);
  }
  return closingSheet(line, saved, context);
}

function readOnlySheet(line, saved, done) {
  openSheet({
    title: line.nozzle_label,
    body: done
      ? savedRows(line, saved)
      : el("p", { className: "t-caption", text: "No reading was recorded for this nozzle." }),
  });
}

/** The read-only detail rows for a nozzle that already has a reading -- opening, closing,
 * testing, quantity sold, and whatever exceptional flags apply. Extracted so both the closing
 * sheet (editable) and the read-only sheet (a closed/locked shift) can show the same figures. */
function savedRows(line, saved) {
  return el("div", { className: "list" }, [
    labelled("Opening", formatReading(saved.opening_reading)),
    // The chain's prediction is shown beside the confirmed value whenever they differ. §5.2:
    // storing both is what makes "the meter did not say what we expected" a fact on the row
    // rather than an event nobody recorded -- so it is shown, not hidden once resolved.
    saved.chained_opening_reading !== null && saved.chained_opening_reading !== saved.opening_reading
      ? labelled("Chain predicted", formatReading(saved.chained_opening_reading), "text-short")
      : null,
    saved.opening_variance_reason ? labelled("Variance reason", saved.opening_variance_reason) : null,
    saved.chained_opening_reading === null ? labelled("Chain", "anchored — no predecessor") : null,
    labelled("Closing", formatReading(saved.closing_reading, { absent: "not entered" })),
    labelled("Testing", quantity(saved.testing_quantity, line.unit_of_measure)),
    saved.rollover_occurred ? labelled("Rollover", "yes") : null,
    saved.meter_reset_occurred ? labelled("Meter reset", "yes") : null,
    saved.manual_quantity_override !== null
      ? labelled("Manual override", quantity(saved.manual_quantity_override, line.unit_of_measure))
      : null,
    labelled(
      "Quantity sold",
      quantity(saved.quantity_sold, line.unit_of_measure, { absent: "awaiting closing" }),
    ),
  ]);
}

function flaggedNote(saved) {
  if (!saved.requires_review) return null;
  return el("p", {
    className: "t-caption text-short",
    text: saved.review_note
      ? `Flagged for review: ${saved.review_note}`
      : "Flagged for review — a human needs to reconcile this reading.",
  });
}

function labelled(label, value, valueClass = "") {
  return el("div", { className: "list-row" }, [
    el("div", { className: "list-row-main t-caption", text: label }),
    el("div", { className: `list-row-value t-body t-numeric ${valueClass}`, text: value }),
  ]);
}

/* --- creating a reading --------------------------------------------------------
 *
 * One POST carries the opening confirmation, the closing reading and the testing quantity,
 * because they are recorded together on paper and splitting them across two requests would
 * make a half-entered nozzle a state the register does not have.
 *
 * A fresh nozzle needs one of three choices made before that POST -- matches, mismatch or no
 * sale -- and `entrySheet` opens a single sheet for all three rather than a chooser sheet that
 * hands off to a second one. `openSheet` (ui/sheet.js) closes whatever's already open THE
 * INSTANT a new one opens, with no slide-out; fine when the sheet being replaced is unrelated,
 * wrong here, where the choice and the form are two steps of one action and a jump-cut between
 * them would read as broken rather than as a transition. `buildEntryForm` therefore takes the
 * already-open sheet and swaps its body in place via `sheet.setBody`.
 */

function entrySheet(line, context) {
  const sheet = openSheet({
    title: line.nozzle_label,
    body: chooserBody(line, {
      onMatches: () => buildEntryForm(sheet, line, context, { matches: true }),
      onMismatch: () => buildEntryForm(sheet, line, context, { matches: false }),
      onNoSale: () => buildEntryForm(sheet, line, context, { matches: true, noSale: true }),
    }),
  });
}

function chooserBody(line, { onMatches, onMismatch, onNoSale }) {
  return el("div", { className: "stack" }, [
    el("div", { className: "col" }, [
      el("span", { className: "t-micro", text: "Chain says this nozzle opens at" }),
      // Large and NOT an input. §4.7: "pre-filled, not typeable". Rendering it as a field
      // with a value in it invites a glance-and-tab-past, which is the assumption this whole
      // section exists to prevent.
      el("div", { className: "t-amount", text: formatReading(line.chained_opening_reading) }),
    ]),
    el("p", {
      className: "t-caption",
      text: "Read the physical meter before you touch this. If it does not match, say so — that is the signal, not a nuisance.",
    }),
    el("div", { className: "stack" }, [
      el("button", {
        className: "confirm",
        attrs: { type: "button", "data-state": "unconfirmed" },
        text: "✓  The meter reads exactly this",
        on: { click: onMatches },
      }),
      el("button", {
        className: "confirm",
        attrs: { type: "button", "data-state": "unconfirmed" },
        text: "✕  The meter reads something else",
        on: { click: onMismatch },
      }),
      el("button", {
        className: "btn btn-block",
        text: "No sale — nozzle stayed dry",
        attrs: { type: "button" },
        on: { click: onNoSale },
      }),
    ]),
  ]);
}

function anchorSheet(line, context) {
  const isAdmin = satisfies(context.session.me.role, "admin");
  const sheet = openSheet({
    title: line.nozzle_label,
    body: anchorBody(isAdmin, () => buildEntryForm(sheet, line, context, { matches: false, anchor: true })),
  });
}

function anchorBody(isAdmin, onAnchor) {
  return el("div", { className: "stack" }, [
    pill("needs anchoring", "review"),
    el("p", {
      className: "t-caption",
      text:
        "This nozzle has no previous reading, so there is nothing to carry forward. Its first " +
        "reading anchors the chain and is recorded as the starting point.",
    }),
    isAdmin
      ? el("button", {
          className: "btn btn-primary btn-block",
          text: "Anchor this nozzle",
          attrs: { type: "button" },
          on: { click: onAnchor },
        })
      : // A dead control with no explanation is the thing §16's wayfinding rule forbids. The
        // reason is shown instead of the button, so the attendant knows who to ask.
        el("p", {
          className: "t-caption",
          text: "Only an admin can set a starting reading. Ask an admin to anchor it before this shift is closed.",
        }),
  ]);
}

/** Builds the confirm/mismatch/anchor form into an already-open sheet and wires its submit.
 * Called by entrySheet (matches/mismatch/no-sale) and anchorSheet (anchor) -- one function, so
 * the POST body and validation logic exist in exactly one place regardless of which of the
 * three states a nozzle is in. */
function buildEntryForm(sheet, line, context, { matches, anchor = false, noSale = false }) {
  const unit = line.unit_of_measure === "kilogram" ? "kg" : "litres";

  const fields = {};

  if (!matches) {
    fields.opening_reading = field({
      name: "opening_reading",
      label: anchor ? "Starting reading on the meter" : "What the meter actually reads",
      type: "number",
      step: "0.01",
      min: "0",
      required: true,
      inputMode: "decimal",
      hint: anchor
        ? "This becomes the anchor for every future shift on this nozzle."
        : `The chain predicted ${formatReading(line.chained_opening_reading)}. Enter the real figure.`,
    });

    if (!anchor) {
      fields.opening_variance_reason = field({
        name: "opening_variance_reason",
        label: "Why does it differ?",
        required: true,
        hint: "Raises this row for review before anybody is blamed. Up to 500 characters.",
      });
    }
  }

  // "No sale" pre-fills a guess -- closing = opening, testing = 0 -- but this sheet submits
  // via form.values() below, which sends every field regardless of whether it was touched.
  // The attendant still sees both figures and still has to tap "Save reading": this is a
  // convenience that fills in a guess a human can see, edit, and reject, never a default the
  // system asserts on its own. Same distinction the chained opening reading itself relies on.
  fields.closing_reading = field({
    name: "closing_reading",
    label: "Closing reading",
    type: "number",
    step: "0.01",
    min: "0",
    inputMode: "decimal",
    value: noSale ? String(line.chained_opening_reading) : "",
    hint: "Leave blank if the shift is still running.",
  });

  // §4.2 / §14: an answer, never an omission. Deliberately NOT pre-filled with 0, except by
  // "No sale" above -- which is a human's explicit tap, not a form default.
  fields.testing_quantity = field({
    name: "testing_quantity",
    label: `Testing quantity (${unit})`,
    type: "number",
    step: "0.001",
    min: "0",
    inputMode: "decimal",
    value: noSale ? "0" : "",
    hint:
      line.unit_of_measure === "kilogram"
        ? "CBG is not calibration-tested here, so this is normally 0 — but enter it rather than leaving it blank."
        : "The 5-litre standard measure poured back into the tank. It was dispensed but never sold.",
  });

  const form = new Form(fields);

  const rollover = el("input", {
    attrs: { type: "checkbox", id: "f-rollover", name: "rollover_occurred" },
    className: "field-checkbox",
  });
  const meterReset = el("input", {
    attrs: { type: "checkbox", id: "f-meter-reset", name: "meter_reset_occurred" },
    className: "field-checkbox",
  });

  const submit = el("button", {
    className: "btn btn-primary btn-block",
    text: "Save reading",
    attrs: { type: "button" },
  });

  submit.addEventListener("click", async () => {
    form.clearErrors();
    const values = form.values();

    // testing_quantity must be answered, and this check is the client half of §4.2. The
    // server defaults it to 0, so an omission here would be silently accepted -- which is
    // exactly the "field looking correctly filled in" failure §14 warns about.
    if (values.testing_quantity === "") {
      form.showErrors([
        {
          loc: ["body", "testing_quantity"],
          msg: "Enter a figure. Zero is a valid answer; blank is not.",
        },
      ]);
      return;
    }

    const body = {
      nozzle_id: line.nozzle_id,
      // The one field §4.7 will not let be a default. It is true here because the user
      // pressed a button that says so, not because a form initialised it.
      opening_confirmed: true,
      testing_quantity: values.testing_quantity,
    };
    if (values.opening_reading) body.opening_reading = values.opening_reading;
    if (values.opening_variance_reason) {
      body.opening_variance_reason = values.opening_variance_reason;
    }
    if (values.closing_reading) body.closing_reading = values.closing_reading;
    if (rollover.checked) body.rollover_occurred = true;
    if (meterReset.checked) body.meter_reset_occurred = true;

    submit.disabled = true;
    try {
      // No Idempotency-Key: UNIQUE (shift_id, nozzle_id) makes a retry a 409
      // READING_ALREADY_EXISTS rather than a second row, which is why §6.10 built the store
      // in Phase 6 and not Phase 5.
      await api.post(`/shifts/${context.shiftId}/readings`, body);
      sheet.close();
      notify.success(`${line.nozzle_label} recorded.`);
      renderReadings(context.container, context, { silent: true });
    } catch (error) {
      submit.disabled = false;
      const unmatched = error.isValidation ? form.showErrors(error.detail) : [];
      if (!error.isValidation || unmatched.length) {
        notify.error(explain(error), { requestId: error.requestId });
      }
    }
  });

  sheet.setBody(
    el("div", { className: "stack" }, [
      matches && !anchor
        ? el("div", { className: "card" }, [
            el("p", { className: "t-caption", text: "Opening confirmed against the meter" }),
            el("p", { className: "t-title t-numeric", text: formatReading(line.chained_opening_reading) }),
          ])
        : null,
      ...form.nodes(),
      el("label", { className: "field row", attrs: { for: "f-rollover" } }, [
        rollover,
        el("span", { className: "grow" }, [
          el("span", { className: "t-body", text: "The meter rolled over" }),
          el("p", {
            className: "t-caption",
            text: `Past its maximum of ${formatReading(line.totalizer_max_value)} and back to zero.`,
          }),
        ]),
      ]),
      el("label", { className: "field row", attrs: { for: "f-meter-reset" } }, [
        meterReset,
        el("span", { className: "grow" }, [
          el("span", { className: "t-body", text: "The meter was repaired or replaced" }),
          el("p", {
            className: "t-caption",
            text: "The reading pair then means nothing, and an admin has to enter the quantity manually.",
          }),
        ]),
      ]),
    ]),
    el("div", { style: { padding: "0 1rem 1rem" } }, [submit]),
  );
}

/* --- closing an already-open reading ------------------------------------------ */

function closingSheet(line, saved, context) {
  const unit = line.unit_of_measure === "kilogram" ? "kg" : "litres";

  const closing = field({
    name: "closing_reading",
    label: "Closing reading",
    type: "number",
    step: "0.01",
    min: "0",
    value: saved.closing_reading ?? "",
    required: true,
    inputMode: "decimal",
    hint: `Opening was ${formatReading(saved.opening_reading)}.`,
  });

  const testing = field({
    name: "testing_quantity",
    label: `Testing quantity (${unit})`,
    type: "number",
    step: "0.001",
    min: "0",
    value: saved.testing_quantity ?? "",
    inputMode: "decimal",
  });

  const form = new Form({ closing_reading: closing, testing_quantity: testing });

  const submit = el("button", {
    className: "btn btn-primary btn-block",
    text: "Save",
    attrs: { type: "button" },
  });

  submit.addEventListener("click", async () => {
    form.clearErrors();
    submit.disabled = true;
    try {
      // Only what changed. ReadingUpdate has no nozzle_id and no opening_reading -- §4.7:
      // "a wrong opening is a wrong chain, fixed at its source", never edited here.
      const body = form.changes();
      if (Object.keys(body).length === 0) {
        sheet.close();
        return;
      }
      await api.patch(`/shifts/${context.shiftId}/readings/${line.nozzle_id}`, body);
      sheet.close();
      notify.success("Reading updated.");
      renderReadings(context.container, context, { silent: true });
    } catch (error) {
      submit.disabled = false;
      const unmatched = error.isValidation ? form.showErrors(error.detail) : [];
      if (!error.isValidation || unmatched.length) {
        notify.error(explain(error), { requestId: error.requestId });
      }
    }
  });

  // Only offered before a closing exists. Once one is recorded, changing it to "no sale" is a
  // correction to real data, not a first entry -- that goes through the ordinary fields above
  // like any other edit, not this shortcut.
  const noSaleBtn =
    saved.closing_reading === null
      ? el("button", {
          className: "btn btn-block",
          text: "No sale — same as opening",
          attrs: { type: "button" },
          on: {
            // Sets the already-rendered inputs directly, after Form snapshotted its `initial`
            // values at construction above. form.changes() -- unmodified -- compares each
            // field's LIVE value against that snapshot, so both fields now read as touched and
            // are included in the PATCH, exactly as if the attendant had typed them. This is
            // not a bypass of §4.7/§4.2: nothing is sent until Save is tapped, and the two
            // figures are visible and editable right up to that tap.
            click: () => {
              closing._input.value = String(saved.opening_reading);
              testing._input.value = "0";
            },
          },
        })
      : null;

  const isAdmin = satisfies(context.session.me.role, "admin");
  // §6.2's meter-reset escape hatch: admin only, reason mandatory, audit-logged. Opens its own
  // sheet (overrideSheet) rather than swapping this one's body -- rare and admin-only, unlike
  // the match/mismatch/no-sale choice every attendant makes on every nozzle.
  const overrideBtn =
    context.editable && saved.meter_reset_occurred && isAdmin
      ? el("button", {
          className: "btn btn-block",
          text: "Set manual quantity",
          attrs: { type: "button" },
          on: { click: () => overrideSheet(line, context) },
        })
      : null;

  const sheet = openSheet({
    title: line.nozzle_label,
    body: el("div", { className: "stack" }, [
      flaggedNote(saved),
      savedRows(line, saved),
      noSaleBtn,
      ...form.nodes(),
      overrideBtn,
    ]),
    footer: el("div", { style: { padding: "0 1rem 1rem" } }, [submit]),
  });
}

/* --- §6.2's meter-reset override ---------------------------------------------- */

function overrideSheet(line, context) {
  const unit = line.unit_of_measure === "kilogram" ? "kg" : "litres";

  const amount = field({
    name: "manual_quantity_override",
    label: `Quantity actually sold (${unit})`,
    type: "number",
    step: "0.001",
    min: "0",
    required: true,
    inputMode: "decimal",
  });
  const reason = field({
    name: "override_reason",
    label: "Why is this being entered by hand?",
    required: true,
    hint: "Mandatory in the database, so this figure can never be unexplained. 3–500 characters.",
  });

  const form = new Form({ manual_quantity_override: amount, override_reason: reason });

  const submit = el("button", {
    className: "btn btn-primary btn-block",
    text: "Save override",
    attrs: { type: "button" },
  });

  submit.addEventListener("click", async () => {
    form.clearErrors();
    submit.disabled = true;
    try {
      await api.post(
        `/shifts/${context.shiftId}/readings/${line.nozzle_id}/override`,
        form.values(),
      );
      sheet.close();
      notify.success("Override recorded.");
      renderReadings(context.container, context, { silent: true });
    } catch (error) {
      submit.disabled = false;
      const unmatched = error.isValidation ? form.showErrors(error.detail) : [];
      if (!error.isValidation || unmatched.length) {
        notify.error(explain(error), { requestId: error.requestId });
      }
    }
  });

  const sheet = openSheet({
    title: "Manual quantity",
    body: el("div", { className: "stack" }, [
      el("p", {
        className: "t-caption",
        text:
          "The meter was reset, so the opening and closing pair has no meaning. §6.2 does not " +
          "try to infer the split — enter what was actually sold.",
      }),
      ...form.nodes(),
    ]),
    footer: el("div", { style: { padding: "0 1rem 1rem" } }, [submit]),
  });
}
