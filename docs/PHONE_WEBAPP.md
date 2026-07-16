# Phone Web Control Center

The Phone Web Control Center is a React PWA served by `coord web` (port 7434).
It gives you a full pipeline view — one-tap gate actions — and, as of **v2**, a
pop-open terminal to **finish a live session you started at your desk**, from
any device on your Tailscale network, including a phone.

Part of the **Web: Phone Control Center** milestone (#16). v1 (#700–#703) shipped
the headless pipeline view; v2 (epic #1064, #1065–#1072) added the human-attended
terminal takeover described below.

---

## What it is

The phone webapp is a **Progressive Web App (PWA)** that:

- Shows every in-flight pipeline item as a tappable card (Home screen), with
  **working / in-flight sessions surfaced first** (v2, #1067) — the things
  you'd actually want to resume from your phone.
- Lets you tap into a Detail screen per item to see stage status, test verdict,
  review findings, and one-tap actions: Pass / Fail test, Start Review, Approve
  / Request Changes, Enqueue, Merge, Dispatch Fix, Cancel Stuck.
- From an in-progress item with a live interactive session, **pops open a real
  terminal** (top half of the screen) attached to that session, plus a mobile
  key bar (bottom half) built for driving `claude`'s TUI from a phone
  soft-keyboard — see "Terminal takeover (v2)" below.
- Auto-refreshes every 4 s (React Query polling).
- Supports pull-to-refresh on mobile.
- Is installable as a home-screen app ("Add to Home Screen") on iOS and Android — `vite-plugin-pwa` generates the service worker and web manifest.

It is still **not** a full coordinator — no chat-driven planning, no board view,
no proposal approval. It's deliberately narrow: the gate actions and the one
"finish what I started at my desk" move you need *away from your desk*.

---

## Terminal takeover (v2, epic #1064)

v1 could show you an in-flight item but never let you see or type into the
live worker behind it. v2 adds exactly one new capability on top: **take over
a session you left running at your desk, from your phone.**

- **Attachable-sessions API** (`GET /api/sessions`, #1066) lists the live
  interactive `coord-*` tmux sessions across the fleet — machine, repo, issue,
  tmux name, whether a bridge is already attached — sourced from the same
  fleet session roster `coord sessions` itself reads (milestone #32), not a
  parallel discovery path.
- **PTY↔WebSocket bridge** (`GET /ws/terminal/{session_id}`, #1065,
  `coord/dashboard/terminal.py`) `tmux attach`es to that session — over `ssh
  <host>` when the session's actual host differs from the dashboard host —
  and relays bytes both ways. On disconnect it **detaches, never kills**: the
  session keeps running at your desk exactly as if you'd stepped away from
  the keyboard.
- **xterm.js terminal pane** (top half, #1068) renders the live byte stream.
- **Mobile key bar** (bottom half, #1070) gives you Esc, arrows, Enter/submit,
  Ctrl-C, Tab, and `/` — the keys a phone soft-keyboard makes painful, that
  `claude`'s TUI needs constantly.
- **Reconnect resilience** (#1071): mobile networks drop WebSockets
  constantly (backgrounding, wifi↔cellular handoff). The frontend reopens a
  fresh WebSocket to the same `session_id` with exponential backoff (1s → 10s
  cap) without recreating the `xterm.js` instance — `tmux attach`'s own
  redraw-on-reattach repaints the existing pane. A `4404` close code means the
  session is genuinely gone (not a transient drop); the UI shows a terminal
  "ended" state and stops retrying.
- **Playwright E2E** (#1072, `coord/dashboard/webapp/e2e/terminal.spec.ts`)
  drives the real open → type → detach flow headless against a seeded fake
  session + fake PTY bridge — the milestone's browser acceptance bar. Routed
  to a `browser`-capable machine via `smoke_tests.capability_rules`.

See "ToS posture" below for why this is allowed under §3.7 despite the v1
doc's original headless-only stance.

---

## The "one backend, two thin clients" model

```
coord serve (port 7435)   ←— optional board-daemon on the always-on host
         │
         ▼
~/.coord/coord.db  +  coordinator.yml
         │
   ┌─────┴─────┐
   │           │
coord-tui    coord web (port 7434)   ←— THIS DOCUMENT
(Rust TUI)   (Python + React PWA)
```

`coord web` and `coord-tui` are **peer clients** of the same state. Both read `~/.coord/coord.db` directly (or via `coord serve` as a daemon). Neither is a layer on top of the other.

The phone webapp calls the same `GET /api/pipeline` + `POST /api/pipeline/action` endpoints that the TUI would call if it had an HTTP mode. There is no phone-specific backend — the dashboard server (`coord/dashboard/server.py`) is general-purpose.

**When `coord serve` is running on the always-on host (e.g. dellserver):** every machine sees the same board because they are all thin clients of the daemon's SQLite. `coord web` can run on the same always-on host and is then accessible from any Tailscale peer without port forwarding.

**Without `coord serve`:** `coord web` reads the local `~/.coord/coord.db`. Run it on whichever machine owns the DB (usually the machine you run `coord notify` on).

---

## Setup: no manual build needed (PyPI ≥ 0.4.71)

As of **0.4.71** the compiled React bundle (`dist/`) is bundled into the PyPI wheel by the release workflow (`npm ci && npm run build` runs before `python -m build`). A plain `pip install claude-coordinator` or `coord agent update` is all you need — no Node.js, no checkout, no `npm run build` on the dashboard host.

The legacy static `index.html` is served as a fallback if `dist/` does not exist (pre-0.4.71 wheel, or an editable install without a local build) — it shows a plain JSON board view, not the PWA.

### Building from source (contributors / dev installs)

If you are running from an editable checkout, you must build the bundle yourself:

```bash
# One-time build (requires Node ≥ 18 + npm)
cd coord/dashboard/webapp
npm install
npm run build          # produces coord/dashboard/webapp/dist/
```

**Rebuild** whenever you pull upstream changes to `coord/dashboard/webapp/src/`:

```bash
cd coord/dashboard/webapp && npm run build
```

---

## Running: start `coord web` on the daemon host

`coord web` binds to `0.0.0.0:7434` by default, so it is accessible from any machine on your Tailscale network without extra configuration.

```bash
# On the always-on host (e.g. dellserver):
coord web                        # http://0.0.0.0:7434
coord web --port 7434            # explicit port (default)
coord web --host 127.0.0.1       # localhost-only (if you want a reverse proxy)
```

**Run it as a service** so it stays up between sessions. A ready-made systemd
*user* unit ships at [`deploy/coord-web.service`](../deploy/coord-web.service):

```bash
cp deploy/coord-web.service ~/.config/systemd/user/
loginctl enable-linger "$USER"
systemctl --user daemon-reload && systemctl --user enable --now coord-web
```

---

## Accessing from a phone over Tailscale

1. **Find the Tailscale IP / hostname of the daemon host:**

   ```bash
   tailscale status       # lists machines; find the always-on host's name or IP
   ```

   Or use the Tailscale MagicDNS hostname: `http://dellserver:7434` (replace with your machine's MagicDNS name from the Tailscale admin console).

2. **Open on your phone:**

   ```
   http://dellserver.your-tailnet.ts.net:7434
   ```

   Or with the numeric Tailscale IP: `http://100.x.y.z:7434`

3. **Install as a home-screen app (recommended):**
   - **iOS/Safari:** tap the Share button → *Add to Home Screen*
   - **Android/Chrome:** tap the menu → *Add to Home Screen* (or the install prompt if it appears)

   Once installed as a PWA the app works offline using cached pipeline data, though gate actions require network access to the daemon host.

---

## API surface

`coord web` exposes a JSON REST API at `/api/`. All writes go through `POST /api/pipeline/action`. The phone webapp is the only current caller, but the endpoints are general-purpose and curl-friendly.

| Method + Path | Purpose |
|---|---|
| `GET  /api/board` | Raw board state: `{round_number, active, completed}` (last 20) |
| `GET  /api/machines` | Machine list with live ping + agent assignment status |
| `GET  /api/proposals` | Pending `coord plan` proposals |
| `POST /api/approve` | Approve proposals (body: `{ids, briefings?}`) |
| `POST /api/reject` | Reject proposals (body: `{ids}`) |
| `GET  /api/pipeline` | Pipeline views for all `type="work"` assignments — see below |
| `POST /api/pipeline/action` | Advance a pipeline gate — see below |
| `GET  /api/diff/{id}` | Unified diff for an assignment (PR diff or compare API) |
| `POST /api/chat` | Chat with the coordinator assistant (streaming SSE) |
| `GET  /events` | SSE: `board_updated`, `assignment_completed`, `assignment_failed`, … |
| `GET  /api/sessions` | **(v2, #1066)** Live interactive `coord-*` tmux sessions the phone may attach to |
| `GET  /ws/terminal/{session_id}` | **(v2, #1065)** WebSocket PTY bridge — human-attended only, see "Terminal takeover" above |

### `GET /api/pipeline` — PipelineView fields

Returns a JSON array of `PipelineView` objects, one per `type="work"` assignment.

| Field | Type | Description |
|---|---|---|
| `assignment_id` | `string` | Unique assignment ID |
| `issue_number` | `int` | GitHub issue number |
| `issue_title` | `string` | Issue title |
| `repo_name` | `string` | Repo name from `coordinator.yml` |
| `machine_name` | `string` | Machine that ran (or is running) the work |
| `stages` | `PipelineStage[]` | Ordered stage list (`coding → test → review → merge`) |
| `current_stage` | `string` | Fine-grained stage key for colour-coding (see below) |
| `available_gates` | `PipelineGate[]` | Gate actions currently open for human input |
| `progress_pct` | `int` | 0–100 progress estimate |
| `review_findings_pending` | `bool` | `true` when review completed but findings not yet posted to GitHub |
| `review_verdict` | `"approve"\|"request-changes"\|null` | Parsed verdict from the reviewer's structured output block (added #698) |
| `review_findings_body` | `string\|null` | Full text of the review findings as cached in the DB (added #698) |
| `test_verdict` | `"passed"\|"failed"\|"skipped"\|null` | Human test-gate verdict recorded via `coord test` or `POST /api/pipeline/action` (added #698) |

`current_stage` values: `"coding"`, `"failed"`, `"done"`, `"review_running"`, `"review_done"`, `"review_failed"`, `"smoke_running"`, `"smoke_passed"`, `"smoke_failed"`, `"merge_ready"`, `"merging"`, `"merged"`.

### `POST /api/pipeline/action` — supported actions

Body: `{"assignment_id": "...", "action": "...", ...extra}`

| Action | Extra fields | Description |
|---|---|---|
| `dispatch_review` | — | Dispatch an adversarial review to another machine |
| `dispatch_smoke` | — | Dispatch a smoke-test assignment |
| `enqueue` | — | Add to the merge queue |
| `merge` | `force?: bool` | Merge a queued PR (must be in `pending` state) |
| `post_findings` | — | Post orphaned review findings to GitHub |
| `unstick` | — | Cancel a stuck assignment and mark it failed |
| `test-verdict` | `verdict: "pass"\|"fail"\|"skip"`, `reason?: string` | Record a human test-gate verdict |
| `record-review-verdict` | `verdict: "approve"\|"request-changes"`, `body: string` | Record a parsed review verdict + findings text |
| `dispatch_fix` | `parent_type?: "work"\|"review"` | Dispatch a fix worker for a test failure or review request-changes |
| `retry` | — | *(501 — not yet implemented)* |

---

## ToS posture

ToS §3.7 forbids **unattended TTY-scraping / automation** of the `claude` CLI —
a program reading or driving a `claude` session with no human in the loop. It
does **not** forbid a human remotely operating their own session; that's the
case #437 preserved, and it's what v2's terminal takeover is: **a remote
keyboard + screen for a session you started yourself, attached only when you
open it and typed into only by you.**

v1 was deliberately headless because the terminal-bridge design work hadn't
been done yet, not because a human-attended terminal is disallowed. v2 (epic
#1064) builds exactly that, with the bridge holding the line at "relay a live
human," never "read or drive autonomously":

- **You open it, you type.** The WS bridge (`GET /ws/terminal/{session_id}`)
  only exists while a browser tab holds it open; nothing dispatches, injects
  keystrokes, or reads output on a timer or without your tab connected.
- **Detach, never kill.** Every code path that ends the bridge connection
  (clean close, network drop, tab closed) detaches the underlying `tmux
  attach` — it never sends a kill signal to the session. Your desk session
  keeps running exactly as you left it whether or not the phone is attached.
- **No parallel automation path.** The attachable-sessions list (`GET
  /api/sessions`) and the bridge both read the same fleet session roster
  `coord sessions` uses — there is no separate "drive this session for me"
  API; the only way bytes reach the PTY is through the WebSocket a human
  browser tab is holding open.
- Everything from the v1 stance still holds for the **non-terminal** parts of
  the app: gate actions (`dispatch_review`, `enqueue`, `merge`,
  `test-verdict`, …) are stateless POSTs that never attach to a live worker;
  `/api/diff/{id}` is a static diff fetch; `/events` emits board-level
  notifications, not raw worker output; `/api/chat` opens a **new** headless
  `claude -p` session scoped to one question, never a relay of an existing one.

**Still out of scope** (and still deferred for the same ToS reason — no human
necessarily attached at dispatch time): an authoring/scoping view that writes
and *dispatches* briefings from the phone. Dispatch is a coordinator-approval
action, not a live-session relay, so it's a separate design problem, tracked
separately from the terminal takeover above.

**Reviewers of any future change to `coord/dashboard/terminal.py` or the
terminal frontend: reject any path that reads or drives a session without an
open, human-held WebSocket connection.**

---

## Testing

The webapp ships with two test tiers:

1. **Vitest unit tests** (`coord/dashboard/webapp/src/components/__tests__/`) — component rendering and filter-logic contracts, including `Terminal.test.tsx`, `MobileKeyBar.test.tsx`, `SessionCard.test.tsx`, `Home.test.tsx`. Run with `npm test` inside `coord/dashboard/webapp/`.

2. **Python integration tests** (`tests/test_dashboard.py`, `tests/test_dashboard_terminal.py`) — the `build_app()` Starlette server and the PTY↔WS bridge (`SessionAttacher` seam) tested via `TestClient`. These run as part of the normal `pytest` suite.

3. **Playwright E2E tests** (`coord/dashboard/webapp/e2e/terminal.spec.ts`, #1072) — shipped. Drives a real headless browser through open-terminal → type → detach against the dashboard server seeded with a fake attachable session + fake PTY bridge (no real ssh/tmux/claude). Run with `npm run test:e2e` (or `test:e2e:ui`) inside `coord/dashboard/webapp/`. Routed to a `browser`-capable machine at Test-stage time via `smoke_tests.capability_rules` in `coordinator.yml`.

---

## File map

| Path | What lives there |
|---|---|
| `coord/dashboard/server.py` | Starlette app: all API routes + SPA serving + SSE poller + the terminal WS route |
| `coord/dashboard/terminal.py` | **(v2, #1065)** `SessionAttacher` seam — real `tmux attach-session` (local or `ssh <host> -tt` remote) behind a PTY; `resolve_session_target()` maps `session_id` → host/tmux name off the board |
| `coord/dashboard/webapp/` | React / Vite / TypeScript PWA source |
| `coord/dashboard/webapp/src/api/client.ts` | Typed API client + all wire types |
| `coord/dashboard/webapp/src/App.tsx` | React Router root (two routes: `/` Home, `/detail/:id` Detail) |
| `coord/dashboard/webapp/src/components/Home.tsx` | Pipeline card list + filter tabs + **in-progress/live sessions surfaced first (v2, #1067)** + pull-to-refresh |
| `coord/dashboard/webapp/src/components/Detail.tsx` | Per-item detail: test gate, review section, merge section, diff viewer |
| `coord/dashboard/webapp/src/components/PipelineCard.tsx` | Card component for Home screen |
| `coord/dashboard/webapp/src/components/SessionCard.tsx` | **(v2, #1067)** Live-session card — tap to open the terminal takeover view |
| `coord/dashboard/webapp/src/components/Terminal.tsx` | **(v2, #1068/#1071)** xterm.js pane + WS client + reconnect/backoff + "ended" state |
| `coord/dashboard/webapp/src/components/MobileKeyBar.tsx` | **(v2, #1070)** Esc / arrows / Enter / Ctrl-C / Tab / `/` key bar for the terminal pane |
| `coord/dashboard/webapp/e2e/terminal.spec.ts` | **(v2, #1072)** Playwright E2E for the takeover flow |
| `coord/dashboard/webapp/vite.config.ts` | Vite + PWA plugin config |
| `coord/dashboard/webapp/dist/` | **Built output** (gitignored locally; bundled into the PyPI wheel by the release workflow as of 0.4.71, #758; run `npm run build` locally for editable installs) |
| `coord/pipeline.py` | `PipelineView` / `PipelineGate` / `compute_pipeline()` — pure-computation pipeline state |
| `tests/test_dashboard.py` | Python-level API integration tests |
| `tests/test_dashboard_terminal.py` | **(v2, #1065)** PTY↔WS bridge integration tests |
