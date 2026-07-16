import { BrowserRouter, Routes, Route } from 'react-router-dom'
import Home from '@/components/Home'
import Detail from '@/components/Detail'
import TerminalComingSoon from '@/components/TerminalComingSoon'

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
          <Route path="/terminal/:sessionId" element={<TerminalComingSoon />} />
        </Routes>
      </BrowserRouter>
    </div>
  )
}
