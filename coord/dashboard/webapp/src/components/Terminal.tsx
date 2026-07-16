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
 * Reconnect / detach resilience (#1071): mobile networks drop and resume
 * WebSockets constantly (backgrounding the tab, wifi<->cellular handoff).
 * An *unexpected* close (any code other than the bridge's session-gone
 * signal) re-opens a fresh WebSocket to the same `session_id` with
 * exponential backoff -- the xterm.js `Terminal` instance itself is never
 * recreated, so `tmux attach`'s redraw on reattach just paints into the
 * existing pane. The bridge closes with code 4404 when `session_id` no
 * longer resolves to an active assignment (`resolve_session_target` in
 * `coord/dashboard/terminal.py`) -- that's a *terminal* state (the worker
 * session itself is gone, not just this connection), so it is surfaced
 * distinctly and never retried.
 *
 * Layout: the pane occupies the top half of the screen (`h-[50vh]`); the
 * bottom half hosts `MobileKeyBar` (#1070) -- the soft-key row + line-input
 * for driving the interactive `claude` TUI from a phone. iOS/Android
 * safe-area insets pad the header so the close control clears the
 * notch/status bar (`index.html` sets `viewport-fit=cover` so
 * `env(safe-area-inset-*)` resolves to something other than 0).
 */
import { useEffect, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { Terminal as XTerm } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import '@xterm/xterm/css/xterm.css'
import { terminalWebSocketUrl } from '@/api/client'
import MobileKeyBar from '@/components/MobileKeyBar'

type ConnectionState = 'connecting' | 'open' | 'reconnecting' | 'ended'

const STATUS_LABEL: Record<ConnectionState, string> = {
  connecting: 'Connecting…',
  open: 'Live',
  reconnecting: 'Reconnecting…',
  ended: 'Session ended',
}

// Bridge close code for "session_id no longer resolves to an active
// assignment" -- see `resolve_session_target` / `terminal_ws` in
// `coord/dashboard/terminal.py`. Distinct from every other close (which is
// treated as a transient drop worth retrying).
const SESSION_GONE_CODE = 4404
const RECONNECT_BASE_MS = 1000
const RECONNECT_MAX_MS = 10000

export default function Terminal() {
  const { sessionId } = useParams<{ sessionId: string }>()
  const navigate = useNavigate()
  const containerRef = useRef<HTMLDivElement | null>(null)
  const wsRef = useRef<WebSocket | null>(null)
  // Set right before any *deliberate* close (the ✕ button, unmount) so the
  // reconnect logic below can tell "we closed this on purpose" apart from
  // "the network/tab dropped it out from under us."
  const intentionalCloseRef = useRef(false)
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

    let reconnectAttempt = 0
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null

    const sendResize = (ws: WebSocket) => {
      if (ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'resize', cols: term.cols, rows: term.rows }))
      }
    }

    const connect = () => {
      const ws = new WebSocket(terminalWebSocketUrl(sessionId))
      ws.binaryType = 'arraybuffer'
      wsRef.current = ws

      ws.onopen = () => {
        reconnectAttempt = 0
        setState('open')
        sendResize(ws)
      }
      ws.onclose = (event) => {
        if (intentionalCloseRef.current) return
        if (event.code === SESSION_GONE_CODE) {
          // The worker session itself is gone (not just this connection) --
          // a terminal state. Retrying would spin forever against a
          // session_id that will never resolve again.
          setState('ended')
          return
        }
        setState('reconnecting')
        const delay = Math.min(RECONNECT_BASE_MS * 2 ** reconnectAttempt, RECONNECT_MAX_MS)
        reconnectAttempt += 1
        reconnectTimer = setTimeout(connect, delay)
      }
      // The WebSocket spec always fires "close" right after "error", so
      // state transitions + reconnect scheduling live solely in onclose --
      // duplicating them here would double-schedule reconnects.
      ws.onerror = () => {}
      ws.onmessage = (event) => {
        if (typeof event.data === 'string') {
          term.write(event.data)
        } else {
          term.write(new Uint8Array(event.data as ArrayBuffer))
        }
      }
    }

    connect()

    const dataDisposable = term.onData((data) => {
      const ws = wsRef.current
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(new TextEncoder().encode(data))
      }
    })
    const resizeDisposable = term.onResize(() => {
      const ws = wsRef.current
      if (ws) sendResize(ws)
    })

    const handleWindowResize = () => {
      try {
        fit.fit()
      } catch {
        // see the mount-time try/catch above
      }
    }
    window.addEventListener('resize', handleWindowResize)

    return () => {
      intentionalCloseRef.current = true
      if (reconnectTimer) clearTimeout(reconnectTimer)
      window.removeEventListener('resize', handleWindowResize)
      dataDisposable.dispose()
      resizeDisposable.dispose()
      wsRef.current?.close()
      term.dispose()
      wsRef.current = null
    }
  }, [sessionId])

  const handleClose = () => {
    intentionalCloseRef.current = true
    wsRef.current?.close()
    navigate(-1)
  }

  /**
   * Write raw bytes (a JS string, one char per terminal byte) down the same
   * WebSocket path `term.onData` uses for real keystrokes -- the wire format
   * PTY bridge (#1065) expects for input. Used by `MobileKeyBar` (#1070) for
   * its soft keys and line-input submit.
   */
  const sendRaw = (data: string) => {
    const ws = wsRef.current
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(new TextEncoder().encode(data))
    }
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

      {/* Top half: the live pane. Bottom half: the mobile key bar (#1070). */}
      <div className="relative h-[50vh] min-h-0 shrink-0 border-b border-border bg-[#0d1117] px-2 py-2">
        <div ref={containerRef} className="h-full w-full" data-testid="xterm-container" />
        {state === 'ended' && (
          <div
            role="status"
            className="absolute inset-0 flex items-center justify-center bg-[#0d1117]/90 px-6 text-center text-sm text-muted-foreground"
          >
            Session ended — the worker session is no longer available.
          </div>
        )}
      </div>
      <div
        className="min-h-0 flex-1"
        style={{ paddingBottom: 'env(safe-area-inset-bottom)' }}
      >
        <MobileKeyBar onSend={sendRaw} />
      </div>
    </div>
  )
}
