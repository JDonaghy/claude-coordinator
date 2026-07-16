import '@testing-library/jest-dom'

// jsdom has no ResizeObserver implementation. The Terminal view (#1067) uses
// one to re-fit xterm.js on viewport changes; stub it globally so any test
// that mounts Terminal doesn't need to know about this jsdom gap.
if (typeof globalThis.ResizeObserver === 'undefined') {
  class ResizeObserverStub {
    observe(): void {}
    unobserve(): void {}
    disconnect(): void {}
  }
  globalThis.ResizeObserver = ResizeObserverStub as unknown as typeof ResizeObserver
}
