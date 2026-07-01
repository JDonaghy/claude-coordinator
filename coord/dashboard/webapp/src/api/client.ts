/**
 * Typed API client for coord/dashboard/server.py.
 *
 * The wire types themselves (`Assignment`, `PipelineStage`, `PipelineGate`,
 * `PipelineView`, `AssignmentStatus`, `AssignmentType`, `TestVerdict`,
 * `PipelineAction`) are generated from the Python dataclasses that produce
 * these payloads — see `./generated.ts` and `scripts/codegen.py` (#750).
 * Regenerate with `.venv/bin/python scripts/codegen.py` after any Python
 * dataclass field change; do not hand-edit the generated file.
 *
 * Actions marked "(forthcoming)" in `PipelineAction`'s doc comment are
 * defined ahead of their backend implementation so TypeScript callers can
 * reference them; they will return HTTP 501 until the matching server PR
 * merges.
 */

import type {
  Assignment,
  AssignmentStatus,
  AssignmentType,
  PipelineAction,
  PipelineGate,
  PipelineStage,
  PipelineView,
  TestVerdict,
} from './generated'

export type {
  Assignment,
  AssignmentStatus,
  AssignmentType,
  PipelineAction,
  PipelineGate,
  PipelineStage,
  PipelineView,
  TestVerdict,
}

// ── GET /api/board ────────────────────────────────────────────────────────────

export interface BoardData {
  round_number: number
  active: Assignment[]
  /** Last 20 completed assignments. */
  completed: Assignment[]
}

// ── GET /api/diff/{id} ────────────────────────────────────────────────────────

export interface DiffResult {
  diff: string
  /** "pr" when fetched from a GitHub PR; "compare" when fetched from the compare API. */
  source: 'pr' | 'compare'
}

// ── POST /api/pipeline/action ─────────────────────────────────────────────────

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
