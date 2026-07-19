/**
 * E2E tests for the phone terminal takeover flow (#1072).
 *
 * Drives the complete open → PTY output renders → key-bar key → detach
 * flow against a fake WebSocket — no real ssh/tmux or Python daemon required.
 *
 * Uses page.routeWebSocket() (Playwright ≥ 1.48) to intercept
 * /ws/terminal/{session_id}: the async handler keeps the connection alive,
 * echoes a banner, and records incoming keystrokes.  The async form is
 * required: if the handler returns synchronously Playwright auto-closes the
 * WS, which also triggers React 18 StrictMode's double-invoke cleanup.
 *
 * Run:  npm run test:e2e
 */

import { test, expect } from '@playwright/test'

// ── Seeded data ───────────────────────────────────────────────────────────────

const SESSION_ID = 'work-abc-1'

/** One in-progress interactive session surfaced by GET /api/sessions. */
const SEEDED_SESSION = {
  session_id: SESSION_ID,
  session_name: `coord-${SESSION_ID}`,
  machine: 'laptop',
  host: null,
  repo: 'api',
  issue: 42,
  issue_title: 'Implement terminal takeover',
  stage: 'work',
  status: 'running',
  attached: false,
  pane_dead: false,
}

/**
 * Banner the fake PTY sends immediately on connect.
 * Chosen to be unambiguous in the xterm-rows DOM (no HTML metacharacters).
 */
const FAKE_PTY_BANNER = 'PTY-E2E-BANNER-OK\r\n'

// ── Helpers ───────────────────────────────────────────────────────────────────

