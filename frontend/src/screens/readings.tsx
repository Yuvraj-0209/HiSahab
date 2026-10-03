/* The reading worksheet: the screen §4.7 was written about. Rebuilt in Phase 23 from
 * readings.js, with every rule below unchanged.
 *
 * §4.7's conclusion is an interface requirement, not a data one:
 *
 *   "pre-filled, not typeable, and confirmed against the physical meter, with a mismatch path
 *    that captures the real reading and raises it for review before anybody is blamed. Zero
 *    typing on a normal day; the abnormal day becomes visible instead of reassigned."
 *
 * And the cost of getting it wrong: "An assumed opening therefore converts theft into a debt
 * owed by someone who did nothing wrong." So `opening_confirmed` is sent as `true` only because
 * a person pressed a button that says so -- never because a form initialised it (§14).
 *
 * ## Three states per nozzle, and the second one is the point
 *
 *   matches   the meter reads what the chain predicted. One tap. Zero typing.
 *   mismatch  it reads something else. The real value is captured, a reason is required, and the
 *             row is flagged for review: visible, not absorbed.
 *   anchor    no predecessor exists. The opening becomes required and the caller must be an
 *             admin (§4.7).
 *
 * ## testing_quantity is a question, never a pre-filled zero (§4.2, §14)
 *
 * Zero is a real answer (CBG is not calibration-tested, §4.5), but it has to be *an answer*:
 * the field is blank until touched, and blank is refused here because the server would
 * otherwise default it to 0 -- "the field looking correctly filled in".
 *
 * "No sale" fills in a guess (closing = opening, testing = 0) for a nozzle that stayed dry. It
 * is a convenience a person can see, edit and reject; Save is still a tap they have to make.
 *
 * No Idempotency-Key: UNIQUE (shift_id, nozzle_id) makes a retried POST a 409, not a second row.
 */

import { type ReactNode, useEffect, useRef, useState } from "react";
import { useParams } from "react-router";
import { AnchorIcon, CheckIcon, XIcon } from "@phosphor-icons/react";
import { api } from "../api/client";
import { useApiQuery, useRefreshApi } from "../api/queries";
import type { Schemas } from "../api/types";
import { ScreenTitle } from "../app/chrome";
import { useSession } from "../app/session";
import { quantity, reading } from "../lib/money";
import { satisfies } from "../lib/roles";
import { reportFailure } from "../ui/feedback";
import { CheckboxField, TextField, useForm } from "../ui/form";
import { DURATION, EASE, gsap, useMotion } from "../motion/gsap";
import { useArrival } from "../ui/motion";
import { Button, Card, Empty, ErrorCard, ListRow, Pill, type PillKind, SectionLabel, Skeleton } from "../ui/primitives";
import { Sheet } from "../ui/Sheet";
import { notify } from "../ui/toast";


type Line = Schemas["WorksheetLine"];
type Saved = Schemas["ReadingResponse"];

function unitWord(line: Line): string {
  return line.unit_of_measure === "kilogram" ? "kg" : "litres";
}

/** fuel_type_code is admin-managed data with no fixed set (§5.1), so groups follow whatever codes
 * are present, in first-seen order: the order the admin entered the nozzles in. */
function groupByFuel(lines: Line[]): { code: string; lines: Line[] }[] {
  const groups = new Map<string, Line[]>();
  for (const line of lines) groups.set(line.fuel_type_code, [...(groups.get(line.fuel_type_code) ?? []), line]);
  return Array.from(groups, ([code, members]) => ({ code, lines: members }));
}

function status(line: Line): { text: string; kind: PillKind } {
  const saved = line.reading;
  if (!saved) return line.requires_anchor ? { text: "needs anchor", kind: "review" } : { text: "not started", kind: "neutral" };
  if (saved.requires_review) return { text: "flagged", kind: "review" };
  return saved.closing_reading === null ? { text: "opening only", kind: "neutral" } : { text: "recorded", kind: "open" };
}

