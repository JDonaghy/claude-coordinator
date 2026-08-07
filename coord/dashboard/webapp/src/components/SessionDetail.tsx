/**
 * SessionDetail — the detail slot for one live session, at `/sessions/:id`
 * (#1548).
 *
 * Mirrors `Detail`'s shape (panel content, not a screen; a back control; a
 * not-found state for a stale id) so a session row in `SessionsList` behaves
 * like a pipeline row in `Home` — tap the row, the shell fills the detail
 * column (wide) or drills in (narrow), and the address bar carries exactly
 * what's selected. The one action here is "Take over", which hands off to
 * the full-screen `/terminal/:sessionId` PTY view (#1065) — that route stays
 * deliberately outside the shell (see `App.tsx`), so this is a small
 * summary card plus a door to it, not a second terminal implementation.
 */
import { useNavigate, useParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'

import { fetchSessions, type SessionInfo } from '@/api/client'
import { paths } from '@/routes/paths'
import { cn } from '@/lib/utils'

const detailShellClass = 'mx-auto w-full max-w-3xl px-4 py-5 md:px-6'

const ASSIGNMENT_TYPE_LABEL: Record<string, string> = {
  smoke: 'test',
  review: 'review',
  'conflict-fix': 'merge',
}

function stageLabel(stage: string): string {
  return ASSIGNMENT_TYPE_LABEL[stage] ?? stage
}

function statusInfo(session: SessionInfo): { label: string; className: string } {
  if (session.pane_dead) {
    return { label: 'ended', className: 'bg-secondary text-secondary-foreground' }
  }
  if (session.attached) {
    return { label: 'attached', className: 'bg-yellow-600 text-black' }
  }
  return { label: 'live', className: 'bg-primary text-primary-foreground' }
}

export default function SessionDetail() {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()

  const { data: sessions, isLoading, isError } = useQuery({
    queryKey: ['sessions'],
    queryFn: fetchSessions,
  })

  const session = sessions?.find((s) => s.session_id === id) ?? null

  if (isLoading) {
    return (
      <div className={detailShellClass}>
        <p className="py-12 text-center text-sm text-muted-foreground">Loading…</p>
      </div>
    )
  }

  if (isError) {
    return (
      <div className={detailShellClass}>
        <p className="py-12 text-center text-sm text-destructive">Failed to load sessions</p>
      </div>
    )
  }

  if (!session) {
    return (
      <div className={detailShellClass}>
        <header className="mb-6 flex items-center gap-3">
          <button
            type="button"
            onClick={() => navigate(-1)}
            className="rounded p-1 text-muted-foreground hover:text-foreground"
            aria-label="Back"
          >
            ←
          </button>
          <h1 className="text-step-1 font-semibold text-foreground">Not found</h1>
        </header>
        <p className="text-sm text-muted-foreground">
          Session <span className="font-mono">{id}</span> is no longer live.
        </p>
      </div>
    )
  }

  const { label: statusLabel, className: statusClass } = statusInfo(session)
  const title = session.issue_title ?? session.session_name

  return (
    <div className={detailShellClass}>
      <header className="mb-5">
        <div className="flex items-start gap-2">
          <button
            type="button"
            onClick={() => navigate(-1)}
            aria-label="Back"
            className="-ml-1 shrink-0 rounded p-1 text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            ←
          </button>
          <div className="min-w-0 flex-1">
            <p className="text-xs text-muted-foreground">
              {session.repo && <span className="font-mono">{session.repo}</span>}
              {session.issue !== null && <span className="font-mono"> #{session.issue}</span>}
              {(session.repo || session.issue !== null) && session.machine && ' · '}
              {session.machine && <span className="font-mono">{session.machine}</span>}
            </p>
            <h2 className="mt-0.5 text-base font-semibold text-foreground">{title}</h2>
          </div>
          <span className={cn('shrink-0 rounded-full px-2.5 py-0.5 text-xs font-semibold', statusClass)}>
            {statusLabel}
          </span>
        </div>

        {session.stage && (
          <div className="mt-3 flex flex-wrap gap-1.5">
            <span className="rounded border border-border px-1.5 py-0.5 text-xs font-medium text-muted-foreground">
              {stageLabel(session.stage)}
            </span>
          </div>
        )}
      </header>

      {!session.pane_dead && (
        <button
          type="button"
          onClick={() => navigate(paths.terminal(session.session_id))}
          className="w-full rounded-lg bg-primary px-4 py-2.5 text-sm font-medium text-primary-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          Take over
        </button>
      )}
    </div>
  )
}
