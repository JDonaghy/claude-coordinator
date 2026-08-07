# Store Service — a stable API contract over a pluggable backend

**Milestone:** `Store Service` (#60) · **Tracking epic:** #1949 · **Status:** planning, not dispatched

The machine-readable ordering lives in #1949's `## Work order` — `coord milestone order
claude-coordinator 1949` is authoritative for what is ready and what is blocked. The ready
frontier is deliberately just **#1849** and **#1942**; everything else is gated behind them.

This is the plan of record for turning `coord serve` into what it is already trying to
be: **one storage service with a contract that does not change when the storage engine
does.** Read it before starting any issue in this milestone.

The organising constraint is that **none of this may disturb the running fleet.** Every
phase is additive; the old surface keeps working until telemetry proves nothing uses it.

---

## 1. The reframe: the service already exists

It is worth being precise about what is missing, because it is smaller and more specific
than "build a DB microservice."

`coord serve` **is** the storage microservice today. Thin clients carry no local DB;
`coord.board_service.resolve()` routes reads and writes to it; `coordinator.remote.yml`
is a cache of what it serves; the TUI, the webapp and the Python CLI all read through it.
That architecture is built and in production.

What is missing is **contract discipline**. Three specific defects, measured against the
tree on 2026-08-07:

| defect | measurement |
|---|---|
| The wire schema *is* the SQLite DDL | `serve_app.py:1482` builds `GET /board`'s OpenAPI schema by `PRAGMA`-introspecting an in-memory SQLite DB (`openapi.py:178`) |
| The API is RPC-shaped, not resource-shaped | 55 routes; ~50 verb-per-endpoint, **3** resource-shaped |
| The store seam holds 6% of the SQL | **226** `execute` calls across 23 files — `state.py` **128 (57%)**, `dao.py` **13 (6%)** |

Sizes for scale: `serve_app.py` 6,581 lines, `state.py` 5,201, `dao.py` 483,
`models.py` 825 (9 dataclasses, used internally — never as the wire schema).

---

## 2. Why "swap the DAO" is not the job

`CoordStore` / `coord/dao.py` is the **read** waist for the board projection. It is not
where writes live: #590 landed in `coord/state.py` + `coord/board_service.py`, and
`dao.py`'s three write methods are dead stubs that raise `NotImplementedError` (#1823).

So implementing a Postgres adapter "behind the storage-agnostic DAO" yields a Postgres
**read** adapter while 128 write paths still speak SQLite dialect. That is the actual
difficulty of this program, and it is why #827 is correctly gated on milestone 19.

---

## 3. What is already built (assets, not work)

This program is mostly *applying* machinery that exists:

- **`openapi.dataclass_schema()`** (`coord/openapi.py:120`) — the explicit-DTO path
  `POST /assign` already uses. `/board`'s seven tables simply never adopted it.
- **`scripts/codegen.py`** (#750) — generates the webapp's TS types from the schema, with
  `--check` enforced in CI (`webapp-types` job, `tests/test_generated_types_fixture.py`).
  Declared DTOs get client regeneration nearly free.
- **`_DROP_COLUMNS` / `_JSON_COLUMNS`** (`coord/dao.py`) — column-to-wire *policy* already
  sits on the storage-neutral side of the seam. Only the introspection *mechanism* is
  SQLite-bound.
- **Golden `/board` fixture + round-trip parse test** (#748) — a contract test harness
  already exists to extend.
- **`SCHEMA_VERSION`** (`coord/dao.py:35`, currently `1`) — served on `/healthz` and in
  the board payload. A version *signal* exists; a version *negotiation* does not.

---

## 4. Strategy: expand → migrate → contract

Every phase adds alongside what exists. Nothing is removed until usage telemetry says
zero. This is not a stylistic preference — it is forced by two facts about this fleet:

**Agents run pinned releases.** A merged change is not a live change (see
[`docs/OPERATING_GOTCHAS.md`](OPERATING_GOTCHAS.md)). Server and client update on
different lanes, days apart.

**Endpoint and caller must never change in one commit.** Deploy server-first, always;
the alternative is the 405 trap where a new client meets an old daemon.

### Why a server-side feature flag is the wrong mechanism

A boolean on the daemon (`use_dto_schema: true`) flips the contract for **every client at
once** — the Rust TUI, the React webapp, the Python CLI, and every pinned agent — which is
precisely the disturbance this program is supposed to avoid.

The right mechanism is **client-driven negotiation**: a request header
(`X-Coord-Schema: 2`, or `Accept: application/vnd.coord.v2+json`) whose **absence means
today's shape**. Old clients are then unaffected *by construction* rather than by
discipline, each client migrates on its own deploy lane, and rollback is a client-side
one-liner rather than a daemon restart.

A server-side flag is still appropriate for one thing: **choosing the storage backend**
(Phase D), because that is genuinely a deployment property and not a per-client contract.

### Retirement is evidence-driven

The contract phase (removing the old surface) starts only when deprecation telemetry
shows zero calls from any client version over a defined window. "We think everyone
upgraded" is how the 405 trap happens.

---

## 5. The phases

| phase | issues | what | disturbance |
|---|---|---|---|
| **A — Contract** | #1849, #1942, #1939, #1941 | Declare DTOs; sever the wire from the DDL; gate the clients; build the store contract suite | none (shape unchanged) |
| **B — REST** | #1943 → #1944 → #1945 → #1946 → #1947 | Negotiate; add resource routes; measure; migrate per client; retire on evidence | none until retirement |
| **C — Store seam** | #1948 | Get `state.py`'s 128 SQL calls behind `CoordStore` | none (refactor) |
| **D — Second backend** | #827 → #828 → #829 | Postgres proves the seam is real | opt-in per deployment |

### Phase A — Contract

The cheapest phase and the one that pays off even if nothing else ships, because today a
column rename in `coord/db.py` is a silent breaking wire change to three clients.

- **#1849** — define `/board`'s seven projections as explicit dataclasses, not
  `PRAGMA table_info`. Already filed, `tier:large`, and the prerequisite for everything
  below.
- **#1939** — the first real exercise of the boundary: decide what belongs on the wire
  independent of what is in the table (2.22 MB of issue bodies that no list view renders).
- **Rust client types generated and CI-gated**, as the TS types already are. Today only
  the webapp has a drift gate; the TUI's structs are hand-written.
- **A store contract test suite** — one suite any `CoordStore` implementation must pass.
  Phase D is unverifiable without it.

### Phase B — REST

`PATCH /issue/{repo}/{n}` replacing ten verb endpoints, `PATCH /assignment/{id}` replacing
four field-setters. Mechanical, wide, and low-risk *if* sequenced as expand/migrate/contract.

Order matters: negotiation first, then routes, then telemetry, then per-client migration,
then retirement. Telemetry before migration, so retirement has evidence rather than hope.

### Phase C — Store seam

The hard part, and the reason this program is 20–30 issues rather than 10. It is a
refactor of the two largest modules in the tree (11.8k lines combined) that the entire
fleet runs on.

It decomposes only *after* #1823's inventory lands — writing the slice list before the
inventory is guesswork. Expect the slices to follow domain boundaries (assignments,
issues, merge queue, drive queue) rather than file boundaries.

### Phase D — Second backend

#827 → #828 → #829, already filed and already correctly ordered. Their value here is as
**proof**: a second backend is the only way to know the seam is real rather than nominal.
Postgres is the chosen prover; SQLite remains supported.

---

## 6. What this program does not claim

It does not claim the fleet needs Postgres. SQLite on the daemon host is serving a
three-machine fleet adequately, and #1825's framing — *"Postgres locally, Azure only if it
pays"* — is the right posture. Phase D is a proof of the seam and an option on
multi-writer, not a performance fix for a problem we have measured.

It does not claim the current API is bad engineering. RPC endpoints accreted because each
one solved a real dispatch problem quickly; the codebase has been honest about it (`_DROP_COLUMNS`
is described in its own comments as a patch over a known leak). The cost only becomes
material once there are three client languages and a second backend — which is now.

It also does not promise that Phase C is safe to rush. 128 SQL calls in a 5,201-line
module that every command path touches is the highest-risk refactor in the repo, and it
has no acceptance oracle until Phase A's contract test suite exists.

---

## 7. References

- **#1849** — sever `/board`'s wire schema from the SQLite DDL (Phase A prerequisite)
- **#1939** — `/board` ships 2.22 MB of issue bodies; the DTO boundary's first real decision
- **#1823** — dead write stubs + SQLite dialect inventory (Phase C prerequisite)
- **#827 / #828 / #829** — Postgres adapter → migration tool → cutover
- **#1825** — state durability & the relocatable daemon
- **#282** — multi-user / team mode, the eventual consumer of a real store seam
- **#750 / #748 / #757** — codegen, golden fixture, OpenAPI spec (the assets §3 lists)
- [`docs/PLATFORM_EVOLUTION.md`](PLATFORM_EVOLUTION.md) — the cloud/portal direction
- [`docs/OPERATING_GOTCHAS.md`](OPERATING_GOTCHAS.md) — deploy lanes; why server-first is mandatory