export function ReadingsScreen() {
  const { shiftId = "" } = useParams();
  const worksheet = useApiQuery<Schemas["Worksheet"]>(`/shifts/${shiftId}/readings`);
  const grid = useRef<HTMLDivElement>(null);
  useArrival(grid, Boolean(worksheet.data));
  const [selected, setSelected] = useState<string | null>(null);

  if (worksheet.isPending) {
    return (
      <>
        <ScreenTitle title="Readings" />
        <Skeleton rows={4} />
      </>
    );
  }
  if (worksheet.isError || !worksheet.data) {
    return (
      <>
        <ScreenTitle title="Readings" />
        <ErrorCard error={worksheet.error} onRetry={() => void worksheet.refetch()} />
      </>
    );
  }

  const data = worksheet.data;
  const editable = data.shift_status === "open";
  const line = data.lines.find((candidate) => candidate.nozzle_id === selected) ?? null;

  return (
    <>
      <ScreenTitle title="Readings" subtitle={`${data.lines.length} nozzle${data.lines.length === 1 ? "" : "s"}`} />
      <div className="flex flex-col gap-5">
        <Card>
          <div className="flex items-center justify-between gap-3">
            <span className="text-[0.8125rem] font-medium text-ink-muted">Shift</span>
            <Pill kind={data.shift_status === "open" ? "open" : data.shift_status === "locked" ? "locked" : "closed"}>
              {data.shift_status}
            </Pill>
          </div>
          <p className="mt-2 text-[0.875rem] text-ink-muted">
            {editable
              ? "Each opening is carried forward from that nozzle's last closing reading. Confirm it against the meter. Do not assume it."
              : "This shift is no longer open, so readings cannot be changed. Corrections happen through an admin reopen."}
          </p>
        </Card>

        <div ref={grid} className="flex flex-col gap-5">
          {data.lines.length === 0 ? <Empty>No active nozzles at this outlet.</Empty> : null}
          {groupByFuel(data.lines).map((group) => (
            <section key={group.code}>
              <SectionLabel>{group.code}</SectionLabel>
              <div className="grid grid-cols-2 gap-2.5 sm:grid-cols-3 lg:grid-cols-4">
                {group.lines.map((member) => (
                  <NozzleTile key={member.nozzle_id} line={member} onOpen={() => setSelected(member.nozzle_id)} />
                ))}
              </div>
            </section>
          ))}
        </div>
      </div>

      <Sheet
        open={line !== null}
        onClose={() => setSelected(null)}
        title={line?.nozzle_label ?? ""}
        subtitle={line ? `${line.dispenser_label} · ${line.fuel_type_code}` : undefined}
      >
        {line ? (
          <NozzleSheet key={line.nozzle_id} line={line} shiftId={shiftId} editable={editable} onDone={() => setSelected(null)} />
        ) : null}
      </Sheet>
    </>
  );
}

/** One nozzle as a tile. When its state changes after a save, it settles in with a brief scale
 * -- a state transition the attendant can see land, not decoration. */
