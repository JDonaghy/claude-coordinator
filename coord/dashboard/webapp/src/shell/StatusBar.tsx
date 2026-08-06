/**
 * Shell status bar (#1547) — the mock's `.statusbar`, promoted from inside
 * `.main` to a full-width foot row so it is one element at every breakpoint
 * instead of appearing and disappearing with the detail column.
 *
 * It is also now the app's *only* `ConnectionBadge`. It used to be rendered
 * once per screen (Home and Detail each had their own); with wide showing both
 * at once that would be two live-region `role="status"` nodes saying the same
 * thing, which is noise for a screen reader and an ambiguous locator for a
 * test.
 *
 * Everything else here is mono, because everything else here is a value the
 * machine owns. Nothing is invented: counts come from the same React-Query
 * caches the panels read, and a figure with no data source (the mock's
 * "$14.82 today") is simply absent until Spend lands.
 */
import { ConnectionBadge } from '@/components/ConnectionBadge'

export interface StatusBarProps {
  /** Pipeline items not yet merged. */
  inFlight?: number
  /** Live interactive sessions. */
  sessions?: number
  /** Distinct machines those sessions are on. */
  machines?: string[]
}

export function StatusBar({ inFlight, sessions, machines }: StatusBarProps) {
  return (
    <footer
      aria-label="Status"
      className="flex items-center gap-4 overflow-x-auto whitespace-nowrap border-t border-border bg-surface px-3 py-1.5 font-mono text-[.69rem] text-faint md:px-5"
    >
      <ConnectionBadge />
      {inFlight != null && <span>{inFlight} in flight</span>}
      {sessions != null && (
        <span>
          {sessions} {sessions === 1 ? 'session' : 'sessions'}
        </span>
      )}
      {machines && machines.length > 0 && (
        <span className="hidden md:inline">{machines.join(' · ')}</span>
      )}
    </footer>
  )
}
