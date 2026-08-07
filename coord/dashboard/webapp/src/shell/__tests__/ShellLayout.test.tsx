/**
 * Integration tests for the responsive shell (#1547) and its route tree
 * (#1548).
 *
 * These mount the *real* composition — a route tree shaped like `App.tsx`'s,
 * `ShellLayout`, `AppShell`, `ActivityRail` and the real Home/Detail panels —
 * with only the API client mocked, because the thing under test is precisely
 * how those fit together at each breakpoint and how the URL drives them.
 * Anything that asserted against a hand-rolled fixture shell would pass while
 * the app was broken.
 *
 * jsdom has no layout, so `stubViewport` drives `matchMedia` instead; see
 * `stubViewport.ts` for why that is the only lever available here.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, fireEvent, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Navigate, Routes, Route, useLocation } from 'react-router-dom'

import { ThemeProvider } from '@/components/ui/theme-provider'
import Detail from '@/components/Detail'
import SessionDetail from '@/components/SessionDetail'
import { type PipelineView, type SessionInfo } from '@/api/client'
import { paths } from '@/routes/paths'
import { ShellLayout } from '../ShellLayout'
import { EmptyDetail } from '../EmptyDetail'
import { LIST_WIDTH_DEFAULT_PX, SHELL_STORAGE_KEY } from '../shellState'
import { MEDIUM_PX, NARROW_PX, WIDE_PX, restoreViewport, stubViewportWidth } from './stubViewport'

vi.mock('@/api/client', () => ({
  fetchPipeline: vi.fn(),
  fetchSessions: vi.fn(),
  fetchDiff: vi.fn(),
  pipelineAction: vi.fn(),
}))

import { fetchPipeline, fetchSessions } from '@/api/client'

// ── fixtures ──────────────────────────────────────────────────────────────────

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
      { name: 'merge', status: 'waiting', is_current: false },
    ],
    available_gates: [],
    progress_pct: 10,
    review_findings_pending: false,
    review_verdict: null,
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

function makeSession(overrides: Partial<SessionInfo> = {}): SessionInfo {
  return {
    session_id: 'sess-1',
    session_name: 'coord-sess-1',
    machine: 'dellserver',
    host: 'dellserver.tailnet.ts.net',
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

/** Surfaces the router's current path in the DOM, for deep-link / back-forward assertions. */
function LocationProbe() {
  const location = useLocation()
  return <div data-testid="location-pathname">{location.pathname}</div>
}

function renderShell(initialPath = paths.pipeline()) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <ThemeProvider>
        <MemoryRouter initialEntries={[initialPath]}>
          <LocationProbe />
          <Routes>
            {/* Deliberately a *sibling* of ShellLayout, not a child of it —
                see App.tsx's doc comment: AppShell only mounts the detail
                slot (where a child route's element renders) when an item is
                selected, so a redirect nested under the shell would silently
                never fire on a narrow cold load at /. This mirrors the real
                route tree exactly so this test file can catch that class of
                bug instead of hiding it. */}
            <Route path="/" element={<Navigate to={paths.pipeline()} replace />} />
            <Route element={<ShellLayout />}>
              <Route path="/pipeline" element={<EmptyDetail />} />
              <Route path="/pipeline/:repo/:issue" element={<Detail />} />
              <Route path="/pipeline/:repo/:issue/:tab" element={<Detail />} />
              <Route path="/sessions" element={<EmptyDetail />} />
              <Route path="/sessions/:id" element={<SessionDetail />} />
              <Route path="/board" element={null} />
              <Route path="*" element={null} />
            </Route>
          </Routes>
        </MemoryRouter>
      </ThemeProvider>
    </QueryClientProvider>,
  )
}

const listRegion = () => screen.queryByRole('region', { name: 'List' })
const detailRegion = () => screen.queryByRole('main', { name: 'Detail' })
const railRegion = () => screen.getByRole('navigation', { name: 'Views' })
const locationPath = () => screen.getByTestId('location-pathname').textContent

