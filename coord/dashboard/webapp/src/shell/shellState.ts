/**
 * Persisted shell geometry + selected view (#1547).
 *
 * "Panel sizes and the selected view survive a reload" is an acceptance
 * criterion, so all of it lives in one localStorage blob under a single key,
 * written on every change and read once at mount. Reads and writes are
 * individually guarded: `localStorage` throws outright in Safari private
 * browsing and in lockdown modes, and a shell that refuses to render because
 * it couldn't remember a panel width would be a worse bug than forgetting the
 * width. Same posture (and same key prefix) as `ui/theme-provider.tsx`.
 *
 * The *selected view* is shell state rather than a route on purpose: routing
 * and deep links are explicitly the next story (#1548). Keeping the view here
 * means `/`, `/detail/:id` and `/terminal/:sessionId` all keep working exactly
 * as they do today while the rail still gets somewhere to point.
 */
import { useCallback, useState } from 'react'

export const SHELL_STORAGE_KEY = 'coord-web-shell'

/** Every entry the activity rail can show, built or not. */
export type ShellView =
  | 'pipeline'
  | 'board'
  | 'sessions'
  | 'terminal'
  | 'machines'
  | 'merge-queue'
  | 'milestones'
  | 'audit'
  | 'spend'
  | 'settings'

/** Views that actually render something today; the rest are rail placeholders. */
export const READY_VIEWS: ReadonlySet<ShellView> = new Set<ShellView>(['pipeline', 'sessions'])

export const LIST_WIDTH_MIN_PX = 260
export const LIST_WIDTH_MAX_PX = 640
export const LIST_WIDTH_DEFAULT_PX = 360

export const RAIL_WIDTH_PX = 216
export const RAIL_WIDTH_COLLAPSED_PX = 60

export interface ShellState {
  view: ShellView
  railCollapsed: boolean
  listCollapsed: boolean
  listWidthPx: number
}

const DEFAULT_STATE: ShellState = {
  view: 'pipeline',
  railCollapsed: false,
  listCollapsed: false,
  listWidthPx: LIST_WIDTH_DEFAULT_PX,
}

export function clampListWidth(px: number): number {
  if (!Number.isFinite(px)) return LIST_WIDTH_DEFAULT_PX
  return Math.min(LIST_WIDTH_MAX_PX, Math.max(LIST_WIDTH_MIN_PX, Math.round(px)))
}

function isShellView(value: unknown): value is ShellView {
  return (
    typeof value === 'string' &&
    (
      [
        'pipeline',
        'board',
        'sessions',
        'terminal',
        'machines',
        'merge-queue',
        'milestones',
        'audit',
        'spend',
        'settings',
      ] as string[]
    ).includes(value)
  )
}

/**
 * Parse a persisted blob field-by-field rather than trusting its shape.
 * The key is user-writable (devtools) and, more practically, will be read by
 * future builds after this schema grows — an unknown or half-written value
 * must degrade to the default, never to `NaN` grid columns.
 */
export function parseShellState(raw: string | null): ShellState {
  if (!raw) return DEFAULT_STATE
  let parsed: unknown
  try {
    parsed = JSON.parse(raw)
  } catch {
    return DEFAULT_STATE
  }
  if (typeof parsed !== 'object' || parsed === null) return DEFAULT_STATE
  const obj = parsed as Record<string, unknown>

  // A view that was persisted while it was "ready" but has since been removed
  // (or was never ready) must not strand the user on a blank panel.
  const view = isShellView(obj.view) && READY_VIEWS.has(obj.view) ? obj.view : DEFAULT_STATE.view

  return {
    view,
    railCollapsed:
      typeof obj.railCollapsed === 'boolean' ? obj.railCollapsed : DEFAULT_STATE.railCollapsed,
    listCollapsed:
      typeof obj.listCollapsed === 'boolean' ? obj.listCollapsed : DEFAULT_STATE.listCollapsed,
    listWidthPx:
      typeof obj.listWidthPx === 'number'
        ? clampListWidth(obj.listWidthPx)
        : DEFAULT_STATE.listWidthPx,
  }
}

function readShellState(): ShellState {
  if (typeof window === 'undefined') return DEFAULT_STATE
  try {
    return parseShellState(window.localStorage.getItem(SHELL_STORAGE_KEY))
  } catch {
    return DEFAULT_STATE
  }
}

function writeShellState(state: ShellState): void {
  if (typeof window === 'undefined') return
  try {
    window.localStorage.setItem(SHELL_STORAGE_KEY, JSON.stringify(state))
  } catch {
    // Persistence is a nice-to-have; the layout still applies this session.
  }
}

export interface UseShellState extends ShellState {
  setView: (view: ShellView) => void
  setRailCollapsed: (collapsed: boolean) => void
  toggleRail: () => void
  setListCollapsed: (collapsed: boolean) => void
  toggleList: () => void
  setListWidthPx: (px: number) => void
}

export function useShellState(): UseShellState {
  const [state, setState] = useState<ShellState>(readShellState)

  const update = useCallback((patch: Partial<ShellState>) => {
    setState((prev) => {
      const next = { ...prev, ...patch }
      // Cheap identity guard: a pointer-drag resize fires many times a second
      // and would otherwise re-render (and re-serialise) on every no-op move.
      if (
        next.view === prev.view &&
        next.railCollapsed === prev.railCollapsed &&
        next.listCollapsed === prev.listCollapsed &&
        next.listWidthPx === prev.listWidthPx
      ) {
        return prev
      }
      writeShellState(next)
      return next
    })
  }, [])

  return {
    ...state,
    setView: useCallback((view: ShellView) => update({ view }), [update]),
    setRailCollapsed: useCallback(
      (railCollapsed: boolean) => update({ railCollapsed }),
      [update],
    ),
    toggleRail: useCallback(
      () => setState((prev) => {
        const next = { ...prev, railCollapsed: !prev.railCollapsed }
        writeShellState(next)
        return next
      }),
      [],
    ),
    setListCollapsed: useCallback(
      (listCollapsed: boolean) => update({ listCollapsed }),
      [update],
    ),
    toggleList: useCallback(
      () => setState((prev) => {
        const next = { ...prev, listCollapsed: !prev.listCollapsed }
        writeShellState(next)
        return next
      }),
      [],
    ),
    setListWidthPx: useCallback(
      (px: number) => update({ listWidthPx: clampListWidth(px) }),
      [update],
    ),
  }
}
