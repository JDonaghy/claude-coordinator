/**
 * Detail — per-item detail screen for a pipeline assignment.
 *
 * Shows the assignment header, test-gate actions, review section (findings +
 * verdict), merge section (gate status + merge / force), optional smoke/unstick
 * actions, and a collapsible diff viewer with syntax highlighting.
 *
 * All write operations go through POST /api/pipeline/action; the pipeline data
 * is read from the same ['pipeline'] React-Query cache used by the Home screen
 * so updates are reflected immediately on both screens.
 */
import { useCallback, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  fetchPipeline,
  fetchDiff,
  pipelineAction,
  type PipelineView,
  type PipelineActionRequest,
} from '@/api/client'
import { cn } from '@/lib/utils'

// ── Toast ─────────────────────────────────────────────────────────────────────

interface Toast {
  id: string
  message: string
  type: 'success' | 'error'
}

interface ToastListProps {
  toasts: Toast[]
}

function ToastList({ toasts }: ToastListProps) {
  if (toasts.length === 0) return null
  return (
    <div
      role="status"
      aria-live="polite"
      className="fixed bottom-6 left-0 right-0 z-50 flex flex-col items-center gap-2 px-4 pointer-events-none"
    >
      {toasts.map((t) => (
        <div
          key={t.id}
          className={cn(
            'w-full max-w-sm rounded-lg px-4 py-3 text-sm font-medium shadow-lg',
            t.type === 'success'
              ? 'bg-green-700 text-white'
              : 'bg-destructive text-destructive-foreground',
          )}
        >
          {t.message}
        </div>
      ))}
    </div>
  )
}

// ── Fail dialog ───────────────────────────────────────────────────────────────

interface FailDialogProps {
  reason: string
  onReasonChange: (v: string) => void
  onConfirm: () => void
  onCancel: () => void
  disabled: boolean
}

function FailDialog({ reason, onReasonChange, onConfirm, onCancel, disabled }: FailDialogProps) {
  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label="Record test failure"
      className="fixed inset-0 z-40 flex items-end justify-center bg-black/60 px-4 pb-8"
    >
      <div className="w-full max-w-sm rounded-xl border border-border bg-card p-4 shadow-xl">
        <h2 className="mb-3 text-sm font-semibold text-card-foreground">Record test failure</h2>
        <label className="mb-1 block text-xs text-muted-foreground" htmlFor="fail-reason">
          Reason (optional)
        </label>
        <textarea
          id="fail-reason"
          value={reason}
          onChange={(e) => onReasonChange(e.target.value)}
          rows={3}
          placeholder="What failed?"
          className="mb-3 w-full resize-none rounded border border-border bg-background px-2 py-1.5 text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-ring"
        />
        <div className="flex gap-2">
          <button
            type="button"
            onClick={onCancel}
            disabled={disabled}
            className="flex-1 rounded-lg border border-border py-2 text-sm text-muted-foreground disabled:opacity-50"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={onConfirm}
            disabled={disabled}
            className="flex-1 rounded-lg bg-destructive py-2 text-sm font-medium text-destructive-foreground disabled:opacity-50"
          >
            {disabled ? 'Recording…' : 'Confirm Fail'}
          </button>
        </div>
      </div>
    </div>
  )
}

// ── Diff viewer ───────────────────────────────────────────────────────────────

interface DiffViewerProps {
  assignmentId: string
  expanded: boolean
  onToggle: () => void
}

function DiffViewer({ assignmentId, expanded, onToggle }: DiffViewerProps) {
  const { data, isLoading, isError } = useQuery({
    queryKey: ['diff', assignmentId],
    queryFn: () => fetchDiff(assignmentId),
    enabled: expanded,
    staleTime: 60_000,
  })

  return (
    <section className="mt-4" aria-label="Diff viewer">
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={expanded}
        className="flex w-full items-center justify-between rounded-lg border border-border bg-card px-4 py-3 text-sm font-medium text-card-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        <span>Diff</span>
        <span className="text-muted-foreground" aria-hidden="true">
          {expanded ? '▲' : '▼'}
        </span>
      </button>

      {expanded && (
        <div className="mt-1 rounded-lg border border-border bg-black/30 p-3">
          {isLoading && (
            <p className="text-xs text-muted-foreground">Loading diff…</p>
          )}
          {isError && (
            <p className="text-xs text-destructive">Failed to load diff</p>
          )}
          {data && (
            <>
              <pre className="overflow-x-auto text-xs leading-5" aria-label="Code diff">
                {data.diff.split('\n').map((line, i) => (
                  <span
                    key={i}
                    className={cn(
                      'block',
                      line.startsWith('+++') || line.startsWith('---')
                        ? 'text-muted-foreground'
                        : line.startsWith('+')
                          ? 'text-green-400'
                          : line.startsWith('-')
                            ? 'text-red-400'
                            : line.startsWith('@@')
                              ? 'text-blue-400'
                              : 'text-foreground',
                    )}
                  >
                    {line || ' '}
                  </span>
                ))}
              </pre>
              <p className="mt-2 text-right text-xs text-muted-foreground">
                source: {data.source}
              </p>
            </>
          )}
        </div>
      )}
    </section>
  )
}

