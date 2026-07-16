/**
 * Terminal — the live take-over pane for a `coord-*` interactive session
 * (#1068). Reached via `SessionCard` (#1067) at `/terminal/:sessionId`,
 * replacing the `TerminalComingSoon` stub now that the pane itself exists.
 *
 * Opens a WebSocket to `/ws/terminal/{session_id}` (#1065's PTY<->WebSocket
 * bridge) and renders the byte stream with xterm.js:
 *   - browser keystrokes -> binary WS frames (the bridge's `message["bytes"]`
 *     path writes them straight to the PTY's stdin)
 *   - PTY output (binary WS frames) -> `term.write(Uint8Array)`
 *   - terminal size -> a JSON text control frame
 *     `{"type": "resize", "cols": .., "rows": ..}` on connect, on xterm
 *     resize, and on window resize (FitAddon keeps cols/rows matched to the
 *     pane's actual size)
 *
 * Closing the pane (the ✕ button, back navigation, or unmount) closes the
 * WebSocket. That alone is enough to hit the bridge's detach path -- the
 * server's `finally` block calls `attached.detach()` on every disconnect,
 * local tmux session included, and detach never kills the session (#1065).
 *
 * Layout: the pane occupies the top half of the screen (`h-[50vh]`); the
 * bottom half is left empty, reserved for the mobile key bar (a later issue
 * in the #1064 epic). iOS/Android safe-area insets pad the header so the
 * close control clears the notch/status bar (`index.html` sets
 * `viewport-fit=cover` so `env(safe-area-inset-*)` resolves to something
 * other than 0).
 */
import { useEffect, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { Terminal as XTerm } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import '@xterm/xterm/css/xterm.css'
import { terminalWebSocketUrl } from '@/api/client'

type ConnectionState = 'connecting' | 'open' | 'closed'

const STATUS_LABEL: Record<ConnectionState, string> = {
  connecting: 'Connecting…',
  open: 'Live',
  closed: 'Disconnected',
}

export default function Terminal() {
  const { sessionId } = useParams<{ sessionId: string }>()
  const navigate = useNavigate()
  const containerRef = useRef<HTMLDivElement | null>(null)
  const wsRef = useRef<WebSocket | null>(null)
  const [state, setState] = useState<ConnectionState>('connecting')

  useEffect(() => {
    if (!sessionId || !containerRef.current) return

    const term = new XTerm({
      convertEol: true,
      cursorBlink: true,
      fontSize: 13,
      theme: { background: '#0d1117', foreground: '#e6edf3' },
    })
    const fit = new FitAddon()
    term.loadAddon(fit)
    term.open(containerRef.current)
    try {
      fit.fit()
    } catch {
      // container may not have a laid-out size yet (e.g. under test) -- xterm
      // still opens fine, it just keeps its default 80x24 until the next fit.
    }

    const ws = new WebSocket(terminalWebSocketUrl(sessionId))
    ws.binaryType = 'arraybuffer'
    wsRef.current = ws

    const sendResize = () => {
      if (ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'resize', cols: term.cols, rows: term.rows }))
      }
    }

    ws.onopen = () => {
      setState('open')
      sendResize()
    }
    ws.onclose = () => setState('closed')
    ws.onerror = () => setState('closed')
    ws.onmessage = (event) => {
      if (typeof event.data === 'string') {
        term.write(event.data)
      } else {
        term.write(new Uint8Array(event.data as ArrayBuffer))
      }
    }

    const dataDisposable = term.onData((data) => {
      if (ws.readyState === WebSocket.OPEN) {
        ws.send(new TextEncoder().encode(data))
      }
    })
    const resizeDisposable = term.onResize(sendResize)

    const handleWindowResize = () => {
      try {
        fit.fit()
      } catch {
        // see the mount-time try/catch above
      }
    }
    window.addEventListener('resize', handleWindowResize)

    return () => {
      window.removeEventListener('resize', handleWindowResize)
      dataDisposable.dispose()
      resizeDisposable.dispose()
      ws.close()
      term.dispose()
      wsRef.current = null
    }
  }, [sessionId])

  const handleClose = () => {
    wsRef.current?.close()
    navigate(-1)
  }

  return (
    <div className="flex h-screen flex-col bg-background">
      <header
        className="flex items-center justify-between gap-3 border-b border-border px-4 pb-3"
        style={{ paddingTop: 'calc(env(safe-area-inset-top) + 0.75rem)' }}
      >
        <div className="min-w-0">
          <h1 className="truncate text-sm font-semibold text-foreground">Terminal</h1>
          <p className="truncate font-mono text-xs text-muted-foreground">
            {sessionId} · {STATUS_LABEL[state]}
          </p>
        </div>
        <button
          type="button"
          onClick={handleClose}
          aria-label="Close terminal"
          className="shrink-0 rounded p-2 text-muted-foreground hover:text-foreground"
        >
          ✕
        </button>
      </header>

      {/* Top half: the live pane. Bottom half stays empty -- reserved for
          the mobile key bar (a later issue in the #1064 epic). */}
      <div className="h-[50vh] min-h-0 shrink-0 border-b border-border bg-[#0d1117] px-2 py-2">
        <div ref={containerRef} className="h-full w-full" data-testid="xterm-container" />
      </div>
      <div className="flex-1" />
    </div>
  )
}
