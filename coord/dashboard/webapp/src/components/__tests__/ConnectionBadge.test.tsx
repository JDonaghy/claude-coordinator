/**
 * Component tests for ConnectionBadge (#1549) — the honest
 * live/reconnecting/stale-since-<time> indicator. Renders directly against
 * the real ConnectionStatusContext by wrapping with RealtimeProvider's
 * context provider indirectly via a minimal test double: since the context
 * itself isn't exported, these tests drive it through the provider's public
 * surface (RealtimeProvider + a fake EventSource) rather than reaching into
 * internals.
 */
import { describe, it, expect } from 'vitest'
import { act, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { RealtimeProvider } from '@/realtime/RealtimeProvider'
import type { SseHandle } from '@/realtime/connection'
import { ConnectionBadge } from '../ConnectionBadge'

class FakeEventSource implements SseHandle {
  static instances: FakeEventSource[] = []
  url: string
  onopen: ((ev: unknown) => void) | null = null
  onerror: ((ev: unknown) => void) | null = null
  constructor(url: string) {
    this.url = url
    FakeEventSource.instances.push(this)
  }
  addEventListener(): void {}
  close(): void {}
  emitOpen(): void {
    this.onopen?.({})
  }
  emitError(): void {
    this.onerror?.({})
  }
}

function lastInstance(): FakeEventSource {
  const inst = FakeEventSource.instances[FakeEventSource.instances.length - 1]
  if (!inst) throw new Error('no instance')
  return inst
}

function renderBadge() {
  FakeEventSource.instances = []
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <RealtimeProvider
        createEventSource={(url) => new FakeEventSource(url) as unknown as SseHandle}
        backoffScheduleMs={[5]}
      >
        <ConnectionBadge />
      </RealtimeProvider>
    </QueryClientProvider>,
  )
}

describe('ConnectionBadge', () => {
  it('shows "Connecting…" before the first connect', async () => {
    renderBadge()
    await waitFor(() => expect(FakeEventSource.instances.length).toBeGreaterThan(0))
    expect(screen.getByRole('status')).toHaveTextContent('Connecting…')
  })

  it('shows "Live" once the stream opens', async () => {
    renderBadge()
    await waitFor(() => expect(FakeEventSource.instances.length).toBeGreaterThan(0))
    act(() => lastInstance().emitOpen())
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Live'))
  })

  it('shows a stale-since time then Reconnecting… after a drop', async () => {
    renderBadge()
    await waitFor(() => expect(FakeEventSource.instances.length).toBeGreaterThan(0))
    act(() => lastInstance().emitOpen())
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Live'))

    act(() => lastInstance().emitError())
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent(/Stale since/))

    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Reconnecting…'))
  })
})
