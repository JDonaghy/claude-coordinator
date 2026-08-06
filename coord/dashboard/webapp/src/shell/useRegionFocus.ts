/**
 * Keyboard region cycling for the shell (#1547) — the web-idiomatic answer to
 * coord-tui's `FocusedRegion` / Ctrl-W.
 *
 * The binding is **F6 / Shift+F6**, which is the long-standing platform
 * convention for "move to the next pane" (Firefox and Chrome both use it to
 * cycle browser panes, VS Code uses it for editor groups, and Windows uses it
 * app-wide). It is also, unusually for a function key, safe: no browser
 * assigns F6 to anything that a web page stealing it would break, and it
 * doesn't collide with a text field the way a Ctrl-letter chord does — so the
 * handler doesn't need to special-case typing in the list panel's filter box.
 *
 * Focus lands on the *region container* (each is `tabIndex={-1}` with an
 * accessible name), not on the first control inside it. That's the standard
 * landmark-navigation behaviour: a screen reader announces the region you
 * arrived in, and a plain Tab from there walks into its controls.
 */
import { useCallback, useEffect, useRef, useState } from 'react'

export type ShellRegion = 'rail' | 'list' | 'detail'

export interface UseRegionFocus {
  /** The region focus was last moved to via F6, for a visible ring. */
  focusedRegion: ShellRegion | null
  /** Ref callback factory: `ref={registerRegion('list')}`. */
  registerRegion: (region: ShellRegion) => (el: HTMLElement | null) => void
  /** Programmatic move (used by the resize handle's Escape, and by tests). */
  focusRegion: (region: ShellRegion) => void
}

/**
 * @param visibleRegions Regions currently on screen, in visual order. Regions
 *   omitted here are skipped by F6 — on narrow only one of list/detail exists
 *   at a time, and cycling into a `display:none` container would silently drop
 *   focus to `<body>`.
 */
export function useRegionFocus(visibleRegions: ShellRegion[]): UseRegionFocus {
  const elements = useRef<Partial<Record<ShellRegion, HTMLElement | null>>>({})
  const [focusedRegion, setFocusedRegion] = useState<ShellRegion | null>(null)

  // The keydown listener is attached once; reading the order through a ref
  // keeps it from being torn down and rebuilt on every layout change.
  const orderRef = useRef(visibleRegions)
  orderRef.current = visibleRegions

  const registerRegion = useCallback(
    (region: ShellRegion) => (el: HTMLElement | null) => {
      elements.current[region] = el
    },
    [],
  )

  const focusRegion = useCallback((region: ShellRegion) => {
    const el = elements.current[region]
    if (!el) return
    el.focus()
    setFocusedRegion(region)
  }, [])

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'F6') return
      const order = orderRef.current
      if (order.length === 0) return

      // Where are we now? Prefer the region that actually contains
      // `document.activeElement` over the last F6 target, so an F6 after a
      // mouse click continues from where the user is rather than from where
      // the keyboard last was.
      const active = document.activeElement as HTMLElement | null
      let currentIndex = order.findIndex((region) => {
        const el = elements.current[region]
        return !!el && !!active && el.contains(active)
      })
      if (currentIndex === -1) currentIndex = order.indexOf(focusedRegion as ShellRegion)

      const step = event.shiftKey ? -1 : 1
      // `currentIndex === -1` (focus is outside every region) + step 1 lands
      // on index 0, which is the intended "enter the shell at the rail".
      const nextIndex = (currentIndex + step + order.length) % order.length
      const next = order[nextIndex]
      const el = elements.current[next]
      if (!el) return

      event.preventDefault()
      el.focus()
      setFocusedRegion(next)
    }

    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [focusedRegion])

  return { focusedRegion, registerRegion, focusRegion }
}