function NozzleTile({ line, onOpen }: { line: Line; onOpen: () => void }) {
  const saved = line.reading;
  const pill = status(line);
  const scope = useRef<HTMLButtonElement>(null);
  const previous = useRef(pill.text);

  useMotion(
    (play) => {
      if (previous.current === pill.text) return;
      previous.current = pill.text;
      play(() => {
        gsap.fromTo(scope.current, { scale: 0.96 }, { scale: 1, duration: DURATION.medium, ease: EASE.settle.gsap, clearProps: "transform" });
      });
    },
    { dependencies: [pill.text], scope },
  );

  return (
    <button
      ref={scope}
      type="button"
      data-arrive
      onClick={onOpen}
      className={`pressable flex flex-col items-start gap-2 rounded-[var(--radius-card)] border bg-surface p-3.5 text-left shadow-1 ${
        saved?.requires_review ? "border-warning" : "border-hairline"
      }`}
    >
      <span className="text-[0.9375rem] font-semibold text-ink">{line.nozzle_label}</span>
      <Pill kind={pill.kind}>{pill.text}</Pill>
      {saved ? (
        <span className="tabular grid w-full grid-cols-[auto_1fr] gap-x-2 text-[0.75rem] text-ink-muted">
          <span className="text-ink-faint">Open</span>
          <span className="text-right">{reading(saved.opening_reading)}</span>
          <span className="text-ink-faint">Close</span>
          <span className="text-right">{reading(saved.closing_reading)}</span>
          <span className="text-ink-faint">Sold</span>
          <span className="text-right text-ink">{quantity(saved.quantity_sold, line.unit_of_measure)}</span>
        </span>
      ) : null}
    </button>
  );
}

/** Decides what a tap opens. Each branch is the Phase 12 flow for that state, unchanged. */
function NozzleSheet({ line, shiftId, editable, onDone }: { line: Line; shiftId: string; editable: boolean; onDone: () => void }) {
  const saved = line.reading;
  if (!editable) {
    return saved ? <SavedRows line={line} saved={saved} /> : <p className="text-[0.875rem] text-ink-muted">No reading was recorded for this nozzle.</p>;
  }
  if (!saved) return line.requires_anchor ? <AnchorStep line={line} shiftId={shiftId} onDone={onDone} /> : <FirstEntry line={line} shiftId={shiftId} onDone={onDone} />;
  return <ClosingForm line={line} saved={saved} shiftId={shiftId} onDone={onDone} />;
}

function SavedRows({ line, saved }: { line: Line; saved: Saved }) {
  // The chain's prediction is shown beside the confirmed value whenever they differ (§5.2): it is
  // what makes "the meter did not say what we expected" a fact on the row.
  const disagreed = saved.chained_opening_reading !== null && saved.chained_opening_reading !== saved.opening_reading;
  return (
    <div>
      <ListRow label="Opening" value={reading(saved.opening_reading)} />
      {disagreed ? <ListRow label="Chain predicted" value={reading(saved.chained_opening_reading)} valueClassName="text-short" /> : null}
      {saved.opening_variance_reason ? <ListRow label="Variance reason" value={saved.opening_variance_reason} /> : null}
      {saved.chained_opening_reading === null ? <ListRow label="Chain" value="anchored, no predecessor" /> : null}
      <ListRow label="Closing" value={reading(saved.closing_reading, { absent: "not entered" })} />
      <ListRow label="Testing" value={quantity(saved.testing_quantity, line.unit_of_measure)} />
      {saved.rollover_occurred ? <ListRow label="Rollover" value="yes" /> : null}
      {saved.meter_reset_occurred ? <ListRow label="Meter reset" value="yes" /> : null}
      {saved.manual_quantity_override !== null ? (
        <ListRow label="Manual override" value={quantity(saved.manual_quantity_override, line.unit_of_measure)} />
      ) : null}
      <ListRow label="Quantity sold" value={quantity(saved.quantity_sold, line.unit_of_measure, { absent: "awaiting closing" })} strong />
    </div>
  );
}

/* --- a first reading: choose, then fill in ---------------------------------------------
 *
 * One sheet for both steps: the choice and the form are two steps of one action, and a close-
 * then-reopen between them would read as broken rather than as a transition. */

type Choice = { matches: boolean; anchor?: boolean; noSale?: boolean };

