/**
 * Activity rail (#1547) — the coord-tui `SidebarView` twin, and the *same*
 * component at every viewport.
 *
 * Three presentations, one component and one item list (`railItems.ts`):
 *   wide      216px labelled column, grouped, with counts and a rail foot.
 *   collapsed 60px icon strip (labels, counts, group headings and the
 *             wordmark all drop out; icons centre).
 *   narrow    a bottom row of icon-over-label tabs. Only `ready` entries
 *             appear — a phone's bottom nav is thumb real estate, and a
 *             dimmed un-tappable "soon" item there is noise, not a progress
 *             bar. On wide there is room for the whole programme, so the
 *             whole programme shows.
 *
 * Matches docs/mocks/web/pipeline-wide.html: the selected item takes the
 * accent wash plus a 3px accent bar on its leading edge, `soon` items sit at
 * ~42% opacity behind a `soon` pill, and counts are mono (a value the machine
 * owns) — amber-washed when the count is something needing a human.
 */
import {
  ChevronLeft,
  ChevronRight,
  Moon,
  PanelLeftClose,
  PanelLeftOpen,
  Settings2,
  Sun,
} from 'lucide-react'

import { cn } from '@/lib/utils'
import { useTheme } from '@/components/ui/theme-provider'
import type { ShellMode } from './breakpoints'
import { RAIL_GROUPS, type RailItem } from './railItems'
import type { ShellView } from './shellState'

export interface ActivityRailProps {
  mode: ShellMode
  collapsed: boolean
  /** `null` when the current URL doesn't belong to any rail view (#1548) —
   * e.g. a genuinely unknown route. Nothing in the rail lights up rather
   * than falling back to a view that isn't actually selected. */
  view: ShellView | null
  onSelect: (view: ShellView) => void
  onToggleCollapsed: () => void
  /** Wide only: whether the list column is minimised away. */
  listCollapsed?: boolean
  onToggleList?: () => void
  /** Badge counts by view; absent or 0 renders no badge. */
  counts?: Partial<Record<ShellView, number>>
  /** Views whose count should read as "needs a human" (amber, not grey). */
  attentionViews?: ReadonlySet<ShellView>
  /** F6 region registration — see `useRegionFocus`. */
  regionRef?: (el: HTMLElement | null) => void
  /** True while F6 last landed here; draws the region ring. */
  regionFocused?: boolean
}

const navItemBase =
  'relative flex w-full items-center rounded-sm text-left text-step-0 ' +
  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-0'

function NavItem({
  item,
  mode,
  collapsed,
  selected,
  count,
  attention,
  onSelect,
}: {
  item: RailItem
  mode: ShellMode
  collapsed: boolean
  selected: boolean
  count?: number
  attention?: boolean
  onSelect: (view: ShellView) => void
}) {
  const soon = item.status === 'soon'
  const Icon = item.icon
  const narrow = mode === 'narrow'

  return (
    <button
      type="button"
      // A `soon` entry is a signpost, not a control: `aria-disabled` (rather
      // than `disabled`) keeps it reachable by a screen reader reading the
      // rail as a table of contents, while the no-op handler and
      // `cursor-default` keep it inert to a mouse.
      aria-disabled={soon || undefined}
      aria-current={selected ? 'page' : undefined}
      title={item.hint ?? item.label}
      onClick={soon ? undefined : () => onSelect(item.id)}
      className={cn(
        navItemBase,
        narrow
          ? 'min-w-[60px] flex-1 flex-col justify-center gap-0.5 px-1 py-1.5'
          : 'gap-2.5 px-2 py-1.5',
        collapsed && !narrow && 'justify-center px-0 py-2',
        soon
          ? 'cursor-default text-muted-foreground opacity-40 hover:opacity-70'
          : 'text-muted-foreground hover:bg-surface-2 hover:text-foreground',
        selected && !narrow && 'bg-brand-wash text-brand hover:bg-brand-wash hover:text-brand',
        selected && narrow && 'text-brand',
      )}
    >
      {/* Leading accent bar on the selected row — mock's `.nav-item::before`.
          Suppressed on narrow, where the mock drops it too. */}
      {selected && !narrow && (
        <span
          aria-hidden="true"
          className="absolute -left-2 top-1/2 h-[17px] w-[3px] -translate-y-1/2 rounded-r-[3px] bg-brand"
        />
      )}
      <Icon className={cn('shrink-0', narrow ? 'h-[19px] w-[19px]' : 'h-[17px] w-[17px]')} aria-hidden="true" />
      {(!collapsed || narrow) && (
        <span
          className={cn(
            'overflow-hidden text-ellipsis whitespace-nowrap',
            narrow ? 'text-[.6rem] leading-tight' : 'min-w-0 flex-1',
          )}
        >
          {item.label}
        </span>
      )}
      {!collapsed && !narrow && soon && (
        <span className="shrink-0 rounded-full border border-line-strong px-1 text-[.58rem] uppercase tracking-[.06em] text-faint">
          soon
        </span>
      )}
      {!collapsed && !narrow && !soon && count != null && count > 0 && (
        <span
          className={cn(
            'rounded-full px-1.5 font-mono text-[.7rem]',
            attention ? 'bg-attn-wash text-attn' : 'bg-surface-2 text-faint',
          )}
        >
          {count}
        </span>
      )}
    </button>
  )
}

