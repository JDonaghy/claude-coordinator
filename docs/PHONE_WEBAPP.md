# Phone Web Control Center

The Phone Web Control Center is a React PWA served by `coord web` (port 7434).
It gives you a full pipeline view — and one-tap gate actions — from any device on your Tailscale network, including a phone.

Part of the **Web: Phone Control Center (v1)** milestone (#700–#703).

---

## What it is

The phone webapp is a **Progressive Web App (PWA)** that:

- Shows every in-flight pipeline item as a tappable card (Home screen).
- Lets you tap into a Detail screen per item to see stage status, test verdict, review findings, and one-tap actions: Pass / Fail test, Start Review, Approve / Request Changes, Enqueue, Merge, Dispatch Fix, Cancel Stuck.
- Auto-refreshes every 4 s (React Query polling).
- Supports pull-to-refresh on mobile.
- Is installable as a home-screen app ("Add to Home Screen") on iOS and Android — `vite-plugin-pwa` generates the service worker and web manifest.

It is **not** a full coordinator — it does not have a chat interface, a board view, or proposal approval. It is deliberately narrow: pipeline gate actions that you need to do *away from your desk*, from your phone.

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

## Setup: build the React bundle

The compiled React bundle (`dist/`) is **not** committed to the repository (it is gitignored). You must build it once from source before `coord web` can serve the SPA. The legacy static `index.html` is served as a fallback if `dist/` does not exist — it shows a plain JSON board view, not the phone app.

```bash
# One-time build (requires Node ≥ 18 + npm)
cd coord/dashboard/webapp
npm install
npm run build          # produces coord/dashboard/webapp/dist/
```

After this, `coord web` will automatically serve the React SPA from `dist/`. You do not need to restart the server — the compiled bundle is served as static files.

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

**Run it as a service** (systemd, launchd, or tmux) so it stays up between sessions:

```ini
# ~/.config/systemd/user/coord-web.service
[Unit]
Description=coord web dashboard
After=network.target

[Service]
ExecStart=%h/.coord-venv/bin/coord web
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

```bash
systemctl --user enable coord-web
systemctl --user start coord-web
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

The Phone Web Control Center is **headless-only**. It does not embed a live terminal or any UI element that could display a running `claude -p` session to a remote user.

- Gate actions (`dispatch_review`, `enqueue`, `merge`, `test-verdict`, …) are stateless POST calls — they mutate the DB and/or call agent endpoints, but they do not attach to, display, or control a live worker subprocess.
- The `/api/diff/{id}` endpoint fetches a static unified diff via the GitHub API — not a live log stream.
- The `/events` SSE stream emits board-level notifications (`assignment_completed`, `board_updated`) — not raw worker output.
- The `/api/chat` endpoint opens a **new** headless `claude -p` session scoped to answering one question about the board state. It is a fresh headless invocation, not a relay of an existing session.

**What is explicitly out of scope for v1** (and deferred to prevent ToS exposure):
- Live terminal / log streaming to the phone (deferred beyond v1).
- An authoring / scoping view that lets you write and dispatch briefings from the phone (deferred beyond v1).

Both of these require careful design around the human-attended session requirement (ToS §3.7) and are tracked separately.

---

## Testing

The webapp ships with two test tiers:

1. **Vitest unit tests** (`coord/dashboard/webapp/src/components/__tests__/`) — component rendering and filter-logic contracts. Run with `npm test` inside `coord/dashboard/webapp/`.

2. **Python integration tests** (`tests/test_dashboard.py`) — the `build_app()` Starlette server tested via `TestClient`. These run as part of the normal `pytest` suite.

3. **Playwright E2E tests** — *forthcoming*. The acceptance bar for the webapp requires a browser-driven E2E suite that starts the dashboard server against a seeded board and asserts on the rendered DOM. See `CLAUDE.md` for the planned harness design.

---

## File map

| Path | What lives there |
|---|---|
| `coord/dashboard/server.py` | Starlette app: all API routes + SPA serving + SSE poller |
| `coord/dashboard/webapp/` | React / Vite / TypeScript PWA source |
| `coord/dashboard/webapp/src/api/client.ts` | Typed API client + all wire types |
| `coord/dashboard/webapp/src/App.tsx` | React Router root (two routes: `/` Home, `/detail/:id` Detail) |
| `coord/dashboard/webapp/src/components/Home.tsx` | Pipeline card list + filter tabs + pull-to-refresh |
| `coord/dashboard/webapp/src/components/Detail.tsx` | Per-item detail: test gate, review section, merge section, diff viewer |
| `coord/dashboard/webapp/src/components/PipelineCard.tsx` | Card component for Home screen |
| `coord/dashboard/webapp/vite.config.ts` | Vite + PWA plugin config |
| `coord/dashboard/webapp/dist/` | **Built output** (gitignored — run `npm run build` to produce) |
| `coord/pipeline.py` | `PipelineView` / `PipelineGate` / `compute_pipeline()` — pure-computation pipeline state |
| `tests/test_dashboard.py` | Python-level API integration tests |
