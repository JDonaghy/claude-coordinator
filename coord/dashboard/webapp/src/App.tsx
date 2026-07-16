import { lazy, Suspense } from 'react'
import { BrowserRouter, Routes, Route } from 'react-router-dom'
import Home from '@/components/Home'
import Detail from '@/components/Detail'

// Lazy-loaded: xterm.js pulls its weight into the main bundle (#1067 pulled
// it past Rollup's 500kB chunk-size warning threshold). Most visits never
// open a terminal, so split it into its own chunk fetched only on navigation
// to /terminal/:sessionId — this also matters on a phone's cellular link.
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
              <Suspense
                fallback={
                  <p className="py-12 text-center text-sm text-muted-foreground">
                    Loading terminal…
                  </p>
                }
              >
                <Terminal />
              </Suspense>
            }
          />
        </Routes>
      </BrowserRouter>
    </div>
  )
}
