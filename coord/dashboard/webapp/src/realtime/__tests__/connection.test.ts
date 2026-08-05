/**
 * Unit tests for the framework-agnostic SSE client (#1549).
 *
 * A fake `EventSource`-shaped handle stands in for the browser API so these
 * tests can drive open/error/message deterministically and assert on the
 * exact connection-state sequence, without any real network or timers other
 * than Vitest's fake ones.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { createSseConnection, type ConnectionStatus, type SseHandle } from '../connection'

// ── Fake EventSource ─────────────────────────────────────────────────────────

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

/** `Array.prototype.at` needs es2022 lib; the project targets es2020 (tsconfig.json). */
function last<T>(arr: readonly T[]): T | undefined {
  return arr[arr.length - 1]
}

beforeEach(() => {
  FakeEventSource.reset()
  vi.useFakeTimers()
})

afterEach(() => {
  vi.useRealTimers()
})

// ── Helpers ──────────────────────────────────────────────────────────────────

function setup(overrides: Partial<Parameters<typeof createSseConnection>[0]> = {}) {
  const events: Array<{ type: string; data: unknown }> = []
  const statuses: ConnectionStatus[] = []
  const conn = createSseConnection({
    url: '/events',
    eventTypes: ['assignment_started', 'board_updated'],
    onEvent: (type, data) => events.push({ type, data }),
    onStatusChange: (status) => statuses.push({ ...status }),
    createEventSource: (url) => new FakeEventSource(url) as unknown as SseHandle,
    backoffScheduleMs: [100, 200, 400],
    jitterFraction: 0,
    now: () => Date.now(),
    ...overrides,
  })
  return { conn, events, statuses }
}

// ── Tests ────────────────────────────────────────────────────────────────────

describe('createSseConnection', () => {
  it('opens a socket at the given URL and reports live on open', () => {
    const { conn, statuses } = setup()
    conn.start()

    expect(FakeEventSource.instances).toHaveLength(1)
    expect(lastInstance().url).toBe('/events')
    expect(last(statuses)).toMatchObject({ state: 'connecting' })

    lastInstance().emitOpen()

    expect(last(statuses)).toMatchObject({ state: 'live', attempt: 0 })
    expect(last(statuses)?.lastLiveAt).not.toBeNull()
  })

  it('forwards subscribed events as parsed JSON', () => {
    const { conn, events } = setup()
    conn.start()
    lastInstance().emitOpen()

    lastInstance().emitMessage('assignment_started', { assignment_id: 'w-1' })
    lastInstance().emitMessage('board_updated', { timestamp: 123 })

    expect(events).toEqual([
      { type: 'assignment_started', data: { assignment_id: 'w-1' } },
      { type: 'board_updated', data: { timestamp: 123 } },
    ])
  })

  it('goes disconnected -> reconnecting -> live on a drop and recovery', () => {
    const { conn, statuses } = setup()
    conn.start()
    lastInstance().emitOpen()
    expect(last(statuses)?.state).toBe('live')

    // The stream drops.
    lastInstance().emitError()
    expect(last(statuses)).toMatchObject({ state: 'disconnected', attempt: 1 })

    // Backoff timer fires -> a new attempt starts.
    vi.advanceTimersByTime(200)
    expect(FakeEventSource.instances).toHaveLength(2)
    expect(last(statuses)).toMatchObject({ state: 'reconnecting', attempt: 1 })

    // The new socket opens successfully.
    lastInstance().emitOpen()
    expect(last(statuses)).toMatchObject({ state: 'live', attempt: 0 })
  })

  it('uses the backoff schedule and caps at its last entry', () => {
    const { conn, statuses } = setup()
    conn.start()
    lastInstance().emitOpen()

    // Fail three times in a row; each retry should wait the schedule's next
    // entry (100, 200, 400), and a fourth failure should reuse 400 (the cap).
    lastInstance().emitError() // attempt 1 scheduled, delay 100
    vi.advanceTimersByTime(99)
    expect(FakeEventSource.instances).toHaveLength(1) // not yet retried

    vi.advanceTimersByTime(1)
    expect(FakeEventSource.instances).toHaveLength(2) // retried at 100ms

    lastInstance().emitError() // attempt 2 scheduled, delay 200
    vi.advanceTimersByTime(199)
    expect(FakeEventSource.instances).toHaveLength(2)
    vi.advanceTimersByTime(1)
    expect(FakeEventSource.instances).toHaveLength(3)

    lastInstance().emitError() // attempt 3 scheduled, delay 400
    vi.advanceTimersByTime(400)
    expect(FakeEventSource.instances).toHaveLength(4)

    lastInstance().emitError() // attempt 4 scheduled, delay capped at 400
    vi.advanceTimersByTime(400)
    expect(FakeEventSource.instances).toHaveLength(5)

    expect(last(statuses)?.state).toBe('reconnecting')
  })

  it('start() is idempotent-safe: a second start() supersedes the first socket', () => {
    const { conn, statuses } = setup()
    conn.start()
    const first = lastInstance()

    conn.start()
    const second = lastInstance()

    expect(first).not.toBe(second)
    expect(first.closed).toBe(true)

    // The superseded socket's late callbacks must not affect state.
    const countBefore = statuses.length
    first.emitOpen()
    expect(statuses.length).toBe(countBefore)

    // Only the current socket's events matter.
    second.emitOpen()
    expect(last(statuses)).toMatchObject({ state: 'live' })
  })

  it('stop() closes the socket, cancels pending retries, and silences further callbacks', () => {
    const { conn, statuses } = setup()
    conn.start()
    lastInstance().emitOpen()
    lastInstance().emitError()
    expect(last(statuses)?.state).toBe('disconnected')

    conn.stop()
    expect(lastInstance().closed).toBe(true)

    const countBeforeAdvance = statuses.length
    vi.advanceTimersByTime(10_000)
    // No retry socket should have been created, and no further status changes.
    expect(FakeEventSource.instances).toHaveLength(1)
    expect(statuses.length).toBe(countBeforeAdvance)
  })

  it('resets intent/backoff state on every start(), mirroring the StrictMode double-mount replay', () => {
    const { conn, statuses } = setup()

    // Mount -> effect -> cleanup -> mount, exactly as StrictMode replays in dev.
    conn.start()
    conn.stop()
    conn.start()

    expect(FakeEventSource.instances).toHaveLength(2)
    expect(FakeEventSource.instances[0].closed).toBe(true)

    // The live connection is the second socket; opening it must report
    // attempt 0 -- proof that stop()+start() didn't leak stale backoff state.
    lastInstance().emitOpen()
    expect(last(statuses)).toMatchObject({ state: 'live', attempt: 0 })
  })

  it('treats a synchronously-throwing EventSource factory as a failed attempt', () => {
    let calls = 0
    const statusesFor: ConnectionStatus[] = []
    const conn = createSseConnection({
      url: '/events',
      eventTypes: [],
      onEvent: () => {},
      onStatusChange: (s) => statusesFor.push({ ...s }),
      createEventSource: () => {
        calls += 1
        if (calls === 1) throw new Error('boom')
        return new FakeEventSource('/events') as unknown as SseHandle
      },
      backoffScheduleMs: [50],
      jitterFraction: 0,
    })

    conn.start()
    expect(last(statusesFor)?.state).toBe('disconnected')

    vi.advanceTimersByTime(50)
    expect(calls).toBe(2)
  })
})