/**
 * The list panel by DOM presence rather than by role.
 *
 * `queryByRole` walks the accessibility tree, so it cannot see the list once
 * the medium overlay marks it `aria-hidden` — which is exactly the state the
 * overlay tests need to assert *about*.
 */
const listElement = () => document.querySelector('[data-region="list"]')

beforeEach(() => {
  vi.clearAllMocks()
  window.localStorage.clear()
  vi.mocked(fetchPipeline).mockResolvedValue([makeView()])
  vi.mocked(fetchSessions).mockResolvedValue([makeSession()])
})

afterEach(() => {
  restoreViewport()
})

// ── wide ──────────────────────────────────────────────────────────────────────

describe('shell — wide (>= 1024px)', () => {
  beforeEach(() => stubViewportWidth(WIDE_PX))

  it('renders rail, list and detail simultaneously', async () => {
    renderShell()

    expect(railRegion()).toBeInTheDocument()
    expect(listRegion()).toBeInTheDocument()
    expect(detailRegion()).toBeInTheDocument()
    expect(await screen.findByText('Fix the thing')).toBeInTheDocument()
  })

  it('shows the whole programme in the rail, with unbuilt panels marked "soon"', async () => {
    renderShell()

    const rail = railRegion()
    // Built.
    expect(await screen.findByRole('button', { name: /Pipeline/ })).toBeInTheDocument()
    // Not built yet, but visibly coming rather than silently absent.
    for (const label of ['Board', 'Machines', 'Merge queue', 'Milestones', 'Audit', 'Spend']) {
      const item = screen.getByRole('button', { name: new RegExp(label) })
      expect(item).toHaveAttribute('aria-disabled', 'true')
    }
    expect(rail.textContent).toContain('soon')
  })

  it('badges the Pipeline entry with the in-flight count', async () => {
    vi.mocked(fetchPipeline).mockResolvedValue([
      makeView({ assignment_id: 'a' }),
      makeView({ assignment_id: 'b' }),
      makeView({ assignment_id: 'c', current_stage: 'merged' }),
    ])
    renderShell()

    // Two in flight; the merged one doesn't count.
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /^Pipeline/ })).toHaveTextContent('2'),
    )
  })

  it('shows the empty-selection placeholder in the detail column at /pipeline', async () => {
    renderShell()
    expect(await screen.findByText('Nothing selected')).toBeInTheDocument()
  })

  it('fills the detail column from the route without unmounting the list', async () => {
    renderShell(paths.pipelineItem('myrepo', 42))

    expect(listRegion()).toBeInTheDocument()
    // The row in the list *and* the heading in the detail — the wide layout's
    // whole point is that both are on screen at once.
    await waitFor(() => expect(screen.getAllByText('Fix the thing').length).toBe(2))
  })

  it('cycles focus rail -> list -> detail -> rail on F6', async () => {
    renderShell()
    await screen.findByText('Fix the thing')

    fireEvent.keyDown(window, { key: 'F6' })
    expect(document.activeElement).toBe(railRegion())

    fireEvent.keyDown(window, { key: 'F6' })
    expect(document.activeElement).toBe(listRegion())

    fireEvent.keyDown(window, { key: 'F6' })
    expect(document.activeElement).toBe(detailRegion())

    fireEvent.keyDown(window, { key: 'F6' })
    expect(document.activeElement).toBe(railRegion())
  })

  it('cycles backwards on Shift+F6', async () => {
    renderShell()
    await screen.findByText('Fix the thing')

    fireEvent.keyDown(window, { key: 'F6' })
    expect(document.activeElement).toBe(railRegion())

    fireEvent.keyDown(window, { key: 'F6', shiftKey: true })
    expect(document.activeElement).toBe(detailRegion())

    fireEvent.keyDown(window, { key: 'F6', shiftKey: true })
    expect(document.activeElement).toBe(listRegion())
  })

  it('resizes the list from the keyboard and persists the new width', async () => {
    renderShell()
    const separator = await screen.findByRole('separator', { name: 'Resize list panel' })
    expect(separator).toHaveAttribute('aria-valuenow', String(LIST_WIDTH_DEFAULT_PX))

    fireEvent.keyDown(separator, { key: 'ArrowRight' })
    expect(separator).toHaveAttribute('aria-valuenow', String(LIST_WIDTH_DEFAULT_PX + 16))

    fireEvent.keyDown(separator, { key: 'End' })
    const persisted = JSON.parse(window.localStorage.getItem(SHELL_STORAGE_KEY) ?? '{}')
    expect(persisted.listWidthPx).toBe(640)
  })

  it('restores a persisted panel width from localStorage; the URL (not localStorage) picks the view', async () => {
    window.localStorage.setItem(
      SHELL_STORAGE_KEY,
      JSON.stringify({ listWidthPx: 480, railCollapsed: true }),
    )
    renderShell(paths.sessions())

    const separator = await screen.findByRole('separator', { name: 'Resize list panel' })
    expect(separator).toHaveAttribute('aria-valuenow', '480')
    // The Sessions view, because that's what the URL says — not because
    // anything was persisted about "the view" (that concept no longer exists,
    // see shellState.ts).
    expect(await screen.findByRole('heading', { name: 'Sessions' })).toBeInTheDocument()
  })
})

