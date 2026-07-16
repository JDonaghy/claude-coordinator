/**
 * SessionCard — a phone-friendly card for one live coord-* interactive
 * tmux session (#1066's `GET /api/sessions`). Tapping navigates to the
 * `/terminal/:sessionId` take-over view (#1065's `/ws/terminal/{id}` bridge).
 */
import { cn } from '@/lib/utils'
import { type SessionInfo } from '@/api/client'

interface StatusInfo {
  label: string
  className: string
}

/** Badge reflecting whether the session is live/attached/ended. */
function sessionStatusInfo(session: SessionInfo): StatusInfo {
  if (session.pane_dead) {
    return { label: 'ended', className: 'bg-secondary text-secondary-foreground' }
  }
  if (session.attached) {
    return { label: 'attached', className: 'bg-yellow-600 text-black' }
  }
  return { label: 'live', className: 'bg-primary text-primary-foreground' }
}

export interface SessionCardProps {
  session: SessionInfo
  onClick: () => void
}

export function SessionCard({ session, onClick }: SessionCardProps) {
  const { label: statusLabel, className: statusClass } = sessionStatusInfo(session)
  const title = session.issue_title ?? session.session_name

  return (
    <button
      type="button"
      onClick={onClick}
      className="w-full rounded-lg border border-border bg-card p-4 text-left shadow-sm transition-colors active:bg-secondary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
    >
      {/* Top row: title + status badge */}
      <div className="flex items-start justify-between gap-3">
        <p className="flex-1 truncate text-sm font-medium text-card-foreground">
          {title}
        </p>
        <span
          className={cn(
            'shrink-0 rounded-full px-2 py-0.5 text-xs font-semibold',
            statusClass,
          )}
        >
          {statusLabel}
        </span>
      </div>

      {/* Second row: repo#N + machine, when the session maps to a tracked assignment */}
      {(session.repo || session.machine) && (
        <p className="mt-1 text-xs text-muted-foreground">
          {session.repo}
          {session.issue !== null && (
            <span className="font-mono"> #{session.issue}</span>
          )}
          {session.repo && session.machine && ' · '}
          {session.machine}
        </p>
      )}
    </button>
  )
}
