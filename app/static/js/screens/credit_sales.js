/* Udhaar issued (CLAUDE.md §5.2, §6.6).
 *
 * ## The receipt is not optional here, and the UI must not pretend otherwise
 *
 * `credit_sales.attachment_id` is `NOT NULL` **at the database level**, and §6.6 calls that
 * belt and braces: "a client can bypass JavaScript, but not a database constraint." §5.2 goes
 * further and refuses to weaken it to a CHECK even to make room for §6.9's reversals -- a
 * reversal inherits the original's attachment instead.
 *
 * So the submit button here is disabled until a receipt has actually uploaded. Not because
 * that enforces anything -- it does not, and §8 is clear that hiding a control is UX -- but
 * because letting somebody fill in a whole form and then meet a 422 is a bad way to learn a
 * rule that was never going to bend.
 *
 * ## The credit limit
 *
 * §6.6: `outstanding + amount > credit_limit` -> 409, strictly `>`, and **`credit_limit IS
 * NULL` means no limit and must never be read as zero** -- coercing it refuses every sale to
 * the customers who are trusted most. This screen never computes that comparison; it shows
 * the server's outstanding figure and lets the server refuse. §13.13 notes the check is not
 * serialised anyway, so a client-side copy would be wrong as well as redundant.
 *
 * An admin may override with a mandatory reason, which lands **on the row** as well as in the
 * audit log -- §6.6: "Nothing reads audit_logs at report time; a column is visible next to
 * the figure it explains." A non-admin supplying one is 403.
 *
 * ## The customer picker uses the lean list, for every role
 *
 * `GET /credit-customers` returns name and vehicles only -- no phone, no limit, no balance --
 * and it returns that projection to admins too. The detailed route is a different endpoint at
 * a manager floor (§8, §9). An admin filling in this form does not need a phone number, so
 * this screen asks for the lean list like everybody else.
 */

import { el, empty, pill, render } from "../dom.js";
import { api, explain, Submission, uploadReceipt } from "../api.js";
import { format, quantity } from "../money.js";
import { field, Form, select } from "../ui/field.js";
import { openSheet } from "../ui/sheet.js";
import { openReversalSheet, reversalBadge } from "../ui/reversal.js";
import { notify } from "../ui/toast.js";
import { satisfies } from "../ui/nav.js";
import { errorCard } from "./today.js";

export async function renderCreditSales(container, { session, navigate, shiftId }) {
  const { shell } = session;
  shell.setTab("entry");
  shell.setTitle("Credit sales");

  render(container, el("div", { className: "t-caption", text: "Loading…" }));

  let page;
  let shift;
  let customers;
  let fuelTypes;
  try {
    [page, shift, customers, fuelTypes] = await Promise.all([
      api.get(`/shifts/${shiftId}/credit-sales`),
      api.get(`/shifts/${shiftId}`),
      api.get("/credit-customers"),
      api.get("/fuel-types"),
    ]);
  } catch (error) {
    render(
      container,
      errorCard(error, () => renderCreditSales(container, { session, navigate, shiftId })),
    );
    return;
  }

  const editable = shift.status === "open";
  const context = { session, container, navigate, shiftId, editable, customers, fuelTypes };
  const byId = new Map(customers.map((customer) => [customer.id, customer]));

  shell.setActions(
    editable
      ? el("button", {
          className: "btn btn-primary",
          text: "Add",
          attrs: { type: "button" },
          on: { click: () => saleSheet(null, context) },
        })
      : null,
  );

  render(
    container,
    el("div", { className: "stack" }, [
      el("div", { className: "card stack" }, [
        el("div", { className: "t-micro", text: "Udhaar issued this shift" }),
        el("div", { className: "t-amount", text: format(page.total) }),
        el("p", {
          className: "t-caption",
          text: "Subtracted from what the salesman should be holding — the fuel left, the cash did not.",
        }),
      ]),

      page.items.length
        ? el(
            "div",
            { className: "stack" },
            page.items.map((sale) => saleCard(sale, byId, context)),
          )
        : empty("No credit sales recorded for this shift."),

      page.truncated
        ? el("div", {
            className: "truncation-notice t-caption",
            text: "This shift has more credit sales than this view lists.",
          })
        : null,
    ]),
  );
}

