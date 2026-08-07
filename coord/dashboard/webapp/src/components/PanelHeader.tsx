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
 *
 * The theme toggle (#1551) is narrow-only for the same reason and by the same
 * mechanism: `docs/mocks/web/pipeline-narrow.html`'s per-view topbar carries
 * its own Theme icon-button (`id="theme"`) because the bottom nav has no room
 * for it — narrow's rail deliberately shows only `ready` nav entries
 * (`ActivityRail.tsx`) and a dimmed un-tappable settings-style control there
 * would eat thumb real estate. From 768px up the rail foot already has
 * "Theme" (`ActivityRail.tsx`), so this one is `md:hidden` — CSS `display:
 * none`, which Playwright's (and every screen reader's) accessibility-tree
 * role queries already exclude, exactly like the `co` mark above — never two
 * "Switch to … theme" buttons live in the tree at once.
 */
import type { ReactNode } from 'react'
import { ThemeToggle } from '@/components/ui/theme-toggle'

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
      <div className="ml-auto flex items-center gap-2">
        {children}
        <div className="md:hidden">
          <ThemeToggle />
        </div>
      </div>
    </header>
  )
}
