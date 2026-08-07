/**
 * The list panel's content for a rail-listed view that has a route (#1548
 * asks for "placeholders for the panels M-W5+ will add") but no panel yet —
 * Board, Machines, Merge queue, Milestones, Audit, Spend, Settings.
 *
 * These routes exist for exactly one reason: so a link to `/board` or
 * `/spend` is a real, bookmarkable, not-found-free address today, and
 * swapping this placeholder for the real panel later is a one-line change in
 * `App.tsx`, not a new route. The rail itself already marks these `'soon'`
 * (see `railItems.ts`) and won't navigate here on a click — this is what a
 * *typed or pasted* link to one of them lands on in the meantime.
 */
import { RAIL_ITEMS } from './railItems'
import type { ShellView } from './shellState'

export interface ComingSoonProps {
  view: ShellView
}

export function ComingSoon({ view }: ComingSoonProps) {
  const item = RAIL_ITEMS.find((i) => i.id === view)
  const Icon = item?.icon

  return (
    <div className="flex h-full flex-col items-center justify-center gap-3 px-6 py-12 text-center">
      {Icon && <Icon className="h-7 w-7 text-faint" aria-hidden="true" />}
      <p className="text-sm font-medium text-foreground">{item?.label ?? 'Coming soon'}</p>
      <p className="max-w-xs text-xs text-muted-foreground">
        {item?.hint ?? 'This panel is not built yet.'}
      </p>
    </div>
  )
}