function saleCard(sale, byId, context) {
  const customer = byId.get(sale.credit_customer_id);
  const live = !sale.reverses_id && !sale.is_reversed;
  const fuel = context.fuelTypes.find((type) => type.id === sale.fuel_type_id);

  return el("div", { className: "card stack" }, [
    el("div", { className: "row-between" }, [
      el("div", { className: "grow" }, [
        el("div", { className: "t-headline", text: customer?.name ?? "Unknown customer" }),
        el("div", {
          className: "t-caption",
          text: [
            fuel?.code,
            // §4.5: the unit comes from the fuel type. Nothing in the credit path assumes
            // litres -- a CBG udhaar is in kilograms.
            sale.quantity !== null && fuel
              ? quantity(sale.quantity, fuel.unit_of_measure)
              : null,
            sale.vehicle_number,
          ]
            .filter(Boolean)
            .join(" · "),
        }),
      ]),
      el("div", { className: "row" }, [
        reversalBadge(sale),
        el("span", { className: "t-title t-numeric", text: format(sale.amount) }),
      ]),
    ]),

    sale.limit_override_reason
      ? el("div", { className: "stack" }, [
          pill("limit overridden", "review"),
          el("p", { className: "t-caption", text: sale.limit_override_reason }),
        ])
      : null,

    sale.reversal_reason
      ? el("p", { className: "t-caption", text: `Reason: ${sale.reversal_reason}` })
      : null,

    el("div", { className: "row" }, [
      context.editable && live
        ? el("button", {
            className: "btn grow",
            text: "Edit",
            attrs: { type: "button" },
            on: { click: () => saleSheet(sale, context) },
          })
        : null,
      live && satisfies(context.session.me.role, "manager")
        ? el("button", {
            className: "btn btn-danger",
            text: "Reverse",
            attrs: { type: "button" },
            on: {
              click: () =>
                openReversalSheet({
                  title: "Reverse credit sale",
                  path: `/shifts/${context.shiftId}/credit-sales/${sale.id}/reversals`,
                  amount: sale.amount,
                  // §6.11 / §5.3: no receipt is asked for. A cancellation is not a spend,
                  // and the reversal inherits the original's attachment.
                  description: `${customer?.name ?? "customer"} — the receipt carries over, no re-upload needed`,
                  onDone: () => renderCreditSales(context.container, context),
                }),
            },
          })
        : null,
    ]),
  ]);
}

/* --- creating ------------------------------------------------------------------ */

