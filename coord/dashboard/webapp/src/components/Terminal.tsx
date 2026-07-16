/**
 * Terminal — full-screen take-over view for one live coord-* interactive
 * session (#1067), driving the #1065 `/ws/terminal/{session_id}` PTY bridge.
 *
 * ToS §3.7 / #437: this view only ever relays a *live human* — keystrokes
 * typed here go straight to the PTY, PTY output is rendered as-is. No
 * autonomous injection, scraping, or decision-making happens client-side.
 *
 * Wire protocol (must match `coord/dashboard/terminal.py::terminal_ws`):
 *   - Binary WS frames both ways carry raw PTY bytes (keystrokes in, output
 *     out). xterm's `onData` callback hands back a *string*, so it's
 *     re-encoded with `TextEncoder` before sending — a text frame would be
 *     misparsed server-side as a JSON control message and dropped.
 *   - A JSON **text** frame `{"type":"resize","cols":..,"rows":..}` tells
 *     the server to propagate a resize to the PTY (TIOCSWINSZ). Sent once
 *     on connect (after the initial fit) and again on every viewport change.
 */
import { useEffect, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { Terminal as XTerm } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import { terminalWsUrl } from '@/api/client'
import '@xterm/xterm/css/xterm.css'

type ConnectionState = 'connecting' | 'open' | 'closed'

const STATUS_LABEL: Record<ConnectionState, string> = {
  connecting: 'Connecting…',
  open: 'Connected',
  closed: 'Disconnected',
}

export default function Terminal() {
  const { sessionId } = useParams<{ sessionId: string }>()
  const navigate = useNavigate()

  const containerRef = useRef<HTMLDivElement | null>(null)
  const [connectionState, setConnectionState] = useState<ConnectionState>('connecting')

  useEffect(() => {
    if (!sessionId || !containerRef.current) return

    const term = new XTerm({
      cursorBlink: true,
      convertEol: true,
      fontSize: 13,
      theme: {
        background: '#0d1117',
        foreground: '#c9d1d9',
      },
    })
    const fitAddon = new FitAddon()
    term.loadAddon(fitAddon)
    term.open(containerRef.current)
    fitAddon.fit()

    const ws = new WebSocket(terminalWsUrl(sessionId))
    ws.binaryType = 'arraybuffer'
    const encoder = new TextEncoder()

    const sendResize = () => {
      if (ws.readyState !== WebSocket.OPEN) return
      ws.send(JSON.stringify({ type: 'resize', cols: term.cols, rows: term.rows }))
    }

    ws.onopen = () => {
      setConnectionState('open')
      fitAddon.fit()
      sendResize()
    }
    ws.onclose = () => setConnectionState('closed')
    ws.onerror = () => setConnectionState('closed')
    ws.onmessage = (event) => {
      if (event.data instanceof ArrayBuffer) {
        // Write raw PTY bytes directly — xterm parses the byte stream itself
        // (handles ANSI escapes / multi-byte UTF-8 split across chunks)
        // rather than decoding to a JS string first.
        term.write(new Uint8Array(event.data))
      } else if (typeof event.data === 'string') {
        term.write(event.data)
      }
    }

    const dataDisposable = term.onData((data) => {
      if (ws.readyState === WebSocket.OPEN) {
        ws.send(encoder.encode(data))
      }
    })

    const resizeObserver = new ResizeObserver(() => {
      fitAddon.fit()
      sendResize()
    })
    resizeObserver.observe(containerRef.current)

    return () => {
      resizeObserver.disconnect()
      dataDisposable.dispose()
      ws.close()
      term.dispose()
    }
  }, [sessionId])

  return (
    <div className="flex h-screen flex-col bg-background">
      {/* Header */}
      <header className="flex items-center justify-between border-b border-border px-4 py-3">
        <button
          type="button"
          onClick={() => navigate('/')}
          className="text-sm font-medium text-primary"
        >
          ← Back
        </button>
        <p className="font-mono text-xs text-muted-foreground">{sessionId}</p>
        <span
          className={
            connectionState === 'open'
              ? 'text-xs font-medium text-green-500'
              : connectionState === 'closed'
                ? 'text-xs font-medium text-destructive'
                : 'text-xs font-medium text-muted-foreground'
          }
        >
          {STATUS_LABEL[connectionState]}
        </span>
      </header>

      {/* Terminal surface */}
      <div ref={containerRef} className="min-h-0 flex-1 overflow-hidden px-2 py-2" />
    </div>
  )
}