function FirstEntry({ line, shiftId, onDone }: { line: Line; shiftId: string; onDone: () => void }) {
  const [choice, setChoice] = useState<Choice | null>(null);
  if (choice) return <EntryForm line={line} shiftId={shiftId} choice={choice} onDone={onDone} />;
  return (
    <div className="flex flex-col gap-4">
      <div>
        <p className="text-[0.8125rem] font-medium text-ink-muted">The chain says this nozzle opens at</p>
        {/* Large and NOT an input: §4.7's "pre-filled, not typeable". A field with a value in it
         * invites a glance-and-tab-past, which is the assumption this screen exists to prevent. */}
        <p className="tabular mt-1 text-[2.25rem] leading-none font-semibold tracking-[-0.03em] text-ink">
          {reading(line.chained_opening_reading)}
        </p>
      </div>
      <p className="text-[0.875rem] text-ink-muted">
        Read the physical meter before you touch this. If it does not match, say so. That is the signal, not a nuisance.
      </p>
      <div className="flex flex-col gap-2">
        <ChoiceButton icon={<CheckIcon size={20} weight="bold" aria-hidden />} onClick={() => setChoice({ matches: true })}>
          The meter reads exactly this
        </ChoiceButton>
        <ChoiceButton icon={<XIcon size={20} weight="bold" aria-hidden />} onClick={() => setChoice({ matches: false })}>
          The meter reads something else
        </ChoiceButton>
        <Button block onClick={() => setChoice({ matches: true, noSale: true })}>
          No sale, the nozzle stayed dry
        </Button>
      </div>
    </div>
  );
}

/** A confirm control that starts unconfirmed, every time: it is a deliberate act (§14). */
function ChoiceButton({ icon, children, onClick }: { icon: ReactNode; children: ReactNode; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="pressable flex min-h-14 w-full items-center gap-3 rounded-[var(--radius-control)] border-2 border-hairline-strong bg-surface px-4 text-left text-[0.9375rem] font-medium text-ink hover:border-accent"
    >
      <span className="grid size-8 place-items-center rounded-full bg-surface-sunken text-ink-muted">{icon}</span>
      {children}
    </button>
  );
}

function AnchorStep({ line, shiftId, onDone }: { line: Line; shiftId: string; onDone: () => void }) {
  const { me } = useSession();
  const [anchoring, setAnchoring] = useState(false);
  if (anchoring) return <EntryForm line={line} shiftId={shiftId} choice={{ matches: false, anchor: true }} onDone={onDone} />;
  return (
    <div className="flex flex-col items-start gap-4">
      <Pill kind="review">needs anchoring</Pill>
      <p className="text-[0.875rem] text-ink-muted">
        This nozzle has no previous reading, so there is nothing to carry forward. Its first reading anchors the chain and is recorded as the starting point.
      </p>
      {satisfies(me.role, "admin") ? (
        <Button variant="primary" block icon={<AnchorIcon size={18} aria-hidden />} onClick={() => setAnchoring(true)}>
          Anchor this nozzle
        </Button>
      ) : (
        // A dead control with no explanation is what §16's wayfinding rule forbids.
        <p className="text-[0.875rem] text-ink-muted">
          Only an admin can set a starting reading. Ask an admin to anchor it before this shift is closed.
        </p>
      )}
    </div>
  );
}

