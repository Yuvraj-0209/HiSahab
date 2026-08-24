/* Admin: the reference data every money rule reads from (CLAUDE.md §5.1, §8, §11).
 *
 * §11's Phase 11 note is the argument for why these screens matter more than "settings"
 * suggests: three of these tables **gate money rules directly**.
 *
 *   expense_categories.requires_receipt   the knob §6.11 exists to give the admin
 *   credit_customers.credit_limit         decides what §6.6 refuses, so raising it is how
 *                                         an over-limit sale becomes a legal one
 *   outlet_shift_templates.starts_at_local  supplies the instant §6.3 prices a whole shift from
 *
 * Every write here is audit-logged server-side (Phase 11's retrofit), and that is why this
 * screen can be honest about consequences instead of hedging: an admin changing a limit is
 * making a decision with their name on it.
 *
 * ## Two rules this whole section is built around
 *
 * **Nothing is deleted.** §3 rule 6 forbids hard deletes on financial tables, and every
 * reference table retires a row with `is_active` instead. So there is no delete button
 * anywhere below -- only Deactivate, which refuses *new* rows while historical ones keep
 * reading and reporting.
 *
 * **Codes are immutable.** `fuel_types.code`, `expense_categories.code` and
 * `fuel_types.unit_of_measure` cannot change after creation, and the API refuses them with a
 * 422 rather than silently ignoring them. §5.1 gives the reason: changing a unit would
 * reinterpret every quantity ever recorded against that fuel, and changing a code relabels
 * every expense ever filed under it. The forms show these as read-only once a row exists,
 * rather than offering a field that would be rejected.
 */

import { el, empty, pill, render } from "../dom.js";
import { api, explain } from "../api.js";
import { format, quantity } from "../money.js";
import { localTime } from "../time.js";
import { checkbox, field, Form, select } from "../ui/field.js";
import { openSheet } from "../ui/sheet.js";
import { notify } from "../ui/toast.js";
import { errorCard } from "./today.js";

/* --- the hub ------------------------------------------------------------------ */

const SECTIONS = [
  { label: "Fuel types", hint: "Products sold, and the unit each is measured in", route: "#/admin/fuel-types" },
  { label: "Nozzles", hint: "Meters, their fuel, and the rollover ceiling", route: "#/admin/nozzles" },
  { label: "Prices", hint: "Effective-dated rates — append-only", route: "#/admin/prices" },
  { label: "Margins", hint: "Dealer commission per unit — append-only", route: "#/admin/margins" },
  { label: "Expense categories", hint: "What an expense can be filed under, and which need a receipt", route: "#/admin/categories" },
  { label: "Credit customers", hint: "Who may take udhaar, and their limit", route: "#/admin/customers" },
  { label: "Shift templates", hint: "The hours this outlet usually trades", route: "#/admin/shift-templates" },
  { label: "Users", hint: "Who may sign in, and what they may do", route: "#/admin/users" },
  { label: "Audit log", hint: "Who changed what, and what it was before", route: "#/admin/audit" },
];

export function renderAdmin(container, { session, navigate }) {
  const { shell } = session;
  shell.setTab("admin");
  shell.setTitle("Admin");
  shell.setActions();

  render(
    container,
    el("div", { className: "stack" }, [
      el(
        "div",
        { className: "list" },
        SECTIONS.map((section) =>
          el(
            "button",
            {
              className: "list-row",
              attrs: { type: "button" },
              on: { click: () => navigate(section.route) },
              style: {
                width: "100%", background: "none", border: 0, textAlign: "left",
                font: "inherit", color: "inherit", cursor: "pointer",
              },
            },
            [
              el("div", { className: "list-row-main" }, [
                el("div", { className: "t-body", text: section.label }),
                el("div", { className: "t-caption", text: section.hint }),
              ]),
              el("div", { className: "t-body", text: "›", attrs: { "aria-hidden": "true" } }),
            ],
          ),
        ),
      ),
      el("p", {
        className: "t-caption",
        text: "Nothing here can be deleted. A row that is no longer used is deactivated: it refuses new entries while everything historical keeps reading and reporting.",
      }),
    ]),
  );
}

/* --- shared plumbing ----------------------------------------------------------
 *
 * One loader shape for every list screen. Deliberately a small helper rather than a generic
 * CRUD abstraction: each screen below still writes its own fields and its own rules, because
 * the rules are what differ and hiding them behind a config object is how a receipt flag
 * ends up looking like an ordinary boolean.
 */

