/**
 * PipelineCard — a phone-friendly card for one in-flight pipeline item.
 *
 * Shows: issue title, repo + issue number, stage chips (work→test→review→merge),
 * machine name, and an overall status badge.
 */
import { cn } from '@/lib/utils'
import { type PipelineView, type PipelineStage } from '@/api/client'

// ── Stage display helpers ─────────────────────────────────────────────────────

/**
 * Map the internal stage name from pipeline.py to the user-facing label.
 * Work Order matches the TUI: Work → Test → Review → Merge.
 *
 * Exported so `SessionCard` can reuse the same vocabulary for the
 * assignment-type chip (#1276) rather than inventing new labels — the
 * `stage`/`assignment.type` value ("work"/"smoke"/"review"/"merge"/...)
 * falls back to the raw string for anything not in this map (e.g. "fix",
 * "plan").
 */
export const STAGE_LABEL: Record<string, string> = {
  coding: 'work',
  smoke:  'test',
  review: 'review',
  merge:  'merge',
}

/**
 * Fine-grained `current_stage` values that indicate the pipeline is currently
 * running (i.e. a subprocess is in progress — no human action required yet).
 */
const RUNNING_STAGES = new Set([
  'coding',
  'review_running',
  'smoke_running',
  'merging',
])

/**
 * Fine-grained `current_stage` values that are terminal failures or need
 * human attention before the pipeline can advance.
 */
const FAILED_STAGES = new Set([
  'failed',
  'review_failed',
  'smoke_failed',
])

/** Return Tailwind classes for a single stage chip. */
function stageChipClass(stage: PipelineStage, currentStage: string): string {
  const base = 'rounded px-1.5 py-0.5 text-xs font-medium'

  if (stage.is_current) {
    // Active stage — colour by failure vs running
    if (FAILED_STAGES.has(currentStage)) {
      return cn(base, 'bg-destructive text-destructive-foreground')
    }
    return cn(base, 'bg-primary text-primary-foreground')
  }

  switch (stage.status) {
    case 'completed':
      return cn(base, 'bg-green-700 text-white')
    case 'skipped':
      return cn(base, 'border border-border text-muted-foreground opacity-40')
    default:
      // "waiting"
      return cn(base, 'border border-border text-muted-foreground')
  }
}

// ── Overall status badge ──────────────────────────────────────────────────────

interface StatusInfo {
  label: string
  className: string
}

/** Map fine-grained current_stage to a human badge + colour. */
function stageStatusInfo(currentStage: string): StatusInfo {
  if (RUNNING_STAGES.has(currentStage)) {
    return { label: 'running', className: 'bg-primary text-primary-foreground' }
  }
  if (FAILED_STAGES.has(currentStage)) {
    return { label: 'failed', className: 'bg-destructive text-destructive-foreground' }
  }
  switch (currentStage) {
    case 'merged':
      return { label: 'merged', className: 'bg-green-700 text-white' }
    case 'merge_ready':
      return { label: 'mergeable', className: 'bg-yellow-600 text-black' }
    case 'review_done':
      return { label: 'review ✓', className: 'bg-yellow-600 text-black' }
    case 'smoke_passed':
      return { label: 'test ✓', className: 'bg-yellow-600 text-black' }
    case 'done':
      return { label: 'work done', className: 'bg-yellow-600 text-black' }
    default:
      return { label: currentStage.replace(/_/g, ' '), className: 'bg-secondary text-secondary-foreground' }
  }
}

// ── Component ─────────────────────────────────────────────────────────────────

export interface PipelineCardProps {
  view: PipelineView
  onClick: () => void
}

export function PipelineCard({ view, onClick }: PipelineCardProps) {
  const { label: statusLabel, className: statusClass } = stageStatusInfo(view.current_stage)

  return (
    <button
      type="button"
      onClick={onClick}
      className="w-full rounded-lg border border-border bg-card p-4 text-left shadow-sm transition-colors active:bg-secondary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
    >
      {/* Top row: issue title + status badge */}
      <div className="flex items-start justify-between gap-3">
        <p className="flex-1 truncate text-sm font-medium text-card-foreground">
          {view.issue_title}
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

      {/* Second row: repo#N + machine */}
      <p className="mt-1 text-xs text-muted-foreground">
        {view.repo_name} <span className="font-mono">#{view.issue_number}</span>
        {' · '}
        {view.machine_name}
      </p>

      {/* Stage chips */}
      <div className="mt-3 flex flex-wrap gap-1.5">
        {view.stages.map((stage) => (
          <span
            key={stage.name}
            className={stageChipClass(stage, view.current_stage)}
          >
            {STAGE_LABEL[stage.name] ?? stage.name}
          </span>
        ))}
      </div>

      {/* Review findings pending indicator */}
      {view.review_findings_pending && (
        <p className="mt-2 text-xs text-yellow-500">⚠ Review findings not yet posted</p>
      )}
    </button>
  )
}
