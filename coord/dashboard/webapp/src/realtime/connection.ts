/**
 * Framework-agnostic SSE client: owns one `EventSource`-shaped connection,
 * reconnects with exponential backoff + jitter, and reports an honest
 * connection state so the UI never shows stale data as if it were live
 * (#1549).
 *
 * Deliberately decoupled from React and from `@tanstack/react-query` — this
 * module only knows how to keep a stream open and tell you what it's typed
 * events say and what state the stream is in. `RealtimeProvider.tsx` is the
 * thin layer that wires those events to query-key invalidation.
 *
 * ## The StrictMode double-mount trap
 *
 * React 18 `<StrictMode>` mounts every component twice in dev: mount → effect
 * → cleanup → mount → effect. A naive `useEffect(() => { const es = new
 * EventSource(url); return () => es.close() }, [])` is fine on its own, but
 * once reconnect/backoff state lives outside the effect (as it must, to
 * survive a single real disconnect) two failure modes appear:
 *
 * 1. A "closed" flag set by the first cleanup is never cleared by the second
 *    mount's `start()`, so the second, real connection silently never
 *    retries after its first drop.
 * 2. The first `EventSource`'s `onerror`/`onopen` fires *after* the second
 *    `start()` has already begun (both are async), stomping the real
 *    connection's state with a stale one.
 *
 * `start()` fixes (1) by unconditionally resetting all intent/backoff state
 * — "intent flags reset in `connect()`", not read from whatever the last
 * `stop()` left behind. `stop()`/`start()` fix (2) by minting a new `token`
 * on every call and closing over it in each socket's callbacks; a callback
 * whose captured token no longer matches the connection's current token
 * belongs to a superseded socket and is dropped ("onclose guarded"). This is
 * the same shape whether the second `start()` comes from StrictMode's replay
 * or from a real remount, so there is exactly one code path to test.
 */

// ── Public types ─────────────────────────────────────────────────────────────

/**
 * `disconnected` and `reconnecting` are deliberately distinct: the instant a
 * live stream drops we know only that it's stale (`disconnected`); once a
 * retry attempt is actually in flight the UI can say so (`reconnecting`).
 * `connecting` is the pre-first-connect state, shown only until the very
 * first `open` (or first failure, which moves to `disconnected` too).
 */
export type ConnectionState = 'connecting' | 'live' | 'disconnected' | 'reconnecting'

export interface ConnectionStatus {
  state: ConnectionState
  /** epoch ms the stream was last confirmed open, or null before the first successful connect. */
  lastLiveAt: number | null
  /** consecutive failed attempts since the last live connection (0 while live or before the first attempt has failed). */
  attempt: number
}

/** The slice of the browser `EventSource` API this module depends on. */
export interface SseHandle {
  close(): void
  addEventListener(type: string, listener: (ev: { data: string }) => void): void
  onopen: ((ev: unknown) => void) | null
  onerror: ((ev: unknown) => void) | null
}

export type EventSourceFactory = (url: string) => SseHandle

export interface SseConnectionOptions {
  url: string
  /** SSE `event:` names to subscribe to; anything else on the stream is ignored. */
  eventTypes: readonly string[]
  /** Called for each received event, already JSON-parsed (raw string if parsing fails). */
  onEvent: (type: string, data: unknown) => void
  /** Called whenever the connection state machine transitions. */
  onStatusChange: (status: ConnectionStatus) => void
  /** Defaults to the browser's native `EventSource`; overridable for tests. */
  createEventSource?: EventSourceFactory
  /** Backoff delays in ms, one per consecutive failed attempt; the last entry repeats. */
  backoffScheduleMs?: readonly number[]
  /** Fractional +/- jitter applied to each backoff delay. Default 0.2; tests pass 0 for exact timing. */
  jitterFraction?: number
  now?: () => number
  setTimeoutFn?: typeof setTimeout
  clearTimeoutFn?: typeof clearTimeout
}

export interface SseConnection {
  /** (Re)start the connection, resetting all backoff/intent state. Safe to call while already running. */
  start(): void
  /** Stop for good: closes the socket, cancels any pending retry, no further callbacks fire. */
  stop(): void
}

