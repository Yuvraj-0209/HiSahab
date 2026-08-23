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
 */

import { el, empty, pill, render } from "../dom.js";
import { api, explain } from "../api.js";
import { quantity, reading as formatReading } from "../money.js";
import { field, Form } from "../ui/field.js";
import { openSheet } from "../ui/sheet.js";
import { notify } from "../ui/toast.js";
import { satisfies } from "../ui/nav.js";
import { errorCard } from "./today.js";

export async function renderReadings(container, { session, navigate, shiftId }) {
  const { shell } = session;
  shell.setTab("entry");
  shell.setTitle("Readings");
  shell.setActions();

  render(container, el("div", { className: "t-caption", text: "Loading…" }));

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

      worksheet.lines.length
        ? el(
            "div",
            { className: "stack" },
            worksheet.lines.map((line) =>
              lineCard(line, { session, container, navigate, shiftId, editable }),
            ),
          )
        : empty("No active nozzles at this outlet."),
    ]),
  );
}

/* --- one nozzle --------------------------------------------------------------- */

function lineCard(line, context) {
  const saved = line.reading;
  const done = saved !== null;
  const flagged = done && saved.requires_review;

  return el(
    "div",
    { className: `card stack ${flagged ? "flagged" : ""}` },
    [
      el("div", { className: "row-between" }, [
        el("div", {}, [
          el("div", { className: "t-headline", text: line.nozzle_label }),
          el("div", {
            className: "t-caption",
            text: `${line.dispenser_label} · ${line.fuel_type_code}`,
          }),
        ]),
        done
          ? pill(
              saved.closing_reading === null ? "opening only" : "recorded",
              saved.closing_reading === null ? "neutral" : "open",
            )
          : pill("not started", "neutral"),
      ]),

      flagged
        ? el("p", {
            className: "t-caption text-short",
            text: saved.review_note
              ? `Flagged for review: ${saved.review_note}`
              : "Flagged for review — a human needs to reconcile this reading.",
          })
        : null,

      done ? savedBody(line, saved, context) : unsavedBody(line, context),
    ],
  );
}

function savedBody(line, saved, context) {
  const rows = el("div", { className: "list" }, [
    labelled("Opening", formatReading(saved.opening_reading)),
    // The chain's prediction is shown beside the confirmed value whenever they differ. §5.2:
    // storing both is what makes "the meter did not say what we expected" a fact on the row
    // rather than an event nobody recorded -- so it is shown, not hidden once resolved.
    saved.chained_opening_reading !== null &&
    saved.chained_opening_reading !== saved.opening_reading
      ? labelled("Chain predicted", formatReading(saved.chained_opening_reading), "text-short")
      : null,
    saved.opening_variance_reason
      ? labelled("Variance reason", saved.opening_variance_reason)
      : null,
    saved.chained_opening_reading === null
      ? labelled("Chain", "anchored — no predecessor")
      : null,
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

  const actions = [];
  if (context.editable) {
    actions.push(
      el("button", {
        className: "btn btn-block",
        text: saved.closing_reading === null ? "Enter closing reading" : "Edit closing reading",
        attrs: { type: "button" },
        on: { click: () => closingSheet(line, saved, context) },
      }),
    );
  }
  // §6.2's meter-reset escape hatch: admin only, reason mandatory, audit-logged.
  if (context.editable && saved.meter_reset_occurred && satisfies(context.session.me.role, "admin")) {
    actions.push(
      el("button", {
        className: "btn btn-block",
        text: "Set manual quantity",
        attrs: { type: "button" },
        on: { click: () => overrideSheet(line, context) },
      }),
    );
  }

  return el("div", { className: "stack" }, [rows, ...actions]);
}

function unsavedBody(line, context) {
  if (!context.editable) {
    return el("p", { className: "t-caption", text: "No reading was recorded for this nozzle." });
  }

  if (line.requires_anchor) {
    return anchorBody(line, context);
  }

  return el("div", { className: "stack" }, [
    el("div", { className: "col" }, [
      el("span", { className: "t-micro", text: "Chain says this nozzle opens at" }),
      // Large and NOT an input. §4.7: "pre-filled, not typeable". Rendering it as a field
      // with a value in it invites a glance-and-tab-past, which is the assumption this whole
      // section exists to prevent.
      el("div", {
        className: "t-amount",
        text: formatReading(line.chained_opening_reading),
      }),
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
        on: { click: () => confirmSheet(line, context, { matches: true }) },
      }),
      el("button", {
        className: "confirm",
        attrs: { type: "button", "data-state": "unconfirmed" },
        text: "✕  The meter reads something else",
        on: { click: () => confirmSheet(line, context, { matches: false }) },
      }),
    ]),
  ]);
}

function anchorBody(line, context) {
  const isAdmin = satisfies(context.session.me.role, "admin");

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
          on: { click: () => confirmSheet(line, context, { matches: false, anchor: true }) },
        })
      : // A dead control with no explanation is the thing §16's wayfinding rule forbids. The
        // reason is shown instead of the button, so the attendant knows who to ask.
        el("p", {
          className: "t-caption",
          text: "Only an admin can set a starting reading. Ask an admin to anchor it before this shift is closed.",
        }),
  ]);
}

function labelled(label, value, valueClass = "") {
  return el("div", { className: "list-row" }, [
    el("div", { className: "list-row-main t-caption", text: label }),
    el("div", { className: `list-row-value t-body t-numeric ${valueClass}`, text: value }),
  ]);
}

/* --- creating a reading -------------------------------------------------------
 *
 * One POST carries the opening confirmation, the closing reading and the testing quantity,
 * because they are recorded together on paper and splitting them across two requests would
 * make a half-entered nozzle a state the register does not have.
 */

function confirmSheet(line, context, { matches, anchor = false }) {
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

  fields.closing_reading = field({
    name: "closing_reading",
    label: "Closing reading",
    type: "number",
    step: "0.01",
    min: "0",
    inputMode: "decimal",
    hint: "Leave blank if the shift is still running.",
  });

  // §4.2 / §14: an answer, never an omission. Deliberately NOT pre-filled with 0.
  fields.testing_quantity = field({
    name: "testing_quantity",
    label: `Testing quantity (${unit})`,
    type: "number",
    step: "0.001",
    min: "0",
    inputMode: "decimal",
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
      renderReadings(context.container, context);
    } catch (error) {
      submit.disabled = false;
      const unmatched = error.isValidation ? form.showErrors(error.detail) : [];
      if (!error.isValidation || unmatched.length) {
        notify.error(explain(error), { requestId: error.requestId });
      }
    }
  });

  const sheet = openSheet({
    title: line.nozzle_label,
    body: el("div", { className: "stack" }, [
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
    footer: el("div", { style: { padding: "0 1rem 1rem" } }, [submit]),
  });
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
      renderReadings(context.container, context);
    } catch (error) {
      submit.disabled = false;
      const unmatched = error.isValidation ? form.showErrors(error.detail) : [];
      if (!error.isValidation || unmatched.length) {
        notify.error(explain(error), { requestId: error.requestId });
      }
    }
  });

  const sheet = openSheet({
    title: line.nozzle_label,
    body: el("div", { className: "stack" }, form.nodes()),
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
      renderReadings(context.container, context);
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
