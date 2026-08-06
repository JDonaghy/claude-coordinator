/**
 * ShellLayout (#1547) — the react-router *layout route* that composes the
 * shell, and the single place that decides what goes in which slot.
 *
 * The composition is deliberately boring, because that is the acceptance
 * criterion: there is no `isMobile ? <PhoneHome/> : <DesktopHome/>`. `list`
 * and `detail` are computed once and handed to `AppShell` unchanged; the only
 * thing `mode` decides is where they land and which of them is mounted.
 *
 * The detail slot is `<Outlet/>` — so the child route (`/` → `EmptyDetail`,
 * `/detail/:id` → `Detail`) fills it, and every existing URL keeps working
 * untouched. Deep links and route restructuring are the *next* story (#1548);
 * the selected *view* therefore lives in persisted shell state rather than in
 * the path.
 */
import { Outlet, useMatch } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'

import Home from '@/components/Home'
import SessionsList from '@/components/SessionsList'
import { fetchPipeline, fetchSessions } from '@/api/client'
import { isActive, needsMe } from '@/lib/pipeline'
import { AppShell } from './AppShell'
import { ActivityRail } from './ActivityRail'
import { StatusBar } from './StatusBar'
import { useShellMode } from './breakpoints'
import { useShellState, type ShellView } from './shellState'
import { useRegionFocus, type ShellRegion } from './useRegionFocus'

const ATTENTION_VIEWS: ReadonlySet<ShellView> = new Set<ShellView>(['pipeline'])

export function ShellLayout() {
  const mode = useShellMode()
  const shell = useShellState()

  // `/detail/:id` is the only child route that fills the detail column with
  // something; everything else falls through to EmptyDetail.
  const detailActive = useMatch('/detail/:id') !== null

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

  const list = shell.view === 'sessions' ? <SessionsList /> : <Home />

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
          view={shell.view}
          onSelect={shell.setView}
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
