/**
 * Sessions — the live interactive sessions as a first-class list panel
 * (#1547).
 *
 * No new content: this is the exact `SessionCard` list Home already surfaces
 * above the pipeline (#1067), given its own rail entry so the shell has a
 * second *real* view. That matters for more than symmetry — with one ready
 * view the narrow bottom row degenerates into a single tab, which can't
 * demonstrate (or regression-test) that the rail works as a phone nav at all.
 *
 * Home keeps its own "Live sessions" section unchanged: "attention before
 * detail" says the pipeline view should still open with what's live.
 */
import { useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'

import { fetchSessions } from '@/api/client'
import { SessionCard } from '@/components/SessionCard'
import { PanelHeader } from '@/components/PanelHeader'

export default function SessionsList() {
  const navigate = useNavigate()
  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: ['sessions'],
    queryFn: fetchSessions,
  })

  const sessions = data ?? []

  return (
    <div className="mx-auto w-full max-w-lg px-4 py-4">
      <PanelHeader title="Sessions" count={sessions.length} countLabel="live" />

      {isLoading && (
        <p className="py-12 text-center text-sm text-muted-foreground">Loading sessions…</p>
      )}

      {isError && (
        <div className="rounded-lg border border-destructive/40 bg-destructive/10 px-4 py-6 text-center">
          <p className="text-sm text-destructive">Failed to load sessions</p>
          <button
            type="button"
            onClick={() => void refetch()}
            className="mt-3 rounded-full border border-border px-3 py-1 text-xs text-muted-foreground"
          >
            Retry
          </button>
        </div>
      )}

      {data && sessions.length > 0 && (
        <section className="space-y-3" aria-label="Live sessions">
          {sessions.map((session) => (
            <SessionCard
              key={session.session_id}
              session={session}
              onClick={() => navigate(`/terminal/${session.session_id}`)}
            />
          ))}
        </section>
      )}

      {data && sessions.length === 0 && (
        <div className="py-12 text-center">
          <p className="text-sm font-medium text-foreground">No live sessions</p>
          <p className="mt-1 text-xs text-muted-foreground">
            Interactive coord sessions show up here while they're running.
          </p>
        </div>
      )}
    </div>
  )
}
