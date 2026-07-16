/**
 * MobileKeyBar — the bottom-half input surface for driving the interactive
 * `claude` TUI from a phone (#1070, part of epic #1064). Sits below
 * `Terminal`'s xterm pane and sends the byte sequences a mobile soft-keyboard
 * makes painful to reach: Esc, arrows, Ctrl-C, Tab, `/`, plus a line-input
 * field for typing a command and submitting it.
 *
 * This component knows nothing about WebSockets or xterm -- it just calls
 * `onSend(bytes)` with a raw string (one JS char per terminal byte, e.g.
 * `'\x1b[A'` for Up) and leaves the actual write-to-PTY plumbing to the
 * caller (`Terminal.tsx`'s `sendRaw`, same path `term.onData` already uses).
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
import { useState, type FormEvent, type ReactNode } from 'react'
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

export default function MobileKeyBar({ onSend }: MobileKeyBarProps) {
  const [line, setLine] = useState('')

  const handleSubmit = (e: FormEvent) => {
    e.preventDefault()
    if (!line) return
    onSend(line + KEY_BYTES.enter)
    setLine('')
  }

  return (
    <div className="flex h-full flex-col justify-end gap-2 px-2 py-2" data-testid="mobile-key-bar">
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
