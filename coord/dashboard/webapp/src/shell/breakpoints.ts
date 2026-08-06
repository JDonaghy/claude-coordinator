/**
 * Responsive breakpoints for the app shell (#1547).
 *
 * Three modes, not two — the story asks explicitly that mid widths "degrade
 * sensibly rather than snapping between two extremes":
 *
 *   narrow  (< 768px)      the phone app, preserved verbatim: one column,
 *                          bottom nav, drill-in navigation.
 *   medium  (768–1023px)   rail collapses to its 60px icon strip, the list
 *                          keeps the whole content column, and the detail
 *                          arrives as an overlay above it (still a drill-in,
 *                          just without losing the list underneath).
 *   wide    (>= 1024px)    the `SidebarView` twin: rail + list + detail, all
 *                          three visible at once.
 *
 * The story's stated wide threshold is 1024px and the Gate-A mock
 * (docs/mocks/web/pipeline-wide.html) switches to its bottom-row layout at
 * 900px. Those aren't in conflict once there are three modes: 1024 is where
 * the third column appears, and the phone layout is kept for genuinely
 * phone-sized viewports (< 768) rather than for every tablet.
 *
 * Default when `matchMedia` is unavailable (jsdom, SSR) is `narrow` — the
 * conservative choice, since that's the layout that is in daily use and the
 * one every pre-existing component test was written against.
 */
import { useEffect, useState } from 'react'

export const BREAKPOINT_MEDIUM_PX = 768
export const BREAKPOINT_WIDE_PX = 1024

export type ShellMode = 'narrow' | 'medium' | 'wide'

const WIDE_QUERY = `(min-width: ${BREAKPOINT_WIDE_PX}px)`
const MEDIUM_QUERY = `(min-width: ${BREAKPOINT_MEDIUM_PX}px)`

function readMode(): ShellMode {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') {
    return 'narrow'
  }
  if (window.matchMedia(WIDE_QUERY).matches) return 'wide'
  if (window.matchMedia(MEDIUM_QUERY).matches) return 'medium'
  return 'narrow'
}

/**
 * Current shell mode, recomputed whenever either breakpoint is crossed.
 *
 * The initial value is computed synchronously in the `useState` initialiser
 * (not in an effect) so a wide browser's very first paint is already the wide
 * layout — no one-frame flash of the phone app on a 32" monitor.
 */
export function useShellMode(): ShellMode {
  const [mode, setMode] = useState<ShellMode>(readMode)

  useEffect(() => {
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return

    const wide = window.matchMedia(WIDE_QUERY)
    const medium = window.matchMedia(MEDIUM_QUERY)
    const sync = () => setMode(readMode())

    // Re-sync once on mount: between the render-time read above and this
    // effect, the viewport may have changed (or matchMedia may have been
    // stubbed in late, as the test setup does).
    sync()

    // `addListener`/`removeListener` are the deprecated MediaQueryList API;
    // some of the older WebKit builds this PWA is installed on (iOS < 14)
    // never got `addEventListener` on MediaQueryList, so fall back.
    const subscribe = (mql: MediaQueryList) => {
      if (typeof mql.addEventListener === 'function') {
        mql.addEventListener('change', sync)
        return () => mql.removeEventListener('change', sync)
      }
      if (typeof mql.addListener === 'function') {
        mql.addListener(sync)
        return () => mql.removeListener(sync)
      }
      return () => {}
    }

    const unsubscribes = [subscribe(wide), subscribe(medium)]
    return () => unsubscribes.forEach((fn) => fn())
  }, [])

  return mode
}
