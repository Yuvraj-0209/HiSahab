/* The Entry hub: everything that gets typed into a shift.
 *
 * §4.7: "The sales register has one line per day. The whole day is typed in after the fact,
 * in one sitting." This screen is that sitting -- one list of everything the register holds,
 * in the order somebody works down a page, so nothing is discovered missing at close time.
 *
 * It resolves the open shift itself rather than taking one from the URL, because "Entry" as a
 * tab means "the shift I am working on now". Deep links to a *particular* shift's screens
 * exist too (#/shifts/{id}/readings) and are what the Today screen uses.
 */

import { el, render } from "../dom.js";
import { api, ApiError } from "../api.js";
import { businessDate } from "../time.js";
import { satisfies } from "../ui/nav.js";
import { errorCard } from "./today.js";

export async function renderEntry(container, { session, navigate }) {
  const { shell, me } = session;
  shell.setTab("entry");
  shell.setTitle("Entry");
  shell.setActions();

  render(container, el("div", { className: "t-caption", text: "Loading…" }));

  let shift = null;
  try {
    shift = await api.get("/shifts/current");
  } catch (error) {
    if (error instanceof ApiError && error.code === "NO_OPEN_SHIFT") {
      render(
        container,
        el("div", { className: "card stack" }, [
          el("p", { className: "t-body", text: "There is no open shift to type into." }),
          el("button", {
            className: "btn btn-primary btn-block",
            text: "Go to Today",
            attrs: { type: "button" },
            on: { click: () => navigate("#/today") },
          }),
        ]),
      );
      return;
    }
    render(container, errorCard(error, () => renderEntry(container, { session, navigate })));
    return;
  }

  shell.setTitle("Entry", `${businessDate(shift.business_date)} · shift ${shift.sequence}`);

  const entries = [
    {
      label: "Nozzle readings",
      hint: "Confirm each opening against the meter, then the closing",
      route: `#/shifts/${shift.id}/readings`,
      role: "attendant",
    },
    {
      label: "Collections",
      hint: "Cash, card, UPI and wallet — cash is a declaration",
      route: `#/shifts/${shift.id}/collections`,
      role: "attendant",
    },
    {
      label: "Expenses",
      hint: "What was paid out, and by what method",
      route: `#/shifts/${shift.id}/expenses`,
      role: "attendant",
    },
    {
      label: "Non-fuel sales",
      hint: "Lubricants, coolant — anything no meter counts",
      route: `#/shifts/${shift.id}/non-fuel-sales`,
      role: "attendant",
    },
    {
      label: "Credit sales",
      hint: "Udhaar issued. A receipt is mandatory",
      route: `#/shifts/${shift.id}/credit-sales`,
      role: "attendant",
    },
    {
      label: "Credit repayments",
      hint: "A customer settling an old bill",
      route: `#/shifts/${shift.id}/credit-repayments`,
      role: "attendant",
    },
    {
      label: "Bank deposits",
      hint: "Cash taken out of the locker to the bank",
      route: `#/shifts/${shift.id}/bank-deposits`,
      role: "manager",
    },
  ].filter((entry) => satisfies(me.role, entry.role));

  render(
    container,
    el("div", { className: "stack" }, [
      shift.status !== "open"
        ? el("div", { className: "card" }, [
            el("p", {
              className: "t-caption",
              text: `This shift is ${shift.status}. The screens below still read, but nothing can be added.`,
            }),
          ])
        : null,
      el(
        "div",
        { className: "list" },
        entries.map((entry) =>
          el(
            "button",
            {
              className: "list-row",
              attrs: { type: "button" },
              on: { click: () => navigate(entry.route) },
              style: {
                width: "100%",
                background: "none",
                border: 0,
                textAlign: "left",
                font: "inherit",
                color: "inherit",
                cursor: "pointer",
              },
            },
            [
              el("div", { className: "list-row-main" }, [
                el("div", { className: "t-body", text: entry.label }),
                el("div", { className: "t-caption", text: entry.hint }),
              ]),
              el("div", { className: "t-body", text: "›", attrs: { "aria-hidden": "true" } }),
            ],
          ),
        ),
      ),
    ]),
  );
}