/** Silence /api/pipeline and /api/board so Home renders without errors. */
async function mockBasicApis(page: import('@playwright/test').Page): Promise<void> {
  await page.route('**/api/pipeline', (route) =>
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

/**
 * Register an async fake-PTY WebSocket route for SESSION_ID.
 *
 * The handler is async and awaits a Promise that resolves when the browser
 * closes the WS — this is critical: if the handler returns synchronously,
 * Playwright closes the route (and therefore the WS) immediately, which
 * causes React 18's StrictMode double-invoke to see both mounts hit a
 * "Disconnected" state before the test can observe "Live".
 *
 * With the async handler, the lifecycle is:
 *   StrictMode mount #1 → WS opens → handler awaits → StrictMode cleanup
 *   closes WS → onClose callback fires → Promise resolves → handler returns
 *   StrictMode mount #2 → WS opens → handler awaits → WS stays alive
 *   (no more cleanups) → test proceeds normally
 *
 * Returns shared state updated by the route callbacks.
 */
async function routeFakePty(
  page: import('@playwright/test').Page,
  opts: { banner?: string } = {},
): Promise<{ receivedMessages: (string | Buffer)[]; closeCount: number[] }> {
  const banner = opts.banner ?? FAKE_PTY_BANNER
  const receivedMessages: (string | Buffer)[] = []
  const closeCount = [0] // boxed so the route callback mutates it in-place

  await page.routeWebSocket(`**/ws/terminal/${SESSION_ID}`, async (ws) => {
    // Clear per-connection state so each StrictMode re-mount starts fresh.
    receivedMessages.length = 0

    ws.send(banner)
    ws.onMessage((msg) => receivedMessages.push(msg))

    // Await closure — this keeps the WS alive until the browser closes it.
    await new Promise<void>((resolve) => {
      ws.onClose(() => {
        closeCount[0]++
        resolve()
      })
    })
  })

  return { receivedMessages, closeCount }
}

// ── Tests ─────────────────────────────────────────────────────────────────────

test.describe('terminal takeover flow (#1072)', () => {
  /**
   * Core flow: home → session card → terminal pane → PTY output visible →
   * key-bar Esc → byte reaches PTY → close → clean detach (WS closed, no
   * tmux-session kill).
   *
   * Guards the entire open-terminal path as described in the issue:
   *   - SessionCard click navigates to /terminal/:id
   *   - Binary bytes pushed by the fake PTY appear in the xterm DOM
   *   - MobileKeyBar soft keys transmit the correct byte sequences
   *   - Closing the pane fires a clean WebSocket close (= detach) rather than
   *     an abrupt drop, and navigates the user back to the home screen.
   *
   * The "session NOT killed" invariant is enforced server-side by
   * `attached.detach()` never calling `tmux kill-session` (#1065); here we
   * verify the client-side half: ws.close() is called (clean close) rather
   * than the page being destroyed, which is what the server's `finally` block
   * relies on to trigger detach().
   */
  test('open-terminal → PTY output renders → key-bar Esc → detach on close', async ({
    page,
  }) => {
    // ── 1. Seed REST APIs ────────────────────────────────────────────────────

    await mockBasicApis(page)

    // The home screen Live sessions section calls GET /api/sessions.
    await page.route('**/api/sessions', (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify([SEEDED_SESSION]),
      }),
    )

    // ── 2. Intercept the WebSocket with a fake PTY ───────────────────────────

    const { receivedMessages, closeCount } = await routeFakePty(page)

    // ── 3. Home screen: Live sessions section must show the session card ─────

    await page.goto('/')

    // The SessionCard renders the issue_title as its button text.
    const sessionCard = page.getByRole('button', { name: 'Implement terminal takeover' })
    await expect(sessionCard).toBeVisible()

    // The "live" status badge (lowercase, exact to avoid matching the "Live
    // sessions" section heading — strict mode forbids multi-element matches).
    await expect(page.getByText('live', { exact: true })).toBeVisible()

    // Clicking the card navigates to /terminal/:sessionId.
    await sessionCard.click()

    // ── 4. Terminal view: URL and status label ───────────────────────────────

    await expect(page).toHaveURL(new RegExp(SESSION_ID))

    // "Live" appears once the WebSocket opens (ws.onopen → setState('open')).
    // The async route handler keeps WS #2 alive through React StrictMode's
    // double-invoke so this state is stable by the time we assert it.
    await expect(page.getByText('Live')).toBeVisible({ timeout: 5_000 })

    // ── 5. PTY output must render in the xterm DOM ───────────────────────────

    // xterm.js (v6, DOM renderer — no renderer addon loaded) writes each
    // character into spans inside .xterm-rows.  toContainText() walks the full
    // subtree so it finds the text even when it's split across cells.
    await expect(page.locator('[data-testid="xterm-container"]')).toContainText(
      'PTY-E2E-BANNER-OK',
      { timeout: 5_000 },
    )

    // ── 6. MobileKeyBar: Esc key must reach the fake PTY ─────────────────────

    await page.getByRole('button', { name: 'Escape' }).click()

    // The Esc key sends '\x1b' via TextEncoder → binary WS frame.
    // Playwright delivers binary frames as Buffers.  poll() re-evaluates
    // until the message arrives (short round-trip delay through the WS
    // routing layer).
    await expect
      .poll(
        () =>
          receivedMessages.some((m) => {
            const buf: Buffer = typeof m === 'string' ? Buffer.from(m, 'utf8') : (m as Buffer)
            return buf.includes(0x1b) // ESC byte
          }),
        { timeout: 2_000, message: 'expected ESC (0x1b) byte in received WS messages' },
      )
      .toBe(true)

    // ── 7. Close → clean detach ──────────────────────────────────────────────

    // The ✕ button in Terminal.tsx calls ws.close() (detach) then navigate(-1).
    // We verify: (a) another WS close event fires, (b) URL reverts.
    const closeCountBefore = closeCount[0]

    await page.getByRole('button', { name: 'Close terminal' }).click()

    // A new WS close must happen beyond any StrictMode-induced close.
    await expect
      .poll(
        () => closeCount[0] > closeCountBefore,
        { timeout: 2_000, message: 'expected WebSocket close event after clicking ✕' },
      )
      .toBe(true)

    // navigate(-1) must take the user back to the home screen.
    await expect(page).not.toHaveURL(new RegExp(SESSION_ID))
  })

  /**
   * Session-ended path: when the bridge closes the WebSocket with the
   * "session is gone for good" code (4404 / SESSION_GONE_CODE — see
   * coord/dashboard/server.py:terminal_ws, sent when resolve_session_target
   * no longer finds a live assignment), the status label must flip to
   * "Session ended".
   *
   * Guards the ws.onclose → (event.code === 4404) → setState('ended') →
   * STATUS_LABEL['ended'] path in Terminal.tsx — the user should always know
   * when the session has ended for good rather than seeing a frozen terminal
   * with no feedback. 4404 is a terminal state: unlike other closes it is NOT
   * retried, so this asserts the deterministic end-of-session feedback (a
   * plain close, by contrast, is a transient drop → "Reconnecting…").
   */
  test('terminal shows "Session ended" when the bridge closes with 4404', async ({
    page,
  }) => {
    // Fake bridge that delivers one line then closes with the session-gone
    // code (4404), the way server.py signals a session that no longer resolves.
    await page.routeWebSocket(`**/ws/terminal/${SESSION_ID}`, async (ws) => {
      ws.send('session has ended\r\n')

      // Await a timer then close — gives the banner time to land before the
      // close frame races it in the browser's event loop.
      await new Promise<void>((resolve) => {
        setTimeout(() => {
          ws.close({ code: 4404 })
          resolve()
        }, 80)
      })
    })

    // Navigate directly to the terminal view (no need to go through Home).
    await page.goto(`/terminal/${SESSION_ID}`)

    // The terminal must surface the end-of-session state once the 4404 close
    // arrives (ws.onclose → event.code === SESSION_GONE_CODE → setState('ended')):
    // a role="status" overlay tells the user the session is gone for good
    // instead of leaving a frozen pane. ("Session ended" also appears in the
    // header status label, so scope to the overlay to stay unambiguous.)
    await expect(page.getByRole('status')).toContainText('Session ended', {
      timeout: 5_000,
    })
  })

  /**
   * Key-bar completeness: all four arrow keys must transmit their ANSI escape
   * sequences to the fake PTY.
   *
   * Guards the four arrow SoftKeys in MobileKeyBar and their KEY_BYTES mappings
   * (\x1b[A/B/C/D).  A single key being wired to the wrong sequence, or the
   * onSend prop not being threaded correctly, would fail here without cluttering
   * the main flow test.
   */
  test('MobileKeyBar arrow keys transmit correct ANSI sequences', async ({ page }) => {
    const { receivedMessages } = await routeFakePty(page, { banner: 'ready\r\n' })

    await page.goto(`/terminal/${SESSION_ID}`)

    // Wait for the WS to be open (state = 'Live') before sending keys.
    await expect(page.getByText('Live')).toBeVisible({ timeout: 5_000 })

    const arrowCases: Array<{ label: string; seq: number[] }> = [
      { label: 'Up',    seq: [0x1b, 0x5b, 0x41] }, // \x1b[A
      { label: 'Down',  seq: [0x1b, 0x5b, 0x42] }, // \x1b[B
      { label: 'Right', seq: [0x1b, 0x5b, 0x43] }, // \x1b[C
      { label: 'Left',  seq: [0x1b, 0x5b, 0x44] }, // \x1b[D
    ]

    for (const { label, seq } of arrowCases) {
      const before = receivedMessages.length
      await page.getByRole('button', { name: label }).click()

      await expect
        .poll(
          () => {
            const fresh = receivedMessages.slice(before)
            return fresh.some((m) => {
              const buf: Buffer = typeof m === 'string' ? Buffer.from(m, 'utf8') : (m as Buffer)
              return seq.every((byte, i) => buf[i] === byte)
            })
          },
          { timeout: 2_000, message: `expected ${label} arrow ANSI sequence in WS messages` },
        )
        .toBe(true)
    }
  })
})