// ── narrow ────────────────────────────────────────────────────────────────────

describe('shell — narrow (< 768px): the phone app, preserved', () => {
  beforeEach(() => stubViewportWidth(NARROW_PX))

  it('mounts only the list at /pipeline', async () => {
    renderShell()

    expect(listRegion()).toBeInTheDocument()
    expect(detailRegion()).not.toBeInTheDocument()
    expect(await screen.findByText('Fix the thing')).toBeInTheDocument()
  })

  it('mounts only the detail after a cold-loaded deep link', async () => {
    renderShell(paths.pipelineItem('myrepo', 42))

    await waitFor(() => expect(detailRegion()).toBeInTheDocument())
    expect(listRegion()).not.toBeInTheDocument()
    // Exactly one — the list is gone, so no duplicated row text.
    expect(await screen.findAllByText('Fix the thing')).toHaveLength(1)
  })

  it('drill-in via the list pushes a history entry, and Back goes up (not out of the app)', async () => {
    renderShell()
    expect(locationPath()).toBe(paths.pipeline())

    await userEvent.click(await screen.findByText('Fix the thing'))
    await waitFor(() => expect(detailRegion()).toBeInTheDocument())
    expect(listRegion()).not.toBeInTheDocument()
    expect(locationPath()).toBe(paths.pipelineItem('myrepo', 42))

    const back = screen.getByLabelText('Back')
    await userEvent.click(back)

    // Back peels one level -- to the list at /pipeline -- not out of the SPA.
    await waitFor(() => expect(listRegion()).toBeInTheDocument())
    expect(detailRegion()).not.toBeInTheDocument()
    expect(locationPath()).toBe(paths.pipeline())
  })

  it('shows only built views in the bottom row', async () => {
    renderShell()
    await screen.findByText('Fix the thing')

    expect(screen.getByRole('button', { name: /Pipeline/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Sessions/ })).toBeInTheDocument()
    // A dimmed, un-tappable placeholder is noise on a phone's bottom nav.
    expect(screen.queryByRole('button', { name: /Milestones/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^Spend/ })).not.toBeInTheDocument()
  })

  it('offers no panel separator or rail collapse', async () => {
    renderShell()
    await screen.findByText('Fix the thing')

    expect(screen.queryByRole('separator', { name: 'Resize list panel' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Collapse rail/ })).not.toBeInTheDocument()
  })

  it('cycles F6 over only the two mounted regions', async () => {
    renderShell()
    await screen.findByText('Fix the thing')

    fireEvent.keyDown(window, { key: 'F6' })
    expect(document.activeElement).toBe(railRegion())
    fireEvent.keyDown(window, { key: 'F6' })
    expect(document.activeElement).toBe(listRegion())
    fireEvent.keyDown(window, { key: 'F6' })
    expect(document.activeElement).toBe(railRegion())
  })
})

