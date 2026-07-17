/**
 * Component tests for the Home screen's "Live sessions" section (#1067) and
 * the Active tab's in-progress/done grouping (#1218).
 *
 * Mocks @/api/client entirely; wraps renders in a QueryClientProvider +
 * MemoryRouter so useQuery / useNavigate work correctly, matching
 * Detail.test.tsx's pattern.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import Home from '@/components/Home'
import { type PipelineView, type SessionInfo } from '@/api/client'

// ── Mock API client + navigate ────────────────────────────────────────────────

vi.mock('@/api/client', () => ({
  fetchPipeline: vi.fn(),
  fetchSessions: vi.fn(),
}))

import { fetchPipeline, fetchSessions } from '@/api/client'

const navigateSpy = vi.fn()
vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual<typeof import('react-router-dom')>('react-router-dom')
  return { ...actual, useNavigate: () => navigateSpy }
})

beforeEach(() => {
  vi.clearAllMocks()
})

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
      { name: 'smoke', status: 'waiting', is_current: false },
      { name: 'merge', status: 'waiting', is_current: false },
    ],
    available_gates: [],
    progress_pct: 10,
    review_findings_pending: false,
    review_verdict: null,
    review_findings_body: null,
    test_verdict: null,
    needs_attention: false,
    needs_attention_reason: null,
    needs_attention_detail: null,
    finished_at: null,
    ...overrides,
  }
}

function makeSession(overrides: Partial<SessionInfo> = {}): SessionInfo {
  return {
    session_id: 'work-2',
    session_name: 'coord-work-2',
    machine: 'desktop',
    host: 'desktop.tailnet.ts.net',
    repo: 'otherrepo',
    issue: 7,
    issue_title: 'Live session issue',
    stage: 'work',
    status: 'running',
    attached: false,
    pane_dead: false,
    ...overrides,
  }
}

function createTestQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false, refetchInterval: false, refetchOnWindowFocus: false },
    },
  })
}

function renderHome() {
  return render(
    <QueryClientProvider client={createTestQueryClient()}>
      <MemoryRouter initialEntries={['/']}>
        <Routes>
          <Route path="/" element={<Home />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

// ── Live sessions section ─────────────────────────────────────────────────────

describe('Home — live sessions section', () => {
  it('renders live session cards above the pipeline list', async () => {
    vi.mocked(fetchPipeline).mockResolvedValue([makeView()])
    vi.mocked(fetchSessions).mockResolvedValue([makeSession()])

    renderHome()

    await waitFor(() => {
      expect(screen.getByText('Live sessions')).toBeInTheDocument()
    })
    expect(screen.getByText('Live session issue')).toBeInTheDocument()

    // The "Live sessions" heading appears before the pipeline card in DOM order.
    const heading = screen.getByText('Live sessions')
    const pipelineCard = screen.getByText('Fix the thing')
    expect(
      heading.compareDocumentPosition(pipelineCard) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy()
  })

  it('renders no "Live sessions" heading when there are no live sessions', async () => {
    vi.mocked(fetchPipeline).mockResolvedValue([makeView()])
    vi.mocked(fetchSessions).mockResolvedValue([])

    renderHome()

    await waitFor(() => {
      expect(screen.getByText('Fix the thing')).toBeInTheDocument()
    })
    expect(screen.queryByText('Live sessions')).not.toBeInTheDocument()
  })

  it('navigates to /terminal/:sessionId when a session card is tapped', async () => {
    vi.mocked(fetchPipeline).mockResolvedValue([])
    vi.mocked(fetchSessions).mockResolvedValue([makeSession()])

    renderHome()

    const card = await screen.findByText('Live session issue')
    await userEvent.click(card)

    expect(navigateSpy).toHaveBeenCalledWith('/terminal/work-2')
  })
})

// ── Active tab: in-progress/done grouping (#1218) ───────────────────────────────

describe('Home — Active tab grouping', () => {
  it('renders in-progress items expanded, needs-me first then running', async () => {
    // Incoming (API) order deliberately scrambled: running item first, then
    // a needs-me (failed, offers retry) item — expect needs-me to sort first.
    const running = makeView({
      assignment_id: 'a-running',
      issue_title: 'Running item',
      current_stage: 'coding',
      available_gates: [],
    })
    const needsMe = makeView({
      assignment_id: 'a-needs-me',
      issue_title: 'Failed item needing retry',
      current_stage: 'failed',
      available_gates: [{ action: 'retry', label: 'Retry', endpoint: '/api/pipeline/action' }],
    })
    vi.mocked(fetchPipeline).mockResolvedValue([running, needsMe])
    vi.mocked(fetchSessions).mockResolvedValue([])

    renderHome()

    const needsMeCard = await screen.findByText('Failed item needing retry')
    const runningCard = screen.getByText('Running item')
    expect(
      needsMeCard.compareDocumentPosition(runningCard) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy()
  })

  it('collapses done-ish items into a "Work done (N)" section by default', async () => {
    const running = makeView({
      assignment_id: 'a-running',
      issue_title: 'Running item',
      current_stage: 'coding',
    })
    const done1 = makeView({
      assignment_id: 'a-done-1',
      issue_title: 'Finished thing one',
      current_stage: 'done',
      available_gates: [{ action: 'enqueue', label: 'Queue', endpoint: '/api/pipeline/action' }],
      finished_at: 100,
    })
    const done2 = makeView({
      assignment_id: 'a-done-2',
      issue_title: 'Finished thing two',
      current_stage: 'review_done',
      available_gates: [{ action: 'enqueue', label: 'Queue', endpoint: '/api/pipeline/action' }],
      finished_at: 200,
    })
    vi.mocked(fetchPipeline).mockResolvedValue([running, done1, done2])
    vi.mocked(fetchSessions).mockResolvedValue([])

    renderHome()

    await waitFor(() => {
      expect(screen.getByText('Running item')).toBeInTheDocument()
    })

    // Collapsed by default: header with count shown, done cards not rendered.
    expect(screen.getByText('Work done (2)')).toBeInTheDocument()
    expect(screen.queryByText('Finished thing one')).not.toBeInTheDocument()
    expect(screen.queryByText('Finished thing two')).not.toBeInTheDocument()
  })

  it('expands the Work done section on tap, sorted by recency descending', async () => {
    const older = makeView({
      assignment_id: 'a-done-older',
      issue_title: 'Older done item',
      current_stage: 'done',
      available_gates: [{ action: 'enqueue', label: 'Queue', endpoint: '/api/pipeline/action' }],
      finished_at: 100,
    })
    const newer = makeView({
      assignment_id: 'a-done-newer',
      issue_title: 'Newer done item',
      current_stage: 'smoke_passed',
      available_gates: [{ action: 'enqueue', label: 'Queue', endpoint: '/api/pipeline/action' }],
      finished_at: 200,
    })
    vi.mocked(fetchPipeline).mockResolvedValue([older, newer])
    vi.mocked(fetchSessions).mockResolvedValue([])

    renderHome()

    const toggle = await screen.findByText('Work done (2)')
    await userEvent.click(toggle)

    const newerCard = await screen.findByText('Newer done item')
    const olderCard = await screen.findByText('Older done item')
    expect(
      newerCard.compareDocumentPosition(olderCard) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy()
  })

  it('keeps the "Needs me" tab as a flat, ungrouped list', async () => {
    const done = makeView({
      assignment_id: 'a-done',
      issue_title: 'Finished needing merge',
      current_stage: 'done',
      available_gates: [{ action: 'enqueue', label: 'Queue', endpoint: '/api/pipeline/action' }],
      finished_at: 100,
    })
    vi.mocked(fetchPipeline).mockResolvedValue([done])
    vi.mocked(fetchSessions).mockResolvedValue([])

    renderHome()

    const tab = await screen.findByRole('tab', { name: /Needs me/i })
    await userEvent.click(tab)

    // Rendered directly — no collapsed "Work done" wrapper on this tab.
    expect(await screen.findByText('Finished needing merge')).toBeInTheDocument()
    expect(screen.queryByText(/Work done \(/)).not.toBeInTheDocument()
  })
})