// ── Stage display helpers (mirrors PipelineCard) ──────────────────────────────

const STAGE_LABEL: Record<string, string> = {
  coding: 'work',
  smoke: 'test',
  review: 'review',
  merge: 'merge',
}

const FAILED_STAGES = new Set(['failed', 'review_failed', 'smoke_failed'])

function stageStatus(currentStage: string): { label: string; className: string } {
  const RUNNING = new Set(['coding', 'review_running', 'smoke_running', 'merging'])
  if (RUNNING.has(currentStage)) {
    return { label: 'running', className: 'bg-primary text-primary-foreground' }
  }
  if (FAILED_STAGES.has(currentStage)) {
    return { label: 'failed', className: 'bg-destructive text-destructive-foreground' }
  }
  switch (currentStage) {
    case 'merged':      return { label: 'merged',     className: 'bg-green-700 text-white' }
    case 'merge_ready': return { label: 'mergeable',  className: 'bg-yellow-600 text-black' }
    case 'review_done': return { label: 'review ✓',   className: 'bg-yellow-600 text-black' }
    case 'smoke_passed':return { label: 'test ✓',     className: 'bg-yellow-600 text-black' }
    case 'done':        return { label: 'work done',  className: 'bg-yellow-600 text-black' }
    default:            return { label: currentStage.replace(/_/g, ' '), className: 'bg-secondary text-secondary-foreground' }
  }
}

// ── Main component ────────────────────────────────────────────────────────────