export function ActivityRail({
  mode,
  collapsed,
  view,
  onSelect,
  onToggleCollapsed,
  listCollapsed,
  onToggleList,
  counts,
  attentionViews,
  regionRef,
  regionFocused,
}: ActivityRailProps) {
  const { theme, toggleTheme } = useTheme()
  const narrow = mode === 'narrow'
  // On medium the rail is *always* the icon strip: there isn't room for both
  // a labelled rail and a usable content column, and letting the persisted
  // `collapsed=false` win there is exactly the "snapping between two
  // extremes" the story asks us to avoid.
  const iconOnly = mode === 'medium' || (mode === 'wide' && collapsed)

  const groups = narrow
    ? [{ heading: undefined, items: RAIL_GROUPS.flatMap((g) => g.items).filter((i) => i.status === 'ready') }]
    : RAIL_GROUPS

  return (
    <nav
      ref={regionRef}
      tabIndex={-1}
      data-region="rail"
      aria-label="Views"
      className={cn(
        'flex h-full w-full min-w-0 overflow-hidden bg-surface outline-none',
        narrow
          ? 'flex-row border-t border-border pb-[env(safe-area-inset-bottom,0px)]'
          : 'flex-col border-r border-border',
        regionFocused && 'ring-1 ring-inset ring-ring',
      )}
    >
      {!narrow && (
        <div
          className={cn(
            'flex items-center gap-[9px] border-b border-border',
            iconOnly ? 'justify-center px-0 py-3' : 'px-3.5 pb-3 pt-3.5',
          )}
        >
          <span
            aria-hidden="true"
            className="grid h-[26px] w-[26px] flex-none place-items-center rounded-[7px] bg-gradient-to-br from-brand to-brand-dim font-mono text-[.72rem] font-bold tracking-[-.02em] text-[#0b1013]"
          >
            co
          </span>
          {!iconOnly && (
            <span className="whitespace-nowrap font-semibold tracking-[-.01em]">
              coord <span className="font-normal text-muted-foreground">web</span>
            </span>
          )}
        </div>
      )}

      <div
        className={cn(
          'flex',
          narrow
            ? 'w-full flex-row justify-around gap-0 overflow-x-auto px-0 py-1'
            : 'flex-1 flex-col gap-px overflow-y-auto px-2 py-2.5',
        )}
      >
        {groups.map((group, groupIndex) => (
          <div
            key={group.heading ?? `group-${groupIndex}`}
            className={cn('flex', narrow ? 'w-full flex-row justify-around' : 'flex-col gap-px')}
          >
            {group.heading && !iconOnly && !narrow && (
              <div className="whitespace-nowrap px-2.5 pb-[5px] pt-3.5 text-[.66rem] font-semibold uppercase tracking-[.09em] text-faint">
                {group.heading}
              </div>
            )}
            {group.items.map((item) => (
              <NavItem
                key={item.id}
                item={item}
                mode={mode}
                collapsed={iconOnly}
                selected={item.id === view}
                count={counts?.[item.id]}
                attention={attentionViews?.has(item.id)}
                onSelect={onSelect}
              />
            ))}
          </div>
        ))}
      </div>

      {!narrow && (
        <div className="flex flex-col gap-px border-t border-border px-2 py-2.5">
          <button
            type="button"
            onClick={toggleTheme}
            title={theme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme'}
            aria-label={theme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme'}
            className={cn(
              navItemBase,
              'gap-[11px] px-2.5 py-1.5 text-muted-foreground hover:bg-surface-2 hover:text-foreground',
              iconOnly && 'justify-center px-0 py-2',
            )}
          >
            {theme === 'dark' ? (
              <Sun className="h-[17px] w-[17px] shrink-0" aria-hidden="true" />
            ) : (
              <Moon className="h-[17px] w-[17px] shrink-0" aria-hidden="true" />
            )}
            {!iconOnly && <span className="flex-1 text-left">Theme</span>}
          </button>

          <button
            type="button"
            aria-disabled="true"
            title="Settings panel — M-W3"
            className={cn(
              navItemBase,
              'cursor-default gap-[11px] px-2.5 py-1.5 text-muted-foreground opacity-40 hover:opacity-70',
              iconOnly && 'justify-center px-0 py-2',
            )}
          >
            <Settings2 className="h-[17px] w-[17px] shrink-0" aria-hidden="true" />
            {!iconOnly && (
              <>
                <span className="flex-1 text-left">Settings</span>
                <span className="rounded-full border border-line-strong px-[5px] text-[.6rem] uppercase tracking-[.06em] text-faint">
                  soon
                </span>
              </>
            )}
          </button>

          {/* The mock puts "minimise list" in the panel head; it lives here
              instead so it can't collide with whatever a panel chooses to put
              in its own header, and so the way back (expand) is in the same
              place as the way out. Wide-only: it's the only mode with a third
              column to give the space to. */}
          {mode === 'wide' && onToggleList && (
            <button
              type="button"
              onClick={onToggleList}
              aria-label={listCollapsed ? 'Show list panel' : 'Minimize list panel'}
              aria-pressed={!!listCollapsed}
              title={listCollapsed ? 'Show list panel' : 'Minimize list panel'}
              className={cn(
                navItemBase,
                'gap-[11px] px-2.5 py-1.5 text-muted-foreground hover:bg-surface-2 hover:text-foreground',
                collapsed && 'justify-center px-0 py-2',
              )}
            >
              {listCollapsed ? (
                <PanelLeftOpen className="h-[17px] w-[17px] shrink-0" aria-hidden="true" />
              ) : (
                <PanelLeftClose className="h-[17px] w-[17px] shrink-0" aria-hidden="true" />
              )}
              {!collapsed && <span className="flex-1 text-left">{listCollapsed ? 'Show list' : 'Hide list'}</span>}
            </button>
          )}

          {/* Medium pins the rail to icons, so offering a toggle there would
              be a button that does nothing. */}
          {mode === 'wide' && (
            <button
              type="button"
              onClick={onToggleCollapsed}
              aria-label={collapsed ? 'Expand rail' : 'Collapse rail'}
              aria-expanded={!collapsed}
              title={collapsed ? 'Expand rail' : 'Collapse rail'}
              className={cn(
                navItemBase,
                'gap-[11px] px-2.5 py-1.5 text-muted-foreground hover:bg-surface-2 hover:text-foreground',
                collapsed && 'justify-center px-0 py-2',
              )}
            >
              {collapsed ? (
                <ChevronRight className="h-[17px] w-[17px] shrink-0" aria-hidden="true" />
              ) : (
                <ChevronLeft className="h-[17px] w-[17px] shrink-0" aria-hidden="true" />
              )}
              {!collapsed && <span className="flex-1 text-left">Collapse</span>}
            </button>
          )}
        </div>
      )}
    </nav>
  )
}
