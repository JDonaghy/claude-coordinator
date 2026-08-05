/**
 * Honest connection-state indicator (#1549) — live / reconnecting /
 * stale-since-<time>. Never silently shows stale data as if it were live:
 * the moment the SSE stream drops, this says so.
 */
import { useConnectionStatus } from '@/realtime/RealtimeProvider'
import { cn } from '@/lib/utils'

function formatTime(epochMs: number): string {
  return new Date(epochMs).toLocaleTimeString()
}

export function ConnectionBadge() {
  const { state, lastLiveAt } = useConnectionStatus()

  let label: string
  let dotClass: string
  switch (state) {
    case 'live':
      label = 'Live'
      dotClass = 'bg-green-500'
      break
    case 'reconnecting':
      label = 'Reconnecting…'
      dotClass = 'bg-yellow-500 animate-pulse'
      break
    case 'disconnected':
      label = lastLiveAt ? `Stale since ${formatTime(lastLiveAt)}` : 'Disconnected'
      dotClass = 'bg-destructive'
      break
    case 'connecting':
    default:
      label = 'Connecting…'
      dotClass = 'bg-muted-foreground animate-pulse'
      break
  }

  return (
    <span
      className="flex items-center gap-1.5 text-xs text-muted-foreground"
      role="status"
      aria-label={`Connection: ${label}`}
    >
      <span className={cn('h-2 w-2 shrink-0 rounded-full', dotClass)} aria-hidden="true" />
      {label}
    </span>
  )
}
