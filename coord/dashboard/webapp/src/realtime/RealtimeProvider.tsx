/**
 * Wires the SSE connection (`connection.ts`) to react-query invalidation and
 * exposes the live connection state via context (#1549).
 *
 * Mount once, at the root, inside `QueryClientProvider` (see `main.tsx`).
 * Every screen that wants the honest live/reconnecting/stale indicator reads
 * `useConnectionStatus()`; nothing else needs to know an `EventSource` is
 * involved.
 */
import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from 'react'
import { useQueryClient, type QueryClient } from '@tanstack/react-query'
import {
  createSseConnection,
  type ConnectionStatus,
  type EventSourceFactory,
} from './connection'
import { EVENT_TYPES, EVENT_QUERY_KEYS, RESYNC_QUERY_KEYS } from './events'

const SSE_URL = '/events'

const INITIAL_STATUS: ConnectionStatus = { state: 'connecting', lastLiveAt: null, attempt: 0 }

const ConnectionStatusContext = createContext<ConnectionStatus>(INITIAL_STATUS)

/** The current SSE connection state — live / reconnecting / disconnected / connecting. */
// eslint-disable-next-line react-refresh/only-export-components -- a hook alongside its provider component is the standard context pattern (see components/ui/badge.tsx for the same precedent).
export function useConnectionStatus(): ConnectionStatus {
  return useContext(ConnectionStatusContext)
}

interface RealtimeProviderProps {
  children: ReactNode
  /** Test-only seam; production always uses the browser's native `EventSource`. */
  createEventSource?: EventSourceFactory
  /** Test-only seam for pointing at a fixture/mock stream. */
  url?: string
  /** Test-only seam for exact backoff timing; production uses connection.ts's default schedule. */
  backoffScheduleMs?: readonly number[]
}

function invalidate(queryClient: QueryClient, type: string): void {
  for (const key of EVENT_QUERY_KEYS[type] ?? []) {
    void queryClient.invalidateQueries({ queryKey: key })
  }
}

function resync(queryClient: QueryClient): void {
  for (const key of RESYNC_QUERY_KEYS) {
    void queryClient.invalidateQueries({ queryKey: key })
  }
}

export function RealtimeProvider({
  children,
  createEventSource,
  url,
  backoffScheduleMs,
}: RealtimeProviderProps) {
  const queryClient = useQueryClient()
  const [status, setStatus] = useState<ConnectionStatus>(INITIAL_STATUS)

  // useQuery/useQueryClient give a stable-enough client in practice, but the
  // connection object is only ever created once (see the lazy ref below) —
  // route every callback through a ref so it always sees the current client
  // rather than closing over whatever was live at construction time.
  const queryClientRef = useRef(queryClient)
  queryClientRef.current = queryClient

  // The connection is created exactly once per component instance (lazy
  // useRef initializer) and driven by start()/stop() from the effect below.
  // This is deliberate: StrictMode's mount -> cleanup -> mount replay must
  // reuse the SAME connection object so its start()'s token bump is what
  // guards the superseded socket, rather than constructing two independent
  // connections that both think they're the only one.
  const connRef = useRef<ReturnType<typeof createSseConnection> | null>(null)
  const prevStateRef = useRef<ConnectionStatus['state']>('connecting')
  if (connRef.current === null) {
    connRef.current = createSseConnection({
      url: url ?? SSE_URL,
      eventTypes: EVENT_TYPES,
      createEventSource,
      backoffScheduleMs,
      onEvent: (type, data) => {
        invalidate(queryClientRef.current, type)
        // board_updated carries a fresh timestamp/summary but no assignment
        // identity worth threading further -- data is otherwise unused today.
        void data
      },
      onStatusChange: (next) => {
        // The only path to 'live' is from 'connecting' (first-ever connect,
        // nothing to resync) or from 'reconnecting' (a real recovery, where
        // events may have been missed -- see events.ts RESYNC_QUERY_KEYS).
        if (next.state === 'live' && prevStateRef.current === 'reconnecting') {
          resync(queryClientRef.current)
        }
        prevStateRef.current = next.state
        setStatus(next)
      },
    })
  }

  useEffect(() => {
    const conn = connRef.current
    if (!conn) return
    conn.start()
    return () => conn.stop()
  }, [])

  return (
    <ConnectionStatusContext.Provider value={status}>{children}</ConnectionStatusContext.Provider>
  )
}
