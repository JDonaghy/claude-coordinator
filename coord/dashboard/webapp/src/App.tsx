import { lazy, Suspense } from 'react'
import { BrowserRouter, Routes, Route } from 'react-router-dom'
import Detail from '@/components/Detail'
import { ShellLayout } from '@/shell/ShellLayout'
import { EmptyDetail } from '@/shell/EmptyDetail'
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
 *
 * Routing shape (#1547): `ShellLayout` is a react-router *layout route*, so
 * `/` and `/detail/:id` both render inside the responsive shell (rail + list +
 * detail on wide, the phone app on narrow) with the child route filling the
 * detail slot. The URLs themselves are unchanged — restructuring them for deep
 * links is the next story (#1548).
 *
 * `/terminal/:sessionId` stays deliberately *outside* the shell: the PTY pane
 * wants the whole viewport and brings its own key bar, and framing a terminal
 * in a rail plus a status bar would cost it rows on exactly the device (a
 * phone) where rows are scarcest.
 */
export default function App() {
  return (
    <ThemeProvider>
      <BrowserRouter>
        <Routes>
          <Route element={<ShellLayout />}>
            <Route path="/" element={<EmptyDetail />} />
            <Route path="/detail/:id" element={<Detail />} />
          </Route>
          <Route
            path="/terminal/:sessionId"
            element={
              <div className="min-h-screen bg-background text-foreground">
                <Suspense fallback={null}>
                  <Terminal />
                </Suspense>
              </div>
            }
          />
          {import.meta.env.DEV && (
            <Route
              path="/gallery"
              element={
                <div className="min-h-screen bg-background text-foreground">
                  <Suspense fallback={null}>
                    <Gallery />
                  </Suspense>
                </div>
              }
            />
          )}
        </Routes>
      </BrowserRouter>
      <Toaster />
    </ThemeProvider>
  )
}
