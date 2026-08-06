/**
 * What the wide layout's detail column shows before anything is selected
 * (#1547).
 *
 * Only ever rendered on wide/medium: on narrow the shell mounts the list
 * *instead of* the detail, so there is no "nothing selected" state to fill.
 * It doubles as the shell's one piece of keyboard-affordance signage — F6 is
 * discoverable nowhere else, and this is the emptiest surface in the app.
 */
import { MousePointerClick } from 'lucide-react'

export function EmptyDetail() {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-3 px-6 py-12 text-center">
      <MousePointerClick className="h-7 w-7 text-faint" aria-hidden="true" />
      <p className="text-sm font-medium text-foreground">Nothing selected</p>
      <p className="max-w-xs text-xs text-muted-foreground">
        Pick an item from the list to see its stages, worker and actions here.
      </p>
      <p className="text-xs text-faint">
        <kbd className="rounded border border-line-strong px-1 font-mono text-[.7rem]">F6</kbd>{' '}
        cycles rail → list → detail
      </p>
    </div>
  )
}
