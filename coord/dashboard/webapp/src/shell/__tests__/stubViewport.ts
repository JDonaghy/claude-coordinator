/**
 * `window.matchMedia` stub that answers `(min-width: Npx)` queries as if the
 * viewport were a given width (#1547).
 *
 * jsdom has no layout, so `useShellMode()` has nothing real to read: without
 * this every shell test would run in the fallback `narrow` mode and the wide
 * composition — three columns, F6 across three regions, the resize separator —
 * would be entirely untested. `src/test-setup.ts` installs a stub that always
 * answers `matches: false`; this replaces it for the duration of a test and
 * `restoreViewport()` puts the original back.
 */
const originals: Array<typeof window.matchMedia> = []

/** Widths that land squarely inside each shell mode. */
export const NARROW_PX = 390
export const MEDIUM_PX = 820
export const WIDE_PX = 1440

export function stubViewportWidth(width: number): void {
  originals.push(window.matchMedia)
  window.matchMedia = ((query: string) => {
    const match = /min-width:\s*(\d+)px/.exec(query)
    const minWidth = match ? Number(match[1]) : 0
    return {
      matches: width >= minWidth,
      media: query,
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    }
  }) as typeof window.matchMedia
}

export function restoreViewport(): void {
  const original = originals.pop()
  if (original) window.matchMedia = original
}
