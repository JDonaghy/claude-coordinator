/**
 * The SSE wire vocabulary + which react-query keys each event type affects.
 *
 * NOT just a mirror of `coord/events.py`'s `KNOWN_EVENT_TYPES` — the
 * dashboard server publishes three more event types of its own
 * (`coord/dashboard/server.py`: `ASSIGNMENT_CANCELLED`, `ASSIGNMENT_ADVISORY`,
 * `ASSIGNMENT_NEEDS_ATTENTION`, all pre-existing, not `coord.events`
 * constants). `openSocket()` in `connection.ts` only registers a listener
 * for the `event:` names in `EVENT_TYPES`, so any real server event type
 * missing from this list is simply never delivered to a handler — it is
 * *not* caught by `board_updated`'s catch-all invalidation, because that
 * catch-all only fires for events the client actually subscribed to. This
 * list must therefore cover every event type the dashboard server can
 * publish, not just `coord.events.KNOWN_EVENT_TYPES`. Keep it in sync by
 * hand — neither source is a `scripts/codegen.py` (#750) target (both are
 * sets of string constants, not dataclasses).
 *
 * Query-key granularity here matches what the webapp already has: there is
 * no per-assignment `['pipeline', id]` key (Detail.tsx reads the same
 * `['pipeline']` list Home.tsx does), so "the precise query keys an event
 * affects" bottoms out at `['pipeline']` and `['sessions']` — the two lists
 * every event type below can change the shape of.
 *
 * TODO: once a per-assignment key (`['pipeline', id]`) or `['diff', id]`
 * gets folded into SSE invalidation, revisit every entry below — today they
 * all just invalidate the coarse lists.
 */

export const ASSIGNMENT_STARTED = 'assignment_started'
export const ASSIGNMENT_COMPLETED = 'assignment_completed'
export const ASSIGNMENT_FAILED = 'assignment_failed'
// The three below are dashboard-server-only (coord/dashboard/server.py),
// not coord.events.KNOWN_EVENT_TYPES -- see the module doc comment above.
export const ASSIGNMENT_CANCELLED = 'assignment_cancelled'
export const ASSIGNMENT_ADVISORY = 'assignment_advisory'
export const ASSIGNMENT_NEEDS_ATTENTION = 'assignment_needs_attention'
export const MACHINE_CONNECTED = 'machine_connected'
export const MACHINE_DISCONNECTED = 'machine_disconnected'
export const BOARD_UPDATED = 'board_updated'

/** Every event type the client subscribes to on `/events`. */
export const EVENT_TYPES = [
  ASSIGNMENT_STARTED,
  ASSIGNMENT_COMPLETED,
  ASSIGNMENT_FAILED,
  ASSIGNMENT_CANCELLED,
  ASSIGNMENT_ADVISORY,
  ASSIGNMENT_NEEDS_ATTENTION,
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
 * - `assignment_*` (including the dashboard-server-only `_cancelled` /
 *   `_advisory` / `_needs_attention` variants) changes an item's
 *   stage/verdict/needs-attention flag (-> pipeline) and can start/stop a
 *   live `coord-*` tmux session (-> sessions).
 * - `machine_*` only affects session reachability/attach state (-> sessions).
 * - `board_updated` is the background poller's coarse "something may have
 *   changed" heartbeat (every 30s server-side, `coord/dashboard/server.py`
 *   `_POLL_INTERVAL`) — treated as a catch-all for both lists so a change
 *   this event vocabulary doesn't yet name specifically still surfaces
 *   within one heartbeat instead of never. It is a catch-all for *unnamed*
 *   changes only, not a substitute for subscribing to a named event type —
 *   a type missing from `EVENT_TYPES` above never reaches this map at all
 *   (see that constant's doc comment).
 */
export const EVENT_QUERY_KEYS: Readonly<Record<string, readonly QueryKey[]>> = {
  [ASSIGNMENT_STARTED]: [PIPELINE, SESSIONS],
  [ASSIGNMENT_COMPLETED]: [PIPELINE, SESSIONS],
  [ASSIGNMENT_FAILED]: [PIPELINE, SESSIONS],
  [ASSIGNMENT_CANCELLED]: [PIPELINE, SESSIONS],
  [ASSIGNMENT_ADVISORY]: [PIPELINE, SESSIONS],
  [ASSIGNMENT_NEEDS_ATTENTION]: [PIPELINE],
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
