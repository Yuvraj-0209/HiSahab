/* Users: who may sign in, and what they may do (CLAUDE.md §5.1, §8, §13.25–27).
 *
 * The screen behind `app/api/v1/users.py`. Everything the Admin tab's other screens do,
 * plus three rules that are specific to people and worth stating where somebody would
 * otherwise be surprised by them.
 *
 * ## Creating a person touches two systems
 *
 * Supabase Auth owns the credential; this app owns the profile and the role. So the create
 * form asks for an email and a password even though **neither is a column in this
 * database** — they go straight to the identity provider and are never stored, never read
 * back, and never appear in the audit log (§13.26). That is also why the edit sheet has
 * no email field: there is nothing here to edit, and a form offering it would be lying.
 *
 * ## There is no delete, and no password reset
 *
 * §3 rule 6 forbids the first and fifteen non-cascading foreign keys make it impossible
 * anyway; §12 puts the second out of scope, because Supabase owns it. So the only way to
 * remove somebody is Active → off, which revokes their access at this outlet while every
 * historical row they touched keeps reading and reporting.
 *
 * ## The last admin cannot be removed
 *
 * The server refuses it with 409 LAST_ADMIN_AT_OUTLET (§13.27) and this screen does not
 * try to predict that — it lets the request go and shows the refusal. §8's rule, applied
 * honestly: a client-side guess is not a control, and one that disagreed with the server
 * would be worse than none. The card carries a note so the refusal is not a surprise.
 */

import { el, empty, pill, render } from "../dom.js";
import { api, explain } from "../api.js";
import { dateTime } from "../time.js";
import { checkbox, field, Form, select } from "../ui/field.js";
import { openSheet } from "../ui/sheet.js";
import { notify } from "../ui/toast.js";
import { activePill, loadInto, sheetFooter, wireSubmit } from "./admin.js";

const ROLE_OPTIONS = [
  { value: "attendant", label: "Attendant — records readings, collections and udhaar" },
  { value: "manager", label: "Manager — the above, plus closing shifts and the cash tab" },
  { value: "admin", label: "Admin — the above, plus reference data and locking a day" },
];

/* Descending privilege, matching the order the API returns and the order the Postgres
 * enum's labels are declared in. */
const ROLE_PILL = { admin: "review", manager: "open", attendant: "neutral" };

export function renderUsers(container, context) {
  const reload = () => renderUsers(container, context);

  return loadInto(container, {
    title: "Users",
    session: context.session,
    navigate: context.navigate,
    // include_inactive so retired people stay visible to an admin -- they are not deleted
    // and the screen should not pretend otherwise.
    fetch: () => api.get("/users", { include_inactive: true }),
    action: el("button", {
      className: "btn btn-primary",
      text: "Add",
      attrs: { type: "button" },
      on: { click: () => userSheet(null, reload) },
    }),
    build: (users) =>
      el("div", { className: "stack" }, [
        el("div", { className: "card" }, [
          el("p", {
            className: "t-caption",
            text: "Email and password live in Supabase, not here. This app decides what somebody may do; it never stores how they prove who they are — so there is no password reset and no email change on this screen.",
          }),
        ]),
        users.length
          ? el(
              "div",
              { className: "grid" },
              users.map((user) => userCard(user, reload)),
            )
          : empty("No users yet."),
      ]),
  });
}

function userCard(user, reload) {
  return el("div", { className: "card stack" }, [
    el("div", { className: "row-between" }, [
      el("div", { className: "grow" }, [
        el("div", { className: "t-headline", text: user.full_name }),
      ]),
      activePill(user),
    ]),
    el("div", { className: "row" }, [pill(user.role, ROLE_PILL[user.role] ?? "neutral")]),
    el("button", {
      className: "btn btn-block",
      text: "Edit",
      attrs: { type: "button" },
      // The list is the manager-floor projection and carries no phone (§8), so the sheet
      // fetches the admin-only detail rather than editing from a partial row. Opening a
      // form pre-filled with a blank phone would silently clear it on save.
      on: { click: () => openExistingUser(user.id, reload) },
    }),
  ]);
}

async function openExistingUser(userId, reload) {
  let detail;
  try {
    detail = await api.get(`/users/${userId}`);
  } catch (error) {
    notify.error(explain(error), { requestId: error.requestId });
    return;
  }
  userSheet(detail, reload);
}

function userSheet(existing, onDone) {
  const fields = {};

  if (!existing) {
    fields.email = field({
      name: "email",
      label: "Email",
      type: "email",
      required: true,
      autocomplete: "off",
      hint: "Their Supabase login. PERMANENT — it cannot be changed from this app afterwards.",
    });

    fields.password = field({
      name: "password",
      label: "Temporary password",
      type: "password",
      required: true,
      autocomplete: "new-password",
      hint: "At least 8 characters. Tell it to them directly; it is never shown again and is not stored here.",
    });
  }

  fields.full_name = field({
    name: "full_name",
    label: "Full name",
    value: existing?.full_name ?? "",
    required: true,
    hint: "The name that appears on a shift and on any shortfall booked against them.",
  });

  fields.phone = field({
    name: "phone",
    label: "Phone (optional)",
    type: "tel",
    inputMode: "tel",
    value: existing?.phone ?? "",
  });

  fields.role = select({
    name: "role",
    label: "Role",
    value: existing?.role ?? "attendant",
    options: ROLE_OPTIONS,
    hint: "Roles are a floor, not a list: a manager can do everything an attendant can.",
  });

  if (existing) {
    fields.is_active = checkbox({
      name: "is_active",
      label: "Active",
      checked: existing.is_active,
      hint: "Switching this off revokes their access at this outlet immediately. Nothing they recorded is removed. The last remaining admin cannot be switched off.",
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
        const body = { ...changes };
        // `phone` is the one field where an explicit null CLEARS the column rather than
        // being ignored, which is exactly what an emptied input means (see the router's
        // _NULLABLE_FIELDS). Every other field here is NOT NULL.
        if (body.phone === "") body.phone = null;
        await api.patch(`/users/${existing.id}`, body);
      } else {
        const values = f.values();
        const body = {
          email: values.email,
          password: values.password,
          full_name: values.full_name,
          role: values.role,
        };
        // Omitted rather than sent as "", which would store an empty string as a phone
        // number and read as one in every list.
        if (values.phone) body.phone = values.phone;
        // No Idempotency-Key: a user is reference data, not a money record (§6.10), the
        // same as a credit customer or an expense category.
        await api.post("/users", body);
      }
    },
  });

  sheet = openSheet({
    title: existing ? existing.full_name : "New user",
    body: el("div", { className: "stack" }, [
      ...form.nodes(),
      existing
        ? el("p", {
            className: "t-caption",
            text: `Added ${dateTime(existing.created_at)}.${
              existing.profile_is_active
                ? ""
                : " This account is deactivated across every outlet, which only the server command can undo."
            }`,
          })
        : null,
    ]),
    footer: sheetFooter(submit),
  });
}
