/**
 * Typed API client for coord/dashboard/server.py.
 *
 * All types mirror the server-side payloads.  Actions marked "(forthcoming)"
 * are defined here ahead of their backend implementation so TypeScript callers
 * can reference them; they will return HTTP 501 until the matching server PR
 * merges.
 */

// ── Shared types ──────────────────────────────────────────────────────────────

export type AssignmentStatus =
  | 'running'
  | 'done'
  | 'failed'
  | 'cancelled'
  | 'advisory'

export type AssignmentType =
  | 'work'
  | 'review'
  | 'smoke'
  | 'conflict-fix'
  | 'merge'
  | 'fix'

export type TestVerdict = 'passed' | 'failed' | 'skipped'

export interface Assignment {
  machine_name: string
  repo_name: string
  issue_number: number
  issue_title: string
  assignment_id: string
  status: AssignmentStatus
  type?: AssignmentType | null
  dispatched_at?: number | null
  finished_at?: number | null
  branch?: string | null
  /** The work assignment this review covers. */
  review_of_assignment_id?: string | null
  /** The work assignment this smoke test covers. */
  smoke_of_assignment_id?: string | null
  /** The work assignment this fix targets. */
  fix_of_assignment_id?: string | null
  review_posted_at?: number | null
  test_verdict?: TestVerdict | null
}

// ── GET /api/board ────────────────────────────────────────────────────────────

export interface BoardData {
  round_number: number
  active: Assignment[]
  /** Last 20 completed assignments. */
  completed: Assignment[]
}

// ── GET /api/pipeline ─────────────────────────────────────────────────────────

/**
 * One stage in the pipeline (coding → review → smoke → merge).
 * Mirrors coord/pipeline.py:PipelineStage.
 */
export interface PipelineStage {
  /** Stage key: "coding" | "review" | "smoke" | "merge" */
  name: string
  /** "active" | "completed" | "skipped" | "waiting" */
  status: string
  is_current: boolean
}

/**
 * An available gate action on a pipeline item.
 * Mirrors coord/pipeline.py:PipelineGate.
 */
export interface PipelineGate {
  action: PipelineAction
  label: string
  endpoint: string
}

/**
 * Pipeline state for one work-type assignment.
 * Mirrors coord/pipeline.py:PipelineView (serialised via dataclasses.asdict).
 */
export interface PipelineView {
  assignment_id: string
  issue_number: number
  issue_title: string
  repo_name: string
  machine_name: string
  stages: PipelineStage[]
  /**
   * Fine-grained current stage name used for colour-coding.
   * One of: "coding" | "failed" | "done" | "review_running" | "review_done" |
   * "review_failed" | "smoke_running" | "smoke_passed" | "smoke_failed" |
   * "merge_ready" | "merging" | "merged"
   */
  current_stage: string
  /** Gate actions currently available (empty when no human action is needed). */
  available_gates: PipelineGate[]
  /** Overall progress 0-100. */
  progress_pct: number
  /**
   * True when the review assignment completed but its findings have not yet
   * been posted to GitHub (review_posted_at is None on the review assignment).
   */
  review_findings_pending: boolean
  /** "approve" | "request-changes" | null */
  review_verdict: 'approve' | 'request-changes' | null
  /** Cached review findings body from the DB; null when not yet available. */
  review_findings_body: string | null
  /**
   * Test verdict recorded via `coord test --passed/--failed/--skipped`.
   * Added by #698 (test-verdict / record-review-verdict / findings pipeline fields).
   */
  test_verdict: TestVerdict | null
}

// ── GET /api/diff/{id} ────────────────────────────────────────────────────────

export interface DiffResult {
  diff: string
  /** "pr" when fetched from a GitHub PR; "compare" when fetched from the compare API. */
  source: 'pr' | 'compare'
}

// ── POST /api/pipeline/action ─────────────────────────────────────────────────

/**
 * Actions supported by POST /api/pipeline/action.
 *
 * dispatch_review    — kick off an adversarial review assignment
 * dispatch_smoke     — kick off a smoke-test assignment
 * enqueue            — add to merge queue
 * merge              — merge a queued PR (must be in "pending" state)
 * post_findings      — post orphaned review findings to GitHub
 * unstick            — cancel a stuck assignment and mark it failed
 * retry              — (forthcoming) retry a failed work assignment
 * dispatch_fix       — (forthcoming) dispatch a fix for a test failure / review request-changes
 * test-verdict       — (forthcoming) record passed/failed/skipped test verdict
 * record-review-verdict — (forthcoming) record an approved/changes-requested review verdict
 */
export type PipelineAction =
  | 'dispatch_review'
  | 'dispatch_smoke'
  | 'enqueue'
  | 'merge'
  | 'post_findings'
  | 'unstick'
  | 'retry'
  | 'dispatch_fix'
  | 'test-verdict'
  | 'record-review-verdict'

export interface PipelineActionRequest {
  assignment_id: string
  action: PipelineAction
  /** Additional payload fields for specific actions (e.g. verdict for test-verdict). */
  [key: string]: unknown
}

export interface PipelineActionResult {
  ok: boolean
  error?: string
  /** Machine that accepted the dispatched assignment (dispatch_review / dispatch_smoke). */
  machine_name?: string
  /** Assignment ID created by a dispatch action. */
  assignment_id?: string
  /** Events produced by a merge action. */
  events?: Array<{ kind: string; message: string }>
  /** Human-readable detail (post_findings). */
  detail?: string
}

// ── HTTP helpers ──────────────────────────────────────────────────────────────

/** Same-origin base — webapp is served by coord/dashboard/server.py. */
const API_BASE = ''

async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, init)
  if (!res.ok) {
    const text = await res.text().catch(() => '')
    throw new Error(`${init?.method ?? 'GET'} ${path} → HTTP ${res.status}: ${text}`)
  }
  return res.json() as Promise<T>
}

// ── Public API ────────────────────────────────────────────────────────────────

/** Fetch the full board state (active + last-20 completed assignments). */
export async function fetchBoard(): Promise<BoardData> {
  return apiFetch<BoardData>('/api/board')
}

/** Fetch pipeline views for all work-type assignments. */
export async function fetchPipeline(): Promise<PipelineView[]> {
  return apiFetch<PipelineView[]>('/api/pipeline')
}

/**
 * Fetch the diff for a completed work assignment.
 * Prefers the GitHub PR diff; falls back to the compare API.
 */
export async function fetchDiff(assignmentId: string): Promise<DiffResult> {
  return apiFetch<DiffResult>(`/api/diff/${encodeURIComponent(assignmentId)}`)
}

/** Advance an assignment through a pipeline gate. */
export async function pipelineAction(
  body: PipelineActionRequest,
): Promise<PipelineActionResult> {
  const res = await fetch(`${API_BASE}/api/pipeline/action`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  const data = (await res.json()) as PipelineActionResult
  if (!res.ok) {
    return { ok: false, error: data.error ?? `HTTP ${res.status}` }
  }
  return data
}
