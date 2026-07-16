import '@testing-library/jest-dom'

// jsdom doesn't implement matchMedia (https://github.com/jsdom/jsdom/issues/3522).
// xterm.js's CoreBrowserService queries it (devicePixelRatio change listener) as
// soon as a Terminal is opened, which crashes every Terminal-pane test (#1068)
// without this stub.
if (typeof window !== 'undefined' && !window.matchMedia) {
  window.matchMedia = (query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  })
}

// jsdom's canvas is stubbed (no `canvas` npm package installed) -- getContext('2d')
// logs a noisy "Not implemented" jsdom error and returns undefined either way.
// xterm.js already handles a missing 2D context gracefully (its color helper
// falls back to manual CSS-color parsing), so short-circuit straight to that
// path and skip the console noise.
if (typeof HTMLCanvasElement !== 'undefined') {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any -- matching the DOM lib's overload surface isn't worth it for a test-only stub
  HTMLCanvasElement.prototype.getContext = (() => null) as any
}