// ── Defaults ─────────────────────────────────────────────────────────────────

/** 1s, 2s, 4s, 8s, 15s, then capped at 30s — gentle enough for a single tab, fast enough to feel alive. */
export const DEFAULT_BACKOFF_MS: readonly number[] = [1_000, 2_000, 4_000, 8_000, 15_000, 30_000]

/** +/- 20% jitter so a daemon restart doesn't get every tab retrying in lockstep. */
const DEFAULT_JITTER_FRACTION = 0.2

function withJitter(baseMs: number, jitterFraction: number): number {
  const jitter = baseMs * jitterFraction * (Math.random() * 2 - 1)
  return Math.max(0, Math.round(baseMs + jitter))
}

function defaultEventSourceFactory(url: string): SseHandle {
  return new EventSource(url) as unknown as SseHandle
}

// ── Implementation ───────────────────────────────────────────────────────────

export function createSseConnection(options: SseConnectionOptions): SseConnection {
  const {
    url,
    eventTypes,
    onEvent,
    onStatusChange,
    createEventSource = defaultEventSourceFactory,
    backoffScheduleMs = DEFAULT_BACKOFF_MS,
    jitterFraction = DEFAULT_JITTER_FRACTION,
    now = () => Date.now(),
    setTimeoutFn = setTimeout,
    clearTimeoutFn = clearTimeout,
  } = options

  // Bumped by every start()/stop(); a socket's callbacks close over the token
  // in effect when they were wired up and no-op once it's stale. This is what
  // makes a superseded (StrictMode-cleaned-up, or genuinely replaced) socket
  // inert instead of racing the current one.
  let token = 0
  let socket: SseHandle | null = null
  let timer: ReturnType<typeof setTimeout> | null = null
  let attempt = 0
  let lastLiveAt: number | null = null

  function emitStatus(state: ConnectionState): void {
    onStatusChange({ state, lastLiveAt, attempt })
  }

  function clearPendingRetry(): void {
    if (timer !== null) {
      clearTimeoutFn(timer)
      timer = null
    }
  }

  function closeSocket(): void {
    if (socket !== null) {
      socket.close()
      socket = null
    }
  }

  function scheduleRetry(myToken: number): void {
    // `attempt` counts consecutive failures and was just incremented by
    // handleFailure(), so attempt 1 (the first retry) indexes schedule[0].
    const scheduleIndex = Math.min(attempt - 1, backoffScheduleMs.length - 1)
    const delay = withJitter(backoffScheduleMs[scheduleIndex], jitterFraction)
    timer = setTimeoutFn(() => {
      timer = null
      if (myToken !== token) return
      openSocket(myToken)
    }, delay)
  }

  function handleFailure(myToken: number): void {
    if (myToken !== token) return
    closeSocket()
    attempt += 1
    emitStatus('disconnected')
    scheduleRetry(myToken)
  }

  function openSocket(myToken: number): void {
    if (myToken !== token) return
    emitStatus(attempt > 0 ? 'reconnecting' : 'connecting')

    let handle: SseHandle
    try {
      handle = createEventSource(url)
    } catch {
      handleFailure(myToken)
      return
    }
    socket = handle

    handle.onopen = () => {
      if (myToken !== token) return
      attempt = 0
      lastLiveAt = now()
      emitStatus('live')
    }
    handle.onerror = () => {
      handleFailure(myToken)
    }
    for (const type of eventTypes) {
      handle.addEventListener(type, (ev) => {
        if (myToken !== token) return
        onEvent(type, parsePayload(ev.data))
      })
    }
  }

  return {
    start(): void {
      token += 1
      const myToken = token
      attempt = 0
      clearPendingRetry()
      closeSocket()
      openSocket(myToken)
    },
    stop(): void {
      token += 1
      clearPendingRetry()
      closeSocket()
    },
  }
}

function parsePayload(raw: string): unknown {
  if (!raw) return null
  try {
    return JSON.parse(raw)
  } catch {
    return raw
  }
}
