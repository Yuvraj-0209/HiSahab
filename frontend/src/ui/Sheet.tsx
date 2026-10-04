/* Bottom sheets: grabbable, throwable, interruptible (Phase 12's sheet, rebuilt for React).
 *
 * Every create-and-edit form in this app is a sheet, so this component decides whether the
 * whole thing feels native or like a web page with rounded corners. Four properties produce the
 * difference, and none of them is available from a CSS transition or a GSAP tween:
 *
 *   1. 1:1 tracking with the grab offset respected -- it stays glued to the finger from
 *      wherever it was grabbed.
 *   2. Interruptibility -- it can be caught mid-flight (opening, closing, settling) and
 *      redirected, because the spring carries its live value and velocity through every retarget.
 *      Reopening a sheet that is still on its way out simply turns it round.
 *   3. Velocity hand-off and momentum projection -- at release it continues at the finger's
 *      speed, and where the flick was *going* decides dismiss-versus-settle.
 *   4. Rubber-banding above the open position.
 *
 * The spring owns the sheet's `transform` and the scrim's opacity -- one spring for both, so the
 * dimming tracks a drag continuously -- and GSAP never touches either (§14). The sheet is solid,
 * never blurred: a filter on something that moves re-rasterises every frame (§13.29).
 *
 * While a sheet is open the app behind it is `inert`: keyboard focus cannot wander underneath,
 * and a screen reader hears only the dialog.
 *
 * ## On a desktop it is a dialog, not a sheet (Phase 25 D6)
 *
 * A bottom sheet is a phone's shape: it rises from under the thumb. On a 1440x900 monitor the same
 * motion carried the panel ~550 px up from the screen edge in ~300 ms, and the owner read that as
 * a glitch -- correctly, since nothing a mouse did started at the bottom of the screen. So with a
 * fine pointer and room to spare, the same component presents as a centred dialog: it rises
 * 16 px, scales from 0.97 and fades in on a critically damped spring, with no overshoot and no
 * drag. Open and close are still retargets of one spring, so either interrupts the other.
 *
 * Three more things made opening feel rough, and are fixed here or beside it:
 *
 *   - the page jumped sideways: locking the scroll removed a classic scrollbar and the layout
 *     widened under the scrim. `html { scrollbar-gutter: stable }` (styles.css) keeps the width.
 *   - on a phone the keyboard rose mid-animation, because the first field was focused the moment
 *     the sheet mounted and the viewport resized under the spring. Focus now moves to the dialog
 *     itself, so a screen reader still lands in it, and a field is focused only on a desktop and
 *     only once the dialog has settled.
 */

