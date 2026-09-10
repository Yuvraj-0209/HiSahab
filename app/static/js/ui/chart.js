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

// --- Phase 19: the shapes a dashboard needs -----------------------------------
//
// This module's header predicted these: "A pie chart of expense categories answers neither
// and would be the first thing to add if this file ever grows." Phase 19 is where it grew,
// and the reason the prediction was safe is that the rule did not change -- a slice is
// `value / total`, which is arithmetic on money, so the server sends `share_pct` ready-made
// exactly as it sends `bar_height_pct`, and nothing here divides anything.
//
// ## What IS computed here, and why it is not money
//
// Turning "36.40%" into an arc needs a running offset and some trigonometry. That is
// arithmetic on a *percentage the server already computed* -- a geometry value, not a money
// value -- and it cannot produce a wrong rupee figure because no rupee figure passes through
// it. The distinction is the same one `bar_height_pct` relies on: the division happened in
// `Decimal`, on the server, once.

/** Parse a server percentage string into a number for GEOMETRY only.
 *
 * Deliberately not in money.js, and deliberately not `parseFloat` (which
 * `tests/test_frontend_assets.py` bans outright). The input is never money: it is a
 * percentage the server produced by dividing in `Decimal`, and the output is only ever used
 * to place a point on a circle.
 *
 * `null` means "unknowable" (§6.8's rule reaching the geometry) and yields null rather than
 * zero, so a caller must decide what an unknown slice looks like instead of silently
 * drawing one at zero.
 */
function pctToNumber(value) {
  if (typeof value !== "string") return null;
  const digits = value.trim().replace("%", "");
  if (!/^-?\d+(\.\d+)?$/.test(digits)) return null;
  return Number(digits);
}

const TAU = Math.PI * 2;

/**
 * A donut chart. One arc per slice, sized by the server's `share_pct`.
 *
 * A donut rather than a pie, for one honest reason: the hole holds the total, so the figure
 * and its decomposition are read in one place and cannot be presented apart from each other.
 *
 * @param {Array}  slices  [{label, share_pct, colorIndex, amount}]
 * @param {object} options
 * @param {string} [options.centerLabel]  the caption inside the hole
 * @param {string} [options.centerValue]  the total, already formatted by money.js
 */
export function donut(slices, { centerLabel = "", centerValue = "" } = {}) {
  const drawable = slices
    .map((slice) => ({ ...slice, pct: pctToNumber(slice.share_pct) }))
    .filter((slice) => slice.pct !== null && slice.pct > 0);

  if (!drawable.length) return null;

  const size = 168;
  const mid = size / 2;
  const outer = 76;
  const inner = 50;

  const svg = svgEl("svg", {
    className: "donut",
    attrs: {
      viewBox: `0 0 ${size} ${size}`,
      // Every slice is also a row in the legend beneath, so a screen reader gets the real
      // figures rather than a stream of arc descriptions -- `varianceStrip`'s contract.
      "aria-hidden": "true",
      focusable: "false",
    },
  });

  let cursor = -0.25; // start at twelve o'clock rather than three
  for (const slice of drawable) {
    const sweep = slice.pct / 100;
    // A slice that rounds to the whole circle has no arc to draw -- two identical endpoints
    // produce an invisible path -- so it is drawn as a ring instead.
    if (sweep >= 0.9999) {
      svg.appendChild(
        svgEl("circle", {
          className: `donut-slice cat-${slice.colorIndex}`,
          attrs: { cx: mid, cy: mid, r: (outer + inner) / 2, fill: "none", "stroke-width": outer - inner },
        }),
      );
      break;
    }
    svg.appendChild(
      svgEl("path", {
        className: `donut-slice cat-${slice.colorIndex}`,
        attrs: { d: arcPath(mid, cursor, cursor + sweep, outer, inner) },
      }),
    );
    cursor += sweep;
  }

  const center = el("div", { className: "donut-center" }, [
    centerValue ? el("div", { className: "t-headline t-numeric", text: centerValue }) : null,
    centerLabel ? el("div", { className: "t-micro", text: centerLabel }) : null,
  ]);

  return el("div", { className: "donut-wrap" }, [svg, center]);
}

/** One donut segment as an SVG path: out along the start edge, round, back along the end. */
function arcPath(mid, startTurns, endTurns, outer, inner) {
  const a0 = startTurns * TAU;
  const a1 = endTurns * TAU;
  const large = endTurns - startTurns > 0.5 ? 1 : 0;

  const x = (radius, angle) => (mid + radius * Math.cos(angle)).toFixed(3);
  const y = (radius, angle) => (mid + radius * Math.sin(angle)).toFixed(3);

  return [
    `M ${x(outer, a0)} ${y(outer, a0)}`,
    `A ${outer} ${outer} 0 ${large} 1 ${x(outer, a1)} ${y(outer, a1)}`,
    `L ${x(inner, a1)} ${y(inner, a1)}`,
    `A ${inner} ${inner} 0 ${large} 0 ${x(inner, a0)} ${y(inner, a0)}`,
    "Z",
  ].join(" ");
}

/**
 * Horizontal share bars: a row per category, width from the server's `share_pct`.
 *
 * Used where a donut would be unreadable -- more than about six categories, or values so
 * lopsided that the small slices become slivers. Same input shape as `donut`, so a caller
 * can swap one for the other without reshaping its data.
 *
 * @param {Array} rows  [{label, share_pct, colorIndex, value}] -- `value` pre-formatted
 */
export function shareBars(rows) {
  if (!rows.length) return null;

  return el(
    "div",
    { className: "sharebars" },
    rows.map((row) =>
      el("div", { className: "sharebar" }, [
        el("div", { className: "sharebar-head" }, [
          el("span", { className: "t-caption truncate", text: row.label }),
          el("span", { className: "t-caption t-numeric", text: row.value }),
        ]),
        el("div", { className: "sharebar-track" }, [
          el("div", {
            className: `sharebar-fill cat-${row.colorIndex}`,
            // Assigned, never derived -- the rule this whole module is built around.
            style: { width: row.share_pct ?? "0%" },
          }),
        ]),
        el("div", {
          className: "t-micro",
          // An unknowable share says so rather than rendering as an empty bar, which would
          // read as zero (§6.8).
          text: row.share_pct ?? "share unknown",
        }),
      ]),
    ),
  );
}
