/* The chrome's title and actions, filled in by whichever screen is showing.
 *
 * A screen describes itself by rendering <ScreenTitle> and <ScreenActions>, which portal into
 * slots in the shell's header. Portals rather than state lifted into the shell: the actions stay
 * inside the screen's own React tree (its state, its handlers, its query cache), and nothing has
 * to re-render the shell to change a button.
 *
 * Wayfinding (§16): the document title answers "where am I?" in the tab strip and browser
 * history, not only on screen.
 */

import { createContext, type ReactNode, useContext, useEffect } from "react";
import { createPortal } from "react-dom";

export interface ChromeSlots {
  title: HTMLElement | null;
  actions: HTMLElement | null;
}

export const ChromeContext = createContext<ChromeSlots>({ title: null, actions: null });

/**
 * The screen's name, in the chrome.
 *
 * `large` (Phase 24 D4) is for the six tab roots: the name is also shown big at the top of the
 * screen and collapses into the chrome as you scroll, the way a phone's own apps do it. That is
 * a CSS scroll-driven animation (`.large-title`, `.compact-title` in styles.css) with no
 * JavaScript at all. Where the browser has no scroll timelines, or the phone asks for reduced
 * motion, the large copy is not shown and the chrome looks exactly as it did before.
 *
 * The chrome's copy stays the `<h1>` either way; the large one is a visual duplicate and is
 * hidden from screen readers, so the page never has two headings saying the same thing.
 */
export function ScreenTitle({ title, subtitle, large = false }: { title: string; subtitle?: string | undefined; large?: boolean }) {
  const { title: slot } = useContext(ChromeContext);
  useEffect(() => {
    document.title = `${title} · HiSahab`;
  }, [title]);
  const compact = slot
    ? createPortal(
        <div className={`compact-title min-w-0 ${large ? "is-collapsible" : ""}`}>
          <h1 className="truncate text-lead leading-tight font-semibold tracking-[-0.015em] text-ink">{title}</h1>
          {/* The landing place for a shared-element transition (navigation.ts): a day row's date
           * flies in here as the day screen's subtitle. Named only for the length of one transition. */}
          {subtitle ? (
            <p data-shared-target className="truncate text-footnote leading-tight text-ink-muted">
              {subtitle}
            </p>
          ) : null}
        </div>,
        slot,
      )
    : null;
  if (!large) return compact;
  return (
    <>
      {compact}
      <div className="large-title mb-5" aria-hidden="true">
        <p className="text-title text-ink">{title}</p>
        {/* Always a line, so a subtitle arriving with the data does not push the screen down. */}
        <p className="mt-0.5 min-h-[1.375rem] truncate text-body text-ink-muted">{subtitle ?? ""}</p>
      </div>
    </>
  );
}

export function ScreenActions({ children }: { children: ReactNode }) {
  const { actions: slot } = useContext(ChromeContext);
  if (!slot) return null;
  return createPortal(<div className="flex items-center gap-1.5">{children}</div>, slot);
}
