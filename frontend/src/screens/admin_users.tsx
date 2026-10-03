/* Users: who may sign in, and what they may do (CLAUDE.md §5.1, §8, §13.25-27). Rebuilt in
 * Phase 23 from admin_users.js.
 *
 * ## Creating a person touches two systems
 *
 * Supabase owns the credential; this app owns the profile and the role. So the create form asks
 * for an email and a password even though neither is a column here: they go straight to the
 * identity provider, are never stored, never read back and never audited (§13.26). That is also
 * why the edit form has no email field: there is nothing here to edit.
 *
 * ## No delete, no password reset
 *
 * §3 rule 6 forbids the first; §12 puts the second with Supabase. Active off revokes access at
 * this outlet while every historical row the person touched keeps reading.
 *
 * ## The last admin cannot be removed
 *
 * The server refuses it with 409 LAST_ADMIN_AT_OUTLET (§13.27). This screen does not predict
 * that: a client-side guess is not a control, and one that disagreed with the server would be
 * worse than none (§8). The hint says so, so the refusal is not a surprise.
 */

import { useState } from "react";
import { api } from "../api/client";
import { useApiQuery } from "../api/queries";
import type { Schemas } from "../api/types";
import { dateTime } from "../lib/time";
import { CheckboxField, SelectField, TextField, useForm } from "../ui/form";
import { Empty, ErrorCard, type PillKind, Pill, Skeleton } from "../ui/primitives";
import { Sheet } from "../ui/Sheet";
import { ActivePill, AddAction, AdminCard, AdminGrid, AdminList, Fields, SaveButton, useSave } from "./admin";

type Role = Schemas["Role"];

const ROLE_OPTIONS: { value: Role; label: string }[] = [
  { value: "attendant", label: "Attendant: readings, collections and udhaar" },
  { value: "manager", label: "Manager: also closes shifts and runs the Cash tab" },
  { value: "admin", label: "Admin: also reference data and locking a day" },
];

const ROLE_PILL: Record<Role, PillKind> = { admin: "locked", manager: "open", attendant: "neutral" };

type Action = { kind: "new" } | { kind: "edit"; id: string; name: string } | null;

export function UsersScreen() {
  // include_inactive: retired people stay visible, because they are not deleted.
  const users = useApiQuery<Schemas["UserListItem"][]>("/users", { include_inactive: true });
  const [action, setAction] = useState<Action>(null);

  return (
    <>
      <AddAction onClick={() => setAction({ kind: "new" })} />
      <AdminList title="Users" query={users}>
        {(rows) => (
          <div className="flex flex-col gap-3">
            <p className="text-[0.8125rem] text-ink-muted">
              Email and password live in Supabase, not here. This app decides what somebody may do; it never stores how they prove who they are, so there is no password reset and no
              email change on this screen.
            </p>
            {rows.length ? (
              <AdminGrid>
                {rows.map((user) => (
                  <AdminCard
                    key={user.id}
                    title={user.full_name}
                    status={
                      <>
                        <Pill kind={ROLE_PILL[user.role]}>{user.role}</Pill>
                        <ActivePill active={user.is_active} />
                      </>
                    }
                    // The list is the manager-floor projection with no phone (§8), so the form
                    // loads the admin-only detail rather than editing a partial row: a form
                    // opened with a blank phone would clear it on save.
                    onEdit={() => setAction({ kind: "edit", id: user.id, name: user.full_name })}
                  />
                ))}
              </AdminGrid>
            ) : (
              <Empty>No users yet.</Empty>
            )}
          </div>
        )}
      </AdminList>
      <Sheet open={action !== null} onClose={() => setAction(null)} title={action?.kind === "edit" ? action.name : "New user"}>
        {action?.kind === "new" ? <NewUserForm onDone={() => setAction(null)} /> : null}
        {action?.kind === "edit" ? <ExistingUser key={action.id} id={action.id} onDone={() => setAction(null)} /> : null}
      </Sheet>
    </>
  );
}