// ── medium ────────────────────────────────────────────────────────────────────

describe('shell — medium (768–1023px)', () => {
  beforeEach(() => stubViewportWidth(MEDIUM_PX))

  it('keeps the list mounted under the detail overlay instead of replacing it', async () => {
    renderShell(paths.pipelineItem('myrepo', 42))

    const list = listElement()
    expect(list).toBeInTheDocument()
    expect(detailRegion()).toBeInTheDocument()
    // The list is behind the sheet: hidden from assistive tech and taken out
    // of the tab order, not merely painted over.
    expect(list).toHaveAttribute('aria-hidden', 'true')
    expect(list).toHaveAttribute('inert')
  })

  it('leaves the list interactive when nothing is selected', async () => {
    renderShell()
    await screen.findByText('Fix the thing')

    expect(listElement()).not.toHaveAttribute('aria-hidden')
    expect(listElement()).not.toHaveAttribute('inert')
  })

  it('pins the rail to its icon strip — no labels, no collapse toggle', async () => {
    renderShell()
    await screen.findByText('Fix the thing')

    // Icon-only: the entry is still there and still named, but the visible
    // label text is gone, as is the toggle that would do nothing here.
    expect(screen.getByRole('button', { name: /Pipeline/ })).toBeInTheDocument()
    expect(railRegion().textContent).not.toContain('soon')
    expect(screen.queryByRole('button', { name: /Collapse rail/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('separator', { name: 'Resize list panel' })).not.toBeInTheDocument()
  })
})

// ── view selection ────────────────────────────────────────────────────────────

describe('shell — activity rail view selection', () => {
  beforeEach(() => stubViewportWidth(WIDE_PX))

  it("swaps the list panel by navigating to the view's own URL", async () => {
    const user = userEvent.setup()
    renderShell()
    await screen.findByRole('heading', { name: 'Pipeline' })
    expect(locationPath()).toBe(paths.pipeline())

    await user.click(screen.getByRole('button', { name: /^Sessions/ }))

    expect(await screen.findByRole('heading', { name: 'Sessions' })).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Pipeline' })).not.toBeInTheDocument()
    expect(locationPath()).toBe(paths.sessions())
  })

  it('ignores a click on an unbuilt view', async () => {
    const user = userEvent.setup()
    renderShell()
    await screen.findByRole('heading', { name: 'Pipeline' })

    await user.click(screen.getByRole('button', { name: /Board/ }))

    expect(screen.getByRole('heading', { name: 'Pipeline' })).toBeInTheDocument()
    expect(locationPath()).toBe(paths.pipeline())
  })

  it('collapses and restores the list column from the rail', async () => {
    const user = userEvent.setup()
    renderShell()
    await screen.findByRole('heading', { name: 'Pipeline' })

    await user.click(screen.getByRole('button', { name: 'Minimize list panel' }))
    await waitFor(() => expect(listRegion()).not.toBeInTheDocument())
    expect(detailRegion()).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Show list panel' }))
    await waitFor(() => expect(listRegion()).toBeInTheDocument())
  })

  it('collapses the rail to icons and remembers it', async () => {
    const user = userEvent.setup()
    renderShell()
    await screen.findByRole('heading', { name: 'Pipeline' })

    await user.click(screen.getByRole('button', { name: 'Collapse rail' }))

    expect(screen.getByRole('button', { name: 'Expand rail' })).toBeInTheDocument()
    const persisted = JSON.parse(window.localStorage.getItem(SHELL_STORAGE_KEY) ?? '{}')
    expect(persisted.railCollapsed).toBe(true)
  })
})

// ── deep links (#1548) ───────────────────────────────────────────────────────

describe('shell — deep links and route tree', () => {
  beforeEach(() => stubViewportWidth(WIDE_PX))

  it('redirects a cold load of / to /pipeline', async () => {
    renderShell('/')
    await waitFor(() => expect(locationPath()).toBe(paths.pipeline()))
    expect(await screen.findByRole('heading', { name: 'Pipeline' })).toBeInTheDocument()
  })

  it('cold-loads a pipeline item deep link straight into the detail column', async () => {
    renderShell(paths.pipelineItem('myrepo', 42))

    const detail = detailRegion()
    expect(detail).toBeInTheDocument()
    expect(await within(detail!).findByText('Fix the thing')).toBeInTheDocument()
    // The list is on screen too (wide) -- both agree on the same item.
    expect(listRegion()).toBeInTheDocument()
  })

  it('cold-loads a deep link with a tab segment without crashing or losing the item', async () => {
    renderShell(paths.pipelineItem('myrepo', 42, 'log'))

    expect(locationPath()).toBe('/pipeline/myrepo/42/log')
    // Tab *content* is M-W2 scope -- the acceptance surface for this story is
    // that the route resolves to the right item and doesn't 404 or crash.
    await waitFor(() => expect(screen.getAllByText('Fix the thing').length).toBeGreaterThan(0))
  })

  it('cold-loads a session deep link into the detail column', async () => {
    renderShell(paths.session('sess-1'))

    const detail = detailRegion()
    expect(detail).toBeInTheDocument()
    expect(await within(detail!).findByText('Live session issue')).toBeInTheDocument()
    expect(within(detail!).getByRole('button', { name: 'Take over' })).toBeInTheDocument()
  })

  it('shows a real not-found state for a stale issue rather than a blank panel', async () => {
    renderShell(paths.pipelineItem('myrepo', 999))

    expect(await screen.findByText(/not found in the pipeline/i)).toBeInTheDocument()
    // The list is still there and still usable -- a stale link doesn't take
    // down the rest of the shell.
    expect(listRegion()).toBeInTheDocument()
  })

  it('shows a real not-found state for a session id that is no longer live', async () => {
    renderShell(paths.session('does-not-exist'))

    expect(await screen.findByText(/no longer live/i)).toBeInTheDocument()
  })

  it('renders a not-found state, not a blank panel or a crash, for an unknown route', async () => {
    renderShell('/this/route/does/not/exist')

    expect(await screen.findByText('Page not found')).toBeInTheDocument()
    expect(railRegion()).toBeInTheDocument()
  })
})

// ── deep links at narrow (regression coverage for the / redirect) ───────────

/**
 * `AppShell` only mounts the detail slot — the thing a child route's
 * `element` renders into — when `showDetail` is true, and on narrow that's
 * `false` until an item is selected. A `/` -> `/pipeline` redirect declared
 * as a *child* of `ShellLayout` would therefore silently never fire on a
 * phone-sized cold load: `<Navigate>`'s effect never runs because it's never
 * mounted. This exact bug shipped past an all-wide-viewport test suite
 * (every other deep-link test in this file stubs `WIDE_PX`, where the detail
 * slot is unconditionally mounted) and only showed up in a real browser at a
 * phone width. `App.tsx` fixes it by declaring the redirect as a *sibling* of
 * `ShellLayout`; this test (at `NARROW_PX`) is what would have caught the
 * regression before it shipped.
 */
describe('shell — deep links at narrow', () => {
  beforeEach(() => stubViewportWidth(NARROW_PX))

  it('redirects a cold load of / to /pipeline on narrow, where the detail slot starts unmounted', async () => {
    renderShell('/')
    await waitFor(() => expect(locationPath()).toBe(paths.pipeline()))
    expect(await screen.findByText('Fix the thing')).toBeInTheDocument()
    expect(screen.queryByText('Page not found')).not.toBeInTheDocument()
  })
})
