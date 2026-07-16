/**
 * Component tests for Terminal — the live take-over pane for a `coord-*`
 * session (#1068). `WebSocket` is mocked end-to-end (no real network/PTY);
 * this covers mount -> connect -> render -> teardown per the issue's
 * acceptance criterion. Full browser/terminal-protocol coverage (raw mode,
 * SGR mouse) is out of `TestBackend`-equivalent reach here and is the
 * Playwright E2E issue in group C.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, waitFor, act } from '@testing-library/react'
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
  onclose: (() => void) | null = null
  onerror: (() => void) | null = null
  onmessage: ((event: { data: unknown }) => void) | null = null

  constructor(url: string) {
    this.url = url
    FakeWebSocket.instances.push(this)
  }

  send(data: string | ArrayBuffer | Uint8Array) {
    this.sent.push(data)
  }

  close() {
    this.closed = true
    this.readyState = FakeWebSocket.CLOSED
    this.onclose?.()
  }

  // ── Test helpers, driving the fake from the "server" side. Wrapped in
  // act() since they synchronously trigger a React state update
  // (setState('open'/'closed')) outside of React's own event handling. ──
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
})
