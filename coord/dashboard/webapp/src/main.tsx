import React from 'react'
import ReactDOM from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import App from './App'
import './index.css'

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // 4-second polling interval for board and pipeline queries
      refetchInterval: 4_000,
      // Treat data as fresh for 2 s to avoid redundant background refetches
      staleTime: 2_000,
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
      <App />
    </QueryClientProvider>
  </React.StrictMode>,
)
