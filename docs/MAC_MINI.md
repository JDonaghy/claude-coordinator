# Mac mini — hardware sizing + provisioning runbook

> **Status:** decision + runbook (2026-07-25). Sizing analysis for adding a Mac mini (M4) to the
> fleet as the macOS build/test/attended-session machine. The *port* itself is scoped in
> [`CROSS_PLATFORM.md`](CROSS_PLATFORM.md) (milestone #39 / epic #1160) — this doc is only about the
> **hardware**: what to buy, why not to rent, and how to provision it once it lands.

## The decision

**Buy an M4 Mac mini with 512GB of storage. Do not rent.**

- **16GB RAM is enough** for this workload at `concurrency: 1` (2 at a stretch), with
  `CARGO_BUILD_JOBS` capped. 24GB is the safe upgrade if the budget stretches — it is the *only*
  irreversible decision on the box.
- **256GB storage is not enough.** This is the binding constraint, not RAM (evidence below).
- **Renting breaks even in ~4–6 months**, and the mac is a permanent fleet member, not a
  port-sprint rental.

## Why 16GB is enough

The mac is a fleet agent that runs `claude -p` workers doing `cargo build` + `cargo test` on
quadraui, vimcode, and coord-tui, plus human-attended interactive sessions for macOS GUI polish.

Peak footprint for **one** worker mid-build:

| Component | Peak |
|---|---|
| macOS + logged-in GUI session (WindowServer, Spotlight, …) | ~4–5 GB |
| one `claude -p` worker (node; the model is remote, nothing local) | ~0.5–1.5 GB |
| `cargo build -j10` on quadraui/vimcode-sized crate graphs | ~4–8 GB |
| **total** | **~10–14 GB** |

That fits in 16GB with little headroom. **Two concurrent Rust-building workers will swap** — and
swap on a soldered SSD is a wear problem over years of builds, not just a speed problem. Unified
memory is also shared with the GPU, which matters during live GTK4 smoke work.

For reference, the base M4 is 10 cores (4P/6E) against elitebook's 8 — *more* cores and much faster
P-cores, but half the RAM (16 vs 31GB). Compile wall-clock should improve; concurrency must not.

**Mitigations (apply both):**

- `concurrency: 1` for the machine in `coordinator.yml` (2 only after watching real memory pressure).
- `CARGO_BUILD_JOBS=6` so a parallel-codegen spike can't blow past the ceiling.

## Why 256GB is not enough

Measured on elitebook, 2026-07-25 — **one checkout each, no worktrees**:

```
40G   vimcode/target
11G   quadraui/target
5.9G  claude-coordinator/tui/target
────
57G   of Rust build artifacts
```

Against a 256GB base mini, before any of that: macOS (~20GB), Xcode CLT + Homebrew GTK4 stack
(~5GB), the repos themselves, node/claude, and a **per-worktree `target/` dir** for every entry in
`~/.coord/worktrees/`. That configuration is uncomfortable in month one and hostile by month three.

Storage and RAM are both soldered on Apple silicon — neither is fixable later.

**If a 256GB box is already in hand**, the workable mitigations are:

- Put `~/src` and `~/.coord/worktrees` on a Thunderbolt NVMe enclosure (~3GB/s — fine for cargo).
- Set a shared `CARGO_TARGET_DIR`. Cargo locks the directory so concurrent builds serialize, which
  is what `concurrency: 1` wants anyway, and it collapses the per-worktree `target/` multiplier.
- Sweep stale target dirs on a cron.

## Buy vs. rent

**Rough figures as of 2026-07 (estimates — verify current rates before committing):** a dedicated
Apple-silicon mini rents for roughly $100–170/mo; AWS EC2 Mac is much worse because of its 24-hour
minimum allocation (~$450/mo). Three months of rental ≈ the purchase price of the machine, which
then still has resale value. Break-even is ~4–6 months.

Two reasons the purchase wins regardless of the exact rate:

1. **This is not a 3-month job.** Once quadraui / vimcode / coord-tui ship macOS builds, every
   future change to those crates needs a mac to test on — permanently. It is a standing CI +
   capability-routing target (`os:macos`), not a port sprint that ends.
2. **The work is GUI work, and GUI work over VNC is worse.** vimcode is GTK4 (`gtk4-rs` 0.7 +
   pangocairo) on quartz via Homebrew; quadraui needs native polish. Visual/UX iteration here is
   human-attended manual smoke, and driving that over a datacenter VNC session degrades exactly the
   thing the machine is being bought for. The automated `GtkDriver` harness rasterises offscreen and
   would run fine remotely — eyeballing UI would not.

## Provisioning runbook

Order matters loosely; the coord agent goes last.

1. **macOS setup**
   - **Enable auto-login and Screen Sharing.** macOS GUI apps need a live WindowServer session — a
     plain `ssh` with no console session cannot launch them, so GTK4 live smoke fails without this.
   - Disable sleep (`System Settings → Energy`; `sudo pmset -a sleep 0 disablesleep 1`) — it's a
     server now.
2. **Xcode Command Line Tools only** — `xcode-select --install` (~1.5GB). Rust does not need full
   Xcode (~15GB+); install it only if notarization or Instruments becomes necessary.
3. **Homebrew**, then the GUI stack: `brew install gtk4 pango cairo gdk-pixbuf graphene` (~2–3GB).
   These are vimcode's `gtk4`/`pangocairo` deps — see its `Cargo.toml`.
4. **Rust** via rustup. Set the build-job cap globally in `~/.cargo/config.toml`:
   ```toml
   [build]
   jobs = 6
   ```
5. **Node + Claude Code**, and confirm the binary resolves for non-interactive shells. On the Linux
   fleet `claude` lives at `~/.local/bin/claude` and is zsh-only, which has bitten us over ssh/tmux
   — use absolute paths and verify `ssh <host> 'which claude'` before trusting it. macOS runtime
   parity (`shutil.which("claude")` resolution + launchd) is issue **#1158 / CP-3**.

   **This is not just about `claude` — it applies to every tool the agent shells out to** (#1671).
   What matters is the PATH of the agent's *service* process, which is far narrower than your login
   shell's. On Linux the fix is `Environment=PATH=` in the systemd unit; **the launchd equivalent is
   an `EnvironmentVariables` dict in the plist, so nothing from the Linux fleet's units ports
   across.** Whatever plist this box ends up with must reach `~/.cargo/bin` (rustup, step 4 above)
   and Homebrew's prefix (`/opt/homebrew/bin` on Apple silicon — not on any default PATH systemd or
   launchd hands you). Verify with `coord doctor --machine <name>` before declaring the box ready:
   every capability you give it in step 9 must show a probed version, not "not found".
6. **Tailscale** — join the tailnet; the agent is reached at `:7433` over MagicDNS like any other
   machine.
7. **Clone the repos** into `~/src/` — `quadraui`, `vimcode`, `claude-coordinator`. Remember
   `~/src/<repo>` is the **worker worktree base**; never delete it to "fix" drift.
8. **coord agent last.** Follow [`AGENT_OPERATIONS.md`](AGENT_OPERATIONS.md) end-to-end — do not
   re-derive it. **INVARIANT: `~/.coord-venv` must be a PyPI install
   (`pip install claude-coordinator`), never editable.** Service supervision on mac is launchd, not
   systemd (also #1158 / CP-3) — so `install-agent.sh` and `deploy/coord-agent.service`, which are
   the Linux fleet's source of truth for the agent's PATH (#1671), **do not apply here**. There is
   no checked-in launchd plist yet; writing one, with the PATH entry from step 5, is part of #1158 /
   CP-3. Until then this box's PATH is hand-rolled and will fail *silently* if it's wrong — the
   agent starts, `/health` answers, capabilities read "not found", and smoke dispatch quietly
   refuses to route.
9. **Register in `coordinator.yml`** with `concurrency: 1` and an `os:macos` capability, then route
   the macOS suites to it via `smoke_tests.capability_rules` (#1159 / CP-4 adds the `os:*`
   capability convention and the CI matrix).

## What non-macOS work to route there

The mac is not a single-purpose macOS box. Most of the fleet's work is platform-agnostic and can
route to it — and **it is useful before milestone #39 lands**, which is not obvious from
[`CROSS_PLATFORM.md`](CROSS_PLATFORM.md) alone.

**Two findings that establish this (verified 2026-07-25):**

1. **`coord/` contains zero Linux-isms.** No `systemctl`, `/proc/`, `readlink -f`, `apt-get`, or
   `DISPLAY`/`Xvfb` references, and **no `sys.platform` / `platform.system()` branches at all**. The
   package is written POSIX-generic.
2. **macOS is already an intended agent platform.** `coord/interactive.py`'s module docstring states
   the POSIX imports are guarded because "the stdlib `pty` / `termios` / `fcntl` modules are not
   present on Windows, but agent machines are **Linux/macOS only**." All three modules exist on
   macOS — so **CP-1 (#1156) is a Windows concern, not a mac blocker.**

### Routes cleanly

| Work | Why |
|---|---|
| **coord-tui / quadraui TUI-side Rust** | `TuiDriver` renders to ratatui's `TestBackend` — no TTY, no display, fully portable |
| **Reviews** | `claude -p` + `gh pr diff`; zero platform surface |
| **`claude-coordinator` Python** | portable in principle (finding 1) — but unverified in practice, see below |
| **webapp (React / Vite / vitest)** | portable; Playwright too, once a `browser` capability is staged |

The tui case is where the mac relieves a *current* bottleneck rather than just adding a body.
`coordinator.yml` annotates dellserver `NOTE 4 cores: fine for the Python suite, poor for tui/**
cargo builds`, and records that the fleet "was deadlocking at two concurrent claude-coordinator
items with only precision + elitebook." An M4 is a strong cargo machine and lands directly on that
constraint.

### Does not route there

**vimcode GTK4 work.** GTK4-on-quartz builds via Homebrew but behaves differently enough that a
`gtk` capability on the mac would be a lie for anything visual. Keep GTK work on precision /
elitebook until macOS GTK is a deliberate target.

### Two caveats

- **Portable-in-principle is not verified.** The coord pytest suite has never run on macOS. Finding
  1 covers the *source*; tests are where environment leaks in (BSD vs GNU userland in anything that
  shells out, tmux/ssh behaviour, path assumptions). Run the suite by hand before trusting dispatch.
- **CI is Linux-only until CP-4 (#1159).** Mac-developed work can pass locally and fail CI, or the
  reverse. That cost is real — but it cuts both ways: routing genuine work to the mac is the
  cheapest continuous de-risking of milestone #39, surfacing portability bugs incrementally instead
  of all at once when the mac milestone is finally picked up.

### Staging

Mirror the discipline already recorded for elitebook's `browser` capability — it was verified
locally *before* being added to the config. Start narrow:

```yaml
- name: macmini
  capabilities: [rust, python]            # NOT gtk
  repos: [claude-coordinator, quadraui]   # hold vimcode until GTK4-on-mac is deliberate
```

Then verify by hand (`pytest`; `cargo test` in `tui/`) and widen from there. Note the one real
setup gap: **`coord agent` under launchd is CP-3 (#1158) and unbuilt** — until it lands, run the
agent in a foreground/tmux session or hand-write a plist.

## Ongoing hygiene

- Watch memory pressure before raising concurrency past 1.
- Sweep `target/` dirs periodically — 57GB of artifacts accumulates from *one* checkout each, and
  worktrees multiply it.
- `vimcode` links `quadraui` by relative path (`../quadraui/quadraui`, from vimcode's repo root),
  so the mac needs the same worktree symlink arrangement the Linux agents use — for vimcode
  only. `coord-tui` no longer has this exposure: since #1973 it pins quadraui by git rev in
  `tui/Cargo.toml`, so `cargo build`/`cargo test` in `tui/` fetch quadraui straight from GitHub
  and need no sibling checkout or symlink at all.

## Related

- [`CROSS_PLATFORM.md`](CROSS_PLATFORM.md) — the port itself; milestone #39 / epic #1160 (CP-1 #1156
  POSIX-import guards, CP-2 #1157 single-node local mode, CP-3 #1158 macOS runtime parity, CP-4
  #1159 CI matrix + `os:*` capability rules).
- [`AGENT_OPERATIONS.md`](AGENT_OPERATIONS.md) — agent install, the PyPI-not-editable invariant,
  service restart, `did not come back` triage.
- [`OPERATING_GOTCHAS.md`](OPERATING_GOTCHAS.md) — fleet traps that each cost a real dispatch.