export async function loadInto(container, { title, session, navigate, fetch, build, action }) {
  const { shell } = session;
  shell.setTab("admin");
  shell.setTitle(title);
  shell.setActions(action ?? null);

  render(container, el("div", { className: "t-caption", text: "Loading…" }));

  try {
    const data = await fetch();
    render(container, build(data));
  } catch (error) {
    render(
      container,
      errorCard(error, () =>
        loadInto(container, { title, session, navigate, fetch, build, action }),
      ),
    );
  }
}

export function activePill(row) {
  return row.is_active ? pill("active", "open") : pill("inactive", "neutral");
}

/** The submit handler every sheet below shares. */
export function wireSubmit(submit, form, { run, onDone, sheet }) {
  submit.addEventListener("click", async () => {
    form.clearErrors();
    submit.disabled = true;
    try {
      await run(form);
      sheet().close();
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
}

export function sheetFooter(submit) {
  return el("div", { style: { padding: "0 1rem 1rem" } }, [submit]);
}

/* --- fuel types ---------------------------------------------------------------- */

export function renderFuelTypes(container, context) {
  const reload = () => renderFuelTypes(container, context);

  return loadInto(container, {
    title: "Fuel types",
    session: context.session,
    navigate: context.navigate,
    fetch: () => api.get("/fuel-types", { include_inactive: true }),
    action: el("button", {
      className: "btn btn-primary",
      text: "Add",
      attrs: { type: "button" },
      on: { click: () => fuelTypeSheet(null, reload) },
    }),
    build: (types) =>
      el("div", { className: "stack" }, [
        el("p", {
          className: "t-caption",
          text: "A fuel's code and unit can never change — a fuel that is genuinely different is a new row. Everything else is editable.",
        }),
        types.length
          ? el("div", { className: "grid" }, types.map((type) =>
              el("div", { className: "card stack" }, [
                el("div", { className: "row-between" }, [
                  el("div", {}, [
                    el("div", { className: "t-headline", text: type.display_name }),
                    el("div", { className: "t-caption", text: type.code }),
                  ]),
                  activePill(type),
                ]),
                el("div", { className: "list" }, [
                  el("div", { className: "list-row" }, [
                    el("div", { className: "list-row-main t-body", text: "Measured in" }),
                    el("div", {
                      className: "list-row-value t-body",
                      text: type.unit_of_measure === "kilogram" ? "kilograms" : "litres",
                    }),
                  ]),
                  el("div", { className: "list-row" }, [
                    el("div", { className: "list-row-main t-body", text: "Max flow / minute" }),
                    el("div", {
                      className: "list-row-value t-body t-numeric",
                      text: quantity(type.max_flow_rate_per_minute, type.unit_of_measure),
                    }),
                  ]),
                ]),
                el("button", {
                  className: "btn btn-block",
                  text: "Edit",
                  attrs: { type: "button" },
                  on: { click: () => fuelTypeSheet(type, reload) },
                }),
              ]),
            ))
          : empty("No fuel types."),
      ]),
  });
}

function fuelTypeSheet(existing, onDone) {
  const fields = {};

  if (!existing) {
    fields.code = field({
      name: "code",
      label: "Code",
      required: true,
      hint: "Upper-cased automatically. PERMANENT — it cannot be changed later.",
    });
    fields.unit_of_measure = select({
      name: "unit_of_measure",
      label: "Measured in",
      options: [
        { value: "", label: "Choose…" },
        { value: "litre", label: "Litres" },
        { value: "kilogram", label: "Kilograms (CBG)" },
      ],
      required: true,
      hint: "PERMANENT. Changing it later would reinterpret every quantity ever recorded against this fuel.",
    });
  }

  fields.display_name = field({
    name: "display_name",
    label: "Display name",
    value: existing?.display_name ?? "",
    required: true,
  });

  fields.max_flow_rate_per_minute = field({
    name: "max_flow_rate_per_minute",
    label: "Maximum flow rate per minute",
    type: "number",
    step: "0.001",
    min: "0.001",
    value: existing?.max_flow_rate_per_minute ?? "",
    required: true,
    inputMode: "decimal",
    // §6.2 reads this as the sanity ceiling that refuses a mistyped reading, and §14 records
    // that the seeded CBG figure is a guess nobody has confirmed. Worth saying where it is set.
    hint: "§6.2's sanity ceiling: a reading implying more than this per minute is refused. Too high and it never fires; too low and it refuses a busy day.",
  });

  if (existing) {
    fields.is_active = checkbox({
      name: "is_active",
      label: "Active",
      checked: existing.is_active,
      hint: "Deactivating refuses new nozzles and sales; history keeps reading.",
    });
  }

  const form = new Form(fields);
  const submit = el("button", {
    className: "btn btn-primary btn-block",
    text: existing ? "Save" : "Create",
    attrs: { type: "button" },
  });

  let sheet;
  wireSubmit(submit, form, {
    sheet: () => sheet,
    onDone,
    run: async (f) => {
      if (existing) {
        const changes = f.changes();
        if (!Object.keys(changes).length) return;
        await api.patch(`/fuel-types/${existing.id}`, changes);
      } else {
        await api.post("/fuel-types", f.values());
      }
    },
  });

  sheet = openSheet({
    title: existing ? existing.code : "New fuel type",
    body: el("div", { className: "stack" }, form.nodes()),
    footer: sheetFooter(submit),
  });
}

/* --- nozzles -------------------------------------------------------------------- */

export function renderNozzles(container, context) {
  const reload = () => renderNozzles(container, context);

  return loadInto(container, {
    title: "Nozzles",
    session: context.session,
    navigate: context.navigate,
    fetch: async () => ({
      nozzles: await api.get("/nozzles", { include_inactive: true }),
      fuelTypes: await api.get("/fuel-types"),
    }),
    action: el("button", {
      className: "btn btn-primary",
      text: "Add",
      attrs: { type: "button" },
      // The action button is wired up before `fetch()` below has run, so the fuel types
      // loaded for the Edit sheet aren't in scope here yet — fetch them again on click
      // rather than opening the sheet with nothing to choose from.
      on: { click: async () => nozzleSheet(null, await api.get("/fuel-types"), reload) },
    }),
    build: ({ nozzles, fuelTypes }) =>
      el("div", { className: "stack" }, [
        nozzles.length
          ? el("div", { className: "grid" }, nozzles.map((nozzle) =>
              el("div", { className: "card stack" }, [
                el("div", { className: "row-between" }, [
                  el("div", {}, [
                    el("div", { className: "t-headline", text: nozzle.label }),
                    el("div", {
                      className: "t-caption",
                      text: `${nozzle.dispenser_label} · ${nozzle.fuel_type_code}`,
                    }),
                  ]),
                  activePill(nozzle),
                ]),
                el("div", { className: "list" }, [
                  el("div", { className: "list-row" }, [
                    el("div", { className: "list-row-main t-body", text: "Rollover ceiling" }),
                    el("div", {
                      className: "list-row-value t-body t-numeric",
                      text: nozzle.totalizer_max_value,
                    }),
                  ]),
                ]),
                el("button", {
                  className: "btn btn-block",
                  text: "Edit",
                  attrs: { type: "button" },
                  on: { click: () => nozzleSheet(nozzle, fuelTypes, reload) },
                }),
              ]),
            ))
          : empty("No nozzles configured. Readings cannot be recorded until at least one exists."),
        el("p", {
          className: "t-caption",
          text: "A nozzle's fuel type and rollover ceiling are fixed at creation — both are baked into every reading already recorded against it.",
        }),
      ]),
  });
}

function nozzleSheet(existing, fuelTypes, onDone) {
  const fields = {
    label: field({
      name: "label",
      label: "Nozzle label",
      value: existing?.label ?? "",
      required: true,
      hint: "e.g. DU-1/N-2. Unique at this outlet.",
    }),
    dispenser_label: field({
      name: "dispenser_label",
      label: "Dispenser label",
      value: existing?.dispenser_label ?? "",
      required: true,
      hint: "e.g. DU-1. Used to group nozzles in reports.",
    }),
  };

  if (!existing) {
    fields.fuel_type_id = select({
      name: "fuel_type_id",
      label: "Fuel type",
      options: [
        { value: "", label: "Choose…" },
        ...(fuelTypes ?? []).map((type) => ({ value: type.id, label: type.display_name })),
      ],
      required: true,
      hint: "Fixed once created — every reading on this nozzle is interpreted in this fuel's unit.",
    });
    fields.totalizer_max_value = field({
      name: "totalizer_max_value",
      label: "Rollover ceiling",
      type: "number",
      step: "0.01",
      min: "0.01",
      required: true,
      inputMode: "decimal",
      // §6.2's rollover formula reads this directly. A wrong value silently mis-computes
      // every rollover this meter ever has.
      hint: "The meter's maximum before it wraps to zero, e.g. 999999.99. §6.2's rollover arithmetic reads this.",
    });
    fields.meter_installed_at = field({
      name: "meter_installed_at",
      label: "Meter installed at",
      type: "datetime-local",
      required: true,
      hint: "A reading dated before this is refused.",
    });
  } else {
    fields.is_active = checkbox({
      name: "is_active",
      label: "Active",
      checked: existing.is_active,
      hint: "An inactive nozzle drops out of the reading worksheet and stops blocking a shift close.",
    });
  }

  const form = new Form(fields);
  const submit = el("button", {
    className: "btn btn-primary btn-block",
    text: existing ? "Save" : "Create",
    attrs: { type: "button" },
  });

  let sheet;
  wireSubmit(submit, form, {
    sheet: () => sheet,
    onDone,
    run: async (f) => {
      if (existing) {
        const changes = f.changes();
        if (!Object.keys(changes).length) return;
        await api.patch(`/nozzles/${existing.id}`, changes);
      } else {
        const values = f.values();
        await api.post("/nozzles", {
          ...values,
          // A datetime-local value is naive; every timestamp this API accepts requires an
          // offset (§3 rule 4). Without this the create 422s on NAIVE_TIMESTAMP.
          meter_installed_at: new Date(values.meter_installed_at).toISOString(),
        });
      }
    },
  });

  sheet = openSheet({
    title: existing ? existing.label : "New nozzle",
    body: el("div", { className: "stack" }, form.nodes()),
    footer: sheetFooter(submit),
  });
}

/* --- expense categories ---------------------------------------------------------- */

export function renderCategories(container, context) {
  const reload = () => renderCategories(container, context);

  return loadInto(container, {
    title: "Expense categories",
    session: context.session,
    navigate: context.navigate,
    fetch: () => api.get("/expense-categories", { include_inactive: true }),
    action: el("button", {
      className: "btn btn-primary",
      text: "Add",
      attrs: { type: "button" },
      on: { click: () => categorySheet(null, reload) },
    }),
    build: (categories) =>
      el("div", { className: "stack" }, [
        // §14, twice over. The enum that once made this impossible is gone, so the warning
        // has to live where somebody would otherwise create the category.
        el("div", { className: "card" }, [
          el("p", {
            className: "t-caption text-short",
            text: "Never create a category for a fuel restock, a tanker delivery, or an IOCL/PAD settlement. That money leaves the bank, not the drawer — filing it here makes the cash engine invent a daily shortage that never happened.",
          }),
        ]),
        categories.length
          ? el("div", { className: "grid" }, categories.map((category) =>
              el("div", { className: "card stack" }, [
                el("div", { className: "row-between" }, [
                  el("div", {}, [
                    el("div", { className: "t-headline", text: category.display_name }),
                    el("div", { className: "t-caption", text: category.code }),
                  ]),
                  activePill(category),
                ]),
                category.requires_receipt ? pill("receipt required", "review") : null,
                el("button", {
                  className: "btn btn-block",
                  text: "Edit",
                  attrs: { type: "button" },
                  on: { click: () => categorySheet(category, reload) },
                }),
              ]),
            ))
          : empty("No categories."),
      ]),
  });
}

function categorySheet(existing, onDone) {
  const fields = {};

  if (!existing) {
    fields.code = field({
      name: "code",
      label: "Code",
      required: true,
      hint: "Letters, digits and underscores; upper-cased automatically. PERMANENT — changing it later would relabel every expense ever filed under it.",
    });
  }

  fields.display_name = field({
    name: "display_name",
    label: "Display name",
    value: existing?.display_name ?? "",
    required: true,
  });

  fields.requires_receipt = checkbox({
    name: "requires_receipt",
    label: "Always requires a receipt",
    checked: existing?.requires_receipt ?? false,
    // §6.11: the flag is snapshotted onto each expense at insert. Flipping it does NOT
    // retroactively make old expenses non-compliant, and saying so here is the difference
    // between an admin flipping it confidently and not touching it at all.
    hint: "Applies to new expenses only. Existing ones keep the rule that was in force when they were filed.",
  });

  if (existing) {
    fields.is_active = checkbox({
      name: "is_active",
      label: "Active",
      checked: existing.is_active,
      hint: "An inactive category refuses new expenses; historical ones still read and report.",
    });
  }

  const form = new Form(fields);
  const submit = el("button", {
    className: "btn btn-primary btn-block",
    text: existing ? "Save" : "Create",
    attrs: { type: "button" },
  });

  let sheet;
  wireSubmit(submit, form, {
    sheet: () => sheet,
    onDone,
    run: async (f) => {
      if (existing) {
        const changes = f.changes();
        if (!Object.keys(changes).length) return;
        await api.patch(`/expense-categories/${existing.id}`, changes);
      } else {
        await api.post("/expense-categories", f.values());
      }
    },
  });

  sheet = openSheet({
    title: existing ? existing.code : "New category",
    body: el("div", { className: "stack" }, form.nodes()),
    footer: sheetFooter(submit),
  });
}

/* --- shift templates -------------------------------------------------------------- */

export function renderShiftTemplates(container, context) {
  const reload = () => renderShiftTemplates(container, context);

  return loadInto(container, {
    title: "Shift templates",
    session: context.session,
    navigate: context.navigate,
    fetch: () => api.get("/shift-templates", { include_inactive: true }),
    action: el("button", {
      className: "btn btn-primary",
      text: "Add",
      attrs: { type: "button" },
      on: { click: () => templateSheet(null, reload) },
    }),
    build: (templates) =>
      el("div", { className: "stack" }, [
        el("p", {
          className: "t-caption",
          // §5.1 / §14: the template supplies a default at creation and is never consulted
          // again. Editing it must not revalue a shift that already happened.
          text: "These supply the default start and end times when a shift is opened. Changing one never alters a shift that already exists — the times are copied onto the shift when it is created.",
        }),
        templates.length
          ? el("div", { className: "grid" }, templates.map((template) =>
              el("div", { className: "card stack" }, [
                el("div", { className: "row-between" }, [
                  el("div", {}, [
                    el("div", { className: "t-headline", text: template.label }),
                    el("div", { className: "t-caption", text: `Sequence ${template.sequence}` }),
                  ]),
                  activePill(template),
                ]),
                el("div", { className: "list" }, [
                  el("div", { className: "list-row" }, [
                    el("div", { className: "list-row-main t-body", text: "Trades" }),
                    el("div", {
                      className: "list-row-value t-body",
                      text: `${localTime(template.starts_at_local)} – ${localTime(template.ends_at_local)}${
                        template.crosses_midnight ? " (next day)" : ""
                      }`,
                    }),
                  ]),
                ]),
                el("button", {
                  className: "btn btn-block",
                  text: "Edit",
                  attrs: { type: "button" },
                  on: { click: () => templateSheet(template, reload) },
                }),
              ]),
            ))
          : empty("No templates. A shift can still be opened; its times just have no default."),
      ]),
  });
}

function templateSheet(existing, onDone) {
  const fields = {};

  if (!existing) {
    fields.sequence = field({
      name: "sequence",
      label: "Sequence",
      type: "number",
      min: "1",
      max: "24",
      required: true,
      hint: "1 for the first shift of a day, 2 for the next. Fixed once created.",
    });
  }

  fields.label = field({
    name: "label",
    label: "Label",
    value: existing?.label ?? "",
    required: true,
    hint: "Display only — nothing keys off it.",
  });
  fields.starts_at_local = field({
    name: "starts_at_local",
    label: "Starts (local time)",
    type: "time",
    value: existing?.starts_at_local ?? "",
    required: true,
  });
  fields.ends_at_local = field({
    name: "ends_at_local",
    label: "Ends (local time)",
    type: "time",
    value: existing?.ends_at_local ?? "",
    required: true,
    hint: "An end earlier than the start means the shift crosses midnight.",
  });

  if (existing) {
    fields.is_active = checkbox({
      name: "is_active",
      label: "Active",
      checked: existing.is_active,
    });
  }

  const form = new Form(fields);
  const submit = el("button", {
    className: "btn btn-primary btn-block",
    text: existing ? "Save" : "Create",
    attrs: { type: "button" },
  });

  let sheet;
  wireSubmit(submit, form, {
    sheet: () => sheet,
    onDone,
    run: async (f) => {
      if (existing) {
        const changes = f.changes();
        if (!Object.keys(changes).length) return;
        await api.patch(`/shift-templates/${existing.id}`, changes);
      } else {
        await api.post("/shift-templates", f.values());
      }
    },
  });

  sheet = openSheet({
    title: existing ? existing.label : "New shift template",
    body: el("div", { className: "stack" }, form.nodes()),
    footer: sheetFooter(submit),
  });
}
