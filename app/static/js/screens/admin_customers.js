/* Credit customers: who may take udhaar, and for how much (CLAUDE.md §5.1, §6.6).
 *
 * ## credit_limit: null means NO limit, and never zero
 *
 * §14 names coercing it as a failure mode: "Null means *no limit*; coercing it refuses every
 * sale to the customers who are trusted most." So the field is left genuinely empty rather
 * than defaulted, and an empty limit renders as the words "no limit" rather than as ₹0.00.
 *
 * This is also one of only two routers where an explicit `null` in a PATCH **clears** a
 * column instead of being ignored -- `credit_limit` and `vehicle_numbers`. `Form.changes()`
 * sends only edited fields, so clearing is something the form has to ask for on purpose;
 * see ui/field.js.
 *
 * ## Deactivation is asymmetric, deliberately
 *
 * §5.1: a deactivated customer refuses a new **credit sale** but still **accepts a
 * repayment**. "You deactivate somebody precisely to stop the debt growing while they pay off
 * what they owe; refusing their money would be backwards, and would leave a balance nothing
 * can ever clear." Said on the toggle, because it reads as a bug otherwise.
 *
 * ## The phone is the natural key, not the name
 *
 * §5.1: names genuinely collide -- "a pump has three customers called Ramesh" -- and two rows
 * for one person split one real balance across two ledgers, at which point §6.6's limit never
 * fires against either. Hence 409 CREDIT_CUSTOMER_PHONE_EXISTS, and hence phone being
 * required.
 */

import { el, empty, pill, render } from "../dom.js";
import { api, explain } from "../api.js";
import { format } from "../money.js";
import { dateTime } from "../time.js";
import { checkbox, field, Form } from "../ui/field.js";
import { openSheet } from "../ui/sheet.js";
import { notify } from "../ui/toast.js";
import { errorCard } from "./today.js";

export async function renderCustomers(container, { session, navigate }) {
  const { shell } = session;
  shell.setTab("admin");
  shell.setTitle("Credit customers");

  render(container, el("div", { className: "t-caption", text: "Loading…" }));

  let outstanding;
  try {
    // The manager-floor report, which carries phone, limit and balance -- unlike the lean
    // list every role can read (§8, §9).
    outstanding = await api.get("/credit-customers/outstanding", { include_settled: true });
  } catch (error) {
    render(container, errorCard(error, () => renderCustomers(container, { session, navigate })));
    return;
  }

  const reload = () => renderCustomers(container, { session, navigate });

  shell.setActions(
    el("button", {
      className: "btn btn-primary",
      text: "Add",
      attrs: { type: "button" },
      on: { click: () => customerSheet(null, reload) },
    }),
  );

  render(
    container,
    el("div", { className: "stack" }, [
      outstanding.length
        ? el("div", { className: "grid" }, outstanding.map((customer) =>
            customerCard(customer, { navigate, reload }),
          ))
        : empty("No credit customers yet."),
    ]),
  );
}

function customerCard(customer, { navigate, reload }) {
  const owes = !customer.outstanding.trim().startsWith("-") && customer.outstanding !== "0.00";

  return el("div", { className: "card stack" }, [
    el("div", { className: "row-between" }, [
      el("div", { className: "grow" }, [
        el("div", { className: "t-headline", text: customer.name }),
        el("div", { className: "t-caption", text: customer.phone }),
      ]),
      customer.is_active ? pill("active", "open") : pill("deactivated", "neutral"),
    ]),

    customer.vehicle_numbers?.length
      ? el("div", { className: "row" }, customer.vehicle_numbers.map((v) => pill(v, "neutral")))
      : null,

    el("div", { className: "list" }, [
      el("div", { className: "list-row" }, [
        el("div", { className: "list-row-main t-body", text: "Outstanding" }),
        el("div", {
          className: `list-row-value t-body t-numeric ${owes ? "text-short" : ""}`,
          // §6.6: outstanding may legitimately be negative -- a customer who paid in advance
          // or rounded up is owed money by the pump. Labelled, so a minus sign is not read
          // as a bug.
          text: customer.outstanding.trim().startsWith("-")
            ? `${format(customer.outstanding)} · in credit`
            : format(customer.outstanding),
        }),
      ]),
      el("div", { className: "list-row" }, [
        el("div", { className: "list-row-main t-body", text: "Credit limit" }),
        customer.credit_limit === null
          ? el("div", { className: "list-row-value t-absent", text: "no limit" })
          : el("div", {
              className: "list-row-value t-body t-numeric",
              text: format(customer.credit_limit),
            }),
      ]),
    ]),

    el("div", { className: "row" }, [
      el("button", {
        className: "btn grow",
        text: "Edit",
        attrs: { type: "button" },
        on: { click: () => customerSheet(customer, reload) },
      }),
      el("button", {
        className: "btn",
        text: "Ledger",
        attrs: { type: "button" },
        on: { click: () => navigate(`#/admin/customers/${customer.id}/ledger`) },
      }),
    ]),
  ]);
}

