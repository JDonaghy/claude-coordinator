# Adding a second worker backend — the seam, the gaps, the sequencing

> _2026-08-02._ Findings from evaluating a second `claude -p` alternative for the
> fleet. Tracked as **epic #1709**. Companion to
> [`ARCHITECTURE.md`](ARCHITECTURE.md) (how workers are spawned) and
> [`EPHEMERAL_WORKERS.md`](EPHEMERAL_WORKERS.md) (the on-demand Azure VMs that
> motivated it).

## Why a second backend

One backend means one rate-limit pool, one vendor, one failure mode. The
ephemeral Azure workers exist precisely to run an epic without competing with the
operator's own subscription limits — a goal only half-served while the only
backend is Anthropic's.

Two routes to non-Anthropic models, and they are complementary rather than
competing:

1. **Anthropic-compatible endpoints, still via `claude -p`.** Z.ai
   (`https://api.z.ai/api/anthropic`, GLM) and Moonshot
   (`https://api.moonshot.ai/anthropic`, Kimi) ship first-party drop-in targets.
   Selected purely by `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` /
   `ANTHROPIC_MODEL`, so no code change is needed — only somewhere to put the env
   vars (#1706). Caveats: Anthropic does not support or audit routing Claude Code
   to non-Claude models, and Kimi's endpoint currently needs
   `ENABLE_TOOL_SEARCH=false`. Buy pay-as-you-go API, **not** a subscription
   coding plan — a plan reintroduces the rate limiting this exists to escape.
2. **A second provider binary.** Broader, and the durable answer.

## Why opencode, not aider

**aider is an editor, not an agent.** It has no shell tool, and `--yes-always`
*skips* LLM-suggested shell commands rather than running them
([aider#3903](https://github.com/Aider-AI/aider/issues/3903)). So it cannot
`git checkout -b`, `git push`, or touch `gh` — coord would have to supply the
entire git half from outside the `Provider` ABC, which has no post-run hook. Its
one genuine draw is that `--auto-test --test-cmd` is natively the oracle loop's
tight loop.

**opencode is a real agent** — it runs git and `gh` itself, so
`work → branch → push → PR` needs no wrapper. It carries ~75 model providers,
which is the actual prize: GLM, Kimi, DeepSeek and local weights without needing
each vendor to ship an Anthropic-compatible endpoint.

`coord/providers/opencode.py` already exists but is **unusable**: it was written
without opencode installed (its own module docstring says so) and every
behavioural claim is marked `ASSUMPTION`. The load-bearing ones are wrong *in
opencode's favour* — it does have a per-tool `allow`/`ask`/`deny` permission
system with bash pattern matching, it does have `--format json`, and `--agent`
does supply a real system prompt. So the work is **verification and wiring, not
architecture**.

One inversion to respect: Claude Code is **allow-list** (absent from
`--allowedTools` ⇒ denied); opencode's `--auto` is **deny-list** (auto-approves
anything not explicitly denied). Agent definitions must be written deny-baseline
with explicit allows, so an omission fails closed.

## The finding that matters: the seam is bypassed

The `Provider` ABC defines `parse_log()` as the seam turning a worker log into a
`WorkerSummary`. **Coordinator-side code does not use it.** There are *three*
copies of the "worker logs are claude stream-json" assumption:

| Layer | Where | Status |
|---|---|---|
| 1. The intended one | `coord/worker_events.py` | Correct — it *is* `ClaudeProvider`'s implementation |
| 2. Coordinator consumers | `progress.py`, `usage.py`, `failure_class.py`, `review.py`, `plan_parser.py`, `notify.py`, `conflict_fix.py`, `issue_store.py` | **Bypass the seam** — import `worker_events` directly. Fixed by #1710 |
| 3. coord-tui, in Rust | `render.rs`, `chat.rs`, `data.rs`, `pipeline.rs` — `render.rs:1272` names the `claude -p --output-format stream-json` protocol | **Not fixed by #1710.** Accepted limitation |

`provider.parse_log()` is called only from `coord/agent.py`.

**The failure mode is silence, not error.** Every layer-2 site gates on
`is_stream_json(log)`, which returns `False` for a non-claude log, so the code
falls to a plain-text branch and yields nothing. A second-backend worker would
show no progress, no cost, and an unknown failure reason, with no exception
anywhere to point at the cause.

This is the recurring shape already recorded in
[`OPERATING_GOTCHAS.md`](OPERATING_GOTCHAS.md): *`reconcile()` accretes behaviour
the automatic drivers never invoke.* A seam exists; the drivers route around it.
It also means the #322 abstraction has never had a second implementation run
through it, so its claude-shaped assumptions are unexercised — which is why
**#1710 ships a fake second provider in the test suite**, so the seam is
exercised by two shapes in CI permanently, with no dependency on opencode.

## Machine and provider are orthogonal — keep it that way

`Machine` (`coord/models.py:143`) has no provider field, and resolution is
spec > repo > `providers.default`, independent of which box runs the work. An
Azure epic on claude and a local epic on opencode — or the reverse — is
expressible today in the data model.

What is missing is that **a provider is only usable where its binary is
installed**, and nothing checks it: dispatch to a machine without opencode fails
at spawn, after worktree setup, rather than being refused at the CLI (#1711).
Related: `dispatch.py:442` omits `provider` from the wire payload when it is
`"claude"`, so coordinator and agent can in principle disagree about what ran.

## Why the web UI raises the stakes rather than lowering them

`GOAL.md` points at the coord web control center, which "replaces the phone PWA
and, for most users, the TUI." That makes backend correctness *more* important,
not less:

- **The web API consumes normalized fields** — `coord/dashboard/server.py:200`
  passes through `num_turns`, `total_cost_usd`, `exit_code`, `last_tool`,
  `stop_reason`: `WorkerSummary` over HTTP, not raw logs. So the web app is
  structurally well-placed for a multi-backend fleet — **but only once #1710
  makes those fields correct for non-claude workers.** A new web UI showing blank
  progress and no cost for opencode workers would discredit both at once.
- **coord-tui parses raw logs client-side** (layer 3 above), so it will render
  opencode workers poorly even after #1710. Accepted: it is the demoted surface.
  The fix would be either duplicating #1710 in Rust or making the TUI consume
  normalized fields — adjacent to #1364 in the coord-tui audit.

## Sequencing against the coord-tui audit (#1358)

The Fable 5 static audit of `tui/src/**` (epic #1358, 18 children #1359–#1376,
all open) **does not collide** with this work: Rust vs Python, zero file overlap,
different milestones, no merge-conflict risk. They can run concurrently.

**It should not go first.** `GOAL.md` documents the TUI as being superseded for
most users, so its big structural children (#1360 the 3,900-line
`dispatch_handle`, #1362 the 216-field god struct, #1372 the test split) have the
longest payback period and the highest chance of being wasted.

But the audit's central claim is *measured*, not stylistic — duplicated sidebar
tree logic caused 5+ repeat fixes. So **cherry-pick rather than sequence**: pull
forward #1371 (byte-indexed truncation, a known panic class), #1367/#1368
(diverged lifecycle classifiers and session-liveness predicates — wrong menu
gating wastes dispatches), #1363 (silent failures blinding the operator — the
TUI-layer twin of #1710's defect), and #1374 (a shipped stub that toasts an
apology). Defer the structural ones until the TUI's maintenance horizon is
decided.

## Where the work lives — epic #1709

| # | Issue | Backend-agnostic? |
|---|---|---|
| #1710 | Route coordinator consumers through `provider.parse_log()` | ✅ Highest leverage |
| #1711 | Declare + probe provider availability per machine | ✅ |
| #1706 | Thread `ProviderDef.model` / `env` / `extra_args` (finishes #324) | ✅ |
| #1707 | `coord assign --provider` — per-assignment backend selection | ✅ |
| #1703 | Capture real opencode output (verification pass) | opencode |
| #1704 | Correct `OpenCodeProvider` against verified behaviour | opencode |
| #1705 | Agent definitions; flip `enforces_deny_list` for `work` | opencode |
| #1708 | Proof run on an ephemeral Azure worker | opencode |

Four of the eight are worth landing whether or not opencode is ever adopted —
they are the difference between having a provider abstraction and having a
working one.
