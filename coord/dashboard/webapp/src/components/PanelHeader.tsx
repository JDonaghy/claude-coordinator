/**
 * The list panel's head (#1547) — the mock's `.panel-title`: view name, a
 * mono count of what the machine is tracking, and a slot on the right for
 * whatever the panel wants to say about its own freshness.
 *
 * The `co` mark is narrow-only (`md:hidden`, and Tailwind's `md` is 768px —
 * the same value as `BREAKPOINT_MEDIUM_PX`). On narrow the rail is a bottom
 * icon row with no head, so without this the phone would lose the app's
 * identity entirely; from 768px up the rail head carries it and a second mark
 * here would just be a duplicate.
 */
import type { ReactNode } from 'react'

export interface PanelHeaderProps {
  title: string
  /** Mono count shown after the title; omitted when undefined. */
  count?: number
  /** Word after the count, e.g. "tracked" → `7 tracked`. */
  countLabel?: string
  /** Right-hand slot (freshness, refresh pulse, …). */
  children?: ReactNode
}

export function PanelHeader({ title, count, countLabel = 'tracked', children }: PanelHeaderProps) {
  return (
    <header className="mb-4 flex items-center gap-2.5">
      <span
        aria-hidden="true"
        className="grid h-[22px] w-[22px] flex-none place-items-center rounded-md bg-gradient-to-br from-brand to-brand-dim font-mono text-[.62rem] font-bold tracking-[-.02em] text-[#0b1013] md:hidden"
      >
        co
      </span>
      <h1 className="text-step-1 font-semibold tracking-[-.01em] text-foreground">{title}</h1>
      {count != null && (
        <span className="font-mono text-[.75rem] text-faint">
          {count} {countLabel}
        </span>
      )}
      {children && <div className="ml-auto flex items-center gap-2">{children}</div>}
    </header>
  )
}
