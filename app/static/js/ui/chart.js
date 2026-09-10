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
import { businessDate, businessDateShort, businessDateWeekday } from "../time.js";
import { format, gapLabel } from "../money.js";
import { PRESETS, Spring } from "../motion/spring.js";

/**
 * A column per day, heights supplied by the server, **paged ten at a time**.
 *
 * ## Why this grew a carousel (Phase 19b)
 *
 * Written in Phase 13 for a *seven-day* window, as a flexbox where every column is
 * `flex: 1 1 0`. Phase 19 started handing it up to 366 days, and the arithmetic is brutal:
 * at 30 days each bar is a few pixels wide, at 90 it is a hairline. The chart stopped
 * carrying information at exactly the window sizes the Summary tab exists to enable, which
 * is how the owner reported it -- *"in a 30 day summary everything looked very crammed"*.
 *
 * So the days are cut into pages of `perPage`, and the arrows turn them. **At or below
 * `perPage` days there is one page, no arrows and no pager**, which is why the Phase 13
 * week view (`screens/reports.js`) calls this unchanged and looks exactly as it did.
 *
 * ## What is NOT recomputed per page, and this is the important one
 *
 * `bar_height_pct` stays scaled to the tallest day in the **whole window** -- the server
 * computed it that way (`reporting.range_report`) and it must stay that way. Re-scaling per
 * page would be `value / max` in JavaScript, which §14 forbids outright, and it would also
 * lie: every page's tallest bar would reach 100%, so a quiet week would look identical to a
 * record one. Paging changes what is *visible*, never what anything *means*.
 *
 * ## The motion
 *
 * One `transform` on the rail per page change, driven by a `Spring` (§13.29: continuous
 * motion belongs to `transform`/`opacity` on the compositor, and is never redrawn to say so).
 * The depth -- outgoing page shrinking and fading, incoming growing in -- is a CSS transition
 * keyed off one `data-active` attribute write per turn, not a per-frame style write.
 *
 * `Spring.to()` handles `prefers-reduced-motion` internally by arriving immediately and still
 * firing `onChange`/`onRest`, so there is deliberately no branch for it here.
 *
 * @param {Array}    days       RangeDay rows, ascending by business_date
 * @param {Function} onSelect   called with a day when its column is opened
 * @param {number}   perPage    columns per page; ≤ this many days renders a plain chart
 */
