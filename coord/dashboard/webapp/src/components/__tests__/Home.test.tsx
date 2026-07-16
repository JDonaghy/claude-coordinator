/**
 * Component tests for the Home screen's "Live sessions" section (#1067).
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
