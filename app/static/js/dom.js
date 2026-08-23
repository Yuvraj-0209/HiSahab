/* DOM construction. The only way this application makes elements.
 *
 * **Nothing here touches innerHTML, and that is the point** (CLAUDE.md §14, §13.19).
 *
 * §13.19 records that the session token is readable by JavaScript, because a bearer-token API
 * requires it. That turns markup injection from a cosmetic bug into a session theft: a
 * customer name of `<img src=x onerror="...">` rendered through innerHTML would run with the
 * token in reach. Every value this app displays comes from a database somebody types into.
 *
 * `textContent` is immune by construction -- it cannot produce an element -- so routing every
 * node through here means the safe thing is also the shortest thing to write. That is the
 * only sort of security control that survives contact with a deadline.
 */

/**
 * Build an element.
 *
 * @param {string} tag
 * @param {object} [options]
 * @param {string} [options.className]
 * @param {*}      [options.text]      set via textContent, never parsed as markup
 * @param {object} [options.attrs]     null/undefined values are skipped entirely
 * @param {object} [options.on]        event listeners, {click: fn}
 * @param {object} [options.style]     inline styles, for values only JS can know
 * @param {Array}  [children]          falsy entries are skipped, so `cond && el(...)` works
 */
export function el(tag, options = {}, children = []) {
  const { className, text, attrs, on, style } = options;
  const node = document.createElement(tag);

  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);

  if (attrs) {
    for (const [key, value] of Object.entries(attrs)) {
      // Skipped rather than rendered as the string "null": an attribute that is absent and
      // one whose value is the word "null" behave very differently in CSS and ARIA.
      if (value === undefined || value === null || value === false) continue;
      node.setAttribute(key, value === true ? "" : String(value));
    }
  }

  if (on) {
    for (const [event, handler] of Object.entries(on)) {
      if (handler) node.addEventListener(event, handler);
    }
  }

  if (style) Object.assign(node.style, style);

  for (const child of children) {
    if (!child) continue;
    node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
  }

  return node;
}

const SVG_NS = "http://www.w3.org/2000/svg";

/**
 * The SVG twin of `el`, and it has to exist separately rather than being a flag on it.
 *
 * `document.createElement("rect")` does not fail -- it silently returns an *HTML* unknown
 * element, which lays out as nothing inside an `<svg>` and renders a blank box with no error
 * anywhere. That is the same shape of failure §13.18 says the frontend is most exposed to:
 * invisible in the test suite, total on screen.
 *
 * Deliberately no `text` shortcut and no `innerHTML`. Chart labels go through `textContent`
 * like everything else, so the rule in `test_no_module_builds_markup_from_a_string` holds in
 * the one module most likely to reach for markup (Phase 13).
 *
 * @param {string} tag             e.g. "svg", "g", "rect", "line", "text"
 * @param {object} [options.attrs] null/undefined skipped, same semantics as `el`
 */
export function svgEl(tag, options = {}, children = []) {
  const { className, text, attrs, on, style } = options;
  const node = document.createElementNS(SVG_NS, tag);

  // `className` on an SVG element is a read-only SVGAnimatedString, so it must be set as an
  // attribute. Assigning it the way `el` does silently does nothing.
  if (className) node.setAttribute("class", className);
  if (text !== undefined && text !== null) node.textContent = String(text);

  if (attrs) {
    for (const [key, value] of Object.entries(attrs)) {
      if (value === undefined || value === null || value === false) continue;
      node.setAttribute(key, value === true ? "" : String(value));
    }
  }

  if (on) {
    for (const [event, handler] of Object.entries(on)) {
      if (handler) node.addEventListener(event, handler);
    }
  }

  if (style) Object.assign(node.style, style);

  for (const child of children) {
    if (!child) continue;
    node.appendChild(child);
  }

  return node;
}

/** Replace a container's children. */
export function render(container, ...nodes) {
  container.replaceChildren(...nodes.filter(Boolean));
}

/** A document fragment, for returning several siblings from one function. */
export function fragment(...nodes) {
  const frag = document.createDocumentFragment();
  for (const node of nodes) if (node) frag.appendChild(node);
  return frag;
}

/** A button that responds on press.
 *
 * §1 of the design vocabulary: feedback belongs on pointer-*down*, not on release. The scale
 * change lives in app.css's `.btn:active`, which fires the instant the pointer goes down --
 * waiting for `click` is a ~100ms lie that reads as lag even when the work is instant.
 */
export function button(label, { onClick, className = "btn", disabled, type = "button", reason } = {}) {
  const node = el("button", {
    className,
    text: label,
    attrs: { type, disabled: disabled || undefined },
    on: { click: onClick },
  });

  // A disabled control must say why (§16's wayfinding rule). `title` alone is invisible on a
  // phone, so callers pass `reason` and get a line under the button instead -- see the
  // shortfall screen, where booking is disabled until cash has been declared.
  if (disabled && reason) {
    return fragment(node, el("p", { className: "t-caption btn-reason", text: reason }));
  }
  return node;
}

/** A labelled row for a value, the shape most of this app's reading surfaces take. */
export function row(label, value, { className = "", valueClass = "" } = {}) {
  return el("div", { className: `list-row ${className}` }, [
    el("div", { className: "list-row-main" }, [
      el("span", { className: "t-body", text: label }),
    ]),
    el("div", {
      className: `list-row-value t-body t-numeric ${valueClass}`,
      text: value,
    }),
  ]);
}

/** A status pill. `kind` maps to the .pill-* classes in app.css. */
export function pill(text, kind = "neutral") {
  return el("span", { className: `pill pill-${kind}`, text });
}

/** Empty-state placeholder. Never leave a blank region -- §16: every screen answers
 * "what's here?", and a screen showing nothing at all cannot. */
export function empty(message) {
  return el("div", { className: "empty t-body", text: message });
}

/** The notice shown when a capped list did not return everything.
 *
 * Several endpoints return `truncated: true` rather than a cursor (§9's considered
 * exceptions). Showing a partial list silently is how a reader trusts a wrong total, so this
 * is never optional when the flag is set.
 */
export function truncationNotice(count) {
  return el("div", {
    className: "truncation-notice t-caption",
    text: `Showing the first ${count}. There are more rows than this view lists.`,
  });
}
