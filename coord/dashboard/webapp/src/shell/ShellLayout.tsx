/**
 * ShellLayout (#1547, routed by #1548) — the react-router *layout route*
 * that composes the shell, and the single place that decides what goes in
 * which slot.
 *
 * The composition is deliberately boring, because that is the acceptance
 * criterion: there is no `isMobile ? <PhoneHome/> : <DesktopHome/>`. `list`
 * and `detail` are computed once and handed to `AppShell` unchanged; the only
 * thing `mode` decides is where they land and which of them is mounted.
 *
 * The detail slot is `<Outlet/>` — so the child route (`/pipeline` ->
 * `EmptyDetail`, `/pipeline/:repo/:issue[/:tab]` -> `Detail`,
 * `/sessions/:id` -> `SessionDetail`, an unbuilt panel's own path ->
 * `ComingSoon`, anything else -> `RouteNotFound`) fills it.
 *
 * The rail selection is derived from the URL (`shellViewFromPath`), not from
 * persisted state: that was the explicit deferral #1547 made ("routing and
 * deep links are the next story"), and this *is* that story. A URL is now
 * the single source of truth for "what view am I looking at" — reload,
 * bookmark, paste into Slack, browser back — all of it falls out of normal
 * router behaviour instead of a second, URL-independent state machine that
 * could disagree with the address bar.
 */
import { Outlet, useLocation, useMatch, useNavigate } from 'react-router-dom'
import { useCallback, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'

import Home from '@/components/Home'
import SessionsList from '@/components/SessionsList'
import { fetchPipeline, fetchSessions } from '@/api/client'
import { isActive, needsMe } from '@/lib/pipeline'
import { RAIL_VIEW_PATH, shellViewFromPath } from '@/routes/paths'
import { AppShell } from './AppShell'
import { ActivityRail } from './ActivityRail'
import { ComingSoon } from './ComingSoon'
import { RouteNotFound } from './RouteNotFound'
import { StatusBar } from './StatusBar'
import { useShellMode } from './breakpoints'
import { useShellState, type ShellView } from './shellState'
import { useRegionFocus, type ShellRegion } from './useRegionFocus'

const ATTENTION_VIEWS: ReadonlySet<ShellView> = new Set<ShellView>(['pipeline'])

export function ShellLayout() {
  const mode = useShellMode()
  const shell = useShellState()
  const navigate = useNavigate()
  const location = useLocation()

  // Which rail entry the current URL belongs to — `null` for a path with no
  // owning view (an unmatched route; root `/` never reaches here, see
  // `App.tsx`'s redirect).
  const currentView = shellViewFromPath(location.pathname)

  // The detail slot is "active" (narrow: shown instead of the list; medium:
  // overlaid on it) exactly when the route names a specific item — a
  // pipeline issue (with or without a tab segment) or a session. Matched
  // explicitly rather than inferred from `currentView`, because "there is an
  // Outlet route that fills the detail slot" and "which rail item is lit up"
  // are genuinely different questions once there's more than one detail-
  // capable list.
  const pipelineItemMatch = useMatch('/pipeline/:repo/:issue')
  const pipelineItemTabMatch = useMatch('/pipeline/:repo/:issue/:tab')
  const sessionItemMatch = useMatch('/sessions/:id')
  const detailActive = !!(pipelineItemMatch || pipelineItemTabMatch || sessionItemMatch)

  // Same query keys the panels use, so this is a cache read, not a second
  // fetch (see main.tsx: staleTime Infinity + SSE-driven invalidation).
  const { data: pipeline } = useQuery({ queryKey: ['pipeline'], queryFn: fetchPipeline })
  const { data: sessions } = useQuery({ queryKey: ['sessions'], queryFn: fetchSessions })

  const inFlight = pipeline?.filter(isActive).length
  const attention = pipeline?.some(needsMe) ?? false
  // `machine` is nullable on SessionInfo (a session on a host the roster
  // hasn't resolved yet) — a `null` in the status bar would render as an empty
  // segment between two separators.
  const machines = sessions
    ? Array.from(new Set(sessions.map((s) => s.machine).filter((m): m is string => !!m))).sort()
    : []

  // Kept in step with AppShell's own `showList` / `showDetail` — F6 must not
  // offer a region that isn't mounted.
  const listMounted = mode === 'narrow' ? !detailActive : !(mode === 'wide' && shell.listCollapsed)
  const detailMounted = mode === 'wide' || detailActive

  // F6 must skip regions that aren't on screen — cycling into an unmounted
  // container would silently drop focus to <body>.
  const visibleRegions: ShellRegion[] = [
    'rail' as const,
    ...(listMounted ? (['list'] as const) : []),
    ...(detailMounted ? (['detail'] as const) : []),
  ]
  const { focusedRegion, registerRegion } = useRegionFocus(visibleRegions)

  const handleSelectView = useCallback(
    (view: ShellView) => {
      const path = RAIL_VIEW_PATH[view]
      // Every 'ready' rail entry has a path; a 'soon' entry never reaches
      // here (ActivityRail doesn't wire onClick for it). The guard is for
      // type honesty (`RAIL_VIEW_PATH` is a `Partial`), not a real runtime case.
      if (path) navigate(path)
    },
    [navigate],
  )

  let list: ReactNode
  if (currentView === 'pipeline') {
    list = <Home />
  } else if (currentView === 'sessions') {
    list = <SessionsList />
  } else if (currentView === null) {
    list = <RouteNotFound />
  } else {
    list = <ComingSoon view={currentView} />
  }

  return (
    <AppShell
      mode={mode}
      railCollapsed={shell.railCollapsed}
      listCollapsed={shell.listCollapsed}
      listWidthPx={shell.listWidthPx}
      onListWidthChange={shell.setListWidthPx}
      detailActive={detailActive}
      registerRegion={registerRegion}
      focusedRegion={focusedRegion}
      rail={
        <ActivityRail
          mode={mode}
          collapsed={shell.railCollapsed}
          view={currentView}
          onSelect={handleSelectView}
          onToggleCollapsed={shell.toggleRail}
          listCollapsed={shell.listCollapsed}
          onToggleList={shell.toggleList}
          counts={{ pipeline: inFlight, sessions: sessions?.length }}
          attentionViews={attention ? ATTENTION_VIEWS : undefined}
          regionRef={registerRegion('rail')}
          regionFocused={focusedRegion === 'rail'}
        />
      }
      list={list}
      detail={<Outlet />}
      status={<StatusBar inFlight={inFlight} sessions={sessions?.length} machines={machines} />}
    />
  )
}
