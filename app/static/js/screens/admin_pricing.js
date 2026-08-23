/* Prices and margins: append-only, effective-dated (CLAUDE.md §4.1, §4.6, §5.1).
 *
 * ## Why there is no edit button anywhere on this screen
 *
 * Both tables are **append-only, never updated, never deleted**. §4.1 gives the reason and it
 * is the sharpest sentence in the section: storing "current price" as a mutable column
 * "silently corrupts every historical report the moment the price changes."
 *
 * A price is not an attribute of a fuel. It is a *dated record*. Correcting one means adding
 * another with a later `effective_from`, and the old row stays -- because a shift that closed
 * last Tuesday was priced at what the rate was last Tuesday, and it must still say so.
 *
 * ## Backdating is allowed, and warned about loudly
 *
 * The API permits an `effective_from` in the past and flags the row `is_backdated`. §11 calls
 * this out as the sharpest case for the audit retrofit: "a backdated `effective_from`, which
 * the router permits and merely warns about, can revalue a closed shift with nothing
 * recording who entered it." Phase 11 fixed the recording half. This screen does the other
 * half -- telling the person before they press the button.
 *
 * ## Prices and margins are separate tables, and that is not duplication
 *
 * §5.1: they revise on different schedules. Price moves often; margin almost never. Sharing a
 * row would force re-entry of an unchanged margin at every price revision, "and the first time
 * someone forgets, that period's profit is null or wrong with no error raised."
 *
 * Note what that means for this outlet today: petrol and diesel margins have **never been
 * entered** (§14's open questions), so profit reporting covers CBG alone. Reconciliation is
 * unaffected -- §6.3 splits valuation from profit precisely so a missing margin cannot make a
 * day unreconcilable -- and the empty state below says so rather than looking broken.
 */

import { el, empty, pill, render } from "../dom.js";
import { api, explain } from "../api.js";
import { format } from "../money.js";
import { dateTime } from "../time.js";
import { field, Form, nowLocalValue, select } from "../ui/field.js";
import { openSheet } from "../ui/sheet.js";
import { notify } from "../ui/toast.js";
import { errorCard } from "./today.js";

/** Prices and margins differ only in a handful of nouns, so one renderer serves both.
 *
 * A config object rather than two near-identical files: unlike the reference-data screens in
 * admin.js, these two genuinely have the *same rules* -- append-only, effective-dated,
 * backdating warned about -- so a shared implementation keeps them from drifting apart, which
 * would be worse than the indirection.
 */
const KINDS = {
  price: {
    title: "Prices",
    path: "/fuel-prices",
    amountField: "rate_per_unit",
    amountLabel: "Rate per unit",
    noun: "rate",
    emptyHint:
      "No rates entered. A shift cannot be valued without one — the cash engine refuses rather than valuing a day at zero.",
  },
  margin: {
    title: "Margins",
    path: "/fuel-margins",
    amountField: "margin_per_unit",
    amountLabel: "Dealer margin per unit",
    noun: "margin",
    emptyHint:
      "No margins entered. Reconciliation still works — §6.3 keeps valuation and profit separate — but profit reporting stays blank for any fuel without one.",
  },
};

export async function renderPricing(container, { session, navigate, kind }) {
  const config = KINDS[kind];
  const { shell } = session;
  shell.setTab("admin");
  shell.setTitle(config.title);

  render(container, el("div", { className: "t-caption", text: "Loading…" }));

  let current;
  let history;
  let fuelTypes;
  try {
    [current, history, fuelTypes] = await Promise.all([
      api.get(`${config.path}/current`),
      api.get(config.path, { limit: 50 }),
      api.get("/fuel-types"),
    ]);
  } catch (error) {
    render(
      container,
      errorCard(error, () => renderPricing(container, { session, navigate, kind })),
    );
    return;
  }

  const reload = () => renderPricing(container, { session, navigate, kind });
  const byId = new Map(fuelTypes.map((type) => [type.id, type]));

  shell.setActions(
    el("button", {
      className: "btn btn-primary",
      text: "Add",
      attrs: { type: "button" },
      on: { click: () => entrySheet(config, fuelTypes, reload) },
    }),
  );

  render(
    container,
    el("div", { className: "stack" }, [
      el("div", { className: "section-label t-micro", text: "In force now" }),
      current.length
        ? el("div", { className: "grid" }, current.map((entry) =>
            el("div", { className: "card stack" }, [
              el("div", { className: "t-micro", text: entry.fuel_type_code }),
              el("div", {
                className: "t-amount",
                text: format(entry.rate_per_unit ?? entry.margin_per_unit),
              }),
              el("div", {
                className: "t-caption",
                text: `per ${byId.get(entry.fuel_type_id)?.unit_of_measure === "kilogram" ? "kg" : "litre"}`,
              }),
            ]),
          ))
        : empty(config.emptyHint),

      // Fuels with nothing in force. Stated positively rather than left as an absence --
      // "PETROL has no margin" is the fact somebody needs, and it is invisible in a list that
      // simply does not mention petrol.
      missingCard(config, fuelTypes, current),

      el("div", { className: "section-label t-micro", text: "History" }),
      el("p", {
        className: "t-caption",
        text: "Append-only. Nothing here can be edited or deleted — a correction is a new row with a later effective date, and the old one stays because it is what a closed shift was priced at.",
      }),
      history.items.length
        ? el("div", { className: "list" }, history.items.map((entry) =>
            el("div", { className: "list-row" }, [
              el("div", { className: "list-row-main" }, [
                el("div", {
                  className: "t-body",
                  text: byId.get(entry.fuel_type_id)?.code ?? "unknown fuel",
                }),
                el("div", { className: "t-caption", text: dateTime(entry.effective_from) }),
              ]),
              el("div", { className: "row" }, [
                entry.is_backdated ? pill("backdated", "review") : null,
                el("span", {
                  className: "t-body t-numeric",
                  text: format(entry.rate_per_unit ?? entry.margin_per_unit),
                }),
              ]),
            ]),
          ))
        : empty("Nothing recorded yet."),
    ]),
  );
}