function EntryForm({ line, shiftId, choice, onDone }: { line: Line; shiftId: string; choice: Choice; onDone: () => void }) {
  const refresh = useRefreshApi();
  const { matches, anchor = false, noSale = false } = choice;
  const form = useForm({
    opening_reading: "",
    opening_variance_reason: "",
    // "No sale" fills in a guess a person can see and change; it is never sent without Save.
    closing_reading: noSale ? (line.chained_opening_reading ?? "") : "",
    testing_quantity: noSale ? "0" : "",
    rollover_occurred: false,
    meter_reset_occurred: false,
  });
  const [busy, setBusy] = useState(false);

  async function submit() {
    form.clearErrors();
    const values = form.values;
    // The client half of §4.2: the server defaults testing to 0, so an omission would pass.
    if (values.testing_quantity.trim() === "") {
      form.setError("testing_quantity", "Enter a figure. Zero is a valid answer; blank is not.");
      return;
    }

    const body: Schemas["ReadingCreate"] = {
      nozzle_id: line.nozzle_id,
      // True because the person pressed a button that says so -- not a form default (§4.7).
      opening_confirmed: true,
      testing_quantity: values.testing_quantity,
      // Both default to false on the server; sent explicitly because the person answered them.
      rollover_occurred: values.rollover_occurred,
      meter_reset_occurred: values.meter_reset_occurred,
    };
    if (values.opening_reading) body.opening_reading = values.opening_reading;
    if (values.opening_variance_reason) body.opening_variance_reason = values.opening_variance_reason;
    if (values.closing_reading) body.closing_reading = values.closing_reading;

    setBusy(true);
    try {
      await api.post(`/shifts/${shiftId}/readings`, body);
      onDone();
      notify.success(`${line.nozzle_label} recorded.`);
      await refresh();
    } catch (error) {
      reportFailure(error, form);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-col gap-4">
      {matches && !anchor ? (
        <div className="rounded-[var(--radius-control)] bg-surplus-tint px-4 py-3">
          <p className="text-[0.8125rem] text-surplus">Opening confirmed against the meter</p>
          <p className="tabular text-[1.375rem] font-semibold text-ink">{reading(line.chained_opening_reading)}</p>
        </div>
      ) : null}
      {!matches ? (
        <TextField
          form={form}
          name="opening_reading"
          label={anchor ? "Starting reading on the meter" : "What the meter actually reads"}
          inputMode="decimal"
          required
          hint={
            anchor
              ? "This becomes the anchor for every future shift on this nozzle."
              : `The chain predicted ${reading(line.chained_opening_reading)}. Enter the real figure.`
          }
        />
      ) : null}
      {!matches && !anchor ? (
        <TextField
          form={form}
          name="opening_variance_reason"
          label="Why does it differ?"
          required
          hint="Raises this row for review before anybody is blamed. Up to 500 characters."
        />
      ) : null}
      <TextField form={form} name="closing_reading" label="Closing reading" inputMode="decimal" hint="Leave blank if the shift is still running." />
      <TextField
        form={form}
        name="testing_quantity"
        label={`Testing quantity (${unitWord(line)})`}
        inputMode="decimal"
        hint={
          line.unit_of_measure === "kilogram"
            ? "CBG is not calibration-tested here, so this is normally 0. Enter it rather than leaving it blank."
            : "The 5-litre standard measure poured back into the tank. It was dispensed but never sold."
        }
      />
      <CheckboxField
        form={form}
        name="rollover_occurred"
        label="The meter rolled over"
        hint={`Past its maximum of ${reading(line.totalizer_max_value)} and back to zero.`}
      />
      <CheckboxField
        form={form}
        name="meter_reset_occurred"
        label="The meter was repaired or replaced"
        hint="The reading pair then means nothing, and an admin has to enter the quantity by hand."
      />
      <Button variant="primary" block disabled={busy} onClick={() => void submit()}>
        {busy ? "Saving…" : "Save reading"}
      </Button>
    </div>
  );
}

/* --- editing a saved reading ----------------------------------------------------------- */

/** The edit form. `opening_reading` is not editable here at all: §4.7, "a wrong opening is a
 * wrong chain, fixed at its source". Only what changed is sent. The meter-reset tick is here as
 * well as on the first entry: without it a saved reading could never reach the manual-quantity
 * override (found 19 Sept 2026, when a wrong chained opening left D1 at 0 L). */
function ClosingForm({ line, saved, shiftId, onDone }: { line: Line; saved: Saved; shiftId: string; onDone: () => void }) {
  const { me } = useSession();
  const refresh = useRefreshApi();
  const form = useForm({
    closing_reading: saved.closing_reading ?? "",
    testing_quantity: saved.testing_quantity ?? "",
    meter_reset_occurred: Boolean(saved.meter_reset_occurred),
  });
  const [busy, setBusy] = useState(false);
  const [overriding, setOverriding] = useState(false);

  async function submit() {
    form.clearErrors();
    const body: Schemas["ReadingUpdate"] = form.changes();
    if (Object.keys(body).length === 0) {
      onDone();
      return;
    }
    setBusy(true);
    try {
      await api.patch(`/shifts/${shiftId}/readings/${line.nozzle_id}`, body);
      onDone();
      notify.success("Reading updated.");
      await refresh();
    } catch (error) {
      reportFailure(error, form);
    } finally {
      setBusy(false);
    }
  }

  if (overriding) return <OverrideForm line={line} shiftId={shiftId} onDone={onDone} />;

  return (
    <div className="flex flex-col gap-4">
      {saved.requires_review ? (
        <p className="rounded-[var(--radius-control)] bg-warning-tint px-3.5 py-2.5 text-[0.8125rem] text-warning">
          {saved.review_note ? `Flagged for review: ${saved.review_note}` : "Flagged for review. A person needs to reconcile this reading."}
        </p>
      ) : null}
      <SavedRows line={line} saved={saved} />
      {saved.closing_reading === null ? (
        // Only before a closing exists. Afterwards, "no sale" is a correction to real data and
        // goes through the ordinary fields like any other edit.
        <Button
          block
          onClick={() => {
            form.set("closing_reading", saved.opening_reading);
            form.set("testing_quantity", "0");
          }}
        >
          No sale, same as opening
        </Button>
      ) : null}
      <TextField
        form={form}
        name="closing_reading"
        label="Closing reading"
        inputMode="decimal"
        required
        hint={`Opening was ${reading(saved.opening_reading)}.`}
      />
      <TextField form={form} name="testing_quantity" label={`Testing quantity (${unitWord(line)})`} inputMode="decimal" />
      <CheckboxField
        form={form}
        name="meter_reset_occurred"
        label="The meter was repaired or replaced"
        hint="The reading pair then means nothing. Save, reopen this nozzle, and an admin sets the quantity by hand."
      />
      {saved.meter_reset_occurred && satisfies(me.role, "admin") ? (
        <Button block onClick={() => setOverriding(true)}>
          Set manual quantity
        </Button>
      ) : null}
      <Button variant="primary" block disabled={busy} onClick={() => void submit()}>
        {busy ? "Saving…" : "Save"}
      </Button>
    </div>
  );
}

/* --- §6.2's meter-reset override: admin only, reason mandatory, audit-logged ------------- */

function OverrideForm({ line, shiftId, onDone }: { line: Line; shiftId: string; onDone: () => void }) {
  const refresh = useRefreshApi();
  const form = useForm({ manual_quantity_override: "", override_reason: "" });
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    document.getElementById(form.idFor("manual_quantity_override"))?.focus();
  }, [form]);

  async function submit() {
    form.clearErrors();
    setBusy(true);
    try {
      const body: Schemas["ReadingOverride"] = form.values;
      await api.post(`/shifts/${shiftId}/readings/${line.nozzle_id}/override`, body);
      onDone();
      notify.success("Override recorded.");
      await refresh();
    } catch (error) {
      reportFailure(error, form);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-col gap-4">
      <p className="text-[0.875rem] text-ink-muted">
        The meter was reset, so the opening and closing pair has no meaning. The system does not try to infer the split: enter what was actually sold.
      </p>
      <TextField form={form} name="manual_quantity_override" label={`Quantity actually sold (${unitWord(line)})`} inputMode="decimal" required />
      <TextField
        form={form}
        name="override_reason"
        label="Why is this being entered by hand?"
        required
        hint="Mandatory in the database, so this figure can never be unexplained. 3 to 500 characters."
      />
      <Button variant="primary" block disabled={busy} onClick={() => void submit()}>
        {busy ? "Saving…" : "Save override"}
      </Button>
    </div>
  );
}
