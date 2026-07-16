/**
 * TerminalComingSoon — placeholder for the `/terminal/:sessionId` route.
 *
 * #1067 surfaces live coord-* sessions on Home (SessionCard) as an entry
 * point into a take-over view, but the issue explicitly scopes the actual
 * interactive pane (xterm.js + the `/ws/terminal/{id}` PTY bridge) out to a
 * later issue in the epic: "an entry point to open the terminal (the actual
 * pane is a later issue in this epic)." This stub keeps the entry point
 * navigable without shipping that pane here.
 */
import { useNavigate, useParams } from 'react-router-dom'

export default function TerminalComingSoon() {
  const { sessionId } = useParams<{ sessionId: string }>()
  const navigate = useNavigate()

  return (
    <div className="mx-auto max-w-lg px-4 py-6">
      <header className="mb-6 flex items-center gap-3">
        <button
          type="button"
          onClick={() => navigate(-1)}
          className="rounded p-1 text-muted-foreground hover:text-foreground"
          aria-label="Back"
        >
          ←
        </button>
        <h1 className="text-xl font-bold text-primary">coord</h1>
      </header>
      <div className="py-12 text-center">
        <p className="text-sm font-medium text-foreground">Terminal view coming soon</p>
        <p className="mt-1 text-xs text-muted-foreground">
          Taking over session <span className="font-mono">{sessionId}</span> from the phone
          isn&apos;t wired up yet — that lands in a follow-up issue.
        </p>
      </div>
    </div>
  )
}
