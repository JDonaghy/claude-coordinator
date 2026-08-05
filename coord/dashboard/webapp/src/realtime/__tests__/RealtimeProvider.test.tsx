/**
 * React-level tests for RealtimeProvider (#1549): event -> query-key
 * invalidation, the honest connection-state context, and the StrictMode
 * double-mount trap the module doc comment (connection.ts) describes --
 * exercised here with React's actual `<StrictMode>`, not just simulated.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, render, screen, waitFor } from '@testing-library/react'
import React from 'react'
import { QueryClient, QueryClientProvider, useQuery } from '@tanstack/react-query'
import { RealtimeProvider, useConnectionStatus } from '../RealtimeProvider'
import type { SseHandle } from '../connection'

// ── Fake EventSource (same shape as connection.test.ts's) ────────────────────

class FakeEventSource implements SseHandle {
  static instances: FakeEventSource[] = []
  static reset(): void {
    FakeEventSource.instances = []
  }

  url: string
  closed = false
  onopen: ((ev: unknown) => void) | null = null
  onerror: ((ev: unknown) => void) | null = null
  private listeners = new Map<string, Array<(ev: { data: string }) => void>>()

  constructor(url: string) {
    this.url = url
    FakeEventSource.instances.push(this)
  }

  addEventListener(type: string, listener: (ev: { data: string }) => void): void {
    const list = this.listeners.get(type) ?? []
    list.push(listener)
    this.listeners.set(type, list)
  }

  close(): void {
    this.closed = true
  }

  emitOpen(): void {
    this.onopen?.({})
  }

  emitError(): void {
    this.onerror?.({})
  }

  emitMessage(type: string, data: unknown): void {
    for (const listener of this.listeners.get(type) ?? []) {
      listener({ data: JSON.stringify(data) })
    }
  }
}

function lastInstance(): FakeEventSource {
  const inst = FakeEventSource.instances[FakeEventSource.instances.length - 1]
  if (!inst) throw new Error('no FakeEventSource created')
  return inst
}

function openInstances(): FakeEventSource[] {
  return FakeEventSource.instances.filter((i) => !i.closed)
}

beforeEach(() => {
  FakeEventSource.reset()
})

// ── Test fixtures ────────────────────────────────────────────────────────────

/** Renders the connection status as text, for assertions. */
function StatusProbe() {
  const { state } = useConnectionStatus()
  return <span data-testid="status">{state}</span>
}

/** A query whose fetch count is observable, standing in for ['pipeline']. */
function pipelineFetcher() {
  return vi.fn().mockResolvedValue(['ok'])
}

function PipelineProbe({ queryFn }: { queryFn: () => Promise<string[]> }) {
  const { data } = useQuery({ queryKey: ['pipeline'], queryFn })
  return <span data-testid="pipeline">{data?.length ?? 0}</span>
}

function createTestQueryClient() {
  return new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  })
}

// ── Tests ────────────────────────────────────────────────────────────────────

describe('RealtimeProvider', () => {
  it('exposes connecting -> live via context as the stream opens', async () => {
    render(
      <QueryClientProvider client={createTestQueryClient()}>
        <RealtimeProvider createEventSource={(url) => new FakeEventSource(url) as unknown as SseHandle}>
          <StatusProbe />
        </RealtimeProvider>
      </QueryClientProvider>,
    )

    await waitFor(() => expect(FakeEventSource.instances.length).toBeGreaterThan(0))
    expect(screen.getByTestId('status').textContent).toBe('connecting')

    act(() => lastInstance().emitOpen())
    await waitFor(() => expect(screen.getByTestId('status').textContent).toBe('live'))
  })

  it('invalidates the pipeline query when an assignment_started event arrives', async () => {
    const queryClient = createTestQueryClient()
    const queryFn = pipelineFetcher()

    render(
      <QueryClientProvider client={queryClient}>
        <RealtimeProvider createEventSource={(url) => new FakeEventSource(url) as unknown as SseHandle}>
          <PipelineProbe queryFn={queryFn} />
        </RealtimeProvider>
      </QueryClientProvider>,
    )

    await waitFor(() => expect(queryFn).toHaveBeenCalledTimes(1))
    act(() => lastInstance().emitOpen())

    act(() => lastInstance().emitMessage('assignment_started', { assignment_id: 'w-1' }))

    await waitFor(() => expect(queryFn).toHaveBeenCalledTimes(2))
  })

  it('resyncs the pipeline query on recovery from a drop, not on the first connect', async () => {
    const queryClient = createTestQueryClient()
    const queryFn = pipelineFetcher()

    render(
      <QueryClientProvider client={queryClient}>
        <RealtimeProvider
          createEventSource={(url) => new FakeEventSource(url) as unknown as SseHandle}
          backoffScheduleMs={[5]}
        >
          <PipelineProbe queryFn={queryFn} />
        </RealtimeProvider>
      </QueryClientProvider>,
    )

    await waitFor(() => expect(queryFn).toHaveBeenCalledTimes(1))
    act(() => lastInstance().emitOpen())
    // First-ever connect: no resync, still just the initial fetch.
    expect(queryFn).toHaveBeenCalledTimes(1)

    // Drop, then a fresh socket recovers.
    act(() => lastInstance().emitError())
    await waitFor(() => expect(FakeEventSource.instances).toHaveLength(2))
    act(() => lastInstance().emitOpen())

    // Recovery from a real drop resyncs, on top of whatever the SSE event
    // stream itself triggered.
    await waitFor(() => expect(queryFn.mock.calls.length).toBeGreaterThanOrEqual(2))
  })

  it('survives the StrictMode double-mount replay with exactly one live socket', async () => {
    render(
      <React.StrictMode>
        <QueryClientProvider client={createTestQueryClient()}>
          <RealtimeProvider
            createEventSource={(url) => new FakeEventSource(url) as unknown as SseHandle}
          >
            <StatusProbe />
          </RealtimeProvider>
        </QueryClientProvider>
      </React.StrictMode>,
    )

    // StrictMode's mount -> cleanup -> mount may create an early socket that
    // gets superseded; only one should remain open.
    await waitFor(() => expect(openInstances()).toHaveLength(1))

    act(() => openInstances()[0].emitOpen())
    await waitFor(() => expect(screen.getByTestId('status').textContent).toBe('live'))

    // The status only reflects the surviving socket -- a stale callback from
    // any closed socket must not have flipped it back.
    expect(screen.getByTestId('status').textContent).toBe('live')
  })
})
