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

As of **0.4.71** the compiled React bundle (`dist/`) is bundled into the PyPI wheel by the release workflow (`npm ci && npm run build` runs before `python -m build`). A plain `pip install code-coordinator` or `coord agent update` is all you need — no Node.js, no checkout, no `npm run build` on the dashboard host.

**In production (dellserver) this is the *fallback* path, not the primary one.** `coord-web.service` runs `coord web --dist ~/coord-web-dist`, which tracks merged `main` on a 1-minute timer — see "Going live automatically (#1543)" below. The bundled-wheel path above only matters if `~/coord-web-dist` is ever absent.

`pyproject.toml` also declares `websockets` directly (#1216) — without it uvicorn silently answers a real WS upgrade with a plain HTTP 200 instead of 101, so the `/ws/terminal` bridge fails only in a real browser, not in `curl`/mocked tests. A plain `pip install` already pulls it in; no separate step needed.

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

> **Repo split (#2004):** `coord/dashboard/webapp/**` is moving to its own
> `coord-web` repo (epic #2002). The mechanism below is the answer for how a
> built bundle reaches this daemon host both before and after that move —
> see [`docs/ADR_COORD_WEB_DIST.md`](ADR_COORD_WEB_DIST.md) for the decision
> and the rejected alternatives.

## Going live automatically (#1543): merged main, not release cadence

**On dellserver in production, a merged `coord/dashboard/webapp/**` change
goes live at `http://dellserver:7434` within about a minute, with no ssh and
no PyPI release** — and it does so *without upgrading `~/.coord-venv`*, the
same venv `coord-agent` and `coord-serve` `ExecStart` from on that host.

### Why this needed its own mechanism

`coord-web`, `coord-agent`, and `coord-serve` all `ExecStart` from
`~/.coord-venv` (see `deploy/*.service`). Before #1543, "ship a webapp
change" meant `pip install --upgrade code-coordinator` on that venv — which
also upgrades the board daemon and the agent runtime on that host, and
`coord agent update` is already known to kill running headless workers
(#1543's issue body). During a program that merges dozens of webapp PRs while
workers are running, a timer that did that automatically would be actively
dangerous. So the fix targets the coupling, not the cadence.

### The mechanism: `coord web --dist`

`coord web` takes a `--dist PATH` flag (env `$COORD_WEB_DIST`) that serves
the built webapp bundle from an arbitrary directory instead of the one bundled
inside the installed package (`coord/dashboard/webapp/dist`,
`coord/dashboard/server.py`'s `WEBAPP_DIST`). `deploy/coord-web.service`
points it at `~/coord-web-dist`, a symlink that
[`deploy/coord-web-dist-build.sh`](../deploy/coord-web-dist-build.sh) —
installed as `coord-web-dist-build.timer`, firing every minute — keeps
pointed at a fresh build of `coord/dashboard/webapp` from `origin/main`:

```
coord-web-dist-build.timer (every 1min)
  → fetch origin/main in ~/src/claude-coordinator (read-only: fetch + rev-parse only)
  → build that SHA in a DEDICATED worktree (~/.coord-web-checkout — never the
    operator's own checkout, never a `coord drive` worktree)
  → npm ci && npm run build
  → atomically repoint ~/coord-web-dist -> ~/.coord-web-releases/<sha>
```

Publishing is a single `rename(2)` of a symlink — Starlette's
`StaticFiles`/`FileResponse` re-resolve the directory on every request, so
**no restart of `coord-web` happens on a normal publish.** That means a
webapp deploy can never interrupt an attended `/ws/terminal` session (there
is no process restart to interrupt it with) and needs no "quiet window"
scheduling — it satisfies the "must not fire mid-session" constraint by
construction, not by timing. A restart (or first `coord web` start) is only
needed once, before the very first build exists, so that the static-serving
routes get registered — see `deploy/coord-web.service`'s header.

If `~/coord-web-dist` is ever absent (timer disabled, fresh install before
the first build) `--dist` falls back to the bundled
`coord/dashboard/webapp/dist` inside the venv — the same legacy fallback
that existed before #1543 (and further back, the single-file legacy
dashboard if that's absent too).

### Proof this leaves the daemon/agent versions untouched

```bash
# Before shipping a webapp change:
~/.coord-venv/bin/coord --version          # e.g. 0.4.105 (unchanged by the timer)
readlink -f ~/coord-web-dist               # ~/.coord-web-releases/<old-sha>

# Merge a coord/dashboard/webapp/** PR, wait <= 1 minute...

# After:
~/.coord-venv/bin/coord --version          # STILL 0.4.105 — the timer never
                                            # touches this venv
readlink -f ~/coord-web-dist               # ~/.coord-web-releases/<new-sha>
```

### Health-check before cutover (#1560)

A build succeeding (`npm run build` exits 0) and the resulting page actually
working are different claims. Before a release is ever symlinked live,
`coord-web-dist-build.sh` boots it as a **scratch** `coord web` instance —
bound to `127.0.0.1` only, on a throwaway port, `--fixture
tests/fixtures/board-pipeline-basic.json` (the #1538 deterministic seeded
board, so the probe needs no DB/fleet/network and can't race or corrupt the
real `~/.coord/coord.db` the already-running production `coord-web` /
`coord-serve` / `coord-agent` are using) — and probes it exactly like a
browser would:

- `GET /` must return **200** and contain the real SPA's `id="root"` mount
  point, not the legacy single-file fallback dashboard that `--dist` quietly
  serves instead for a missing/broken bundle (`coord/commands/lifecycle.py`'s
  `--dist` help documents that fallback).
- `GET /api/pipeline` must return **200** and parse as a non-empty JSON
  array.

Only a release that passes **both** gets `mv`'d into `$RELEASES_DIR` and
symlinked live. A release that fails is deleted — never published, never
left on disk to be mistaken for a rollback target — and the currently-live
release is untouched.

**Why fail-closed instead of auto-revert-after-publish:** the check above
runs *before* the symlink swap, so there is no window, however brief, where
a broken bundle is actually live — detecting breakage after cutover and
reverting would by definition mean it was live for some nonzero time first.
That said, the fixture-based check is necessarily incomplete: it cannot
catch a bug that only manifests as a client-side JS runtime error (still
serves 200 HTML with an `id="root"` div — a headless-browser check could
catch that class of bug too, but running one from a systemd timer every
minute was judged disproportionate for this program's scope). **That gap is
exactly what the manual rollback below is for**, and it is what the #1560
acceptance drill exercises.

### Rollback: one command, reachable without this issue in hand (#1560)

```bash
ssh <dellserver-tailnet-name> ~/.local/bin/coord-web-rollback.sh
```

[`deploy/coord-web-rollback.sh`](../deploy/coord-web-rollback.sh) repoints
`~/coord-web-dist` at the release directory that was live just before the
current one — still on disk under `~/.coord-web-releases` (`KEEP_RELEASES=3`
by default). No restart needed, same atomic `rename(2)`-over-symlink publish
as a forward deploy. It refuses to run (exit 1, clear stderr message,
`~/coord-web-dist` untouched) if fewer than two releases exist on disk, or if
the candidate target has no `index.html` — it will not publish a release it
cannot at least confirm looks like a built bundle.

Because a release only ever lands in `~/.coord-web-releases` **after**
passing the health check above, "second-newest directory on disk" and "last
known GOOD release" are the same thing — a failed build never gets there to
be rolled back into by mistake.

Equivalent by hand, if the script itself is ever unavailable:

```bash
ln -sfn "$(ls -dt ~/.coord-web-releases/*/ | sed -n 2p)" ~/coord-web-dist
```

(Skips the rollback-sentinel protection described below — prefer the script
form when it's available.)

**`coord-serve` (7435) and `coord-agent` (7433) are untouched by both the
deploy and the revert** — neither script does anything but build/symlink
inside `~/coord-web-dist` / `~/.coord-web-releases`; see
`tests/test_deploy_coord_web_dist.py` and
`tests/test_deploy_coord_web_rollback.py` for the regression guards.

#### The rollback is not durable against the 1-minute build timer on its own

`coord-web-dist-build.timer` fires every ~1 minute (see "Going live
automatically" above). Fixing or reverting a bad commit on `main`
realistically takes longer than that. So immediately after a rollback,
`origin/main`'s tip is **still the bad SHA** — the build timer's own
"up to date at `$NEW_SHA`" short-circuit compares against the *currently
live* SHA (now the good one you just rolled back to), which no longer
matches, so it does **not** fire. Left alone, the very next tick would
rebuild that identical bad commit, run it through the identical health
check it passed the first time (for the identical reason — a client-side-
only JS runtime error is exactly the bug class that check cannot catch),
and republish it live — **silently undoing the rollback about a minute
after you performed it, with no other warning.**

`coord-web-rollback.sh` closes this gap two ways:

1. **It tells you, in its own output**, that the timer is about to try
   again, and gives you the pause command if you need more than a minute:
   ```
   WARNING: coord-web-dist-build.timer fires again within about a minute. It will refuse to
   auto-republish <bad-sha> (the SHA you just rolled back FROM) thanks to the sentinel
   just written to ~/.coord-web-releases/.rollback-blocked-sha — but that protection is scoped
   to that exact SHA: if origin/main has ALREADY moved past it with more bad commits behind it,
   the timer will build and publish those instead, and this script cannot protect you from that.
   Fix or revert the bad commit on main as soon as you can. If you'd rather the timer not run
   at all until you have, pause it:
     systemctl --user stop coord-web-dist-build.timer
     systemctl --user start coord-web-dist-build.timer   # resume when ready
   ```
2. **It writes a sentinel** (`~/.coord-web-releases/.rollback-blocked-sha`)
   naming the exact SHA you rolled back *from*. `coord-web-dist-build.sh`
   checks that sentinel before it ever touches the build worktree or npm:
   if `origin/main`'s tip still matches it, the build **refuses to run**
   (exit 1, nothing rebuilt, nothing republished, `~/coord-web-dist`
   untouched) until either `main` has moved past that SHA — in which case
   the sentinel is cleared automatically on the next tick — or an operator
   deliberately removes the sentinel file by hand (e.g. because the
   rollback turns out to have been a false alarm).

This is a **fail-closed guard against exactly one thing**: re-publishing
the specific SHA just rolled back from. It does not stop the timer from
building and publishing a *different* bad commit that lands on `main`
afterward — pausing the timer (`systemctl --user stop
coord-web-dist-build.timer`) is still the answer if you need `main` to
simply stop being deployed at all while you work.

#### #1560 acceptance drill: deploying a broken bundle and recovering

Run **2026-08-05**, against scratch `$RELEASES_DIR`/`$LIVE_LINK` directories
fed by a throwaway local `git` remote (not the real dellserver deploy — this
repro touches no production state) driving the real
`deploy/coord-web-dist-build.sh` and `deploy/coord-web-rollback.sh` end to
end, in two parts because the two scripts guard against two different
failure classes.

**Part A — the health check catches an obviously broken build.** Starting
from a good build already live at SHA `17b949e` (real `origin/main` tip),
`coord/dashboard/webapp/index.html`'s SPA root div was corrupted
(`id="root"` → `id="not-root"`) in a new commit `8f48ced` and
`coord-web-dist-build.sh` run against it:

```
[10:34:17] building 8f48ced8651fc0d66c8283e0dd11dbc37c0f0882
           ... npm ci && npm run build (succeeds — vite doesn't know the div id is wrong) ...
[10:34:28] health-checking 8f48ced8651fc0d66c8283e0dd11dbc37c0f0882 on 127.0.0.1:18500 before cutover
[10:34:29] ERROR: http://127.0.0.1:18500/ returned 200 but not the SPA (no id="root" marker) —
           likely the legacy fallback dashboard, meaning .../releases/8f48ced8... has no usable dist/
[10:34:29] ERROR: health check failed for 8f48ced8651fc0d66c8283e0dd11dbc37c0f0882 — refusing to
           publish, live dashboard unchanged (still 17b949eb76cd80e9f6204e7285dcdbbe05b63c22)
```

Exit 1, 12.3s wall clock (almost all of it `npm ci && npm run build`).
`readlink -f $LIVE_LINK` still resolved to `17b949e...` and `$RELEASES_DIR`
contained **only** `17b949e...` — the broken release directory was deleted,
not left on disk. **The broken bundle was never live, so there was nothing
to recover from.** This is the fail-closed path described above; it's the
expected, and cheapest possible, outcome for the class of bug the health
check can see (build "succeeds" but the served HTML is wrong).

**Part B — a bad bundle that gets past static checks anyway, then a timed
manual rollback.** To exercise the actual revert command — the path that
matters for a bug class the fixture-based check *can't* see, e.g. a
client-side JS runtime error that still serves 200 HTML with a valid
`id="root"` div — a second release directory (`deadbeef…`, a copy of the
good build with a `throw new Error(...)` appended to its main JS bundle) was
placed directly under `$RELEASES_DIR` and `$LIVE_LINK` force-pointed at it
**by hand, bypassing the build script entirely** — simulating "this got live
anyway, however it happened":

```
$ readlink -f $LIVE_LINK
/…/releases/deadbeefdeadbeefdeadbeefdeadbeefdeadbeef        # the "bad" one, live

$ time RELEASES_DIR=… LIVE_LINK=… ~/.local/bin/coord-web-rollback.sh
[10:34:42] rolling back: .../releases/deadbeef... -> .../releases/17b949e...
[10:34:42] done: $LIVE_LINK -> .../releases/17b949e... (no coord-web restart needed)
[10:34:42] coord-serve (7435) and coord-agent (7433) were not touched by this script.

real    0m0.007s
```

**Recovery time: ~7 milliseconds — well under the issue's one-minute bar.**
`readlink -f $LIVE_LINK` confirmed it repointed to the good release; `grep
'id="'` against its `index.html` confirmed `id="root"` (the real SPA, not
the corrupted one) was live again. `coord-serve`/`coord-agent` were never
started during this drill at all — confirming (along with
`tests/test_deploy_coord_web_rollback.py`'s
`test_reports_no_restart_and_other_lanes_untouched`) that neither script
reaches for those services by construction, not merely because they
happened not to be running.

> **Updated 2026-08-05 (review fix):** the Part B transcript above predates
> the rollback-sentinel guard added below Part C. Run today, the same
> rollback also writes `.rollback-blocked-sha` under `$RELEASES_DIR` and
> prints a `WARNING:` block about the build timer's next tick — see Part C,
> which exercises exactly that interaction end to end (the gap this
> transcript itself didn't originally cover).

**Part C — the interaction with the live build timer that Part B didn't
cover, and the fix for it.** Part A and Part B each ran their script in
isolation; neither exercised what happens when `coord-web-dist-build.timer`
actually fires again shortly after a manual rollback while `origin/main` is
still sitting on the bad commit — which is the realistic case, since fixing
a bad commit on `main` takes a lot longer than the timer's ~1 minute
cadence. This drill exercises that interaction directly, driving the real
`deploy/coord-web-rollback.sh` and `deploy/coord-web-dist-build.sh` end to
end against a scratch, fully-local git repo standing in for `origin/main`
(no network, no real npm build needed — the guard below sits before either
script ever gets there).

Starting state: a bug that ships a client-side-only JS runtime error (still
renders a valid `id="root"` SPA shell, so the fixture-based health check
cannot catch it — the exact class of bug Part B's manual rollback exists
for) is live at `8c42f95c...`, having gotten past the health check the same
way Part B's `deadbeef...` release did. `origin/main`'s tip is still that
same commit — the fix hasn't landed yet.

```
$ readlink -f $LIVE_LINK
/…/releases/8c42f95c48e5d2652d52e27f9f22df08bd92a4f8        # the bad one, live

$ time RELEASES_DIR=… LIVE_LINK=… bash deploy/coord-web-rollback.sh
[11:32:55] rolling back: .../releases/8c42f95c... -> .../releases/f4c176b2...
[11:32:55] done: $LIVE_LINK -> .../releases/f4c176b2... (no coord-web restart needed)
[11:32:55] coord-serve (7435) and coord-agent (7433) were not touched by this script.
[11:32:55] WARNING: coord-web-dist-build.timer fires again within about a minute. It will refuse to
[11:32:55] auto-republish 8c42f95c48e5d2652d52e27f9f22df08bd92a4f8 (the SHA you just rolled back FROM) thanks to the sentinel
[11:32:55] just written to .../releases/.rollback-blocked-sha — but that protection is scoped to that exact SHA: if
[11:32:55] origin/main has ALREADY moved past it with more bad commits behind it, the timer will
[11:32:55] build and publish those instead, and this script cannot protect you from that.
[11:32:55] Fix or revert the bad commit on main as soon as you can. If you'd rather the timer not run
[11:32:55] at all until you have, pause it:
[11:32:55]   systemctl --user stop coord-web-dist-build.timer
[11:32:55]   systemctl --user start coord-web-dist-build.timer   # resume when ready

real    0m0.017s
```

Recovery itself: unchanged, ~17ms. Then, simulating the timer's very next
tick with `origin/main` still unfixed (the realistic case):

```
$ time BASE_CHECKOUT=… BRANCH=main WEBAPP_CHECKOUT=… RELEASES_DIR=… LIVE_LINK=… \
    bash deploy/coord-web-dist-build.sh
[11:33:00] REFUSING to build/publish 8c42f95c48e5d2652d52e27f9f22df08bd92a4f8: this is the exact SHA an operator rolled back FROM
[11:33:00] (sentinel: .../releases/.rollback-blocked-sha, written by coord-web-rollback.sh). Publishing it now would
[11:33:00] silently undo that rollback about a minute after they performed it, with no other warning.
[11:33:00] Fix or revert the bad commit on origin/main, or pause this timer entirely while you work on it:
[11:33:00]   systemctl --user stop coord-web-dist-build.timer
[11:33:00]   systemctl --user start coord-web-dist-build.timer   # resume once main is fixed
[11:33:00] If you are certain 8c42f95c48e5d2652d52e27f9f22df08bd92a4f8 is actually fine and the rollback was a false alarm, clear the
[11:33:00] sentinel to allow it again:
[11:33:00]   rm .../releases/.rollback-blocked-sha

$ echo $?
1
$ readlink -f $LIVE_LINK
/…/releases/f4c176b2...        # STILL the good release -- the rollback held
$ ls $WEBAPP_CHECKOUT 2>&1
No such file or directory       # refused before ever touching the build worktree
```

**The rollback held.** Exit 1, no rebuild, no republish, `$LIVE_LINK`
unchanged — this is the bug the review flagged, fixed: before this guard
existed, this second command would have rebuilt `8c42f95c...` from scratch,
passed it through the identical health check it passed the first time, and
silently republished it, undoing the rollback above about a minute after it
ran.

Finally, once the actual fix lands on `main` (a new commit, `7d9aff55...`),
the very next tick clears the sentinel on its own and proceeds normally:

```
$ time BASE_CHECKOUT=… BRANCH=main WEBAPP_CHECKOUT=… RELEASES_DIR=… LIVE_LINK=… \
    bash deploy/coord-web-dist-build.sh
[11:33:06] origin/main (7d9aff55...) has moved past the previously-blocked SHA 8c42f95c... — clearing sentinel .../releases/.rollback-blocked-sha
[11:33:06] creating dedicated worktree at $WEBAPP_CHECKOUT
[11:33:06] building 7d9aff55...
... (npm ci && npm run build proceeds normally from here) ...

$ ls $RELEASES_DIR/.rollback-blocked-sha
No such file or directory       # sentinel cleared -- future ticks are unblocked
```

No operator action was needed to unblock deploys once `main` was actually
fixed — only a bad commit sitting unfixed at the tip stays blocked.
`tests/test_deploy_coord_web_dist.py`'s
`test_build_script_refuses_to_republish_a_just_rolled_back_from_sha` and
`test_build_script_clears_sentinel_once_main_moves_past_the_blocked_sha`
are the automated regression guards for both halves of this drill.

**Takeaway:** the three mechanisms cover three different failure surfaces —
the health check stops most broken deploys from ever going live at all
(zero recovery time needed, because there was nothing to recover from); the
one-command rollback is the fallback for the narrower class that gets
through anyway, and a millisecond-scale symlink swap is fast enough that
"ssh in from a phone and run one command" is a realistic answer to "the
dogfood loop just went down"; and the rollback-sentinel guard is what makes
that rollback actually *stick* against a 1-minute build timer that would
otherwise silently undo it about a minute later, for precisely the bug
class (client-side-only JS runtime errors) the manual rollback exists to
catch in the first place.

### Install

```bash
mkdir -p ~/.config/systemd/user
cp deploy/coord-web-dist-build.sh deploy/coord-web-rollback.sh ~/.local/bin/
chmod +x ~/.local/bin/coord-web-dist-build.sh ~/.local/bin/coord-web-rollback.sh
~/.local/bin/coord-web-dist-build.sh                # first build, BEFORE starting coord-web
cp deploy/coord-web.service deploy/coord-web-dist-build.service \
    deploy/coord-web-dist-build.timer ~/.config/systemd/user/
loginctl enable-linger "$USER"
systemctl --user daemon-reload
systemctl --user enable --now coord-web coord-web-dist-build.timer
```

See `deploy/coord-web-dist-build.sh`'s header comment for the full mechanism
and edge cases (locking against overlapping runs, a failed health check
leaving the previous release live, pruning old releases), and
`deploy/coord-web-rollback.sh`'s header comment for the rollback script
installed alongside it above.

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
| `deploy/coord-web-dist-build.sh` | **(#1543, health check #1560)** builds merged `main`'s webapp into `~/coord-web-dist`, decoupled from `~/.coord-venv`; health-checks a release on a scratch port before ever publishing it — see "Going live automatically" and "Health-check before cutover" above |
| `deploy/coord-web-dist-build.service` / `.timer` | **(#1543)** systemd units that run the script above every minute |
| `deploy/coord-web-rollback.sh` | **(#1560)** one-command last-known-good rollback — repoints `~/coord-web-dist` at the previous release; see "Rollback" above |
| `coord/pipeline.py` | `PipelineView` / `PipelineGate` / `compute_pipeline()` — pure-computation pipeline state |
| `coord/dashboard/fixture.py` | **(#1538)** `--fixture` seeded-board mode — same routes/handlers, no DB/fleet/network; what the #1560 health check boots the scratch instance with |
| `tests/fixtures/board-pipeline-basic.json` | **(#1538)** the reference seeded board the #1560 health check probes against |
| `tests/test_dashboard.py` | Python-level API integration tests |
| `tests/test_dashboard_terminal.py` | **(v2, #1065)** PTY↔WS bridge integration tests |
| `tests/test_deploy_coord_web_dist.py` | **(#1543, #1560)** content pins for the deploy units + build script, including the health-check gate |
| `tests/test_deploy_coord_web_rollback.py` | **(#1560)** behavioural tests for `coord-web-rollback.sh`'s symlink-swap logic (real subprocess execution — no systemd/network/npm needed) |
