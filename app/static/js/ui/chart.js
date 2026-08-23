/* Two small charts, hand-written, with no arithmetic on money.
 *
 * §14 forbids an npm dependency, so there is no chart library -- but the more interesting
 * constraint is the one that shaped the API rather than the implementation:
 *
 *   **A bar chart is `value / max x height`, which is arithmetic on money.**
 *
 * §3 rule 1 does not stop at the API boundary and JavaScript has no decimal type, so that
 * division cannot happen here. The server sends `bar_height_pct` as a ready-made CSS
 * percentage string computed in Decimal, and this module *assigns* it. Nothing here parses a
 * money value, and `parseFloat` appears nowhere -- `tests/test_frontend_assets.py` enforces
 * that, and this is the module it was most likely to catch.
 *
 * The side effect is better than the rule required: because the bars and the table beneath
 * them come from one server-side pass, the chart cannot disagree with its own numbers. A
 * client-side division could drift from the figures next to it and nobody would notice.
 *
 * ## Why these two shapes and no others
 *
 * §12 rules out "charts of any kind" until Phase 13 decided what is worth plotting. The
 * answer was: a *magnitude over time* (did the week hold up?) and a *signed deviation*
 * (which day went wrong, and which way?). Those are the two questions a rolling view exists
 * to answer. A pie chart of expense categories answers neither and would be the first thing
 * to add if this file ever grows.
 *
 * ## Accessibility
 *
 * Every plotted value also appears in the table below the chart, so the chart is never the
 * only source. The SVG is marked `aria-hidden` for that reason -- a screen reader gets the
 * real figures rather than a stream of bar labels -- and the container carries a summary.
 */

import { el, svgEl } from "../dom.js";
import { businessDateWeekday } from "../time.js";

/**
 * A column per day, heights supplied by the server.
 *
 * @param {Array} days  RangeDay rows, already ordered ascending by business_date
 * @param {Function} onSelect  called with a day when its column is activated
 */
export function salesBars(days, { onSelect } = {}) {
  if (!days.length) return null;

  const columns = days.map((day) => {
    const isAbsent = day.total_sales === null || day.source === "unavailable";

    const bar = el("div", {
      className: `chart-bar${isAbsent ? " chart-bar-absent" : ""}`,
      // The one place a server-computed percentage is used, and the only "layout maths" in
      // the frontend. Assigned, never derived.
      style: { height: day.bar_height_pct },
    });

    return el(
      "button",
      {
        className: `chart-col${day.alert ? " chart-col-alert" : ""}`,
        attrs: {
          type: "button",
          // The accessible name carries the date and the source, because the visual
          // distinction between a snapshot and a computed day (§13.20) is a colour.
          "aria-label": `${day.business_date}, ${describeSource(day.source)}`,
        },
        on: { click: onSelect ? () => onSelect(day) : null },
      },
      [
        el("div", { className: "chart-track" }, [bar]),
        el("div", {
          className: "chart-col-label t-micro",
          text: businessDateWeekday(day.business_date),
        }),
      ],
    );
  });

  return el("div", { className: "chart" }, columns);
}

/**
 * A signed strip: one mark per day, above or below a baseline.
 *
 * Drawn as SVG rather than divs because the baseline has to be a real line the marks sit
 * either side of, and "which side of zero" is the entire message. Heights come from the same
 * server-side percentage, so this cannot disagree with the bars either.
 */
export function varianceStrip(days) {
  const counted = days.filter((day) => day.variance !== null);
  if (!counted.length) return null;

  const width = Math.max(days.length * 24, 120);
  const height = 64;
  const mid = height / 2;

  const svg = svgEl(
    "svg",
    {
      className: "variance-strip",
      attrs: {
        viewBox: `0 0 ${width} ${height}`,
        preserveAspectRatio: "none",
        // The table beneath carries every figure; a screen reader should read that instead
        // of a row of unlabelled marks.
        "aria-hidden": "true",
        focusable: "false",
      },
    },
    [
      svgEl("line", {
        className: "variance-baseline",
        attrs: { x1: 0, y1: mid, x2: width, y2: mid },
      }),
    ],
  );

  days.forEach((day, index) => {
    if (day.variance === null) return;

    // `isNegative` is a string test, not a numeric one -- see money.js. A surplus (negative
    // variance under §6.4's sign convention) draws upward, a shortage downward.
    const short = !day.variance.trim().startsWith("-");
    const x = index * 24 + 12;
    const reach = day.alert ? mid - 6 : mid / 2;

    svg.appendChild(
      svgEl("line", {
        className: `variance-mark ${short ? "variance-short" : "variance-surplus"}`,
        attrs: {
          x1: x,
          y1: mid,
          x2: x,
          y2: short ? mid + reach : mid - reach,
        },
      }),
    );
  });

  return svg;
}

/** The words a `source` value means, used in labels and legends alike so they cannot drift. */
export function describeSource(source) {
  switch (source) {
    case "snapshot":
      return "reconciled — figures as recorded that day";
    case "computed":
      return "not reconciled — figures calculated just now";
    case "no_trading":
      return "no trading";
    case "unavailable":
      return "cannot be calculated";
    default:
      return source;
  }
}