function customerSheet(existing, onDone) {
  const name = field({
    name: "name",
    label: "Name",
    value: existing?.name ?? "",
    required: true,
  });

  const phone = field({
    name: "phone",
    label: "Phone",
    type: "tel",
    value: existing?.phone ?? "",
    required: true,
    inputMode: "tel",
    hint: "The unique key for a customer at this outlet — names collide, phone numbers do not.",
  });

  const vehicles = field({
    name: "vehicle_numbers",
    label: "Vehicle numbers (optional)",
    value: existing?.vehicle_numbers?.join(", ") ?? "",
    hint: "Comma-separated. Upper-cased and de-duplicated automatically.",
  });

  const limit = field({
    name: "credit_limit",
    label: "Credit limit (optional)",
    type: "number",
    step: "0.01",
    min: "0",
    value: existing?.credit_limit ?? "",
    inputMode: "decimal",
    hint: "Leave blank for NO limit. Blank is not zero — a zero limit would refuse every sale.",
  });

  const fields = { name, phone, vehicle_numbers: vehicles, credit_limit: limit };

  if (existing) {
    fields.is_active = checkbox({
      name: "is_active",
      label: "Active",
      checked: existing.is_active,
      hint: "A deactivated customer cannot take new udhaar but can still repay what they owe.",
    });
  }

  const form = new Form(fields);

  const submit = el("button", {
    className: "btn btn-primary btn-block",
    text: existing ? "Save" : "Create",
    attrs: { type: "button" },
  });

  submit.addEventListener("click", async () => {
    form.clearErrors();
    submit.disabled = true;

    const shape = (values) => {
      const body = {};
      if (values.name !== undefined) body.name = values.name;
      if (values.phone !== undefined) body.phone = values.phone;
      if (values.is_active !== undefined) body.is_active = values.is_active;
      if (values.vehicle_numbers !== undefined) {
        const parsed = values.vehicle_numbers
          .split(",")
          .map((entry) => entry.trim())
          .filter(Boolean);
        // Explicit null CLEARS on this router, which is exactly what an emptied field means.
        body.vehicle_numbers = parsed.length ? parsed : null;
      }
      if (values.credit_limit !== undefined) {
        body.credit_limit = values.credit_limit === "" ? null : values.credit_limit;
      }
      return body;
    };

    try {
      if (existing) {
        const changes = form.changes();
        if (!Object.keys(changes).length) {
          sheet.close();
          return;
        }
        await api.patch(`/credit-customers/${existing.id}`, shape(changes));
      } else {
        await api.post("/credit-customers", shape(form.values()));
      }
      sheet.close();
      notify.success("Saved.");
      onDone();
    } catch (error) {
      submit.disabled = false;
      const unmatched = error.isValidation ? form.showErrors(error.detail) : [];
      if (!error.isValidation || unmatched.length) {
        notify.error(explain(error), { requestId: error.requestId });
      }
    }
  });

  const sheet = openSheet({
    title: existing ? existing.name : "New credit customer",
    body: el("div", { className: "stack" }, form.nodes()),
    footer: el("div", { style: { padding: "0 1rem 1rem" } }, [submit]),
  });
}

/* --- the ledger ------------------------------------------------------------------
 *
 * §6.6: outstanding is COMPUTED from sales minus repayments, summing **every** row including
 * reversals -- they carry negative amounts and net out. §14 forbids maintaining a
 * denormalised total anywhere, and Phase 9 deleted `credit_sales.is_settled` for the same
 * reason, so this screen only ever displays what the server computed.
 */

export async function renderCustomerLedger(container, { session, navigate, customerId }) {
  const { shell } = session;
  shell.setTab("admin");
  shell.setTitle("Ledger");

  render(container, el("div", { className: "t-caption", text: "Loading…" }));

  let customer;
  let ledger;
  try {
    [customer, ledger] = await Promise.all([
      api.get(`/credit-customers/${customerId}`),
      api.get(`/credit-customers/${customerId}/ledger`, { limit: 100 }),
    ]);
  } catch (error) {
    render(
      container,
      errorCard(error, () =>
        renderCustomerLedger(container, { session, navigate, customerId }),
      ),
    );
    return;
  }

  shell.setTitle("Ledger", customer.name);

  render(
    container,
    el("div", { className: "stack" }, [
      el("div", { className: "card stack" }, [
        el("div", { className: "t-micro", text: "Outstanding" }),
        el("div", { className: "t-amount", text: format(customer.outstanding) }),
        el("p", {
          className: "t-caption",
          text: "Computed from every sale and repayment, reversals included — never stored, so it cannot drift.",
        }),
      ]),

      ledger.items.length
        ? el("div", { className: "list" }, ledger.items.map((entry) =>
            el("div", { className: "list-row" }, [
              el("div", { className: "list-row-main" }, [
                el("div", {
                  className: "t-body",
                  text: entry.kind === "sale" ? "Udhaar issued" : "Repayment",
                }),
                el("div", { className: "t-caption", text: dateTime(entry.created_at) }),
              ]),
              el("div", { className: "row" }, [
                entry.is_reversal ? pill("reversal", "neutral") : null,
                el("span", {
                  className: `t-body t-numeric ${
                    entry.balance_delta.trim().startsWith("-") ? "text-surplus" : ""
                  }`,
                  text: format(entry.balance_delta, { sign: true }),
                }),
              ]),
            ]),
          ))
        : empty("Nothing on this ledger yet."),
    ]),
  );
}