function saleSheet(existing, context) {
  const { customers, fuelTypes, session } = context;
  const isAdmin = satisfies(session.me.role, "admin");

  // §5.1: a deactivated customer refuses a NEW sale (409 CREDIT_CUSTOMER_INACTIVE) but still
  // accepts a repayment. So inactive customers are absent here and present on the repayment
  // screen -- the asymmetry is deliberate and worth not "fixing".
  const active = customers.filter((customer) => customer.is_active);

  const customerSelect = select({
    name: "credit_customer_id",
    label: "Customer",
    options: [
      { value: "", label: "Choose…" },
      ...active.map((customer) => ({
        value: customer.id,
        label: customer.vehicle_numbers?.length
          ? `${customer.name} (${customer.vehicle_numbers.join(", ")})`
          : customer.name,
      })),
    ],
    value: existing?.credit_customer_id ?? "",
    required: true,
    hint: "Only active customers can take new udhaar. A deactivated one can still repay.",
  });

  const amount = field({
    name: "amount",
    label: "Amount",
    type: "number",
    step: "0.01",
    min: "0.01",
    value: existing?.amount ?? "",
    required: true,
    inputMode: "decimal",
  });

  const fuelSelect = select({
    name: "fuel_type_id",
    label: "Fuel (optional)",
    options: [
      { value: "", label: "Not a fuel sale" },
      ...fuelTypes.map((type) => ({ value: type.id, label: type.display_name })),
    ],
    value: existing?.fuel_type_id ?? "",
    hint: "Leave empty for a non-fuel credit sale.",
  });

  const quantityField = field({
    name: "quantity",
    label: "Quantity",
    type: "number",
    step: "0.001",
    min: "0.001",
    value: existing?.quantity ?? "",
    inputMode: "decimal",
    // §14 / the API: a quantity with no fuel type is refused, because a measure with no unit
    // is meaningless. Said here so it is not discovered as a 422.
    hint: "Requires a fuel type — a quantity with no unit means nothing.",
  });

  const vehicle = field({
    name: "vehicle_number",
    label: "Vehicle number (optional)",
    value: existing?.vehicle_number ?? "",
  });

  const fields = {
    credit_customer_id: customerSelect,
    amount,
    fuel_type_id: fuelSelect,
    quantity: quantityField,
    vehicle_number: vehicle,
  };

  if (isAdmin && !existing) {
    fields.limit_override_reason = field({
      name: "limit_override_reason",
      label: "Credit limit override reason (admin)",
      hint: "Only fill this in if the sale needs to go past the customer's limit. Stored on the row, not just in the audit log.",
    });
  }

  const form = new Form(fields);

  /* --- the mandatory receipt ---------------------------------------------- */

  let attachmentId = existing?.attachment_id ?? null;

  const fileInput = el("input", {
    attrs: { type: "file", accept: "image/jpeg,image/png", id: "f-credit-receipt" },
    className: "field-input",
  });
  const receiptStatus = el("p", { className: "t-caption text-short" });

  const submit = el("button", {
    className: "btn btn-primary btn-block",
    text: existing ? "Save" : "Record credit sale",
    attrs: { type: "button", disabled: existing ? undefined : true },
  });

  function refreshReceiptState() {
    if (existing) return; // attachment_id is immutable and forbidden on a PATCH
    if (attachmentId) {
      receiptStatus.textContent = "Receipt attached.";
      receiptStatus.className = "t-caption";
      submit.disabled = false;
    } else {
      receiptStatus.textContent = "A receipt is required before this can be saved.";
      receiptStatus.className = "t-caption text-short";
      submit.disabled = true;
    }
  }

  fileInput.addEventListener("change", async () => {
    const file = fileInput.files?.[0];
    if (!file) return;
    receiptStatus.textContent = "Uploading…";
    receiptStatus.className = "t-caption";
    try {
      const result = await uploadReceipt(file, context.shiftId);
      attachmentId = result.attachment_id;
      refreshReceiptState();
    } catch (error) {
      attachmentId = null;
      refreshReceiptState();
      notify.error(explain(error), { requestId: error.requestId });
    }
  });

  const submission = existing
    ? null
    : new Submission("POST", `/shifts/${context.shiftId}/credit-sales`);

  submit.addEventListener("click", async () => {
    form.clearErrors();
    const values = form.values();

    if (values.quantity && !values.fuel_type_id) {
      form.showErrors([
        {
          loc: ["body", "quantity"],
          msg: "A quantity needs a fuel type — a measure with no unit is meaningless.",
        },
      ]);
      return;
    }

    submit.disabled = true;
    try {
      if (existing) {
        // CreditSaleUpdate forbids customer, attachment and fuel type. Note that an explicit
        // null DOES clear quantity and vehicle_number on this router -- one of only two that
        // behave that way -- so changes() sending only edited fields matters here.
        const changes = form.changes();
        delete changes.credit_customer_id;
        delete changes.fuel_type_id;
        delete changes.limit_override_reason;
        if (Object.keys(changes).length === 0) {
          sheet.close();
          return;
        }
        await api.patch(`/shifts/${context.shiftId}/credit-sales/${existing.id}`, changes);
      } else {
        const body = {
          credit_customer_id: values.credit_customer_id,
          amount: values.amount,
          attachment_id: attachmentId,
        };
        if (values.fuel_type_id) body.fuel_type_id = values.fuel_type_id;
        if (values.quantity) body.quantity = values.quantity;
        if (values.vehicle_number) body.vehicle_number = values.vehicle_number;
        if (values.limit_override_reason) {
          body.limit_override_reason = values.limit_override_reason;
        }
        await submission.run(body);
      }
      sheet.close();
      notify.success(existing ? "Updated." : "Credit sale recorded.");
      renderCreditSales(context.container, context);
    } catch (error) {
      submit.disabled = false;
      const unmatched = error.isValidation ? form.showErrors(error.detail) : [];
      if (!error.isValidation || unmatched.length) {
        notify.error(explain(error), {
          requestId: error.requestId,
          detail:
            error.code === "CREDIT_LIMIT_EXCEEDED" && isAdmin
              ? "As an admin you can override this by entering a reason."
              : undefined,
          action:
            error.name === "NetworkError"
              ? { label: "Retry", onClick: () => submit.click() }
              : undefined,
        });
      }
    }
  });

  refreshReceiptState();

  const sheet = openSheet({
    title: existing ? "Edit credit sale" : "Record udhaar",
    body: el("div", { className: "stack" }, [
      ...form.nodes(),
      el("div", { className: "field" }, [
        el("span", { className: "field-label t-caption", text: "Receipt (required)" }),
        existing
          ? el("p", {
              className: "t-caption",
              text: "The receipt cannot be changed. Correct this sale with a reversal instead.",
            })
          : fileInput,
        receiptStatus,
      ]),
    ]),
    footer: el("div", { style: { padding: "0 1rem 1rem" } }, [submit]),
  });
}
