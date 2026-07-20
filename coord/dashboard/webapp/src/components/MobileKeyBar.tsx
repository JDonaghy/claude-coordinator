/**
 * MobileKeyBar — the bottom-half input surface for driving the interactive
 * `claude` TUI from a phone (#1070, part of epic #1064). Sits below
 * `Terminal`'s xterm pane and sends the byte sequences a mobile soft-keyboard
 * makes painful to reach: Esc, arrows, Ctrl-C, Tab, `/`, plus a line-input
 * field for typing a command and submitting it.
 *
 * This component knows nothing about WebSockets or xterm -- it just calls
 * `onSend(bytes)` with a raw string (one JS char per terminal byte, e.g.
 * `'\x1b[A'` for Up) and `onControl(action)` for copy-mode control frames,
 * leaving the actual write-to-PTY / JSON-frame plumbing to the caller
 * (`Terminal.tsx`'s `sendRaw` / `sendControl`, same path `term.onData` uses).
 *
 * ## Scroll mode (#1299)
 *
 * Tapping **Scroll** enters a server-driven copy-mode (via
 * `{"type":"copy-mode","action":"enter"}`) so the user can scroll back through
 * tmux pane history from the phone without needing access to the prefix key or
 * tmux's copy-mode key bindings. While in scroll mode:
 *   - An unmistakable **SCROLL** badge makes the current mode obvious.
 *   - The normal key rows are replaced by Page Up / Page Down / Top / Bottom
 *     controls and an Exit button.
 *   - Submitting the line-input sends `exit` first so the text lands in the
 *     live pane, not in tmux's copy-mode search.
 *
 * `connectionKey` is incremented by `Terminal.tsx` on every WebSocket open
 * (including reconnects). A reconnect resets copy-mode server-side (a fresh
 * `tmux attach-session` starts outside copy-mode), so the component resets the
 * scroll toggle on `connectionKey` change so the badge always reflects reality.
 *
 * Multi-line submission to `claude` needs bracketed paste + a settle delay +
 * a *separate* trailing `\r` (see the #425/#426 PTY-submission notes) -- out
 * of scope here. The line-input only ever sends a single line followed by
 * `\r`, matching the issue's "get single-line + Enter solid first" guidance.
 *
 * Every soft key is a `type="button"` with `onMouseDown` preventing the
 * default focus change, so tapping a key never blurs the line-input field --
 * if the phone's native keyboard is up because the user is mid-line, a
 * Ctrl-C or arrow tap shouldn't dismiss it.
 */
import { useEffect, useState, type FormEvent, type ReactNode } from 'react'
import { ArrowUp, ArrowDown, ArrowLeft, ArrowRight, CornerDownLeft } from 'lucide-react'
import { cn } from '@/lib/utils'

/** Escape / control byte sequences a real terminal would emit for these keys. */
export const KEY_BYTES = {
  escape: '\x1b',
  up: '\x1b[A',
  down: '\x1b[B',
  right: '\x1b[C',
  left: '\x1b[D',
  enter: '\r',
  ctrlC: '\x03',
  tab: '\x09',
  slash: '/',
} as const

export interface MobileKeyBarProps {
  /** Send raw bytes (as a JS string) down the terminal's input path. */
  onSend: (data: string) => void
  /** Send a copy-mode control action (e.g. "enter", "page-up") to the server. */
  onControl: (action: string) => void
  /**
   * Incremented by `Terminal.tsx` on every WebSocket open (initial connect and
   * reconnects). A change signals that server-side copy-mode state has been
   * cleared, so the component resets its scroll toggle to avoid a stale badge.
   */
  connectionKey?: number
}

interface SoftKeyProps {
  ariaLabel: string
  bytes: string
  onSend: (data: string) => void
  className?: string
  children: ReactNode
}

function SoftKey({ ariaLabel, bytes, onSend, className, children }: SoftKeyProps) {
  return (
    <button
      type="button"
      aria-label={ariaLabel}
      onMouseDown={(e) => e.preventDefault()}
      onClick={() => onSend(bytes)}
      className={cn(
        'flex items-center justify-center rounded-md border border-border bg-card py-3',
        'text-sm font-medium text-foreground active:bg-accent',
        className,
      )}
    >
      {children}
    </button>
  )
}

interface ControlKeyProps {
  ariaLabel: string
  action: string
  onControl: (action: string) => void
  className?: string
  children: ReactNode
}

function ControlKey({ ariaLabel, action, onControl, className, children }: ControlKeyProps) {
  return (
    <button
      type="button"
      aria-label={ariaLabel}
      onMouseDown={(e) => e.preventDefault()}
      onClick={() => onControl(action)}
      className={cn(
        'flex items-center justify-center rounded-md border border-border bg-card py-3',
        'text-sm font-medium text-foreground active:bg-accent',
        className,
      )}
    >
      {children}
    </button>
  )
}

