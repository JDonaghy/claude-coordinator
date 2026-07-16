/**
 * Component tests for Terminal — the live take-over pane for a `coord-*`
 * session (#1068). `WebSocket` is mocked end-to-end (no real network/PTY);
 * this covers mount -> connect -> render -> teardown per the issue's
 * acceptance criterion. Full browser/terminal-protocol coverage (raw mode,
 * SGR mouse) is out of `TestBackend`-equivalent reach here and is the
 * Playwright E2E issue in group C.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, waitFor, act, fireEvent } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import Terminal from '@/components/Terminal'

// ── Fake WebSocket ───────────────────────────────────────────────────────────

class FakeWebSocket {
  static readonly CONNECTING = 0
  static readonly OPEN = 1
  static readonly CLOSING = 2
  static readonly CLOSED = 3
  static instances: FakeWebSocket[] = []

  url: string
  readyState = FakeWebSocket.CONNECTING
  binaryType = 'blob'
  sent: Array<string | ArrayBuffer | Uint8Array> = []
  closed = false
  onopen: (() => void) | null = null
  onclose: ((event: { code: number }) => void) | null = null
  onerror: (() => void) | null = null
  onmessage: ((event: { data: unknown }) => void) | null = null

  constructor(url: string) {
    this.url = url
    FakeWebSocket.instances.push(this)
  }

  send(data: string | ArrayBuffer | Uint8Array) {
    this.sent.push(data)
  }

  // A locally-initiated close (the ✕ button / unmount calling ws.close())
  // -- code defaults to the normal-closure value real browsers use.
  close(code = 1000) {
    this.closed = true
    this.readyState = FakeWebSocket.CLOSED
    this.onclose?.({ code })
  }

  // ── Test helpers, driving the fake from the "server" side. Wrapped in
  // act() since they synchronously trigger a React state update
  // (setState('open'/'reconnecting'/'ended')) outside of React's own event
  // handling. ──
  simulateOpen() {
    act(() => {
      this.readyState = FakeWebSocket.OPEN
      this.onopen?.()
    })
  }

  simulateMessage(data: unknown) {
    act(() => {
      this.onmessage?.({ data })
    })
  }

  // Simulates the "server"/network dropping the connection out from under
  // the client -- e.g. a transient network drop (no particular code, or an
  // abnormal-closure-ish one) vs. the bridge's session-gone signal (4404).
  simulateClose(code: number) {
    act(() => {
      this.closed = true
      this.readyState = FakeWebSocket.CLOSED
      this.onclose?.({ code })
    })
  }
}

function renderTerminal(sessionId: string) {
  return render(
    <MemoryRouter initialEntries={[`/terminal/${sessionId}`]}>
      <Routes>
        <Route path="/terminal/:sessionId" element={<Terminal />} />
      </Routes>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  FakeWebSocket.instances = []
  vi.stubGlobal('WebSocket', FakeWebSocket)
})

describe('Terminal', () => {
  it('mounts and connects a WebSocket to /ws/terminal/{sessionId}', () => {
    renderTerminal('work-2')

    expect(screen.getByTestId('xterm-container')).toBeInTheDocument()
    expect(FakeWebSocket.instances).toHaveLength(1)
    expect(FakeWebSocket.instances[0].url).toContain('/ws/terminal/work-2')
  })

  it('sends a resize control frame once the socket opens', () => {
    renderTerminal('work-2')
    const ws = FakeWebSocket.instances[0]

    ws.simulateOpen()

    const resizeFrame = ws.sent.find(
      (frame) => typeof frame === 'string' && frame.includes('"type":"resize"'),
    )
    expect(resizeFrame).toBeDefined()
  })

  it('renders PTY output pushed over the WebSocket', async () => {
    renderTerminal('work-2')
    const ws = FakeWebSocket.instances[0]
    ws.simulateOpen()

    ws.simulateMessage(new TextEncoder().encode('hello from pty').buffer)

    await waitFor(() =>
      expect(screen.getByTestId('xterm-container')).toHaveTextContent('hello from pty'),
    )
  })

  it('closes the WebSocket (hitting the bridge detach path) when the close control is pressed', async () => {
    renderTerminal('work-2')
    const ws = FakeWebSocket.instances[0]
    ws.simulateOpen()

    await userEvent.click(screen.getByRole('button', { name: 'Close terminal' }))

    expect(ws.closed).toBe(true)
  })

  it('closes the WebSocket on unmount', () => {
    const { unmount } = renderTerminal('work-2')
    const ws = FakeWebSocket.instances[0]
    ws.simulateOpen()

    unmount()

    expect(ws.closed).toBe(true)
  })

  it('wires MobileKeyBar soft keys to the same WebSocket write path as typed input (#1070)', async () => {
    renderTerminal('work-2')
    const ws = FakeWebSocket.instances[0]
    ws.simulateOpen()

    await userEvent.click(screen.getByRole('button', { name: 'Ctrl-C' }))

    // `TextEncoder` (used by both `term.onData` and `MobileKeyBar`'s
    // wiring) returns a Uint8Array from the environment's own realm, which
    // isn't always `instanceof` the test file's `Uint8Array` under jsdom --
    // decode it back instead of asserting the constructor identity.
    const sent = ws.sent[ws.sent.length - 1] as ArrayBufferLike
    expect(new TextDecoder().decode(sent)).toBe('\x03')
  })

  it('does not write to a WebSocket that is not open yet', async () => {
    renderTerminal('work-2')
    const ws = FakeWebSocket.instances[0]
    // Still 'connecting' -- no simulateOpen().

    await userEvent.click(screen.getByRole('button', { name: 'Escape' }))

    expect(ws.sent).toHaveLength(0)
  })

  // ── #1071: reconnect / detach resilience ──────────────────────────────

  describe('reconnect resilience (#1071)', () => {
    beforeEach(() => {
      vi.useFakeTimers()
    })

    afterEach(() => {
      vi.useRealTimers()
    })

    it('reconnects with backoff after an unexpected drop and resumes streaming', async () => {
      renderTerminal('work-2')
      const first = FakeWebSocket.instances[0]
      first.simulateOpen()

      // Abnormal closure -- e.g. wifi<->cellular handoff, tab backgrounded.
      first.simulateClose(1006)

      expect(screen.getByText(/work-2/)).toHaveTextContent('Reconnecting…')
      expect(FakeWebSocket.instances).toHaveLength(1)

      // First backoff step (1000ms) elapses -- a fresh WebSocket opens to
      // the *same* session_id.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1000)
      })

      expect(FakeWebSocket.instances).toHaveLength(2)
      const second = FakeWebSocket.instances[1]
      expect(second.url).toContain('/ws/terminal/work-2')

      second.simulateOpen()
      expect(screen.getByText(/work-2/)).toHaveTextContent('Live')

      second.simulateMessage(new TextEncoder().encode('resumed').buffer)
      // xterm.js flushes writes via its own internal queue (a timer under
      // the hood), so nudge fake time forward to let it drain -- `waitFor`'s
      // poll loop also relies on real timers and would stall here.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(50)
      })
      expect(screen.getByTestId('xterm-container')).toHaveTextContent('resumed')
    })

    it('backs off exponentially across repeated drops', async () => {
      renderTerminal('work-2')
      FakeWebSocket.instances[0].simulateOpen()
      FakeWebSocket.instances[0].simulateClose(1006)

      // First retry fires at 1000ms...
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1000)
      })
      expect(FakeWebSocket.instances).toHaveLength(2)

      // ...and drops again without ever reaching 'open' (attempt count
      // keeps climbing), so the *next* retry waits 2000ms, not 1000ms.
      FakeWebSocket.instances[1].simulateClose(1006)

      await act(async () => {
        await vi.advanceTimersByTimeAsync(1000)
      })
      expect(FakeWebSocket.instances).toHaveLength(2) // not yet -- only 1s of a 2s wait

      await act(async () => {
        await vi.advanceTimersByTimeAsync(1000)
      })
      expect(FakeWebSocket.instances).toHaveLength(3)
    })

    it('shows a session-ended state and never retries when the bridge reports the session is gone (4404)', async () => {
      renderTerminal('work-2')
      const ws = FakeWebSocket.instances[0]
      ws.simulateOpen()

      ws.simulateClose(4404)

      expect(screen.getByText(/work-2/)).toHaveTextContent('Session ended')
      expect(screen.getByRole('status')).toHaveTextContent('Session ended')

      // No reconnect attempt now or ever, however long we wait.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(60_000)
      })
      expect(FakeWebSocket.instances).toHaveLength(1)
    })

    it('does not reconnect after a deliberate close (close button)', async () => {
      renderTerminal('work-2')
      const ws = FakeWebSocket.instances[0]
      ws.simulateOpen()

      // fireEvent, not userEvent -- userEvent's own internal delays use
      // real timers under the hood and would hang against vi.useFakeTimers().
      act(() => {
        fireEvent.click(screen.getByRole('button', { name: 'Close terminal' }))
      })
      expect(ws.closed).toBe(true)

      await act(async () => {
        await vi.advanceTimersByTimeAsync(60_000)
      })
      expect(FakeWebSocket.instances).toHaveLength(1)
    })
  })
})
