/**
 * E2E tests for the SSE-driven live data layer (#1549).
 *
 * There's no Playwright `routeEventSource()` (unlike `routeWebSocket()`,
 * which `terminal.spec.ts` uses for the PTY bridge) and `page.route()` can
 * only fulfil a static, connection-closing body -- not a genuinely long-lived
 * push stream. So this suite injects a controllable fake `EventSource` as
 * `window.EventSource` via `page.addInitScript()`, the same technique
 * `terminal.spec.ts` uses for its fake WebSocket, just hand-rolled since
 * Playwright has no built-in for this transport. Every assertion here is
 * driven by explicit `emitOpen`/`emitError`/`emitMessage` calls standing in
 * for a real `coord web --fixture <board.json>` scripted SSE sequence
 * (`coord/dashboard/fixture.py`'s `ScriptedEvent` -- `{type, data, after}`
 * played over `/events`); the wire shape (`event: <type>` + JSON `data:`)
 * is identical, only the transport is faked, exactly like every other spec
 * in this suite fakes its own backend per playwright.config.ts's contract.
 *
 * Run:  npm run test:e2e
 */

import { test, expect, type Page } from '@playwright/test'

// ── Fake EventSource injection ──────────────────────────────────────────────

/**
 * Installs `window.EventSource` before any app script runs, and a
 * `window.__sse` control surface the test drives from Node via
 * `page.evaluate()`. Must be called before `page.goto()`.
 */
async function installFakeEventSource(page: Page): Promise<void> {
  await page.addInitScript(() => {
    class FakeEventSource {
      url: string
      closed = false
      onopen: ((ev: unknown) => void) | null = null
      onerror: ((ev: unknown) => void) | null = null
      private listeners: Record<string, Array<(ev: { data: string }) => void>> = {}

      constructor(url: string) {
        this.url = url
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        ;(window as any).__sse.instances.push(this)
      }

      addEventListener(type: string, cb: (ev: { data: string }) => void): void {
        ;(this.listeners[type] ??= []).push(cb)
      }

      close(): void {
        this.closed = true
      }

      emitOpen(): void {
        this.onopen?.({})
      }

      emitError(): void {
        this.onerror?.({})
      }

      emitMessage(type: string, data: unknown): void {
        for (const cb of this.listeners[type] ?? []) cb({ data: JSON.stringify(data) })
      }
    }

    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    ;(window as any).__sse = { instances: [] as FakeEventSource[] }
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    ;(window as any).EventSource = FakeEventSource
  })
}

/** Number of currently-open (non-superseded) fake EventSource instances. */
function openCount(page: Page): Promise<number> {
  return page.evaluate(() => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const sse = (window as any).__sse
    return sse.instances.filter((i: { closed: boolean }) => !i.closed).length
  })
}

/** Runs `fn` (by name) against the current open instance -- 'open' | 'error' | ['message', type, data]. */
async function emit(
  page: Page,
  action: 'open' | 'error' | { message: [string, unknown] },
): Promise<void> {
  await page.waitForFunction(() => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const sse = (window as any).__sse
    return sse.instances.filter((i: { closed: boolean }) => !i.closed).length === 1
  })
  await page.evaluate((act) => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const sse = (window as any).__sse
    const open = sse.instances.filter((i: { closed: boolean }) => !i.closed)
    const inst = open[open.length - 1]
    if (act === 'open') inst.emitOpen()
    else if (act === 'error') inst.emitError()
    else inst.emitMessage(act.message[0], act.message[1])
  }, action)
}

// ── Seeded pipeline data (mutable across the fetch lifetime) ────────────────

function makeItem(currentStage: string) {
  return {
    assignment_id: 'work-1',
    issue_number: 42,
    issue_title: 'Fix the dashboard rendering',
    repo_name: 'api',
    machine_name: 'laptop',
    current_stage: currentStage,
    stages: [
      { name: 'coding', status: currentStage === 'coding' ? 'active' : 'completed', is_current: currentStage === 'coding' },
      { name: 'review', status: 'waiting', is_current: false },
      { name: 'smoke', status: 'waiting', is_current: false },
      { name: 'merge', status: 'waiting', is_current: false },
    ],
    available_gates: [],
    progress_pct: 20,
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
  }
}

/** Serves /api/pipeline from a mutable box so a test can change it mid-run. */
async function mockPipeline(page: Page, box: { stage: string }): Promise<void> {
  await page.route('**/api/pipeline', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify([makeItem(box.stage)]),
    }),
  )
  await page.route('**/api/sessions', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: '[]' }),
  )
  await page.route('**/api/board', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ round_number: 1, active: [], completed: [] }),
    }),
  )
}

// ── Tests ────────────────────────────────────────────────────────────────────

test.describe('SSE live data layer (#1549)', () => {
  /**
   * The board-change acceptance criterion: a scripted SSE event changes what
   * the *next* /api/pipeline fetch would return, and the card updates with
   * no manual refresh action from the test -- only the invalidation the
   * event triggers.
   */
  test('a board change (assignment_failed) is reflected on Home with no manual refresh', async ({
    page,
  }) => {
    const pipelineState = { stage: 'coding' }
    await installFakeEventSource(page)
    await mockPipeline(page, pipelineState)

    await page.goto('/')

    // Initial state: running.
    await expect(page.getByText('running', { exact: true })).toBeVisible()

    await emit(page, 'open')

    // The server-side state changes (as if a worker just failed) and the
    // scripted event announces it -- exactly the shape
    // coord/events.py publish_assignment_failed / fixture.py ScriptedEvent
    // put on the wire.
    pipelineState.stage = 'failed'
    await emit(page, { message: ['assignment_failed', { assignment_id: 'work-1' }] })

    // No reload, no click -- the badge flips from the SSE-triggered refetch alone.
    await expect(page.getByText('failed', { exact: true })).toBeVisible({ timeout: 5_000 })
  })

  /**
   * The honest connection-state criterion: killing and restoring the stream
   * shows disconnected -> reconnecting -> live, via the header's
   * ConnectionBadge (role="status").
   */
  test('killing and restoring the stream shows disconnected -> reconnecting -> live', async ({
    page,
  }) => {
    await installFakeEventSource(page)
    await mockPipeline(page, { stage: 'coding' })

    await page.goto('/')

    const badge = page.getByRole('status', { name: /Connection:/ })
    await expect(badge).toBeVisible()

    await emit(page, 'open')
    await expect(badge).toHaveText('Live', { timeout: 5_000 })

    // Kill it.
    await emit(page, 'error')
    await expect(badge).toContainText('Stale since', { timeout: 5_000 })

    // Backoff fires (default schedule's first delay, ~1s +/- jitter) and a
    // fresh attempt starts.
    await expect(badge).toHaveText('Reconnecting…', { timeout: 5_000 })

    // Restore it.
    await emit(page, 'open')
    await expect(badge).toHaveText('Live', { timeout: 5_000 })
  })

  /**
   * Guards the StrictMode double-mount trap this module's doc comments
   * describe (connection.ts, RealtimeProvider.tsx): the dev server runs
   * React 18 StrictMode, so mounting Home for real must settle to exactly
   * one open stream, not leak a socket per re-render.
   */
  test('settles to exactly one open stream under StrictMode dev double-mount', async ({
    page,
  }) => {
    await installFakeEventSource(page)
    await mockPipeline(page, { stage: 'coding' })

    await page.goto('/')
    await expect(page.getByText('running', { exact: true })).toBeVisible()

    expect(await openCount(page)).toBe(1)
  })
})