export default function MobileKeyBar({ onSend, onControl, connectionKey }: MobileKeyBarProps) {
  const [line, setLine] = useState('')
  const [scrollMode, setScrollMode] = useState(false)

  // Reset scroll toggle whenever the WebSocket reconnects. A fresh
  // `tmux attach-session` always starts outside copy-mode, so a stale
  // "SCROLL" badge would lie about the server's real state.
  useEffect(() => {
    setScrollMode(false)
  }, [connectionKey])

  const enterScroll = () => {
    setScrollMode(true)
    onControl('enter')
  }

  const exitScroll = () => {
    setScrollMode(false)
    onControl('exit')
  }

  const handleSubmit = (e: FormEvent) => {
    e.preventDefault()
    if (!line) return
    // If we're in scroll mode, exit copy-mode first so the typed text lands in
    // the live pane rather than tmux's copy-mode search buffer.
    if (scrollMode) {
      onControl('exit')
      setScrollMode(false)
    }
    onSend(line + KEY_BYTES.enter)
    setLine('')
  }

  return (
    <div className="flex h-full flex-col justify-end gap-2 px-2 py-2" data-testid="mobile-key-bar">
      {scrollMode ? (
        // ── Scroll mode: copy-mode controls ──────────────────────────────────
        <>
          {/* Badge — always visible so the user knows where taps are going. */}
          <div
            role="status"
            aria-label="Scroll mode active"
            className="flex items-center justify-center rounded-md bg-primary px-3 py-1 text-xs font-bold uppercase tracking-widest text-primary-foreground"
          >
            SCROLL
          </div>

          {/* Scrollback navigation row */}
          <div className="grid grid-cols-4 gap-2">
            <ControlKey ariaLabel="Page Up" action="page-up" onControl={onControl}>
              Pg Up
            </ControlKey>
            <ControlKey ariaLabel="Page Down" action="page-down" onControl={onControl}>
              Pg Dn
            </ControlKey>
            <ControlKey ariaLabel="Top" action="top" onControl={onControl}>
              Top
            </ControlKey>
            <ControlKey ariaLabel="Bottom" action="bottom" onControl={onControl}>
              Bottom
            </ControlKey>
          </div>

          {/* Exit scroll mode */}
          <button
            type="button"
            aria-label="Exit Scroll"
            onMouseDown={(e) => e.preventDefault()}
            onClick={exitScroll}
            className={cn(
              'flex w-full items-center justify-center rounded-md border border-primary py-3',
              'text-sm font-medium text-primary active:bg-accent',
            )}
          >
            Exit Scroll
          </button>
        </>
      ) : (
        // ── Normal mode: standard key rows + Scroll toggle ───────────────────
        <>
          <div className="grid grid-cols-4 gap-2">
            <SoftKey ariaLabel="Escape" bytes={KEY_BYTES.escape} onSend={onSend}>
              Esc
            </SoftKey>
            <SoftKey ariaLabel="Tab" bytes={KEY_BYTES.tab} onSend={onSend}>
              Tab
            </SoftKey>
            <SoftKey ariaLabel="Ctrl-C" bytes={KEY_BYTES.ctrlC} onSend={onSend}>
              Ctrl-C
            </SoftKey>
            <SoftKey ariaLabel="Slash" bytes={KEY_BYTES.slash} onSend={onSend}>
              /
            </SoftKey>
          </div>

          <div className="grid grid-cols-4 gap-2">
            <SoftKey ariaLabel="Left" bytes={KEY_BYTES.left} onSend={onSend}>
              <ArrowLeft size={18} />
            </SoftKey>
            <SoftKey ariaLabel="Up" bytes={KEY_BYTES.up} onSend={onSend}>
              <ArrowUp size={18} />
            </SoftKey>
            <SoftKey ariaLabel="Down" bytes={KEY_BYTES.down} onSend={onSend}>
              <ArrowDown size={18} />
            </SoftKey>
            <SoftKey ariaLabel="Right" bytes={KEY_BYTES.right} onSend={onSend}>
              <ArrowRight size={18} />
            </SoftKey>
          </div>

          {/* Scroll toggle — enters copy-mode for pane history (#1299). */}
          <button
            type="button"
            aria-label="Scroll"
            onMouseDown={(e) => e.preventDefault()}
            onClick={enterScroll}
            className={cn(
              'flex w-full items-center justify-center rounded-md border border-border bg-card py-3',
              'text-sm font-medium text-foreground active:bg-accent',
            )}
          >
            Scroll
          </button>
        </>
      )}

      <SoftKey ariaLabel="Enter" bytes={KEY_BYTES.enter} onSend={onSend} className="w-full gap-2">
        <CornerDownLeft size={16} />
        Enter
      </SoftKey>

      <form onSubmit={handleSubmit} className="flex gap-2">
        <input
          type="text"
          value={line}
          onChange={(e) => setLine(e.target.value)}
          placeholder="Type a command…"
          aria-label="Command line"
          inputMode="text"
          enterKeyHint="send"
          autoCapitalize="off"
          autoCorrect="off"
          autoComplete="off"
          spellCheck={false}
          className="min-w-0 flex-1 rounded-md border border-border bg-card px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-primary"
        />
        <button
          type="submit"
          aria-label="Send line"
          className="shrink-0 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground active:opacity-80"
        >
          Send
        </button>
      </form>
    </div>
  )
}