export default function Detail() {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const queryClient = useQueryClient()

  // Pipeline data — shared cache key with Home screen
  const { data: pipeline, isLoading, isError } = useQuery({
    queryKey: ['pipeline'],
    queryFn: fetchPipeline,
    refetchInterval: 4_000,
  })

  const view: PipelineView | null = pipeline?.find((v) => v.assignment_id === id) ?? null

  // UI state
  const [diffExpanded, setDiffExpanded] = useState(false)
  const [inFlight, setInFlight] = useState<string | null>(null)
  const [toasts, setToasts] = useState<Toast[]>([])
  const [failDialogOpen, setFailDialogOpen] = useState(false)
  const [failReason, setFailReason] = useState('')
  const [forceMode, setForceMode] = useState(false)

  // Toast helpers
  const addToast = useCallback((message: string, type: 'success' | 'error') => {
    const toastId = String(Date.now())
    setToasts((prev) => [...prev, { id: toastId, message, type }])
    setTimeout(() => {
      setToasts((prev) => prev.filter((t) => t.id !== toastId))
    }, 4_000)
  }, [])

  // Generic action handler — sets in-flight, calls API, shows toast, invalidates cache
  const doAction = useCallback(
    async (req: PipelineActionRequest, key: string, successMsg?: string) => {
      if (inFlight) return
      setInFlight(key)
      try {
        const result = await pipelineAction(req)
        if (result.ok) {
          addToast(successMsg ?? 'Done', 'success')
          void queryClient.invalidateQueries({ queryKey: ['pipeline'] })
        } else {
          addToast(result.error ?? 'Action failed', 'error')
        }
      } catch (e) {
        addToast(e instanceof Error ? e.message : 'Action failed', 'error')
      } finally {
        setInFlight(null)
      }
    },
    [inFlight, addToast, queryClient],
  )

  // Gate availability helper
  const hasGate = (action: string): boolean =>
    view?.available_gates.some((g) => g.action === action) ?? false

  // Shared button base class
  const btnBase =
    'rounded-lg px-4 py-2.5 text-sm font-medium disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring'

  // ── Loading / error / not-found ───────────────────────────────────────────

  if (isLoading) {
    return (
      <div className="mx-auto max-w-lg px-4 py-6">
        <p className="py-12 text-center text-sm text-muted-foreground">Loading…</p>
      </div>
    )
  }

  if (isError) {
    return (
      <div className="mx-auto max-w-lg px-4 py-6">
        <p className="py-12 text-center text-sm text-destructive">Failed to load pipeline</p>
      </div>
    )
  }

  if (!view) {
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
        <p className="text-sm text-muted-foreground">
          Assignment <span className="font-mono">{id}</span> not found in the pipeline.
        </p>
      </div>
    )
  }

  const { label: statusLabel, className: statusClass } = stageStatus(view.current_stage)

  return (
    <div className="mx-auto max-w-lg px-4 py-6 pb-24">
      <ToastList toasts={toasts} />

      {failDialogOpen && (
        <FailDialog
          reason={failReason}
          onReasonChange={setFailReason}
          onConfirm={() => {
            const req: PipelineActionRequest = {
              assignment_id: view.assignment_id,
              action: 'test-verdict',
              verdict: 'failed',
              ...(failReason.trim() ? { reason: failReason.trim() } : {}),
            }
            void doAction(req, 'test-fail', 'Test marked failed').then(() => {
              setFailDialogOpen(false)
              setFailReason('')
            })
          }}
          onCancel={() => {
            setFailDialogOpen(false)
            setFailReason('')
          }}
          disabled={inFlight === 'test-fail'}
        />
      )}

      {/* ── Header ────────────────────────────────────────────────────────── */}
      <header className="mb-5">
        <div className="mb-3 flex items-center gap-3">
          <button
            type="button"
            onClick={() => navigate(-1)}
            aria-label="Back"
            className="rounded p-1 text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            ←
          </button>
          <h1 className="text-xl font-bold text-primary">coord</h1>
        </div>

        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <p className="text-xs text-muted-foreground">
              {view.repo_name}{' '}
              <span className="font-mono">#{view.issue_number}</span>
              {' · '}
              {view.machine_name}
            </p>
            <h2 className="mt-0.5 text-base font-semibold text-foreground">
              {view.issue_title}
            </h2>
          </div>
          <span
            className={cn(
              'shrink-0 rounded-full px-2.5 py-0.5 text-xs font-semibold',
              statusClass,
            )}
          >
            {statusLabel}
          </span>
        </div>

        {/* Stage chips */}
        <div className="mt-3 flex flex-wrap gap-1.5">
          {view.stages.map((stage) => {
            const base = 'rounded px-1.5 py-0.5 text-xs font-medium'
            let cls: string
            if (stage.is_current) {
              cls = FAILED_STAGES.has(view.current_stage)
                ? cn(base, 'bg-destructive text-destructive-foreground')
                : cn(base, 'bg-primary text-primary-foreground')
            } else if (stage.status === 'completed') {
              cls = cn(base, 'bg-green-700 text-white')
            } else if (stage.status === 'skipped') {
              cls = cn(base, 'border border-border text-muted-foreground opacity-40')
            } else {
              cls = cn(base, 'border border-border text-muted-foreground')
            }
            return (
              <span key={stage.name} className={cls}>
                {STAGE_LABEL[stage.name] ?? stage.name}
              </span>
            )
          })}
        </div>
      </header>

      {/* ── Test gate ─────────────────────────────────────────────────────── */}
      {(hasGate('test-verdict') || view.test_verdict != null) && (
        <section className="mb-4 rounded-lg border border-border bg-card p-4" aria-label="Test gate">
          <h3 className="mb-3 text-sm font-semibold text-card-foreground">Test</h3>

          {view.test_verdict && (
            <p
              className={cn(
                'mb-3 text-sm font-medium',
                view.test_verdict === 'passed' ? 'text-green-400' : 'text-destructive',
              )}
            >
              Verdict: {view.test_verdict}
            </p>
          )}

          {hasGate('test-verdict') && (
            <div className="flex gap-2">
              <button
                type="button"
                onClick={() =>
                  doAction(
                    { assignment_id: view.assignment_id, action: 'test-verdict', verdict: 'passed' },
                    'test-pass',
                    'Test marked passed',
                  )
                }
                disabled={!!inFlight}
                className={cn(btnBase, 'flex-1 bg-green-700 text-white')}
              >
                {inFlight === 'test-pass' ? 'Recording…' : 'Pass'}
              </button>
              <button
                type="button"
                onClick={() => setFailDialogOpen(true)}
                disabled={!!inFlight}
                className={cn(btnBase, 'flex-1 bg-destructive text-destructive-foreground')}
              >
                Fail
              </button>
            </div>
          )}

          {/* Dispatch-fix offered when test failed */}
          {hasGate('dispatch_fix') && view.test_verdict === 'failed' && (
            <button
              type="button"
              onClick={() =>
                doAction(
                  { assignment_id: view.assignment_id, action: 'dispatch_fix' },
                  'dispatch-fix',
                  'Fix dispatched',
                )
              }
              disabled={!!inFlight}
              className={cn(btnBase, 'mt-3 w-full border border-border text-foreground')}
            >
              {inFlight === 'dispatch-fix' ? 'Dispatching…' : 'Dispatch Fix'}
            </button>
          )}
        </section>
      )}

      {/* ── Dispatch review ───────────────────────────────────────────────── */}
      {hasGate('dispatch_review') && (
        <section className="mb-4 rounded-lg border border-border bg-card p-4" aria-label="Review">
          <h3 className="mb-3 text-sm font-semibold text-card-foreground">Review</h3>
          <button
            type="button"
            onClick={() =>
              doAction(
                { assignment_id: view.assignment_id, action: 'dispatch_review' },
                'dispatch-review',
                'Review dispatched',
              )
            }
            disabled={!!inFlight}
            className={cn(btnBase, 'w-full bg-primary text-primary-foreground')}
          >
            {inFlight === 'dispatch-review' ? 'Dispatching…' : 'Start Review'}
          </button>
        </section>
      )}

      {/* ── Review findings + verdict ──────────────────────────────────────── */}
      {!hasGate('dispatch_review') &&
        (view.review_findings_body !== null ||
          view.review_verdict !== null ||
          hasGate('record-review-verdict') ||
          hasGate('post_findings')) && (
          <section className="mb-4 rounded-lg border border-border bg-card p-4" aria-label="Review">
            <h3 className="mb-3 text-sm font-semibold text-card-foreground">Review</h3>

            {view.review_verdict && (
              <p
                className={cn(
                  'mb-3 text-sm font-medium',
                  view.review_verdict === 'approve' ? 'text-green-400' : 'text-yellow-500',
                )}
              >
                Verdict:{' '}
                {view.review_verdict === 'approve' ? 'Approved' : 'Changes requested'}
              </p>
            )}

            {view.review_findings_body && (
              <div className="mb-3">
                <p className="mb-1 text-xs text-muted-foreground">Findings</p>
                <pre className="max-h-64 overflow-y-auto rounded border border-border bg-black/30 px-3 py-2 text-xs leading-5 text-foreground whitespace-pre-wrap break-words">
                  {view.review_findings_body}
                </pre>
              </div>
            )}

            {view.review_findings_pending && (
              <div className="mb-3 flex items-center justify-between gap-2">
                <p className="text-xs text-yellow-500">⚠ Findings not yet posted to GitHub</p>
                {hasGate('post_findings') && (
                  <button
                    type="button"
                    onClick={() =>
                      doAction(
                        { assignment_id: view.assignment_id, action: 'post_findings' },
                        'post-findings',
                        'Findings posted',
                      )
                    }
                    disabled={!!inFlight}
                    className="rounded border border-yellow-600 px-2 py-1 text-xs text-yellow-500 disabled:opacity-50"
                  >
                    {inFlight === 'post-findings' ? 'Posting…' : 'Post'}
                  </button>
                )}
              </div>
            )}

            {hasGate('record-review-verdict') && (
              <div className="flex gap-2">
                <button
                  type="button"
                  onClick={() =>
                    doAction(
                      {
                        assignment_id: view.assignment_id,
                        action: 'record-review-verdict',
                        verdict: 'approve',
                      },
                      'approve',
                      'Review approved',
                    )
                  }
                  disabled={!!inFlight}
                  className={cn(btnBase, 'flex-1 bg-green-700 text-white')}
                >
                  {inFlight === 'approve' ? 'Approving…' : 'Approve'}
                </button>
                <button
                  type="button"
                  onClick={() =>
                    doAction(
                      { assignment_id: view.assignment_id, action: 'dispatch_fix' },
                      'request-changes',
                      'Fix dispatched',
                    )
                  }
                  disabled={!!inFlight}
                  className={cn(btnBase, 'flex-1 bg-destructive text-destructive-foreground')}
                >
                  {inFlight === 'request-changes' ? 'Dispatching…' : 'Request Changes'}
                </button>
              </div>
            )}
          </section>
        )}

      {/* ── Merge ─────────────────────────────────────────────────────────── */}
      {(hasGate('merge') ||
        hasGate('enqueue') ||
        ['merge_ready', 'merging', 'merged'].includes(view.current_stage)) && (
        <section className="mb-4 rounded-lg border border-border bg-card p-4" aria-label="Merge">
          <h3 className="mb-3 text-sm font-semibold text-card-foreground">Merge</h3>

          {/* Gate status list */}
          <div className="mb-3 space-y-1.5">
            {view.stages.map((stage) => (
              <div key={stage.name} className="flex items-center gap-2 text-xs">
                <span
                  className={cn(
                    'h-1.5 w-1.5 shrink-0 rounded-full',
                    stage.status === 'completed' || stage.status === 'skipped'
                      ? 'bg-green-500'
                      : stage.is_current
                        ? 'bg-primary'
                        : 'bg-border',
                  )}
                />
                <span className="text-muted-foreground">{STAGE_LABEL[stage.name] ?? stage.name}</span>
                <span
                  className={cn(
                    stage.status === 'completed' || stage.status === 'skipped'
                      ? 'text-green-400'
                      : stage.is_current
                        ? 'text-primary'
                        : 'text-muted-foreground',
                  )}
                >
                  {stage.status}
                </span>
              </div>
            ))}
          </div>

          {view.current_stage === 'merged' ? (
            <p className="text-sm font-medium text-green-400">✓ Merged</p>
          ) : (
            <>
              {hasGate('merge') && (
                <label className="mb-3 flex cursor-pointer items-center gap-2 text-xs text-muted-foreground">
                  <input
                    type="checkbox"
                    checked={forceMode}
                    onChange={(e) => setForceMode(e.target.checked)}
                    className="h-3.5 w-3.5"
                  />
                  Force (skip CI / review checks)
                </label>
              )}

              <div className="flex gap-2">
                {hasGate('enqueue') && (
                  <button
                    type="button"
                    onClick={() =>
                      doAction(
                        { assignment_id: view.assignment_id, action: 'enqueue' },
                        'enqueue',
                        'Added to merge queue',
                      )
                    }
                    disabled={!!inFlight}
                    className={cn(btnBase, 'flex-1 border border-border text-foreground')}
                  >
                    {inFlight === 'enqueue' ? 'Queuing…' : 'Enqueue'}
                  </button>
                )}
                {hasGate('merge') && (
                  <button
                    type="button"
                    onClick={() =>
                      doAction(
                        {
                          assignment_id: view.assignment_id,
                          action: 'merge',
                          ...(forceMode ? { force: true } : {}),
                        },
                        'merge',
                        'Merged successfully',
                      )
                    }
                    disabled={!!inFlight}
                    className={cn(
                      btnBase,
                      'flex-1',
                      forceMode
                        ? 'bg-yellow-600 text-black'
                        : 'bg-primary text-primary-foreground',
                    )}
                  >
                    {inFlight === 'merge'
                      ? 'Merging…'
                      : forceMode
                        ? 'Force Merge'
                        : 'Merge'}
                  </button>
                )}
              </div>
            </>
          )}
        </section>
      )}

      {/* ── Dispatch smoke test ────────────────────────────────────────────── */}
      {hasGate('dispatch_smoke') && (
        <section className="mb-4 rounded-lg border border-border bg-card p-4" aria-label="Smoke test">
          <h3 className="mb-3 text-sm font-semibold text-card-foreground">Smoke Test</h3>
          <button
            type="button"
            onClick={() =>
              doAction(
                { assignment_id: view.assignment_id, action: 'dispatch_smoke' },
                'dispatch-smoke',
                'Smoke test dispatched',
              )
            }
            disabled={!!inFlight}
            className={cn(btnBase, 'w-full bg-primary text-primary-foreground')}
          >
            {inFlight === 'dispatch-smoke' ? 'Dispatching…' : 'Start Smoke Test'}
          </button>
        </section>
      )}

      {/* ── Unstick ───────────────────────────────────────────────────────── */}
      {hasGate('unstick') && (
        <section className="mb-4 rounded-lg border border-border bg-card p-4" aria-label="Stuck assignment">
          <h3 className="mb-3 text-sm font-semibold text-card-foreground">Stuck</h3>
          <button
            type="button"
            onClick={() =>
              doAction(
                { assignment_id: view.assignment_id, action: 'unstick' },
                'unstick',
                'Assignment cancelled',
              )
            }
            disabled={!!inFlight}
            className={cn(btnBase, 'w-full border border-destructive text-destructive')}
          >
            {inFlight === 'unstick' ? 'Cancelling…' : 'Cancel (unstick)'}
          </button>
        </section>
      )}

      {/* ── Diff viewer ───────────────────────────────────────────────────── */}
      <DiffViewer
        assignmentId={view.assignment_id}
        expanded={diffExpanded}
        onToggle={() => setDiffExpanded((v) => !v)}
      />
    </div>
  )
}
