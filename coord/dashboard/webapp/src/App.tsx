import { lazy, Suspense } from 'react'
import { BrowserRouter, Routes, Route } from 'react-router-dom'
import Home from '@/components/Home'
import Detail from '@/components/Detail'

// Lazy: xterm.js (#1068) is a sizable dependency that's only needed once a
// user actually taps into a live session's take-over view -- code-splitting
// it out of the main bundle keeps the Home/Detail initial load lean instead
// of shipping the terminal renderer to everyone up front.
const Terminal = lazy(() => import('@/components/Terminal'))

/**
 * App root.  BrowserRouter is used here; the dashboard server serves index.html
 * as a SPA fallback for all non-API paths so deep links work on hard reload.
 */
export default function App() {
  return (
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
        </Routes>
      </BrowserRouter>
    </div>
  )
}