export function salesBars(days, { onSelect, perPage = 10 } = {}) {
  if (!days.length) return null;

  const pages = [];
  for (let index = 0; index < days.length; index += perPage) {
    pages.push(days.slice(index, index + perPage));
  }

  const detail = el("div", { className: "chart-detail" });
  // The bar the panel describes when nothing is being hovered. Hover is transient; this is
  // the sticky one, and it is what a touch device sets by tapping.
  let selected = null;

  const showDetail = (day) => renderDetail(detail, day, onSelect);
  const revertDetail = () => renderDetail(detail, selected, onSelect);

  // A phone reports `hover: none`. Without this guard a tap fires a synthetic pointerenter
  // and the panel would flicker to the tapped bar and back on every touch.
  const canHover =
    typeof matchMedia === "function" && matchMedia("(hover: hover)").matches;

  const columnFor = (day) => {
    const isAbsent = day.total_sales === null || day.source === "unavailable";

    const bar = el("div", {
      className: `chart-bar${isAbsent ? " chart-bar-absent" : ""}`,
      // The one place a server-computed percentage is used, and the only "layout maths" in
      // the frontend. Assigned, never derived.
      style: { height: day.bar_height_pct },
    });

    const label = businessDateShort(day.business_date);

    const column = el(
      "button",
      {
        className: `chart-col${day.alert ? " chart-col-alert" : ""}`,
        attrs: {
          type: "button",
          // The accessible name carries the date, the weekday and the source, because the
          // visual distinction between a snapshot and a computed day (§13.20) is a colour,
          // and because the visible label had to drop the weekday for space.
          "aria-label": `${businessDateWeekday(day.business_date)} ${day.business_date}, ${describeSource(day.source)}`,
        },
        on: {
          /* Two-step, and the same code path on both kinds of device.
           *
           * A phone has no hover, so a bar that only navigated would make the summary panel
           * unreachable on the device the register is actually entered on. First activation
           * selects and describes; the panel's own button opens the day. On a desktop the
           * hover has already filled the panel, so the first click lands on an already
           * described bar -- which reads as confirming, not as an extra step. */
          click: () => {
            selected = day;
            markSelected(column);
            showDetail(day);
          },
          // Keyboard focus describes without selecting, so tabbing across the chart reads
          // it out without committing to anything.
          focus: () => showDetail(day),
          blur: revertDetail,
          pointerenter: canHover ? () => showDetail(day) : null,
        },
      },
      [
        el("div", { className: "chart-track" }, [bar]),
        el("div", { className: "chart-col-label" }, [
          el("div", { className: "chart-col-day", text: label.day }),
          el("div", { className: "chart-col-month t-micro", text: label.month }),
        ]),
      ],
    );

    return column;
  };

  /* A short final page is PADDED with empty columns rather than left to stretch.
   *
   * Each page is exactly the frame's width and each column is `flex: 1 1 0`, so a 31-day
   * window -- three full pages and one leftover day -- would render that day as a single bar
   * spanning the whole chart. It would read as an enormous sales day, which is the precise
   * failure this project exists to avoid: a plausible-looking figure that is completely
   * wrong. The spacers keep every bar the same width on every page. */
  const pageNodes = pages.map((page, index) => {
    const columns = page.map(columnFor);
    for (let pad = page.length; pad < perPage; pad += 1) {
      columns.push(el("div", { className: "chart-col-spacer", attrs: { "aria-hidden": "true" } }));
    }
    return el(
      "div",
      { className: "chart-page", attrs: { "data-active": String(index === 0) } },
      columns,
    );
  });

  const rail = el("div", { className: "chart-rail" }, pageNodes);
  const frame = el("div", { className: "chart-frame" }, [rail]);

  // Seed the panel so it carries its own instructions from the first paint rather than
  // being an unexplained empty box until somebody happens to touch a bar.
  revertDetail();

  // Leaving the whole chart returns the panel to whatever was last chosen, so the reader
  // does not lose the row they were studying by moving the mouse away.
  if (canHover) frame.addEventListener("pointerleave", revertDetail);

  // One page: no arrows, no pager, no spring. Phase 13's week view, byte-for-byte.
  if (pages.length === 1) {
    return el("div", { className: "chart-shell" }, [frame, detail]);
  }

  let page = 0;
  const pager = el("div", { className: "chart-pager t-micro" });
  const dots = el("div", { className: "chart-dots" });

  /* The rail is positioned in PAGE UNITS, not pixels, and the CSS turns that into a
   * translation of `-100% * page`. Pixels would need a measured frame width, which is not
   * known until layout and changes on resize -- a class of bug this avoids entirely by
   * never knowing the width. */
  const spring = new Spring({
    ...PRESETS.move,
    onChange: (value) => {
      rail.style.transform = `translate3d(${value * -100}%, 0, 0)`;
    },
  });

  function paint() {
    pageNodes.forEach((node, index) => {
      node.setAttribute("data-active", String(index === page));
    });
    const first = page * perPage + 1;
    const last = Math.min((page + 1) * perPage, days.length);
    pager.textContent = `${first}–${last} of ${days.length}`;
    dots.replaceChildren(
      ...pages.map((_, index) =>
        el("span", {
          className: `chart-dot${index === page ? " is-current" : ""}`,
          attrs: { "aria-hidden": "true" },
        }),
      ),
    );
  }

  /* Turn one page in the direction pressed.
   *
   * Wrapping is where this gets subtle. Going from the last page to the first is an index
   * jump of -(n-1), and springing that would send the rail travelling backwards across the
   * entire strip -- the opposite direction to the arrow the user pressed, and a very long
   * way. So a wrap re-seats the rail silently on the far side (`set`, no animation) and then
   * springs exactly one page, which means the motion always agrees with the button. */
  function turn(direction) {
    const next = (page + direction + pages.length) % pages.length;
    const wrapped = next !== page + direction;

    if (wrapped) spring.set(next - direction);
    page = next;
    paint();
    spring.to(page);
  }

  const prev = arrowButton("prev", "Previous days", () => turn(-1));
  const next = arrowButton("next", "Next days", () => turn(1));
  frame.append(prev, next);

  paint();
  return el("div", { className: "chart-shell" }, [
    frame,
    el("div", { className: "chart-pager-row" }, [pager, dots]),
    detail,
  ]);
}

function arrowButton(kind, label, onClick) {
  return el("button", {
    className: `chart-arrow is-${kind}`,
    text: kind === "prev" ? "‹" : "›",
    attrs: { type: "button", "aria-label": label },
    on: { click: onClick },
  });
}

/** Mark one column as the selected one, clearing any previous. */
function markSelected(column) {
  const root = column.closest(".chart-shell");
  if (!root) return;
  for (const node of root.querySelectorAll(".chart-col.is-selected")) {
    node.classList.remove("is-selected");
  }
  column.classList.add("is-selected");
}

/**
 * Fill the detail panel for one day.
 *
 * Every figure is the string the server sent, formatted by money.js and never computed
 * (§14). `variance: null` is "not counted" -- which under §6.5's locker model is most days --
 * and is rendered as a word rather than as ₹0.00.
 */
function renderDetail(container, day, onSelect) {
  if (!day) {
    container.replaceChildren(
      el("div", {
        className: "t-caption",
        // The empty state has to teach the interaction, because on a phone there is no
        // hover to discover it by accident.
        text: "Tap a bar for that day's figures.",
      }),
    );
    return;
  }

  const variance = gapLabel(day.variance, { absent: "not counted" });

  container.replaceChildren(
    el("div", { className: "chart-detail-head" }, [
      el("div", { className: "col", style: { gap: "2px", minWidth: 0 } }, [
        el("div", {
          className: "t-body",
          text: `${businessDateWeekday(day.business_date)}, ${businessDate(day.business_date)}`,
        }),
        el("div", { className: "t-caption", text: describeSource(day.source) }),
      ]),
      el("div", { className: "col", style: { alignItems: "flex-end", gap: "2px" } }, [
        el("div", {
          className: "t-body t-numeric",
          text: format(day.total_sales, { absent: "—" }),
        }),
        el("div", {
          className: `t-caption t-numeric ${variance.className}`,
          text: variance.text,
        }),
      ]),
    ]),
    onSelect
      ? el("button", {
          className: "btn btn-plain",
          text: "Open this day",
          attrs: { type: "button" },
          on: { click: () => onSelect(day) },
        })
      : null,
  );
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
