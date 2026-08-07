/**
 * Live update against a REAL `coord web --fixture` process (#1551, M-W1's
 * exit gate).
 *
 * `realtime.spec.ts` proves the app reacts correctly to the SSE wire shape
 * `coord/events.py` / `coord/dashboard/fixture.py` claim to emit, using a
 * hand-rolled fake `EventSource` -- fast and deterministic, but it can never
 * catch the wire shape itself drifting (a rename, a missing `event:` line, a
 * JSON encoding bug) because nothing on the other end is real. This file is
 * the one spec in the repo that boots the actual `coord web --fixture`
 * server (`fixtureServer.ts`) and drives a real browser `EventSource`
 * against it end to end:
 *
 *   1. a genuine `/events` connection reaches "Live" -- proves the real wire
 *      format parses;
 *   2. `POST /api/fixture/events/replay` (the fixture's scripted-event
 *      trigger, `coord/dashboard/fixture.py`'s `ScriptedEvent`) causes a
 *      real, unprompted `GET /api/pipeline` refetch -- "no manual refresh";
 *   3. killing the server process (not a route interception -- the actual
 *      subprocess) makes the badge honestly report the stream is gone.
 *
 * What this does NOT (and structurally cannot) prove: that the refetched
 * data itself changes. `FixtureServer.board()` rebuilds the same static
 * `board_payload` on every call by design (#1538's whole point is a board
 * that "does not move under" an assertion) -- a scripted event announces a
 * change, it does not enact one. So step 2 asserts the real live-update
 * *mechanism* (event -> invalidation -> refetch, with no test-driven
 * reload/click), not a visible content diff; `realtime.spec.ts` already
 * covers "the badge/card text actually changes" against a fake transport
 * that CAN mutate its own seed mid-test.
 *
 * Slower than the rest of this suite -- a production build plus a real
 * subprocess boot, on top of Playwright's own browser launch. Recorded here
 * rather than hidden: this file alone typically adds several seconds to
 * `npm run test:e2e`. If that ever becomes disruptive, split it into its own
 * `npm run test:e2e:live` script rather than skipping the coverage.
 *
 * Run: npm run test:e2e (requires `coord` on $PATH, see fixtureServer.ts)
 */
import { test, expect } from '@playwright/test'
import { startFixtureServer, type FixtureServerHandle } from './fixtureServer'

test.describe.configure({ mode: 'serial' })

test.describe('live update against a real coord web --fixture process (#1551)', () => {
  let server: FixtureServerHandle

  test.beforeAll(async () => {
    server = await startFixtureServer()
  })

  test.afterAll(async () => {
    await server?.stop()
  })

  test('a real SSE connection reaches Live, and a scripted event triggers an unprompted refetch', async ({
    page,
  }) => {
    const pipelineRequests: number[] = []
    page.on('request', (req) => {
      if (new URL(req.url()).pathname === '/api/pipeline') pipelineRequests.push(Date.now())
    })

    await page.goto(server.baseUrl)

    // Seeded fixture: one running item (#4101), among others -- proves the
    // real dist bundle booted against the real fixture data. `#4101` shows up
    // twice (the Live sessions card and the pipeline card both surface the
    // same seeded assignment) -- `.first()` only cares that at least one is
    // there, not which.
    await expect(page.getByText('#4101').first()).toBeVisible()

    const badge = page.getByRole('status', { name: /Connection:/ })
    await expect(badge).toHaveText('Live', { timeout: 10_000 })

    const requestsBeforeReplay = pipelineRequests.length
    expect(requestsBeforeReplay).toBeGreaterThan(0)

    // The fixture's scripted sequence (tests/fixtures/board-pipeline-basic.json
    // `events`) -- played over the REAL /events stream, not injected into the
    // page. Nothing here reloads the page or clicks a refresh control.
    const replay = await page.request.post(`${server.baseUrl}/api/fixture/events/replay`)
    expect(replay.ok()).toBe(true)

    await expect
      .poll(() => pipelineRequests.length, { timeout: 5_000 })
      .toBeGreaterThan(requestsBeforeReplay)
  })

  test('killing the server surfaces the disconnected state', async ({ page }) => {
    await page.goto(server.baseUrl)

    const badge = page.getByRole('status', { name: /Connection:/ })
    await expect(badge).toHaveText('Live', { timeout: 10_000 })

    // A real process death, not a route interception -- the honest-disconnect
    // criterion this test exists to prove is that the browser's own
    // `EventSource` onerror fires against a genuinely severed connection.
    server.kill()

    await expect(badge).toContainText('Stale since', { timeout: 10_000 })

    // Deliberately not also asserting "Reconnecting…" here: against a truly
    // dead port every retry fails via an essentially instant connection
    // refusal, so `connection.ts`'s 'reconnecting' state is a single-tick
    // flash between backoff timers rather than something Playwright's poll
    // can reliably sample -- flaky by construction, not by mistake. The
    // disconnected -> reconnecting -> live transition is already pinned
    // deterministically by `realtime.spec.ts`'s fake-EventSource test, where
    // the test (not a dead socket) controls exactly when each state change
    // happens.
  })
})
