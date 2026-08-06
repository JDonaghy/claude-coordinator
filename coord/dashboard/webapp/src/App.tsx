import { lazy, Suspense } from 'react'
import { BrowserRouter, Routes, Route } from 'react-router-dom'
import Home from '@/components/Home'
import Detail from '@/components/Detail'
import { ThemeProvider } from '@/components/ui/theme-provider'
import { Toaster } from '@/components/ui/toaster'

// Lazy: xterm.js (#1068) is a sizable dependency that's only needed once a
// user actually taps into a live session's take-over view -- code-splitting
// it out of the main bundle keeps the Home/Detail initial load lean instead
// of shipping the terminal renderer to everyone up front.
const Terminal = lazy(() => import('@/components/Terminal'))

// Dev-only component gallery (#1546) — renders every ui/* primitive in both
// themes, so a human (or a Playwright acceptance slice) can see the whole
// system at once without hunting through the app for each one. Route-guarded
// by `import.meta.env.DEV` below so production can never navigate to it; it
// still ends up as its own lazy chunk in `dist/` (Rollup splits on the
// `import()` call site regardless of the guard), so vite.config.ts also
// excludes it from the PWA precache list -- see the comment there.
const Gallery = lazy(() => import('@/components/Gallery'))

/**
 * App root.  BrowserRouter is used here; the dashboard server serves index.html
 * as a SPA fallback for all non-API paths so deep links work on hard reload.
 */
export default function App() {
  return (
    <ThemeProvider>
      <div className="min-h-screen bg-background text-foreground">
        <BrowserRouter>
          <Routes>
            <Route path="/" element={<Home />} />
            <Route path="/detail/:id" element={<Detail />} />
            <Route
              path="/terminal/:sessionId"
              element={
                <Suspense fallback={null}>
                  <Terminal />
                </Suspense>
              }
            />
            {import.meta.env.DEV && (
              <Route
                path="/gallery"
                element={
                  <Suspense fallback={null}>
                    <Gallery />
                  </Suspense>
                }
              />
            )}
          </Routes>
        </BrowserRouter>
        <Toaster />
      </div>
    </ThemeProvider>
  )
}
