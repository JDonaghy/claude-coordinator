/**
 * Home — the main screen of the Phone Control Center.
 *
 * A "Live sessions" section (#1067) surfaces live coord-* interactive
 * sessions (#1066's `GET /api/sessions`) at the TOP of the screen, above
 * everything else — these are in-progress human-attended sessions the phone
 * can take over via the `/terminal/:sessionId` view, so they get first
 * billing over the pipeline list below.
 *
 * Below that: in-flight pipeline items as tappable cards, auto-refreshing
 * every 4 s. Two filter tabs:
 *   Active    — everything currently in the pipeline (not yet merged)
 *   Needs me  — items with at least one available gate action (need human input)
 *
 * Pull-to-refresh: drag down from the top (when already scrolled to top) to
 * trigger an immediate refetch.
 */
import { useCallback, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { fetchPipeline, fetchSessions, type PipelineView, type SessionInfo } from '@/api/client'
import { PipelineCard } from '@/components/PipelineCard'
import { SessionCard } from '@/components/SessionCard'

// ── Filter logic ──────────────────────────────────────────────────────────────

type FilterTab = 'active' | 'needs-me'

/**
 * "Active": items that haven't finished (current_stage !== "merged").
 * This keeps the list focused on in-flight work without cluttering with history.
 */
function isActive(view: PipelineView): boolean {
  return view.current_stage !== 'merged'
}

/**
 * "Needs me": items where at least one human gate action is available.
 * E.g.: work done (needs test dispatch), review approved (needs merge queue),
 * smoke passed (needs merge queue), merge ready (needs merge), failures (need retry/fix).
 */
function needsMe(view: PipelineView): boolean {
  return view.available_gates.length > 0
}

const FILTER_FNS: Record<FilterTab, (v: PipelineView) => boolean> = {
  'active': isActive,
  'needs-me': needsMe,
}

/**
 * `current_stage` values that PipelineCard's `stageStatusInfo` renders as a
 * "done-ish" yellow badge — the work itself has finished, but the item is
 * waiting on a downstream gate (review/smoke/merge) rather than actively
 * running. These are the low-signal cards #1218 asked to group + collapse
 * out of the primary "Active" view by default (e.g. a merged item's
 * penultimate "work done" state sitting near the top of the list).
 */
const DONE_ISH_STAGES = new Set(['done', 'review_done', 'smoke_passed', 'merge_ready'])

function isDoneIsh(view: PipelineView): boolean {
  return DONE_ISH_STAGES.has(view.current_stage)
}

/**
 * `current_stage` values that indicate a subprocess is actively running —
 * mirrors PipelineCard's RUNNING_STAGES so the "in progress" bucket can
 * prioritize live work after needs-me items.
 */
const RUNNING_STAGES = new Set(['coding', 'review_running', 'smoke_running', 'merging'])

/**
 * Split the "Active" tab's items into "in progress" (shown expanded) and
 * "done-ish" (collapsed by default, see DONE_ISH_STAGES). Within "in
 * progress", items needing human input (available_gates.length > 0) sort
 * first, then actively-running items, with the incoming order preserved as
 * the tiebreak (stable sort). "Done-ish" items are returned unsorted here —
 * the caller sorts them by finished_at descending.
 */
function groupActiveItems(views: PipelineView[]): { inProgress: PipelineView[]; done: PipelineView[] } {
  const inProgress: PipelineView[] = []
  const done: PipelineView[] = []
  for (const view of views) {
    if (isDoneIsh(view)) {
      done.push(view)
    } else {
      inProgress.push(view)
    }
  }

  const priority = (view: PipelineView): number => {
    if (needsMe(view)) return 0
    if (RUNNING_STAGES.has(view.current_stage)) return 1
    return 2
  }
  inProgress.sort((a, b) => priority(a) - priority(b))

  // Most recently finished first; items with no finished_at yet sort last.
  done.sort((a, b) => (b.finished_at ?? -Infinity) - (a.finished_at ?? -Infinity))

  return { inProgress, done }
}

// ── Pull-to-refresh ───────────────────────────────────────────────────────────

const PTR_THRESHOLD_PX = 80

interface UsePullToRefreshOptions {
  onRefresh: () => void
}

function usePullToRefresh({ onRefresh }: UsePullToRefreshOptions) {
  const startYRef = useRef(0)

  const onTouchStart = useCallback((e: React.TouchEvent) => {
    startYRef.current = e.touches[0].clientY
  }, [])

  const onTouchEnd = useCallback(
    (e: React.TouchEvent) => {
      const dy = e.changedTouches[0].clientY - startYRef.current
      if (dy > PTR_THRESHOLD_PX && window.scrollY === 0) {
        onRefresh()
      }
    },
    [onRefresh],
  )

  return { onTouchStart, onTouchEnd }
}

// ── Filter tab component ──────────────────────────────────────────────────────

interface FilterTabsProps {
  active: FilterTab
  counts: Record<FilterTab, number>
  onChange: (tab: FilterTab) => void
}

function FilterTabs({ active: activeTab, counts, onChange }: FilterTabsProps) {
  const tabs: Array<{ id: FilterTab; label: string }> = [
    { id: 'active', label: 'Active' },
    { id: 'needs-me', label: 'Needs me' },
  ]

  return (
    <div className="flex gap-2" role="tablist" aria-label="Pipeline filters">
      {tabs.map(({ id, label }) => {
        const isSelected = id === activeTab
        const count = counts[id]
        return (
          <button
            key={id}
            type="button"
            role="tab"
            aria-selected={isSelected}
            onClick={() => onChange(id)}
            className={
              isSelected
                ? 'flex items-center gap-1.5 rounded-full bg-primary px-3 py-1 text-xs font-semibold text-primary-foreground'
                : 'flex items-center gap-1.5 rounded-full border border-border px-3 py-1 text-xs font-medium text-muted-foreground'
            }
          >
            {label}
            {count > 0 && (
              <span
                className={
                  isSelected
                    ? 'rounded-full bg-primary-foreground/20 px-1.5 text-xs font-mono'
                    : 'rounded-full bg-secondary px-1.5 text-xs font-mono'
                }
              >
                {count}
              </span>
            )}
          </button>
        )
      })}
    </div>
  )
}

// ── Live sessions section ───────────────────────────────────────────────────────

interface LiveSessionsProps {
  sessions: SessionInfo[]
  onSelect: (sessionId: string) => void
}

function LiveSessions({ sessions, onSelect }: LiveSessionsProps) {
  if (sessions.length === 0) return null

  return (
    <section aria-label="Live sessions" className="mb-5 space-y-3">
      <h2 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
        Live sessions
      </h2>
      {sessions.map((session) => (
        <SessionCard
          key={session.session_id}
          session={session}
          onClick={() => onSelect(session.session_id)}
        />
      ))}
    </section>
  )
}

// ── Done section (collapsed by default) ─────────────────────────────────────────

interface DoneSectionProps {
  items: PipelineView[]
  onSelect: (assignmentId: string) => void
}

/**
 * Collapsed-by-default "Work done" section for the Active tab's done-ish
 * items (#1218) — keeps merged/finished noise out of the primary view while
 * staying one tap away. Sorted by finished_at descending by the caller
 * (groupActiveItems), so expanding always shows most-recently-finished first.
 */
function DoneSection({ items, onSelect }: DoneSectionProps) {
  const [expanded, setExpanded] = useState(false)

  if (items.length === 0) return null

  return (
    <section aria-label="Work done" className="mt-3">
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        aria-expanded={expanded}
        className="flex w-full items-center justify-between rounded-lg border border-border bg-card px-4 py-2 text-left text-xs font-medium text-muted-foreground"
      >
        <span>Work done ({items.length})</span>
        <span aria-hidden="true">{expanded ? '▾' : '▸'}</span>
      </button>
      {expanded && (
        <div className="mt-3 space-y-3">
          {items.map((view) => (
            <PipelineCard
              key={view.assignment_id}
              view={view}
              onClick={() => onSelect(view.assignment_id)}
            />
          ))}
        </div>
      )}
    </section>
  )
}

// ── Home screen ───────────────────────────────────────────────────────────────

export default function Home() {
  const navigate = useNavigate()
  const [filterTab, setFilterTab] = useState<FilterTab>('active')

  const { data, isLoading, isError, isFetching, dataUpdatedAt, refetch } = useQuery({
    queryKey: ['pipeline'],
    queryFn: fetchPipeline,
    refetchInterval: 4_000,
  })

  const { data: sessions } = useQuery({
    queryKey: ['sessions'],
    queryFn: fetchSessions,
    refetchInterval: 4_000,
  })

  const handleRefresh = useCallback(() => {
    void refetch()
  }, [refetch])

  const { onTouchStart, onTouchEnd } = usePullToRefresh({ onRefresh: handleRefresh })

  const filtered = data ? data.filter(FILTER_FNS[filterTab]) : []

  // Active tab only: group into "in progress" (expanded, needs-me-first) and
  // "done-ish" (collapsed "Work done" section, sorted by recency) — #1218.
  // The "Needs me" tab stays a flat list; its semantics are unchanged.
  const { inProgress, done } =
    filterTab === 'active' ? groupActiveItems(filtered) : { inProgress: filtered, done: [] }

  const counts: Record<FilterTab, number> = {
    'active': data ? data.filter(isActive).length : 0,
    'needs-me': data ? data.filter(needsMe).length : 0,
  }

  const updatedLabel = dataUpdatedAt
    ? new Date(dataUpdatedAt).toLocaleTimeString()
    : null

  return (
    <div
      className="mx-auto max-w-lg px-4 py-6"
      onTouchStart={onTouchStart}
      onTouchEnd={onTouchEnd}
    >
      {/* Header */}
      <header className="mb-5 flex items-center justify-between">
        <div>
          <h1 className="text-xl font-bold text-primary">coord</h1>
          <p className="text-xs text-muted-foreground">pipeline</p>
        </div>
        <div className="flex items-center gap-2">
          {isFetching && !isLoading && (
            <span className="h-2 w-2 animate-pulse rounded-full bg-primary" aria-label="Refreshing" />
          )}
          {updatedLabel && (
            <span className="text-xs text-muted-foreground">{updatedLabel}</span>
          )}
        </div>
      </header>

      {/* Live sessions — surfaced above everything else (#1067) */}
      <LiveSessions
        sessions={sessions ?? []}
        onSelect={(sessionId) => navigate(`/terminal/${sessionId}`)}
      />

      {/* Filter tabs */}
      {data && (
        <div className="mb-4">
          <FilterTabs active={filterTab} counts={counts} onChange={setFilterTab} />
        </div>
      )}

      {/* Loading state */}
      {isLoading && (
        <p className="py-12 text-center text-sm text-muted-foreground">
          Loading pipeline…
        </p>
      )}

      {/* Error state */}
      {isError && (
        <div className="rounded-lg border border-destructive/40 bg-destructive/10 px-4 py-6 text-center">
          <p className="text-sm text-destructive">Failed to load pipeline</p>
          <p className="mt-1 text-xs text-muted-foreground">
            Is the dashboard server running?
          </p>
          <button
            type="button"
            onClick={handleRefresh}
            className="mt-3 rounded-full border border-border px-3 py-1 text-xs text-muted-foreground"
          >
            Retry
          </button>
        </div>
      )}

      {/* Card list */}
      {data && filtered.length > 0 && (
        <>
          <section className="space-y-3" aria-label={filterTab === 'active' ? 'Active items' : 'Items needing attention'}>
            {inProgress.map((view) => (
              <PipelineCard
                key={view.assignment_id}
                view={view}
                onClick={() => navigate(`/detail/${view.assignment_id}`)}
              />
            ))}
          </section>
          <DoneSection items={done} onSelect={(assignmentId) => navigate(`/detail/${assignmentId}`)} />
        </>
      )}

      {/* Empty state */}
      {data && filtered.length === 0 && (
        <div className="py-12 text-center">
          {filterTab === 'needs-me' ? (
            <>
              <p className="text-sm font-medium text-foreground">All clear</p>
              <p className="mt-1 text-xs text-muted-foreground">
                No items are waiting for your input right now.
              </p>
            </>
          ) : (
            <>
              <p className="text-sm font-medium text-foreground">No active pipeline items</p>
              <p className="mt-1 text-xs text-muted-foreground">
                Dispatch a worker to get started.
              </p>
            </>
          )}
        </div>
      )}
    </div>
  )
}
