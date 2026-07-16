/**
 * Component tests for the Terminal take-over view (#1067).
 *
 * Mocks @xterm/xterm and @xterm/addon-fit entirely (jsdom has no canvas, so
 * the real renderer can't mount) and stubs the global WebSocket so the wire
 * protocol can be asserted without a real server — matching how Detail.test.tsx
 * mocks @/api/client rather than hitting the network.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, act } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import Terminal from '@/components/Terminal'

// ── Hoisted shared state (visible inside vi.mock factories AND test bodies) ────

const { mockTerminalInstances, mockFitInstances } = vi.hoisted(() => {
  return {
    mockTerminalInstances: [] as FakeTerminal[],
    mockFitInstances: [] as FakeFitAddon[],
  }
})

interface FakeTerminal {
  cols: number
  rows: number
  written: Array<string | Uint8Array>
  dataHandler: ((data: string) => void) | null
  loadAddon: (addon: unknown) => void
  open: (el: HTMLElement) => void
  write: (data: string | Uint8Array) => void
  onData: (handler: (data: string) => void) => { dispose: () => void }
  dispose: () => void
}

interface FakeFitAddon {
  fit: () => void
}

vi.mock('@xterm/xterm', () => {
  class Terminal implements FakeTerminal {
    cols = 80
    rows = 24
    written: Array<string | Uint8Array> = []
    dataHandler: ((data: string) => void) | null = null
    constructor() {
      mockTerminalInstances.push(this)
    }
    loadAddon(): void {}
    open(): void {}
    write(data: string | Uint8Array): void {
      this.written.push(data)
    }
    onData(handler: (data: string) => void): { dispose: () => void } {
      this.dataHandler = handler
      return { dispose: () => {} }
    }
    dispose(): void {}
  }
  return { Terminal }
})

vi.mock('@xterm/addon-fit', () => {
  class FitAddon implements FakeFitAddon {
    constructor() {
      mockFitInstances.push(this)
    }
    fit(): void {}
  }
  return { FitAddon }
})

// ── Fake WebSocket ───────────────────────────────────────────────────────────

class FakeWebSocket {
  static readonly CONNECTING = 0
  static readonly OPEN = 1
  static readonly CLOSING = 2
  static readonly CLOSED = 3

  readyState = FakeWebSocket.CONNECTING
  binaryType = ''
  url: string
  sent: Array<string | ArrayBuffer | ArrayBufferView> = []
  onopen: (() => void) | null = null
  onclose: (() => void) | null = null
  onerror: (() => void) | null = null
  onmessage: ((event: { data: unknown }) => void) | null = null

  constructor(url: string) {
    this.url = url
    mockWsInstances.push(this)
  }

  send(data: string | ArrayBuffer | ArrayBufferView): void {
    this.sent.push(data)
  }

  close(): void {
    this.readyState = FakeWebSocket.CLOSED
    this.onclose?.()
  }
}

let mockWsInstances: FakeWebSocket[] = []

beforeEach(() => {
  mockTerminalInstances.length = 0
  mockFitInstances.length = 0
  mockWsInstances = []
  vi.stubGlobal('WebSocket', FakeWebSocket)
})

function renderTerminal(sessionId = 'work-9') {
  return render(
    <MemoryRouter initialEntries={[`/terminal/${sessionId}`]}>
      <Routes>
        <Route path="/terminal/:sessionId" element={<Terminal />} />
      </Routes>
    </MemoryRouter>,
  )
}

// ── Tests ────────────────────────────────────────────────────────────────────

describe('Terminal', () => {
  it('opens a WebSocket to /ws/terminal/{sessionId} and shows "Connecting…"', () => {
    renderTerminal('work-9')

    expect(screen.getByText('Connecting…')).toBeInTheDocument()
    expect(mockWsInstances).toHaveLength(1)
    expect(mockWsInstances[0].url).toContain('/ws/terminal/work-9')
  })

  it('sends a resize control frame and flips to "Connected" on open', async () => {
    renderTerminal()
    const ws = mockWsInstances[0]

    act(() => {
      ws.readyState = FakeWebSocket.OPEN
      ws.onopen?.()
    })

    await waitFor(() => expect(screen.getByText('Connected')).toBeInTheDocument())

    const resizeMsg = ws.sent.find(
      (m) => typeof m === 'string' && m.includes('"type":"resize"'),
    ) as string | undefined
    expect(resizeMsg).toBeDefined()
    expect(JSON.parse(resizeMsg!)).toEqual({ type: 'resize', cols: 80, rows: 24 })
  })

  it('writes incoming binary frames straight to the terminal as bytes', () => {
    renderTerminal()
    const ws = mockWsInstances[0]
    // Built from raw char codes rather than TextEncoder: jsdom's TextEncoder
    // returns typed arrays from a different internal realm than the rest of
    // the test globals, which fails `instanceof ArrayBuffer` in production's
    // handler even though the bytes are identical — a jsdom/vitest quirk,
    // not something real browsers exhibit.
    const bytes = Uint8Array.from('hello\r\n'.split('').map((c) => c.charCodeAt(0)))

    ws.onmessage?.({ data: bytes.buffer })

    const term = mockTerminalInstances[0]
    expect(term.written).toHaveLength(1)
    expect(ArrayBuffer.isView(term.written[0])).toBe(true)
    expect(new TextDecoder().decode(term.written[0] as Uint8Array)).toBe('hello\r\n')
  })

  it('sends typed keystrokes as binary frames, not text', () => {
    renderTerminal()
    const ws = mockWsInstances[0]
    ws.readyState = FakeWebSocket.OPEN

    const term = mockTerminalInstances[0]
    term.dataHandler?.('a')

    expect(ws.sent).toHaveLength(1)
    const sentFrame = ws.sent[0]
    // ArrayBuffer.isView (not `toBeInstanceOf(Uint8Array)`) — realm-safe
    // check, see the note above; the point is it's binary, not a text frame.
    expect(ArrayBuffer.isView(sentFrame)).toBe(true)
    expect(new TextDecoder().decode(sentFrame as Uint8Array)).toBe('a')
  })

  it('shows "Disconnected" when the socket closes', async () => {
    renderTerminal()
    const ws = mockWsInstances[0]

    act(() => {
      ws.close()
    })

    await waitFor(() => expect(screen.getByText('Disconnected')).toBeInTheDocument())
  })
})