import { type ReactNode, useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { XIcon } from "@phosphor-icons/react";
import { PRESETS, project, rubberband, Spring } from "../motion/spring";
import { shouldCommit, verticalDrag } from "../motion/gesture";

export interface SheetProps {
  open: boolean;
  onClose: () => void;
  title: string;
  /** A second line under the title: what this sheet is about, e.g. the nozzle or the customer. */
  subtitle?: string | undefined;
  children: ReactNode;
  /** Usually the submit button. Sits below the scrolling body, always reachable. */
  footer?: ReactNode;
}

/** Mounts while open, and stays mounted for its exit so the sheet can leave along the path it
 * came in by. The content seen during the exit is the last content shown while open, so a
 * parent may clear its own state the moment it asks to close. */
export function Sheet({ open, onClose, title, subtitle, children, footer }: SheetProps) {
  const [mounted, setMounted] = useState(open);
  const last = useRef({ title, subtitle, children, footer });
  if (open) last.current = { title, subtitle, children, footer };

  // Mounting is an effect of `open` arriving; unmounting is reported by the frame on exit.
  useEffect(() => {
    if (open) setMounted(true);
  }, [open]);

  const host = typeof document === "undefined" ? null : document.getElementById("layers");
  if (!mounted || !host) return null;

  return createPortal(
    <SheetFrame
      open={open}
      onClose={onClose}
      onExited={() => setMounted(false)}
      title={last.current.title}
      subtitle={last.current.subtitle}
      footer={last.current.footer}
    >
      {last.current.children}
    </SheetFrame>,
    host,
  );
}

interface FrameProps extends SheetProps {
  onExited: () => void;
}

/** Desktop presentation: a fine pointer and room for a centred panel. Decided once, at mount. */
const DIALOG_QUERY = "(min-width: 640px) and (pointer: fine)";
/** How far a dialog rises, and how much it grows, as it arrives. */
const DIALOG_RISE = 16;
const DIALOG_SCALE = 0.03;

function SheetFrame({ open, onClose, onExited, title, subtitle, children, footer }: FrameProps) {
  const sheetRef = useRef<HTMLDivElement>(null);
  const scrimRef = useRef<HTMLDivElement>(null);
  const bodyRef = useRef<HTMLDivElement>(null);
  const springRef = useRef<Spring | null>(null);
  const heightRef = useRef(0);
  const closingRef = useRef(false);
  const onExitedRef = useRef(onExited);
  onExitedRef.current = onExited;
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  const [dialog] = useState(() => typeof window !== "undefined" && typeof window.matchMedia === "function" && window.matchMedia(DIALOG_QUERY).matches);

  // Built before paint, so the first frame the browser draws is the panel at its starting point
  // rather than a flash of it fully open.
  useLayoutEffect(() => {
    const sheet = sheetRef.current;
    const scrim = scrimRef.current;
    const body = bodyRef.current;
    if (!sheet || !scrim || !body) return;

    if (dialog) {
      // The spring runs from 1 (away) to 0 (here); every visual follows that one number.
      const spring = new Spring({
        ...PRESETS.ui,
        value: 1,
        onChange: (away) => {
          sheet.style.transform = `translate3d(0, ${away * DIALOG_RISE}px, 0) scale(${1 - away * DIALOG_SCALE})`;
          sheet.style.opacity = String(1 - away);
          scrim.style.opacity = String(1 - away);
        },
        onRest: (away) => {
          if (closingRef.current && away >= 0.999) onExitedRef.current();
          // Settled open: now a field may take focus without anything moving under it.
          if (!closingRef.current && away <= 0.001 && document.activeElement === sheet) {
            body.querySelector<HTMLElement>("input, select, textarea")?.focus({ preventScroll: true });
          }
        },
      });
      springRef.current = spring;
      spring.set(1);
      spring.to(0);
      return () => spring.stop();
    }

    heightRef.current = sheet.offsetHeight;
    const spring = new Spring({
      ...PRESETS.sheet,
      value: heightRef.current,
      onChange: (y) => {
        sheet.style.transform = `translate3d(0, ${y}px, 0)`;
        const progress = Math.min(1, Math.max(0, y / Math.max(1, heightRef.current)));
        scrim.style.opacity = String(1 - progress);
      },
      onRest: (y) => {
        if (closingRef.current && y >= heightRef.current - 0.5) onExitedRef.current();
      },
    });
    springRef.current = spring;
    spring.set(heightRef.current);
    // Enter. Retargeted from off-screen, so the sheet is grabbable from its first frame.
    spring.to(0);

    // Content can change height while open (a chooser that reveals a form); the travel
    // distance and the dismiss threshold follow it.
    const resize = new ResizeObserver(() => {
      heightRef.current = sheet.offsetHeight;
    });
    resize.observe(sheet);

    let dragStart = 0;
    const detachDrag = verticalDrag(sheet, {
      canStart: (event) => {
        const target = event.target as Element;
        // Inside a scrolled body the gesture belongs to the content, not the sheet.
        if (body.contains(target) && body.scrollTop > 0) return false;
        // Never hijack a press on a control.
        return !target.closest("button, input, select, textarea, a");
      },
      onStart: () => {
        // Take over from wherever the sheet visibly is, mid-flight or at rest.
        spring.stop();
        dragStart = spring.value;
      },
      onMove: (offset) => {
        let y = dragStart + offset;
        if (y < 0) y = -rubberband(-y, heightRef.current);
        spring.track(y, 0);
      },
      onEnd: ({ offset, velocity }) => {
        const projected = dragStart + offset + project(velocity);
        if (shouldCommit(projected, velocity, heightRef.current)) {
          closingRef.current = true;
          spring.to(heightRef.current, { velocity });
          onCloseRef.current();
        } else {
          spring.to(0, { velocity });
        }
      },
    });

    return () => {
      resize.disconnect();
      detachDrag();
      spring.stop();
    };
  }, [dialog]);

  // Open and close are retargets of the same spring, so either can interrupt the other.
  useEffect(() => {
    const spring = springRef.current;
    if (!spring) return;
    if (open) {
      closingRef.current = false;
      spring.to(0);
    } else {
      closingRef.current = true;
      spring.to(dialog ? 1 : heightRef.current);
    }
  }, [open, dialog]);

  // Modal behaviour: the app behind goes inert, the page stops scrolling, Escape closes, and
  // focus returns to whatever opened the sheet.
  useEffect(() => {
    const root = document.getElementById("root");
    const opener = document.activeElement as HTMLElement | null;
    root?.setAttribute("inert", "");
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";

    // Into the dialog, not onto its first field: focusing a field on a phone raises the keyboard
    // mid-animation and resizes the viewport under the spring. A desktop dialog focuses its first
    // field once it has settled (above).
    sheetRef.current?.focus({ preventScroll: true });

    function handleKey(event: KeyboardEvent) {
      if (event.key === "Escape") onCloseRef.current();
    }
    document.addEventListener("keydown", handleKey);

    return () => {
      document.removeEventListener("keydown", handleKey);
      root?.removeAttribute("inert");
      document.body.style.overflow = previousOverflow;
      opener?.focus?.({ preventScroll: true });
    };
  }, []);

  return (
    <div className={`fixed inset-0 z-40 ${dialog ? "flex items-center justify-center p-6" : ""}`}>
      <div
        ref={scrimRef}
        className="scrim absolute inset-0"
        style={{ opacity: 0 }}
        aria-hidden="true"
        onClick={() => onCloseRef.current()}
      />
      <div
        ref={sheetRef}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        tabIndex={-1}
        className={`sheet flex flex-col outline-none ${
          dialog
            ? "relative max-h-[85dvh] w-full max-w-xl rounded-[var(--radius-sheet)]"
            : "absolute inset-x-0 bottom-0 mx-auto max-h-[92dvh] max-w-xl touch-pan-y rounded-t-[var(--radius-sheet)] pb-[env(safe-area-inset-bottom)] sm:bottom-4 sm:rounded-[var(--radius-sheet)]"
        }`}
        style={dialog ? { opacity: 0, transform: `translate3d(0, ${DIALOG_RISE}px, 0) scale(${1 - DIALOG_SCALE})` } : { transform: "translate3d(0, 100vh, 0)" }}
      >
        {dialog ? (
          <div className="pt-4" aria-hidden="true" />
        ) : (
          <div className="flex justify-center pt-2.5 pb-1" aria-hidden="true">
            <div className="h-1.5 w-10 rounded-full bg-hairline-strong" />
          </div>
        )}
        <div className="flex items-start gap-3 px-5 pt-1 pb-3">
          <div className="min-w-0 grow">
            <h2 className="truncate text-[1.1875rem] font-semibold tracking-[-0.015em] text-ink">{title}</h2>
            {subtitle ? <p className="mt-0.5 truncate text-[0.8125rem] text-ink-muted">{subtitle}</p> : null}
          </div>
          <button
            type="button"
            aria-label="Close"
            className="pressable -mr-1 rounded-full bg-surface-sunken p-2 text-ink-muted"
            onClick={() => onCloseRef.current()}
          >
            <XIcon size={16} weight="bold" aria-hidden />
          </button>
        </div>
        <div ref={bodyRef} className="min-h-0 grow overflow-y-auto overscroll-contain px-5 pb-4">
          {children}
        </div>
        {footer ? <div className="border-t border-hairline px-5 pt-3 pb-4">{footer}</div> : null}
      </div>
    </div>
  );
}
