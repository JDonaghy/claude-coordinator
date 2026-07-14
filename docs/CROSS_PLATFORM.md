# Cross-Platform — porting coord to macOS & Windows

> **Status:** design draft / RFC (2026-07-13). Captures the target architecture for running
> claude-coordinator natively on macOS and Windows, and for the single-machine "just my laptop"
> topology. Not committed direction — a thinking artifact to react to, decomposable into a milestone
> when we pick it up.
>
> **Decision on record (2026-07-13):** for Windows attended (human-in-the-loop) sessions we aim at
> **native + a WebSocket-PTY bridge**, *not* WSL2 and *not* headless-only. That single choice drives
> most of the plan below (see [The decision and what it commits us to](#the-decision-and-what-it-commits-us-to)).
>
> **Relationship to [`PLATFORM_EVOLUTION.md`](PLATFORM_EVOLUTION.md):** that RFC splits the system into
> a **control/coordination plane** (cloud-hostable) and an **execution + interactive-I/O plane**
> (fleet-local), and states the interactive PTY is *never* in the coordination-API path — it stays a
> direct operator↔own-machine stream. **This doc does not change that.** It only changes *how* that
> direct stream is carried: today it is tmux/ssh (Unix-only); here it becomes a WebSocket-PTY bridge
> served by the operator's own agent/runner. The stream is still direct client↔runner, still off the
> coordination-API path, still board-driven and never TTY-scraped (ToS §3.7). WS-PTY is the
> cross-platform *form* of the execution-plane PTY stream, not a new plane.

## The reframe — the hard part is smaller and better-fenced than it looks

Two facts from the code change the difficulty estimate:

1. **The headless worker path is already tmux-free and mostly HTTP.** `coord/dispatch.py` never
   shells out to ssh — it POSTs to agent servers over HTTP (`:7433`). Agents spawn `claude -p` with a
   bare `subprocess.Popen(start_new_session=True)` (optionally `bash -c 'exec …'`-wrapped), **not**
   tmux. tmux hosts *only* human-attended interactive sessions. So the entire "dispatch a worker, get
   a PR" loop is already close to portable; its only POSIX pieces are localized in `coord/agent.py`
   (the process-group reaper + the bash-wrap).

2. **The seams already exist.** There is a `Provider` ABC (`coord/providers/base.py` →
   `claude.py` / `claude_pty.py` / `opencode.py`) and a partial `TmuxHost` seam
   (`coord/interactive.py:214`) that *already* builds argv either locally **or** over ssh. We are not
   architecting from scratch — we are swapping POSIX-only implementations behind interfaces that
   partly exist.

So the port is **not** "rewrite coord." It is: ship a clean single-node mode, widen two seams, and
demote ssh/tmux from *required* to *one Unix implementation among several*.

**Scope the effort honestly: macOS is nearly free** — tmux, ssh, the `pty` module, a POSIX shell, and
`os.killpg` all work (Homebrew). The mac port is essentially launchd + paths + claude-binary
resolution + CI. **~90% of the cost is Windows.** Everything below is really about Windows.

## The load-bearing idea: three transport tiers + one universal session substrate

Model deployment as **three transport tiers**, from simplest to most capable:

| Tier | Topology | Needs | Portability |
|---|---|---|---|
| **1. Local** | one machine = `localhost` | nothing (no ssh, no Tailscale) | fully portable; only PTY host is OS-split |
| **2. LAN / Tailscale HTTP** | many machines, agent HTTP API + WS-PTY bridge | HTTP reachability | portable once the session backend is abstracted |
| **3. ssh / tmux** | the current Unix-native path | ssh + tmux + POSIX remote shell | Unix-only; **optional power-user tier** |

And **one universal session substrate** underneath all three: a **WebSocket-PTY bridge** where the
*agent* holds the PTY and the *client* connects/disconnects a socket. "Detach / reattach" becomes
"close / reopen a socket" — identical on every OS, and board-driven by construction (the client never
owns session state, so there is nothing to TTY-scrape). This is the same bridge the **Phone Control
Center v2 (#1064)** is already building; the cross-platform port *reuses* it rather than inventing a
per-OS session mechanism.

Under this model **tmux demotes to a Unix-only optimization / power-user convenience**, not the
backbone. That dissolves "there is no tmux on Windows" structurally instead of per-platform.

### What we're giving up (and must rebuild)

WS-PTY's edge is portability and a unified client (Windows-native; phone / GUI / TUI / laptop all one
path; board-driven and ToS-clean by construction). But ssh/tmux is decades-hardened, and demoting it
is not free. Be explicit about what the substrate must *earn back*, or the port is a **regression vs.
today's tmux sessions on Unix**:

- **Session survival must decouple from the coordinator's lifetime — the one real architecture gap, not
  just maturity.** tmux is a *separate daemon*, so a session outlives the agent crashing, restarting,
  or self-updating via `os.execv` (this is *why* coord uses tmux today — survive TUI/attach crashes).
  A naive WS-PTY bridge has the **agent process holding the PTY** → kill the agent, kill the session.
  To match tmux the agent must (a) spawn sessions in their own process session so they outlive it,
  (b) **re-adopt** them on restart, and (c) keep a server-side **scrollback ring buffer** to replay on
  reconnect (a raw PTY has no history; tmux's scrollback is free). **This is the real work hidden
  inside "reuse the #1064 bridge as the universal substrate"** — a *phone* feature may not need any of
  it; the *universal substrate* does. It belongs in the #1064 design review, not discovered mid-build.
- **Terminal correctness for free.** resize/reflow, SGR, UTF-8 width, copy-mode, every escape
  sequence. With WS-PTY we own the emulator end-to-end (xterm.js in the browser, the ratatui embedded
  pane) *and* the reconnect/replay logic.
- **Native multi-client attach / sharing.** Two people `tmux attach` to one session (pairing /
  over-the-shoulder) with worked-out shared-resize semantics. WS-PTY can fan out, but we build the
  multiplexing and decide whose window size wins.
- **Out-of-band recovery when coord itself is broken.** `ssh host; tmux attach` needs only sshd +
  tmux — neither is coord — so it reaches a stuck session **even when the agent is wedged**. WS-PTY
  requires a *healthy agent*: if the daemon is the thing that failed, your only interactive path failed
  with it. **This is the strongest reason to keep ssh/tmux as the tier-3 Unix path even after WS-PTY is
  the default** — don't let the recovery tool be the same process that broke.
- **Encryption/auth off-tailnet.** Today's agent HTTP is *plain HTTP over Tailscale*
  (`coord/network.py`) — WS-PTY's security model is "trust the tailnet." ssh brings its own
  authenticated, key-based, encrypted layer and works to machines not on the tailnet; WS-PTY there
  would need TLS + auth we haven't built.
- **Scope reminder — not apples-to-apples.** WS-PTY replaces exactly *one* slice of ssh (interactive
  attach). ssh *also* does the remote git-worktree ops, log fetch, and rsync (§2), for free, today.
  Those move to agent HTTP endpoints under this plan — real code we'd be writing, not a like-for-like
  swap.

**Net:** two requirements fall out of this. (1) Keep ssh/tmux as the optional tier-3 Unix path,
principally for out-of-band recovery. (2) The WS-PTY substrate must **explicitly** ship
detached-survival + scrollback replay — treat it as an acceptance requirement of #1064, not a later
enhancement, or Unix users trade "survives a daemon restart" for "dies with the daemon."

## The two seams

Everything portable-vs-POSIX flows through two interfaces.

### Seam 1 — `SessionBackend` (generalize `TmuxHost`)
Today `coord/interactive.py:214` (`TmuxHost`) already abstracts "build a command to run locally or
over ssh." Widen it into a backend interface:

```
create(name, cmd) · attach(name) · detach(name) · kill(name) · list() · send_input(name, bytes) · capture(name)
```

Implementations:
- **`WsPtyBackend`** — the primary path on every OS (agent holds PTY, client speaks WS). Also what the
  quadraui GUI terminal widget and the phone webapp consume.
- **`TmuxBackend`** — the existing Unix path, kept as an optimization / power-user convenience.
- (**`ConPtySupervisorBackend`** — optional, only if we later want tmux-like detached sessions on
  Windows *without* a live agent; not needed for v1 because `WsPtyBackend` covers it.)

**ToS contract baked into the interface, not the implementation:** kill-by-name, detached-only, never
inject `/exit`, never TTY-scrape for state (§3.7). Any backend must honor these.

### Seam 2 — `Provider` (already exists)
`coord/providers/base.py` already abstracts "how to build/run the AI CLI" (`claude`, `claude_pty`,
`opencode`). Keep it. The only cross-platform work here is **binary resolution** (below).

## The obstacles, in priority order

### 1. Detachable interactive sessions without tmux — the one genuinely hard problem
tmux is load-bearing *only* for attended sessions — but that is the whole fleet-control-center /
attended-claude vision (#487, #1064). Solved by **Seam 1 + `WsPtyBackend`**: build the hard part once
(Phone v2), reuse it as the universal backend. Windows never needs a native tmux.

### 2. ssh — shrink it, don't port it
ssh currently does remote-tmux, remote git-worktree ops, remote-log fetch, rsync artifact-pull, and
cross-machine attach — all built with `shlex.quote` / `shlex.join`, which **assumes a POSIX remote
shell**. Porting that quoting to cmd/PowerShell is a tar pit. Don't.

- Every ssh use is a candidate **agent HTTP endpoint**: "run this git op in this worktree," "tail this
  log," "pull this artifact," "attach (WS-PTY)." Move the operation **server-side**, where it runs
  in-process on the remote's *own* OS — the POSIX-remote-shell assumption evaporates.
- The Windows *client* is not the blocker (OpenSSH client ships in-box on Windows now). The blockers
  are the POSIX-remote-shell assumption and tmux-on-the-far-end. Both go away by moving work behind the
  agent API.
- End state: ssh is an **optional Unix power-user transport** (tier 3), not a requirement.

### 3. Single-node "just my laptop" mode — the highest-leverage move
One machine = `localhost` **dissolves ssh + Tailscale + (optionally) tmux entirely.** Dispatch is
already a local subprocess; bind daemon/agent/web to `127.0.0.1`, no MagicDNS. Interactive sessions
use the loopback WS-PTY bridge (all platforms) or local tmux (mac/Linux).

Make it a **first-class topology** in `coordinator.yml` (`transport: local`, one implicit localhost
machine) — *not* a degenerate multi-machine config. This is the on-ramp and the most portable thing to
ship. **Ship it first.**

### 4. The concrete POSIX code: `agent.py` reaper/PTY + `interactive.py` relay
Where the mechanical work actually is:
- `coord/interactive.py:72–88` has **unconditional top-level `import fcntl/termios/tty`** — the module
  won't even *import* on Windows. Step zero is guarding those and splitting the PTY relay
  (`_launch_via_pty`, `:1120–1200`: `pty.fork` + `os.execvp` + `SIGWINCH`) behind Seam 1.
- `coord/agent.py`: `os.killpg` + `start_new_session` + `bash -c 'exec …'` (`:172`, `:248`, `:275–368`,
  `:3300–3349`) → Windows needs **Job Objects** for "kill the whole process tree" and **ConPTY**
  (`pywinpty`) for `pty.openpty`. These two are the concrete Windows engineering nuggets on the agent
  side; the rest is paths + binary resolution.

### 5. Service supervision — dodge it in v1
There is **no `systemctl` in the Python** — service management is confined to `deploy/*.service` +
`install-agent.sh`. mac → launchd plist; Windows → a Service (`pywin32`/`sc`), Task Scheduler, or a
tray app. But in single-node mode we can **skip all of it**: `coord up` runs the daemon
foreground / as a child. Offer OS-service install as a *later* convenience, not a v1 gate. (The
`os.execv` self-restart in `coord/agent_app.py` is portable but pointless without a supervisor —
single-node just restarts the child.)

### 6. Paths & the worktree symlink
`Path.home()/".coord"` (`coord/db.py:19`) is already cross-platform. The real snags:
- `~/.coord-venv/bin`, `~/.local/bin`, and `$HOME`-form strings *sent to remote shells* → adopt
  `platformdirs`; and (per #2) don't send shell strings — server-side ops.
- **Git worktree symlinks on Windows** (the quadraui `../../quadraui/quadraui` path-dep symlink) need
  Developer Mode / admin → use **directory junctions** or absolute-path deps.
- The `/home/john/.coord/…` → `-home-john--coord-…` claude-projects path mangling
  (`coord/interactive.py:1831`) assumes POSIX slashes *and* reverse-engineers claude Code's
  projects-dir layout, which differs per OS → needs an OS-aware variant.

### 7. claude binary resolution
Production uses the bare name `"claude"` on `PATH` (`coord/agent.py:36`) plus a `~/.coord-venv/bin`
prepend (Unix venv layout). Replace with `shutil.which("claude")` + a configured-path fallback, and
drop the venv-layout assumption. The #402 PATH-strip hygiene (strip the agent venv so a worker's bare
`pip` hits system Python) is Unix-specific and either needs a per-OS analog or is moot in single-node
dev where the user manages their own venv.

### 8. The TUI embedded PTY pane + terminal protocols — the untestable frontier
crossterm/ratatui port cheaply, so the TUI *chrome* is easy. The embedded live `claude` pane
(`tui/src/app/terminal.rs`, `fleet_terminals.rs`) is real PTY handling — POSIX pty vs ConPTY (the
`portable-pty` crate covers both). The genuinely hard bit is what we already know: `TuiDriver` can't
reach raw-mode / SGR-mouse / the embedded pane, so **each OS needs live smoke**, and Windows adds
Windows-Terminal-vs-legacy-conhost divergence (target Windows Terminal; treat conhost as unsupported).
Route these via `smoke_tests.capability_rules` with new `os:windows` / `os:macos` capabilities.

**The quadraui GUI backends help here.** A GUI can host a PTY widget (like the phone webapp's
xterm.js) talking to the agent over WS — sidestepping the terminal-multiplexer problem entirely.
**GUI + WS-PTY is arguably the cleanest Windows attended-session answer**, cleaner than a native
terminal.

### 9. CI is Linux-pytest-only
Add a GH Actions matrix (ubuntu/macos/windows) for the portable core; route the PTY / live-smoke tiers
to real machines via `capability_rules` (the mechanism already exists for GTK / browser). The oracle
acceptance suite runs on all three where the driver is portable (`tui-tuidriver` is; the embedded-pane
pty tier is not).

## Staged path

1. **Guard the POSIX imports** (`coord/interactive.py` top-level `fcntl/termios/tty`) so the package
   imports on Win/mac → `coord status/plan/board/config` (the read + planning surface) works
   everywhere on day one.
2. **Ship single-node local mode** (`transport: local`, localhost, no ssh/Tailscale). The on-ramp.
3. **Widen Seam 1** (`SessionBackend`) and land `WsPtyBackend` as the primary attended-session path
   (shared with Phone v2 / the GUI). tmux demotes to a Unix optimization.
4. **Agent on Windows**: ConPTY (`pywinpty`) + Job Objects in `coord/agent.py`; junctions + binary
   resolution ride along.
5. **Move ssh-only ops behind agent HTTP endpoints** → multi-machine over HTTP on every OS; ssh
   demoted to the optional Unix tier.
6. **OS service install last** (or never — foreground/tray suffices).

## The decision and what it commits us to

**Native + WS-PTY** (chosen 2026-07-13) has one clarifying consequence: **the Phone-v2 WebSocket-PTY
bridge (#1064) stops being a phone feature and becomes the universal session substrate.**

- **One backend everywhere.** `WsPtyBackend` is the *primary* attended-session path on Windows, mac,
  the single-node laptop, the phone, and the quadraui GUI terminal alike. tmux is a Unix-only
  convenience.
- **It pulls the agent server onto Windows as a hard requirement**, because the agent hosts the PTY the
  bridge serves. So `coord/agent.py`'s POSIX cluster is in-scope for Windows, not deferrable:
  `pty.openpty` → ConPTY (`pywinpty`); the `os.killpg` process-tree reaper → **Job Objects**.
- **Detach/reattach becomes trivial and identical on every OS** — socket disconnect/reconnect against a
  server-held PTY — and satisfies the ToS posture (board-driven, no TTY-scrape) by construction.

The two rejected options, for the record: **WSL2** (tmux/ssh/pty "just work" with near-zero code
change, but it's Linux-under-the-hood and worktrees/paths live in the WSL filesystem — not true "just
my laptop") and **headless-only on Windows** (smallest scope, but permanently defers the attended
vision the whole tool is built around).

## Issue map

Decomposed 2026-07-13 into **two milestones** — Phase 1 (mac + portable core, independent of #1064)
ships first; Phase 2 (Windows-native attended sessions) follows, gated on #1064. Children are planned,
not yet dispatched.

### Milestone #39 — Cross-Platform Core + macOS · epic #1160

```
CP-1 #1156 ─┬─► CP-2 #1157 ─┐
            └─► CP-3 #1158 ─┴─► CP-4 #1159
```

- **#1156 CP-1** — guard POSIX imports (`interactive.py:72-88`) + `platformdirs` state dirs → package
  imports + read/plan surface run on Win/mac. The foundation.
- **#1157 CP-2** — single-node `transport: local` topology (localhost, no ssh/Tailscale; `coord up`). `{after: #1156}`
- **#1158 CP-3** — macOS runtime parity (launchd + `shutil.which("claude")` binary resolution). `{after: #1156}`
- **#1159 CP-4** — CI matrix (ubuntu/macos/windows) + `os:*` `capability_rules` + live-smoke playbook. `{after: #1157, #1158}`

### Milestone #40 — Windows-native attended sessions · epic #1165

```
CP-5 #1161 ─┬─► CP-6 #1162   (+ cross-milestone #1064)
            └─► CP-7 #1163
CP-8 #1164   (independent root)
```

- **#1161 CP-5** — `SessionBackend` seam (generalize `TmuxHost`); ToS contract baked in. Refactor, no Unix behavior change.
- **#1164 CP-8** — ssh-ops → agent HTTP endpoints; ssh demoted to tier-3. Independent root.
- **#1162 CP-6** — WS-PTY as the universal `SessionBackend`; **verify detached-survival + scrollback** (the regression-guard). `{after: #1161}` + cross-milestone **#1064**.
- **#1163 CP-7** — Windows agent runtime: ConPTY (`pywinpty`) + Job Objects + worktree junctions. `{after: #1161}`

**Cross-milestone / cross-repo edges (prose, not work-order `after:`):** #1064 gates CP-6 (it builds
the survival-capable bridge; CP-6 consumes + Windows-integrates + verifies it); milestone #27
(coord-ui GTK / multi-backend) is the downstream consumer of CP-6's WS-PTY substrate. Phase 2 also
assumes Phase 1's CP-1 (#1156) has landed.