function missingCard(config, fuelTypes, current) {
  const covered = new Set(current.map((entry) => entry.fuel_type_id));
  const missing = fuelTypes.filter((type) => type.is_active && !covered.has(type.id));
  if (!missing.length) return null;

  return el("div", { className: "card stack" }, [
    el("div", { className: "t-micro", text: `No ${config.noun} in force` }),
    el("div", { className: "row" }, missing.map((type) => pill(type.code, "review"))),
    el("p", {
      className: "t-caption",
      text:
        config.noun === "rate"
          ? "Sales for these fuels cannot be valued at all until a rate exists."
          : "These fuels sell and reconcile normally; only their profit figure is missing.",
    }),
  ]);
}

function entrySheet(config, fuelTypes, onDone) {
  const fuelSelect = select({
    name: "fuel_type_id",
    label: "Fuel",
    options: [
      { value: "", label: "Choose…" },
      ...fuelTypes.filter((type) => type.is_active).map((type) => ({
        value: type.id,
        label: type.display_name,
      })),
    ],
    required: true,
  });

  const amount = field({
    name: config.amountField,
    label: config.amountLabel,
    type: "number",
    step: "0.01",
    min: "0.01",
    required: true,
    inputMode: "decimal",
  });

  const effectiveFrom = field({
    name: "effective_from",
    label: "Effective from",
    type: "datetime-local",
    value: nowLocalValue(),
    required: true,
    // §4.1: OMCs publish revised rates at 06:00 IST. Said here because getting the instant
    // right is what makes §6.3 value the right shift at the right rate.
    hint: "Rates revise at 06:00 IST. This is the moment it became live, not the moment you are typing it.",
  });

  const form = new Form({
    fuel_type_id: fuelSelect,
    [config.amountField]: amount,
    effective_from: effectiveFrom,
  });

  const warning = el("p", { className: "t-caption" });

  function refreshWarning() {
    const chosen = effectiveFrom._input.value;
    if (!chosen) {
      warning.textContent = "";
      return;
    }
    const backdated = new Date(chosen).getTime() < Date.now();
    warning.textContent = backdated
      ? "This is in the past. A backdated entry can change what an already-closed shift was worth. It is allowed, it is recorded against your name in the audit trail, and it is worth being sure about."
      : "";
    warning.className = `t-caption ${backdated ? "text-short" : ""}`;
  }
  effectiveFrom._input.addEventListener("input", refreshWarning);

  const submit = el("button", {
    className: "btn btn-primary btn-block",
    text: `Record ${config.noun}`,
    attrs: { type: "button" },
  });

  submit.addEventListener("click", async () => {
    form.clearErrors();
    const values = form.values();
    if (!values.fuel_type_id) {
      form.showErrors([{ loc: ["body", "fuel_type_id"], msg: "Choose a fuel." }]);
      return;
    }

    submit.disabled = true;
    try {
      await api.post(config.path, {
        fuel_type_id: values.fuel_type_id,
        [config.amountField]: values[config.amountField],
        // A datetime-local value is naive and every timestamp this API accepts needs an
        // offset -- without this the very first price entry 422s on NAIVE_TIMESTAMP.
        effective_from: new Date(values.effective_from).toISOString(),
      });
      sheet.close();
      notify.success(`${config.noun === "rate" ? "Rate" : "Margin"} recorded.`);
      onDone();
    } catch (error) {
      submit.disabled = false;
      const unmatched = error.isValidation ? form.showErrors(error.detail) : [];
      if (!error.isValidation || unmatched.length) {
        notify.error(explain(error), { requestId: error.requestId });
      }
    }
  });

  refreshWarning();

  const sheet = openSheet({
    title: config.noun === "rate" ? "New rate" : "New margin",
    body: el("div", { className: "stack" }, [...form.nodes(), warning]),
    footer: el("div", { style: { padding: "0 1rem 1rem" } }, [submit]),
  });
}
