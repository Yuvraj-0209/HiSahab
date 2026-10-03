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

export function ScreenTitle({ title, subtitle }: { title: string; subtitle?: string | undefined }) {
  const { title: slot } = useContext(ChromeContext);
  useEffect(() => {
    document.title = `${title} · HiSahab`;
  }, [title]);
  if (!slot) return null;
  return createPortal(
    <div className="min-w-0">
      <h1 className="truncate text-[1.0625rem] leading-tight font-semibold tracking-[-0.015em] text-ink">{title}</h1>
      {/* The landing place for a shared-element transition (navigation.ts): a day row's date
       * flies in here as the day screen's subtitle. Named only for the length of one transition. */}
      {subtitle ? (
        <p data-shared-target className="truncate text-[0.8125rem] leading-tight text-ink-muted">
          {subtitle}
        </p>
      ) : null}
    </div>,
    slot,
  );
}

export function ScreenActions({ children }: { children: ReactNode }) {
  const { actions: slot } = useContext(ChromeContext);
  if (!slot) return null;
  return createPortal(<div className="flex items-center gap-1.5">{children}</div>, slot);
}
