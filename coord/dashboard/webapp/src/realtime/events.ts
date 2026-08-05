/**
 * The SSE wire vocabulary + which react-query keys each event type affects.
 *
 * Hand-written mirror of `coord/events.py`'s `KNOWN_EVENT_TYPES` — that
 * module isn't a `scripts/codegen.py` (#750) target (it's a set of string
 * constants, not a dataclass), so keep this list in sync by hand when a new
 * event type is added server-side.
 *
 * Query-key granularity here matches what the webapp already has: there is
 * no per-assignment `['pipeline', id]` key (Detail.tsx reads the same
 * `['pipeline']` list Home.tsx does), so "the precise query keys an event
 * affects" bottoms out at `['pipeline']` and `['sessions']` — the two lists
 * every event type below can change the shape of.
 */

export const ASSIGNMENT_STARTED = 'assignment_started'
export const ASSIGNMENT_COMPLETED = 'assignment_completed'
export const ASSIGNMENT_FAILED = 'assignment_failed'
export const MACHINE_CONNECTED = 'machine_connected'
export const MACHINE_DISCONNECTED = 'machine_disconnected'
export const BOARD_UPDATED = 'board_updated'

/** Every event type the client subscribes to on `/events`. */
export const EVENT_TYPES = [
  ASSIGNMENT_STARTED,
  ASSIGNMENT_COMPLETED,
  ASSIGNMENT_FAILED,
  MACHINE_CONNECTED,
  MACHINE_DISCONNECTED,
  BOARD_UPDATED,
] as const

/** A react-query `queryKey`. */
export type QueryKey = readonly unknown[]

const PIPELINE: QueryKey = ['pipeline']
const SESSIONS: QueryKey = ['sessions']

/**
 * Event type -> the query keys it invalidates.
 *
 * - `assignment_*` changes an item's stage/verdict (-> pipeline) and can
 *   start/stop a live `coord-*` tmux session (-> sessions).
 * - `machine_*` only affects session reachability/attach state (-> sessions).
 * - `board_updated` is the background poller's coarse "something may have
 *   changed" heartbeat (every 30s server-side, `coord/dashboard/server.py`
 *   `_POLL_INTERVAL`) — treated as a catch-all for both lists so a change
 *   this event vocabulary doesn't yet name specifically still surfaces
 *   within one heartbeat instead of never.
 */
export const EVENT_QUERY_KEYS: Readonly<Record<string, readonly QueryKey[]>> = {
  [ASSIGNMENT_STARTED]: [PIPELINE, SESSIONS],
  [ASSIGNMENT_COMPLETED]: [PIPELINE, SESSIONS],
  [ASSIGNMENT_FAILED]: [PIPELINE, SESSIONS],
  [MACHINE_CONNECTED]: [SESSIONS],
  [MACHINE_DISCONNECTED]: [SESSIONS],
  [BOARD_UPDATED]: [PIPELINE, SESSIONS],
}

/**
 * Invalidated on every `disconnected`/`reconnecting` -> `live` recovery,
 * regardless of which events fired.
 *
 * The server's event history ring buffer (`EventSource` in `coord/events.py`,
 * `DEFAULT_HISTORY_SIZE = 256`) lets a reconnecting client backfill via
 * `Last-Event-ID` — but a long enough drop can overflow it, and a daemon
 * restart resets it (and the id sequence) entirely. Backfill is best-effort;
 * an explicit resync on recovery is what makes "live" mean "correct", not
 * just "the socket is open again".
 */
export const RESYNC_QUERY_KEYS: readonly QueryKey[] = [PIPELINE, SESSIONS]
