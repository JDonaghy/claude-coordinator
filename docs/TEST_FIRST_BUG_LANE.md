# The Test-First Bug Lane

> **Status: plan of record, 2026-08-07.** Milestone: claude-coordinator #61
> "Test-first bug lane". Companion to [`ORACLE_LOOP.md`](ORACLE_LOOP.md) (the
> feature lane) and [`PIPELINE_V2.md`](PIPELINE_V2.md).

## The goal, stated as an acceptance test

> A bug report arrives as a **screenshot plus a description**. It becomes an issue,
> the issue is dispatched into the queue, and it comes back fixed — with **no manual
> smoke test by the operator at any point**, and with confidence that it is actually
> fixed and stays fixed.

Everything below exists to make that sentence true. If a step doesn't move that
sentence, it isn't in this program.

## Why this is a *second* lane, not more of the first

The oracle loop we have is a **feature lane**: greenfield, multi-issue, a Gate-A
contract authored per **milestone** from a UX mock. It is being proven on the coord
web control center, and it is the right shape for "build me this thing."

It is the wrong shape for "this is broken." A bug has no milestone, no mock, and no
design to sign off. It has an *observed* wrong behaviour and an *expected* right one.
The intake is different, the contract is smaller, and the acceptance bar is different
in a way that matters (see "the red gate" below).

| | feature lane | **bug lane** |
|---|---|---|
| unit | milestone / epic | **single issue** |
| expectation from | UX mock, Gate-A contract | **bug report: expected/actual + screenshot** |
| contract scope | whole milestone | **one behaviour** |
| authoring gate | mock signed off | **the test must fail, for the reported reason** |
| already built? | yes | **no — this program** |

`coord acceptance run --repo --issue N` and `coord acceptance record` already take
**no milestone argument at all**, so the machinery underneath is shared. What is
missing is the intake, the authoring discipline, and the cadence.

## The four things "no manual smoke" actually requires

### 1. A baseline anyone believes (Phase 0)

You cannot claim "the tests prove it" on a suite the team has learned to skip. As of
2026-08-07 vimcode's is exactly that: six `insta` snapshot tests fail for **everyone**
(not just CI — reproduced on two fleet machines, with a fresh `$HOME`, at three
different commits), `.github/workflows/ci.yml` `--skip`s them plus a seventh, and
coord's configured `test_command` is the *unfiltered* command — so the Test stage
fails on **every** vimcode issue for reasons unrelated to the change under test.

That is worse than having no tests, because it trains everyone to read a red Test
stage as noise. **Phase 0 is not optional and it comes first.**

### 2. A standing cadence (Phase 0)

