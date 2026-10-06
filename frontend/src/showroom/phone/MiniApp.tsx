/* The app's chrome, drawn inside the front door's phone (Phase 28 D4): a status bar, the screen
 * title, the content, and the six tabs with the current one lit.
 *
 * The tab bar here is not the app's `.tabbar`: that class carries a unique view-transition name
 * and a backdrop blur, and a second copy of either on the page would break the first. It reuses
 * only the tab pill's colour and the tab list itself (app/tabs.ts, already in the first paint).
 */

import type { ReactNode } from "react";
import { TABS, type TabId } from "../../app/tabs";

export function MiniApp({ title, subtitle, tab, children }: { title: string; subtitle: string; tab: TabId; children: ReactNode }) {
  return (
    <div className="flex h-full flex-col bg-ground">
      <div className="flex h-12 shrink-0 items-end justify-between px-7 pb-1 text-[0.8125rem] font-semibold text-ink">
        <span className="tabular">10:14</span>
        <span className="flex items-center gap-1" aria-hidden="true">
          <span className="h-2.5 w-1 rounded-full bg-ink" />
          <span className="h-3 w-1 rounded-full bg-ink" />
          <span className="h-3.5 w-1 rounded-full bg-ink" />
          <span className="ml-1.5 h-3 w-6 rounded-[4px] border border-ink-muted p-px">
            <span className="block h-full w-4/5 rounded-[2px] bg-ink" />
          </span>
        </span>
      </div>

      <div className="shrink-0 px-4 pt-3 pb-3">
        <p className="text-[1.0625rem] font-semibold text-ink">{title}</p>
        <p className="text-[0.8125rem] text-ink-muted">{subtitle}</p>
      </div>

      <div className="flex min-h-0 grow flex-col gap-3 overflow-hidden px-4">{children}</div>

      <div className="grid shrink-0 grid-cols-6 border-t border-hairline bg-ground px-1 pt-1.5 pb-6">
        {TABS.map(({ id, label, Icon }) => (
          <span key={id} className={`flex h-12 flex-col items-center justify-center gap-0.5 text-[0.6875rem] ${id === tab ? "text-accent" : "text-ink-muted"}`}>
            <span className={`grid h-7 w-12 place-items-center rounded-full ${id === tab ? "tab-pill" : ""}`}>
              <Icon size={20} weight={id === tab ? "fill" : "regular"} />
            </span>
            {label}
          </span>
        ))}
      </div>
    </div>
  );
}
