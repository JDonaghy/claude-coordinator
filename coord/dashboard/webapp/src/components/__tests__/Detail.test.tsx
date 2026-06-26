/**
 * Component tests for the Detail screen.
 *
 * Mocks @/api/client entirely; wraps renders in a QueryClientProvider +
 * MemoryRouter so useQuery / useNavigate / useParams work correctly.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import Detail from '@/components/Detail'
import { type PipelineView, type PipelineActionResult, type DiffResult } from '@/api/client'

// ── Mock API client ───────────────────────────────────────────────────────────

vi.mock('@/api/client', () => ({
  fetchPipeline: vi.fn(),
  fetchDiff: vi.fn(),
  pipelineAction: vi.fn(),
}))

// Import after vi.mock so we get the mocked versions
import { fetchPipeline, fetchDiff, pipelineAction } from '@/api/client'

// Clear mock call history between every test so counts don't bleed across tests
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
    current_stage: 'done',
    stages: [
      { name: 'coding', status: 'completed', is_current: false },
      { name: 'smoke',  status: 'waiting',   is_current: false },
      { name: 'review', status: 'waiting',   is_current: false },
      { name: 'merge',  status: 'waiting',   is_current: false },
    ],
    available_gates: [],
    progress_pct: 25,
    review_findings_pending: false,
    review_verdict: null,
    review_findings_body: null,
    test_verdict: null,
    ...overrides,
  }
}

function makeDiff(overrides: Partial<DiffResult> = {}): DiffResult {
  return {
    diff: '+added line\n-removed line\n unchanged line',
    source: 'pr',
    ...overrides,
  }
}

function createTestQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
        // Disable auto-refetch in tests so we control data
        refetchInterval: false,
        refetchOnWindowFocus: false,
      },
    },
  })
}

function renderDetail(viewOverride?: Partial<PipelineView>) {
  const view = makeView(viewOverride)
  vi.mocked(fetchPipeline).mockResolvedValue([view])
  vi.mocked(fetchDiff).mockResolvedValue(makeDiff())
  vi.mocked(pipelineAction).mockResolvedValue({ ok: true } satisfies PipelineActionResult)

  const queryClient = createTestQueryClient()

  return {
    view,
    ...render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={['/detail/work-1']}>
          <Routes>
            <Route path="/detail/:id" element={<Detail />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    ),
  }
}

// ── Loading / error / not-found ───────────────────────────────────────────────

describe('Detail — loading states', () => {
  it('shows a loading indicator while pipeline is fetching', () => {
    // Never resolves
    vi.mocked(fetchPipeline).mockReturnValue(new Promise(() => undefined))
    vi.mocked(fetchDiff).mockResolvedValue(makeDiff())

    render(
      <QueryClientProvider client={createTestQueryClient()}>
        <MemoryRouter initialEntries={['/detail/work-1']}>
          <Routes>
            <Route path="/detail/:id" element={<Detail />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    )

    expect(screen.getByText(/loading/i)).toBeInTheDocument()
  })

  it('shows an error message when the pipeline fetch fails', async () => {
    vi.mocked(fetchPipeline).mockRejectedValue(new Error('network error'))
    vi.mocked(fetchDiff).mockResolvedValue(makeDiff())

    render(
      <QueryClientProvider client={createTestQueryClient()}>
        <MemoryRouter initialEntries={['/detail/work-1']}>
          <Routes>
            <Route path="/detail/:id" element={<Detail />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    )

    await waitFor(() => {
      expect(screen.getByText(/failed to load pipeline/i)).toBeInTheDocument()
    })
  })

  it('shows not-found message when id is absent from pipeline data', async () => {
    // Pipeline has a different assignment
    vi.mocked(fetchPipeline).mockResolvedValue([makeView({ assignment_id: 'other-id' })])
    vi.mocked(fetchDiff).mockResolvedValue(makeDiff())

    render(
      <QueryClientProvider client={createTestQueryClient()}>
        <MemoryRouter initialEntries={['/detail/work-1']}>
          <Routes>
            <Route path="/detail/:id" element={<Detail />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    )

    await waitFor(() => {
      expect(screen.getByText(/not found in the pipeline/i)).toBeInTheDocument()
    })
  })
})

// ── Header ────────────────────────────────────────────────────────────────────

describe('Detail — header', () => {
  it('renders repo, issue number, title, and machine after data loads', async () => {
    renderDetail()

    await waitFor(() => {
      expect(screen.getByText('Fix the thing')).toBeInTheDocument()
    })

    expect(screen.getByText(/myrepo/)).toBeInTheDocument()
    expect(screen.getByText(/#42/)).toBeInTheDocument()
    expect(screen.getByText(/laptop/)).toBeInTheDocument()
  })

  it('renders a back button', async () => {
    renderDetail()

    await waitFor(() => screen.getByLabelText('Back'))
    expect(screen.getByLabelText('Back')).toBeInTheDocument()
  })

  it('shows the overall status badge', async () => {
    renderDetail({ current_stage: 'done' })

    await waitFor(() => {
      expect(screen.getByText('work done')).toBeInTheDocument()
    })
  })

  it('renders stage chips', async () => {
    renderDetail()

    await waitFor(() => screen.getByText('work'))
    expect(screen.getByText('work')).toBeInTheDocument()
    expect(screen.getByText('review')).toBeInTheDocument()
    expect(screen.getByText('test')).toBeInTheDocument()
    expect(screen.getByText('merge')).toBeInTheDocument()
  })
})

// ── Test gate ─────────────────────────────────────────────────────────────────

describe('Detail — test gate', () => {
  it('shows Pass and Fail buttons when test-verdict gate is available', async () => {
    renderDetail({
      available_gates: [{ action: 'test-verdict', label: 'Record test verdict', endpoint: '/api/pipeline/action' }],
    })

    await waitFor(() => screen.getByText('Pass'))
    expect(screen.getByText('Pass')).toBeInTheDocument()
    expect(screen.getByText('Fail')).toBeInTheDocument()
  })

  it('hides test gate buttons when no test-verdict gate', async () => {
    renderDetail({ available_gates: [] })

    await waitFor(() => screen.getByText('Fix the thing'))
    expect(screen.queryByText('Pass')).not.toBeInTheDocument()
    expect(screen.queryByText('Fail')).not.toBeInTheDocument()
  })

  it('clicking Pass calls pipelineAction with passed verdict', async () => {
    const user = userEvent.setup()
    renderDetail({
      available_gates: [{ action: 'test-verdict', label: 'Record test verdict', endpoint: '/api/pipeline/action' }],
    })

    await waitFor(() => screen.getByText('Pass'))
    await user.click(screen.getByText('Pass'))

    expect(vi.mocked(pipelineAction)).toHaveBeenCalledWith({
      assignment_id: 'work-1',
      action: 'test-verdict',
      verdict: 'passed',
    })
  })

  it('clicking Fail opens the fail dialog', async () => {
    const user = userEvent.setup()
    renderDetail({
      available_gates: [{ action: 'test-verdict', label: 'Record test verdict', endpoint: '/api/pipeline/action' }],
    })

    await waitFor(() => screen.getByText('Fail'))
    await user.click(screen.getByText('Fail'))

    expect(screen.getByRole('dialog', { name: /record test failure/i })).toBeInTheDocument()
  })

  it('confirming the fail dialog calls pipelineAction with failed verdict and reason', async () => {
    const user = userEvent.setup()
    renderDetail({
      available_gates: [{ action: 'test-verdict', label: 'Record test verdict', endpoint: '/api/pipeline/action' }],
    })

    await waitFor(() => screen.getByText('Fail'))
    await user.click(screen.getByText('Fail'))

    const dialog = screen.getByRole('dialog', { name: /record test failure/i })
    const textarea = within(dialog).getByPlaceholderText(/what failed/i)
    await user.type(textarea, 'build error')
    await user.click(within(dialog).getByText('Confirm Fail'))

    expect(vi.mocked(pipelineAction)).toHaveBeenCalledWith({
      assignment_id: 'work-1',
      action: 'test-verdict',
      verdict: 'failed',
      reason: 'build error',
    })
  })

  it('cancelling the fail dialog dismisses it without calling pipelineAction', async () => {
    const user = userEvent.setup()
    renderDetail({
      available_gates: [{ action: 'test-verdict', label: 'Record test verdict', endpoint: '/api/pipeline/action' }],
    })

    await waitFor(() => screen.getByText('Fail'))
    await user.click(screen.getByText('Fail'))
    await user.click(screen.getByText('Cancel'))

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(vi.mocked(pipelineAction)).not.toHaveBeenCalled()
  })

  it('shows existing test verdict when set', async () => {
    renderDetail({ test_verdict: 'passed', available_gates: [] })

    await waitFor(() => {
      expect(screen.getByText(/verdict: passed/i)).toBeInTheDocument()
    })
  })

  it('shows Dispatch Fix when dispatch_fix gate is present and test failed', async () => {
    renderDetail({
      test_verdict: 'failed',
      available_gates: [
        { action: 'dispatch_fix', label: 'Dispatch Fix', endpoint: '/api/pipeline/action' },
      ],
    })

    await waitFor(() => {
      expect(screen.getByText('Dispatch Fix')).toBeInTheDocument()
    })
  })

  it('does not render the test section when test_verdict is absent (undefined) and no gate', async () => {
    // Simulates the real backend before test_verdict was added to PipelineView —
    // dataclasses.asdict() omits undeclared fields so the frontend receives
    // undefined rather than null.  The !=/!== difference matters: `undefined !== null`
    // is true but `undefined != null` is false, so only the loose check is safe.
    const view = makeView()
    const viewWithoutTestVerdict = { ...view } as Partial<PipelineView> & Record<string, unknown>
    delete viewWithoutTestVerdict['test_verdict']
    vi.mocked(fetchPipeline).mockResolvedValue([viewWithoutTestVerdict as PipelineView])
    vi.mocked(fetchDiff).mockResolvedValue(makeDiff())
    vi.mocked(pipelineAction).mockResolvedValue({ ok: true })

    const queryClient = createTestQueryClient()
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={['/detail/work-1']}>
          <Routes>
            <Route path="/detail/:id" element={<Detail />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    )

    await waitFor(() => screen.getByText('Fix the thing'))

    // Test gate section should NOT render because:
    // - no test-verdict gate in available_gates
    // - test_verdict is undefined (missing from backend), which != null is false
    expect(screen.queryByRole('region', { name: /test gate/i })).not.toBeInTheDocument()
    expect(screen.queryByText('Pass')).not.toBeInTheDocument()
    expect(screen.queryByText('Fail')).not.toBeInTheDocument()
  })
})

// ── Review ────────────────────────────────────────────────────────────────────

describe('Detail — review section', () => {
  it('shows Start Review button when dispatch_review gate is available', async () => {
    renderDetail({
      available_gates: [{ action: 'dispatch_review', label: 'Start Review', endpoint: '/api/pipeline/action' }],
    })

    await waitFor(() => {
      expect(screen.getByText('Start Review')).toBeInTheDocument()
    })
  })

  it('renders review findings body when present', async () => {
    renderDetail({
      review_findings_body: 'Found 2 issues:\n1. Missing test\n2. Unused import',
    })

    await waitFor(() => {
      expect(screen.getByText(/Found 2 issues/)).toBeInTheDocument()
    })
  })

  it('shows review verdict when present', async () => {
    renderDetail({ review_verdict: 'approve' })

    await waitFor(() => {
      expect(screen.getByText(/approved/i)).toBeInTheDocument()
    })
  })

  it('shows Approve and Request Changes when record-review-verdict gate is available', async () => {
    renderDetail({
      available_gates: [{ action: 'record-review-verdict', label: 'Record verdict', endpoint: '/api/pipeline/action' }],
    })

    await waitFor(() => screen.getByText('Approve'))
    expect(screen.getByText('Approve')).toBeInTheDocument()
    expect(screen.getByText('Request Changes')).toBeInTheDocument()
  })

  it('Approve calls pipelineAction with record-review-verdict approve', async () => {
    const user = userEvent.setup()
    renderDetail({
      available_gates: [{ action: 'record-review-verdict', label: 'Record verdict', endpoint: '/api/pipeline/action' }],
    })

    await waitFor(() => screen.getByText('Approve'))
    await user.click(screen.getByText('Approve'))

    expect(vi.mocked(pipelineAction)).toHaveBeenCalledWith({
      assignment_id: 'work-1',
      action: 'record-review-verdict',
      verdict: 'approve',
    })
  })

  it('Request Changes calls pipelineAction with dispatch_fix', async () => {
    const user = userEvent.setup()
    renderDetail({
      available_gates: [{ action: 'record-review-verdict', label: 'Record verdict', endpoint: '/api/pipeline/action' }],
    })

    await waitFor(() => screen.getByText('Request Changes'))
    await user.click(screen.getByText('Request Changes'))

    expect(vi.mocked(pipelineAction)).toHaveBeenCalledWith({
      assignment_id: 'work-1',
      action: 'dispatch_fix',
    })
  })

  it('shows Post button when post_findings gate is available', async () => {
    renderDetail({
      review_findings_pending: true,
      available_gates: [{ action: 'post_findings', label: 'Post findings', endpoint: '/api/pipeline/action' }],
    })

    await waitFor(() => {
      expect(screen.getByText('Post')).toBeInTheDocument()
    })
  })
})

// ── Merge ─────────────────────────────────────────────────────────────────────

describe('Detail — merge section', () => {
  it('shows Merge button when merge gate is available', async () => {
    renderDetail({
      current_stage: 'merge_ready',
      available_gates: [{ action: 'merge', label: 'Merge', endpoint: '/api/pipeline/action' }],
    })

    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Merge' })).toBeInTheDocument()
    })
  })

  it('shows Enqueue button when enqueue gate is available', async () => {
    renderDetail({
      available_gates: [{ action: 'enqueue', label: 'Enqueue', endpoint: '/api/pipeline/action' }],
    })

    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Enqueue' })).toBeInTheDocument()
    })
  })

  it('clicking Merge calls pipelineAction with merge action', async () => {
    const user = userEvent.setup()
    renderDetail({
      current_stage: 'merge_ready',
      available_gates: [{ action: 'merge', label: 'Merge', endpoint: '/api/pipeline/action' }],
    })

    const mergeBtn = await screen.findByRole('button', { name: 'Merge' })
    await user.click(mergeBtn)

    expect(vi.mocked(pipelineAction)).toHaveBeenCalledWith({
      assignment_id: 'work-1',
      action: 'merge',
    })
  })

  it('force checkbox changes Merge button to Force Merge and adds force:true to payload', async () => {
    const user = userEvent.setup()
    renderDetail({
      current_stage: 'merge_ready',
      available_gates: [{ action: 'merge', label: 'Merge', endpoint: '/api/pipeline/action' }],
    })

    await screen.findByRole('button', { name: 'Merge' })

    const checkbox = screen.getByRole('checkbox')
    await user.click(checkbox)

    const forceMergeBtn = screen.getByRole('button', { name: 'Force Merge' })
    expect(forceMergeBtn).toBeInTheDocument()
    await user.click(forceMergeBtn)

    expect(vi.mocked(pipelineAction)).toHaveBeenCalledWith({
      assignment_id: 'work-1',
      action: 'merge',
      force: true,
    })
  })

  it('shows ✓ Merged when current_stage is merged', async () => {
    renderDetail({ current_stage: 'merged' })

    await waitFor(() => {
      expect(screen.getByText(/✓ Merged/)).toBeInTheDocument()
    })
  })

  it('shows merge section for merge_ready even without explicit gate', async () => {
    renderDetail({ current_stage: 'merge_ready', available_gates: [] })

    await waitFor(() => {
      const mergeSection = screen.getByRole('region', { name: /merge/i })
      expect(mergeSection).toBeInTheDocument()
    })
  })
})

// ── Diff viewer ───────────────────────────────────────────────────────────────

describe('Detail — diff viewer', () => {
  it('diff section is collapsed by default', async () => {
    renderDetail()

    await waitFor(() => screen.getByText('Fix the thing'))

    // Diff toggle is present but content not shown
    const toggle = screen.getByRole('button', { name: /diff/i })
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByText(/Loading diff/)).not.toBeInTheDocument()
    expect(vi.mocked(fetchDiff)).not.toHaveBeenCalled()
  })

  it('expanding the diff triggers fetchDiff and shows content', async () => {
    const user = userEvent.setup()
    renderDetail()

    await waitFor(() => screen.getByText('Fix the thing'))

    const toggle = screen.getByRole('button', { name: /diff/i })
    await user.click(toggle)

    expect(toggle).toHaveAttribute('aria-expanded', 'true')

    await waitFor(() => {
      expect(vi.mocked(fetchDiff)).toHaveBeenCalledWith('work-1')
    })

    await waitFor(() => {
      expect(screen.getByLabelText('Code diff')).toBeInTheDocument()
    })
  })

  it('collapsing diff after expanding hides the content', async () => {
    const user = userEvent.setup()
    renderDetail()

    await waitFor(() => screen.getByText('Fix the thing'))

    const toggle = screen.getByRole('button', { name: /diff/i })
    await user.click(toggle)

    await waitFor(() => screen.getByLabelText('Code diff'))

    await user.click(toggle)
    expect(screen.queryByLabelText('Code diff')).not.toBeInTheDocument()
  })

  it('shows diff error when fetchDiff rejects', async () => {
    const user = userEvent.setup()
    renderDetail()
    await waitFor(() => screen.getByText('Fix the thing'))

    // Override diff mock to reject BEFORE the user expands the diff section
    vi.mocked(fetchDiff).mockRejectedValue(new Error('diff unavailable'))

    await user.click(screen.getByRole('button', { name: /diff/i }))

    await waitFor(() => {
      expect(screen.getByText(/Failed to load diff/)).toBeInTheDocument()
    })
  })
})

// ── Toast + in-flight ─────────────────────────────────────────────────────────

describe('Detail — toast and in-flight states', () => {
  it('shows a success toast after a successful action', async () => {
    const user = userEvent.setup()
    vi.mocked(pipelineAction).mockResolvedValue({ ok: true })

    renderDetail({
      available_gates: [{ action: 'test-verdict', label: 'Record test verdict', endpoint: '/api/pipeline/action' }],
    })

    await waitFor(() => screen.getByText('Pass'))
    await user.click(screen.getByText('Pass'))

    await waitFor(() => {
      expect(screen.getByRole('status')).toHaveTextContent(/marked passed/i)
    })
  })

  it('shows an error toast when the action returns ok:false', async () => {
    const user = userEvent.setup()
    renderDetail({
      available_gates: [{ action: 'test-verdict', label: 'Record test verdict', endpoint: '/api/pipeline/action' }],
    })

    // Override AFTER data loads so renderDetail's default ok:true mock is replaced
    await waitFor(() => screen.getByText('Pass'))
    vi.mocked(pipelineAction).mockResolvedValue({ ok: false, error: 'Board locked' })

    await user.click(screen.getByText('Pass'))

    await waitFor(() => {
      expect(screen.getByRole('status')).toHaveTextContent('Board locked')
    })
  })

  it('disables action buttons while an action is in-flight', async () => {
    const user = userEvent.setup()
    renderDetail({
      available_gates: [{ action: 'test-verdict', label: 'Record test verdict', endpoint: '/api/pipeline/action' }],
    })

    // Override AFTER data loads so renderDetail's default ok:true mock is replaced
    await waitFor(() => screen.getByText('Pass'))
    // Hang forever so we can inspect the in-flight state
    vi.mocked(pipelineAction).mockReturnValue(new Promise(() => undefined))

    await user.click(screen.getByText('Pass'))

    // The Pass button should show "Recording…" and be disabled
    await waitFor(() => {
      const passBtn = screen.getByRole('button', { name: /Recording/i })
      expect(passBtn).toBeDisabled()
    })
  })
})

// ── Misc gates ────────────────────────────────────────────────────────────────

describe('Detail — misc gate actions', () => {
  beforeEach(() => {
    vi.mocked(pipelineAction).mockResolvedValue({ ok: true })
  })

  it('shows Start Smoke Test button for dispatch_smoke gate', async () => {
    renderDetail({
      available_gates: [{ action: 'dispatch_smoke', label: 'Start smoke', endpoint: '/api/pipeline/action' }],
    })

    await waitFor(() => {
      expect(screen.getByText('Start Smoke Test')).toBeInTheDocument()
    })
  })

  it('shows Cancel (unstick) button for unstick gate', async () => {
    renderDetail({
      available_gates: [{ action: 'unstick', label: 'Unstick', endpoint: '/api/pipeline/action' }],
    })

    await waitFor(() => {
      expect(screen.getByText('Cancel (unstick)')).toBeInTheDocument()
    })
  })

  it('clicking unstick calls pipelineAction with unstick action', async () => {
    const user = userEvent.setup()
    renderDetail({
      available_gates: [{ action: 'unstick', label: 'Unstick', endpoint: '/api/pipeline/action' }],
    })

    await waitFor(() => screen.getByText('Cancel (unstick)'))
    await user.click(screen.getByText('Cancel (unstick)'))

    expect(vi.mocked(pipelineAction)).toHaveBeenCalledWith({
      assignment_id: 'work-1',
      action: 'unstick',
    })
  })
})