A sealed acceptance slice today runs **once** — while its own milestone is being
driven — and never again. No CI job and no Test-stage lane re-runs it
(claude-coordinator#1950). A suite nothing re-runs is not an oracle; it is a green
screenshot from the day it was written.

The fix is structural, not procedural: the acceptance suite must be part of the
repo's **real test command**, so the ordinary Test stage re-runs every prior slice on
every subsequent issue. Then coverage **ratchets** — each bug fixed adds a permanent
guard — instead of rotting. This is the single highest-leverage item in the program.

### 3. An executable expectation format (Phase 1)

This is the new skill. A bug report has to become an assertion.

**Terminals make this unusually tractable.** `TuiDriver` renders to a text grid, so a
terminal screenshot maps to assertions almost 1:1 — `find("text")`, `screen_contains`,
row/column checks. A screenshot of a bordered popup is directly comparable to the grid
that should have been produced. There is no equivalent for a GUI: pixels are not an
oracle, and `GtkDriver` is deliberately structural rather than pixel-based.

That asymmetry is a strategy, not just a convenience — see Phase 2.

**The red gate.** The authoring step's acceptance criterion is that the new test
**fails, and fails for the reported reason**, before any fix is attempted. This is
what separates this from ordinary test-writing:

- it proves the test actually exercises the bug (not a vacuous pass),
- it proves the reporter's expectation was captured, not the current behaviour,
- and it gives the fix an unambiguous done condition.

A test authored green is worthless here and must be rejected at the authoring gate.

### 4. A reference implementation for the other backend (Phase 2)

For anything with two backends, **TUI is the executable specification and GTK must
match it.** This is why the terminal asymmetry above is load-bearing: the cheap,
assertable backend defines the behaviour, and the expensive one is held to it.

`quadraui/tests/cross_backend_parity.rs` already runs one script against both
`TuiDriver` and `GtkDriver`. **But there is a real gap to close before leaning on
it:** it asserts *logical* state via `screen_has(needle) -> bool`. The 2026-08-07
tooltip divergence (quadraui#541 — GTK draws a full box, TUI draws side-bars only)
would **pass** such a test on both backends: both contain the text; only the chrome
differs.

So parity needs two tiers:

| tier | asserts | catches | status |
|---|---|---|---|
| **behavioural** | logical state — text present, interaction outcomes | wrong behaviour | exists, **5 tests** |
| **structural** | which surfaces/primitives each backend emitted | silently dropped chrome | **does not exist** |

Tier 2 is what would have caught #541, and what stops "GTK matches TUI" from being
an aspiration.

## Phases and dependencies

```
Phase 0  trustworthy baseline + standing cadence   ← blocks everything
   │
   ├── Phase 1  the bug lane (intake → red test → seal → fix → re-run)
   │
   └── Phase 2  TUI-as-spec: structural parity tier in quadraui
                   │
                   └── Phase 3  platform-neutrality, so the spec covers the real screen
```

Phase 3 is already most of the way there and is tracked in vimcode's own milestones
(#7 Platform-Neutral, #8 GTK ShellApp Event Dispatch, #9 TUI ShellApp Migration). It
is listed here because **the spec only covers vimcode's actual screen once vimcode's
rendering *is* quadraui primitives** — until then a parity test in quadraui proves
nothing about what vimcode draws.

## Why the two programs are one investment

This is the part worth internalising, because it removes a false choice.

Every bug fixed test-first in vimcode resolves into exactly one of two buckets:

- **It's a quadraui primitive bug.** Fix it once upstream; the parity suite then
  guards *both* backends forever, and every other consumer inherits the fix.
- **It's vimcode-specific.** Then it is platform-specific logic that, by the
  Platform-Neutrality Rule, shouldn't exist — so fixing it advances milestone #7.

There is no third bucket. Funding the bug lane funds platform-neutrality, and vice
versa. vimcode is not a product being maintained here; it is the **testbed that
proves the coordinator does real work**, and the forcing function that finds
quadraui's gaps.

The 2026-08-07 session is the worked example: smoking one vimcode branch produced
quadraui#541 (a framework parity gap), vimcode#625's real root cause (a floating
path-dep silently restating every snapshot), and claude-coordinator#1950 (the cadence
hole) — three findings in three repos, from one bug.

## The intake contract

An issue entering this lane carries:

1. **Expected behaviour** — what should happen, in observable terms.
2. **Actual behaviour** — what happens instead.
3. **Reproduction** — the shortest path to see it.
4. **Evidence** — screenshot, wireframe, or a reference implementation that behaves
   correctly (a sibling backend, or a prior release).

(4) is the highest-value field and the most often skipped. A *reference
implementation* is worth more than a screenshot: it makes the expectation executable
by construction. "GTK should look like TUI here" is a complete specification.

## Known-good worked example

The first pass through this loop should be the vimcode extensions-panel help popup,
found by operator smoke on 2026-08-07:

- **Expected**: full box border, title centred in the top border.
- **Actual**: side-bars only, title demoted to a content row.
- **Evidence**: screenshots of both, plus `develop` as a working reference.
- **Root cause**: already filed upstream as quadraui#541.

It is an ideal rehearsal because the expensive part — characterising the bug — is
already done, and it exercises every stage of the lane including the upstream-vs-local
fork.

## What this program is *not*

- Not a replacement for the feature lane. Multi-issue greenfield work still goes
  through epic → Gate A → oracle loop.
- Not a mandate to backfill vimcode's existing bugs. Coverage **ratchets with churn**:
  a bug gets a test when it gets fixed. Big-bang suite authoring is explicitly
  rejected here for the same reason it is in the repo test policy.
- Not pixel comparison. There is no pixel oracle in this design, on any backend.
