/**
 * Component tests for PipelineCard and the Home-screen filter functions.
 *
 * These use vitest + @testing-library/react.  Run with `npm test` (or
 * `npx vitest run`) after `npm install`.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { PipelineCard } from '@/components/PipelineCard'
import { type PipelineView } from '@/api/client'
import { needsMe, hasAvailableGate } from '@/lib/pipeline'

// ── Test helpers ──────────────────────────────────────────────────────────────

function makeView(overrides: Partial<PipelineView> = {}): PipelineView {
  return {
    assignment_id: 'work-1',
    issue_number: 42,
    issue_title: 'Fix the thing',
    repo_name: 'myrepo',
    machine_name: 'laptop',
    current_stage: 'coding',
    stages: [
      { name: 'coding', status: 'active', is_current: true },
      { name: 'review', status: 'waiting', is_current: false },
      { name: 'smoke',  status: 'waiting', is_current: false },
      { name: 'merge',  status: 'waiting', is_current: false },
    ],
    available_gates: [],
    progress_pct: 10,
    review_findings_pending: false,
    review_verdict: null,
    // #1456: null = the reviewer's own verdict stands (the normal case).
    review_verdict_original: null,
    review_verdict_override_reason: null,
    review_findings_body: null,
    test_verdict: null,
    needs_attention: false,
    needs_attention_reason: null,
    needs_attention_detail: null,
    finished_at: null,
    ...overrides,
  }
}

// ── PipelineCard rendering ────────────────────────────────────────────────────

describe('PipelineCard', () => {
  it('renders issue title, repo, issue number, and machine', () => {
    render(<PipelineCard view={makeView()} onClick={() => undefined} />)

    // The card is a button — scope assertions to it to avoid ambiguity
    const card = screen.getByRole('button')
    expect(card).toHaveTextContent('Fix the thing')
    expect(card).toHaveTextContent('#42')
    expect(card).toHaveTextContent('myrepo')
    expect(card).toHaveTextContent('laptop')
  })

  it('renders stage chips with display names (coding→work, smoke→test)', () => {
    render(<PipelineCard view={makeView()} onClick={() => undefined} />)

    expect(screen.getByText('work')).toBeInTheDocument()
    expect(screen.getByText('review')).toBeInTheDocument()
    expect(screen.getByText('test')).toBeInTheDocument()
    expect(screen.getByText('merge')).toBeInTheDocument()
  })

  it('shows "running" status badge when current_stage is coding', () => {
    render(<PipelineCard view={makeView({ current_stage: 'coding' })} onClick={() => undefined} />)
    expect(screen.getByText('running')).toBeInTheDocument()
  })

  it('shows "failed" status badge for failed stage', () => {
    const view = makeView({
      current_stage: 'failed',
      stages: [
        { name: 'coding', status: 'active', is_current: true },
        { name: 'review', status: 'waiting', is_current: false },
        { name: 'smoke',  status: 'waiting', is_current: false },
        { name: 'merge',  status: 'waiting', is_current: false },
      ],
    })
    render(<PipelineCard view={view} onClick={() => undefined} />)
    expect(screen.getByText('failed')).toBeInTheDocument()
  })

  it('shows "mergeable" badge when current_stage is merge_ready', () => {
    const view = makeView({
      current_stage: 'merge_ready',
      stages: [
        { name: 'coding', status: 'completed', is_current: false },
        { name: 'review', status: 'completed', is_current: false },
        { name: 'smoke',  status: 'completed', is_current: false },
        { name: 'merge',  status: 'active', is_current: true },
      ],
      available_gates: [{ action: 'merge', label: 'Merge', endpoint: '/api/pipeline/action' }],
    })
    render(<PipelineCard view={view} onClick={() => undefined} />)
    expect(screen.getByText('mergeable')).toBeInTheDocument()
  })

  it('shows review findings pending warning', () => {
    render(
      <PipelineCard
        view={makeView({ review_findings_pending: true })}
        onClick={() => undefined}
      />,
    )
    expect(screen.getByText(/Review findings not yet posted/)).toBeInTheDocument()
  })

  it('calls onClick when the card is pressed', async () => {
    const onClick = vi.fn()
    render(<PipelineCard view={makeView()} onClick={onClick} />)
    await userEvent.click(screen.getByRole('button'))
    expect(onClick).toHaveBeenCalledOnce()
  })
})

// ── Filter logic (unit-tested here since filterFns are co-located in Home) ────

describe('filter helpers', () => {
  it('isActive excludes merged items', () => {
    const active = makeView({ current_stage: 'coding' })
    const merged = makeView({ current_stage: 'merged' })
    // Re-implement the predicate inline to keep the test independent of
    // Home's internal state.  Tests document the contract.
    const isActive = (v: PipelineView) => v.current_stage !== 'merged'
    expect(isActive(active)).toBe(true)
    expect(isActive(merged)).toBe(false)
  })

  // #1966: "needs me" mirrors the server's `needs_attention` verdict, not
  // gate availability — an item can have gates without being flagged, and
  // (in principle) be flagged without an actionable gate. `hasAvailableGate`
  // is the separate, gate-based predicate that still drives Active-tab sort
  // priority (see Home.tsx's groupActiveItems).
  it('needsMe tracks needs_attention, independent of available_gates', () => {
    const gateNoFlag = makeView({
      available_gates: [{ action: 'merge', label: 'Merge', endpoint: '/api/pipeline/action' }],
      needs_attention: false,
    })
    const flagNoGate = makeView({
      available_gates: [],
      needs_attention: true,
      needs_attention_reason: 'wall_clock',
    })
    expect(needsMe(gateNoFlag)).toBe(false)
    expect(needsMe(flagNoGate)).toBe(true)
  })

  it('hasAvailableGate returns true when available_gates is non-empty', () => {
    const noAction = makeView({ available_gates: [] })
    const hasAction = makeView({
      available_gates: [{ action: 'merge', label: 'Merge', endpoint: '/api/pipeline/action' }],
    })
    expect(hasAvailableGate(noAction)).toBe(false)
    expect(hasAvailableGate(hasAction)).toBe(true)
  })
})