function NewUserForm({ onDone }: { onDone: () => void }) {
  const { busy, save } = useSave();
  const form = useForm({ email: "", password: "", full_name: "", phone: "", role: "attendant" });

  function submit() {
    const body: Schemas["UserCreate"] = {
      email: form.values.email,
      password: form.values.password,
      full_name: form.values.full_name,
      role: form.values.role as Role,
      // Omitted rather than "", which would store an empty string as a phone number.
      phone: form.values.phone.trim() ? form.values.phone : null,
    };
    // No Idempotency-Key: a user is reference data, not a money record (§6.10).
    void save(form, () => api.post("/users", body), onDone, `${form.values.full_name} can now sign in.`);
  }

  return (
    <Fields>
      <TextField form={form} name="email" label="Email" type="email" autoComplete="off" required hint="Their Supabase login. It cannot be changed from this app afterwards." />
      <TextField
        form={form}
        name="password"
        label="Temporary password"
        type="password"
        autoComplete="new-password"
        required
        hint="At least 8 characters. Tell it to them directly: it is never shown again and is not stored here."
      />
      <TextField form={form} name="full_name" label="Full name" required hint="The name on a shift, and on any shortfall booked against them." />
      <TextField form={form} name="phone" label="Phone (optional)" type="tel" inputMode="tel" />
      <SelectField form={form} name="role" label="Role" options={ROLE_OPTIONS} hint="Roles are a floor: a manager can do everything an attendant can." />
      <SaveButton busy={busy} creating onClick={submit} />
    </Fields>
  );
}

function ExistingUser({ id, onDone }: { id: string; onDone: () => void }) {
  const detail = useApiQuery<Schemas["UserResponse"]>(`/users/${id}`);
  if (detail.isPending) return <Skeleton rows={3} />;
  if (detail.isError || !detail.data) return <ErrorCard error={detail.error} onRetry={() => void detail.refetch()} />;
  return <EditUserForm user={detail.data} onDone={onDone} />;
}

function EditUserForm({ user, onDone }: { user: Schemas["UserResponse"]; onDone: () => void }) {
  const { busy, save } = useSave();
  const form = useForm({ full_name: user.full_name, phone: user.phone ?? "", role: user.role as string, is_active: user.is_active });

  function submit() {
    const changes = form.changes();
    const body: Schemas["UserUpdate"] = {};
    if (changes.full_name !== undefined) body.full_name = changes.full_name;
    // `phone` is the one field where an explicit null CLEARS the column, which is what an
    // emptied input means. Every other field here is NOT NULL.
    if (changes.phone !== undefined) body.phone = changes.phone.trim() === "" ? null : changes.phone;
    if (changes.role !== undefined) body.role = changes.role as Role;
    if (changes.is_active !== undefined) body.is_active = changes.is_active;
    if (!Object.keys(body).length) return onDone();
    void save(form, () => api.patch(`/users/${user.id}`, body), onDone);
  }

  return (
    <Fields>
      <TextField form={form} name="full_name" label="Full name" required />
      <TextField form={form} name="phone" label="Phone (optional)" type="tel" inputMode="tel" />
      <SelectField form={form} name="role" label="Role" options={ROLE_OPTIONS} hint="Roles are a floor: a manager can do everything an attendant can." />
      <CheckboxField
        form={form}
        name="is_active"
        label="Active"
        hint="Switching this off revokes their access at this outlet immediately. Nothing they recorded is removed. The last remaining admin cannot be switched off."
      />
      <p className="text-[0.8125rem] text-ink-muted">
        Added {dateTime(user.created_at)}.
        {user.profile_is_active ? "" : " This account is deactivated across every outlet, which only the server command can undo."}
      </p>
      <SaveButton busy={busy} creating={false} onClick={submit} />
    </Fields>
  );
}
