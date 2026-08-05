import React from 'react'
import ReactDOM from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import App from './App'
import { RealtimeProvider } from './realtime/RealtimeProvider'
import './index.css'

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // #1549: no timer-based polling. The SSE `/events` stream
      // (RealtimeProvider) invalidates exactly the query keys each server
      // event affects, and a stream recovery (reconnecting -> live) triggers
      // an explicit resync -- see src/realtime/events.ts. Data is therefore
      // fresh until something says otherwise, which `refetchOnWindowFocus`'s
      // default (true) still backstops for a tab that was backgrounded long
      // enough to miss its own reconnect.
      staleTime: Infinity,
      // Retry once on error before surfacing the failure state
      retry: 1,
    },
  },
})

// Register service worker (vite-plugin-pwa injects this at build time)
// In dev mode the import is a no-op stub from the virtual module.
import { registerSW } from 'virtual:pwa-register'
registerSW({ immediate: true })

ReactDOM.createRoot(document.getElementById('root') as HTMLElement).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <RealtimeProvider>
        <App />
      </RealtimeProvider>
    </QueryClientProvider>
  </React.StrictMode>,
)
