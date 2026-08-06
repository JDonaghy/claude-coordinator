/**
 * AppShell (#1547) — the responsive frame every panel hangs off.
 *
 * One CSS grid, three compositions, driven entirely by `mode` (see
 * `breakpoints.ts`). The slots are `ReactNode` props rather than a forked
 * component tree: `ShellLayout` passes the *same* list node and the *same*
 * detail node at every viewport, and this file decides only where they land
 * and which of them is on screen.
 *
 *   wide     rail | list | detail, three columns, status bar spanning the
 *            foot. The list/detail boundary is a draggable separator.
 *   medium   rail | content. The detail arrives as an overlay sheet in the
 *            same grid cell as the list, so a drill-in doesn't cost you the
 *            list you drilled in from.
 *   narrow   one column, status bar, then the rail as a bottom row. Exactly
 *            one of list/detail is mounted — this is the phone app's
 *            drill-in, and mounting both would duplicate every row's text in
 *            the accessibility tree.
 *
 * Mirrors docs/mocks/web/pipeline-wide.html's `.app` grid, including its
 * `data-rail` / `data-panel` state attributes (kept as real DOM attributes:
 * they are what an acceptance spec asserts layout state against without
 * having to measure pixels).
 */
import {
  useCallback,
  useEffect,
  useRef,
  type CSSProperties,
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
} from 'react'

import { cn } from '@/lib/utils'
import type { ShellMode } from './breakpoints'
import {
  LIST_WIDTH_MAX_PX,
  LIST_WIDTH_MIN_PX,
  RAIL_WIDTH_COLLAPSED_PX,
  RAIL_WIDTH_PX,
  clampListWidth,
} from './shellState'
import type { ShellRegion } from './useRegionFocus'

/** Keyboard resize step for the separator (Shift = coarse). */
const RESIZE_STEP_PX = 16
const RESIZE_STEP_COARSE_PX = 64

/**
 * `inert` isn't in React 18's JSX attribute types (it landed in React 19), but
 * React does forward unknown lowercase string attributes to the DOM, and every
 * browser that ships `inert` honours the empty-string form. This is the only
 * thing that stops Tab from walking into the list hidden *behind* the medium
 * overlay — `aria-hidden` alone hides it from a screen reader while leaving it
 * in the tab order, which is the worse of the two failure modes.
 */
const INERT_PROPS = { inert: '' } as unknown as Record<string, string>

export interface AppShellProps {
  mode: ShellMode
  railCollapsed: boolean
  listCollapsed: boolean
  listWidthPx: number
  onListWidthChange: (px: number) => void
  /** True when a detail item is selected (on narrow: show detail, hide list). */
  detailActive: boolean
  rail: ReactNode
  list: ReactNode
  detail: ReactNode
  status: ReactNode
  registerRegion: (region: ShellRegion) => (el: HTMLElement | null) => void
  focusedRegion: ShellRegion | null
}

export function AppShell({
  mode,
  railCollapsed,
  listCollapsed,
  listWidthPx,
  onListWidthChange,
  detailActive,
  rail,
  list,
  detail,
  status,
  registerRegion,
  focusedRegion,
}: AppShellProps) {
  const narrow = mode === 'narrow'
  const wide = mode === 'wide'
  // Medium pins the rail to its icon strip regardless of the persisted flag —
  // see ActivityRail for why.
  const railPx = wide && !railCollapsed ? RAIL_WIDTH_PX : RAIL_WIDTH_COLLAPSED_PX

  // Wide is the only mode with a third column to give away, so it's the only
  // mode where "minimise the list" means anything.
  const listHidden = wide && listCollapsed
  const showList = narrow ? !detailActive : !listHidden
  // Wide is the only mode with a column reserved for the detail, so it's the
  // only one that mounts the "nothing selected" placeholder. On medium the
  // detail shares the list's grid cell — mounting it unselected would paint an
  // opaque placeholder straight over the list.
  const showDetail = wide || detailActive
  // On medium the detail sheet sits *on top of* the list in the same cell.
  const detailOverlays = mode === 'medium' && detailActive

  const gridStyle: CSSProperties = narrow
    ? { gridTemplateColumns: 'minmax(0,1fr)', gridTemplateRows: 'minmax(0,1fr) auto auto' }
    : {
        gridTemplateColumns: wide
          ? `${railPx}px ${listHidden ? 0 : listWidthPx}px minmax(0,1fr)`
          : `${railPx}px minmax(0,1fr)`,
        gridTemplateRows: 'minmax(0,1fr) auto',
      }

  const railStyle: CSSProperties = narrow
    ? { gridColumn: 1, gridRow: 3 }
    : { gridColumn: 1, gridRow: 1 }
  const listStyle: CSSProperties = narrow ? { gridColumn: 1, gridRow: 1 } : { gridColumn: 2, gridRow: 1 }
  const detailStyle: CSSProperties = narrow
    ? { gridColumn: 1, gridRow: 1 }
    : { gridColumn: wide ? 3 : 2, gridRow: 1 }
  const statusStyle: CSSProperties = narrow
    ? { gridColumn: 1, gridRow: 2 }
    : { gridColumn: '1 / -1', gridRow: 2 }

  // ── separator drag ────────────────────────────────────────────────────────
  const dragRef = useRef<{ startX: number; startWidth: number } | null>(null)

  const onPointerDown = useCallback(
    (event: ReactPointerEvent<HTMLDivElement>) => {
      // Ignore secondary buttons: a right-click drag would otherwise capture
      // the pointer and never see a matching pointerup.
      if (event.button !== 0) return
      event.preventDefault()
      event.currentTarget.setPointerCapture(event.pointerId)
      dragRef.current = { startX: event.clientX, startWidth: listWidthPx }
      // A col-resize drag that paints half the issue list blue looks broken,
      // and the cursor must not flicker back to `text` when the pointer
      // leaves the 7px handle mid-drag.
      document.body.style.setProperty('user-select', 'none')
      document.body.style.setProperty('cursor', 'col-resize')
    },
    [listWidthPx],
  )

  const onPointerMove = useCallback(
    (event: ReactPointerEvent<HTMLDivElement>) => {
      const drag = dragRef.current
      if (!drag) return
      onListWidthChange(clampListWidth(drag.startWidth + (event.clientX - drag.startX)))
    },
    [onListWidthChange],
  )

  const endDrag = useCallback((event: ReactPointerEvent<HTMLDivElement>) => {
    if (!dragRef.current) return
    dragRef.current = null
    document.body.style.removeProperty('user-select')
    document.body.style.removeProperty('cursor')
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId)
    }
  }, [])

  const onSeparatorKeyDown = useCallback(
    (event: ReactKeyboardEvent<HTMLDivElement>) => {
      const step = event.shiftKey ? RESIZE_STEP_COARSE_PX : RESIZE_STEP_PX
      switch (event.key) {
        case 'ArrowLeft':
          event.preventDefault()
          onListWidthChange(listWidthPx - step)
          break
        case 'ArrowRight':
          event.preventDefault()
          onListWidthChange(listWidthPx + step)
          break
        case 'Home':
          event.preventDefault()
          onListWidthChange(LIST_WIDTH_MIN_PX)
          break
        case 'End':
          event.preventDefault()
          onListWidthChange(LIST_WIDTH_MAX_PX)
          break
        default:
          break
      }
    },
    [listWidthPx, onListWidthChange],
  )

  // Unmounting (or dropping out of wide) mid-drag would otherwise strand the
  // document in `user-select: none; cursor: col-resize` forever.
  useEffect(
    () => () => {
      document.body.style.removeProperty('user-select')
      document.body.style.removeProperty('cursor')
    },
    [],
  )

  return (
    <div
      className="grid h-screen w-full overflow-hidden bg-background text-foreground [height:100dvh]"
      style={gridStyle}
      data-shell-mode={mode}
      data-rail={narrow ? 'bottom' : !wide || railCollapsed ? 'collapsed' : 'expanded'}
      data-panel={listHidden ? 'collapsed' : 'expanded'}
      data-detail={detailActive ? 'active' : 'empty'}
    >
      <div style={railStyle} className="min-w-0 overflow-hidden">
        {rail}
      </div>

      {showList && (
        <section
          ref={registerRegion('list')}
          tabIndex={-1}
          data-region="list"
          aria-label="List"
          aria-hidden={detailOverlays || undefined}
          {...(detailOverlays ? INERT_PROPS : {})}
          style={listStyle}
          className={cn(
            'relative flex min-w-0 flex-col overflow-hidden bg-background outline-none',
            !narrow && 'border-r border-border',
            focusedRegion === 'list' && 'ring-1 ring-inset ring-ring',
          )}
        >
          <div className="min-h-0 flex-1 overflow-y-auto">{list}</div>

          {wide && (
            <div
              role="separator"
              aria-orientation="vertical"
              aria-label="Resize list panel"
              aria-valuenow={listWidthPx}
              aria-valuemin={LIST_WIDTH_MIN_PX}
              aria-valuemax={LIST_WIDTH_MAX_PX}
              tabIndex={0}
              onPointerDown={onPointerDown}
              onPointerMove={onPointerMove}
              onPointerUp={endDrag}
              onPointerCancel={endDrag}
              onKeyDown={onSeparatorKeyDown}
              className="absolute -right-[3px] bottom-0 top-0 z-20 w-[7px] cursor-col-resize touch-none bg-transparent transition-colors hover:bg-brand/40 focus-visible:bg-brand/60 focus-visible:outline-none"
            />
          )}
        </section>
      )}

      {showDetail && (
        <main
          ref={registerRegion('detail')}
          tabIndex={-1}
          data-region="detail"
          aria-label="Detail"
          style={detailStyle}
          className={cn(
            'flex min-w-0 flex-col overflow-hidden bg-background outline-none',
            detailOverlays && 'z-10 border-l border-border shadow-elevation',
            focusedRegion === 'detail' && 'ring-1 ring-inset ring-ring',
          )}
        >
          <div className="min-h-0 flex-1 overflow-y-auto">{detail}</div>
        </main>
      )}

      <div style={statusStyle} className="min-w-0">
        {status}
      </div>
    </div>
  )
}
