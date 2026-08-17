# The Test-First Bug Lane

> **Status: plan of record, 2026-08-07.** Milestone: code-coordinator #61
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
(code-coordinator#1950). A suite nothing re-runs is not an oracle; it is a green
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
path-dep silently restating every snapshot), and code-coordinator#1950 (the cadence
hole) — three findings in three repos, from one bug.

## The intake contract (#1964, shipped)

An issue entering this lane carries four fields, instead of the feature lane's
milestone + UX mock:

1. **Expected behaviour** — what should happen, in observable terms.
2. **Actual behaviour** — what happens instead.
3. **Reproduction** — the shortest path to see it.
4. **Evidence** — screenshot, wireframe, or a reference implementation that behaves
   correctly (a sibling backend, or a prior release).

(4) is the highest-value field and the most often skipped. A *reference
implementation* is worth more than a screenshot: it makes the expectation executable
by construction. "GTK should look like TUI here" is a complete specification.

**The fields survive as structured sections, not prose.** Two front doors, both
backed by the same four headings (`coord/bug_intake.py` is the single source of
truth, so they can never drift apart silently):

- `.github/ISSUE_TEMPLATE/bug_report.md` — a human filing straight through the
  GitHub UI gets the four `##` sections pre-seeded.
- `coord issue create <repo> --title "…" --expected "…" --actual "…" --repro "…"
  --evidence "…"` — the CLI/automation front door. All four are required together
  and mutually exclusive with `--body`/`--body-file`; `coord.bug_intake.
  format_bug_report` renders them as one `##`-headed section apiece so a later
  contract author (hand or agent) reads them directly off the issue instead of
  re-deriving them from a paragraph. `coord.bug_intake.parse_bug_report` is the
  inverse, for the agent-assisted authoring path (below).

No milestone, no `coord acceptance mock`, no Gate-A sign-off happens at intake —
those are feature-lane machinery this issue deliberately does not touch.

## Single-issue contract authoring (#1964, shipped)

The feature lane's contract lives at `tests/acceptance/ms-NN/contract.md`, gated by
Gate A and keyed by a milestone number. A bug has neither. Its contract lives at:

```
tests/acceptance/issue-NN/contract.md      # coord.acceptance.bug_contract_path(NN)
tests/acceptance/issue-NN/manifest.yml     # tests: {<test-id>: NN}
tests/acceptance/issue-NN/<suite files>    # sealed once authored, same as ms-NN
```

`coord.acceptance.issue_dirname(NN)` / `bug_contract_path(NN)` pin the convention;
**everything downstream already works with zero new code**, because the manifest
scanner (`coord.acceptance._manifest_paths`) globs `*/manifest.*` under
`tests/acceptance/` regardless of what the directory is called:

- `coord acceptance run --repo R --issue NN` / `coord acceptance record --repo R
  --issue NN --sha SHA` — already took no milestone argument (this is what the
  issue's own framing points at: "the runner and the trust gate are reusable
  as-is").
- `coord.acceptance.oracle_loop_contract_block` — the "🔒 Oracle-loop acceptance
  contract" block prepended to a Work briefing — is keyed by **issue number**, not
  milestone, and fires whenever the repo has an acceptance driver configured and
  `ms_dir_for_issue` finds a slice for that issue. An `issue-NN/` slice is
  discovered by the exact same call as an `ms-NN/` one. This is why "briefing-
  contract auto-injection" is explicitly out of scope for this issue's deliverable
  list below: it isn't missing, it's already generic. See
  `TestBugLaneNeedsNoMilestone` in `tests/test_acceptance.py` for the end-to-end
  proof (a hand-authored `issue-NN/manifest.yml`, no `ms-*` directory anywhere,
  resolves through `load_manifest` → `ms_dir_for_issue` →
  `oracle_loop_contract_block` unmodified).
- **The four fields themselves already reach the Work briefing, verbatim, with no
  bug-lane-specific code at all.** `coord assign`/`coord dispatch` (the headless
  dispatch every auto-loop uses) auto-generates the briefing from the raw issue
  body whenever no explicit `--briefing` is given
  (`coord/commands/dispatch.py`: `briefing = f"Issue #{issue}: {issue_title}\n\n
  {issue_body}"`) — this is completely generic, not something #1964 added. Because
  `coord issue create --expected/--actual/--repro/--evidence` writes the four
  fields into the issue body as literal `##` sections (Deliverable 1), they ride
  along in that auto-generated briefing intact, underneath the oracle-contract
  block pointing at `contract.md`. Nothing about "make the four fields reach the
  briefing" needed building — it falls out of Deliverable 1 (structured sections,
  not prose) plus machinery that already existed.

**Authoring is hand-first.** Read the issue's four fields directly (no parsing
needed — that's the point of shipping them as sections) and write `contract.md` by
hand, scoped to the **one behaviour** the bug describes — not a whole surface, not
a whole milestone. `JDonaghy/vimcode#622` already establishes this is workable by
hand. An agent-assisted authoring dispatch (mirroring `coord acceptance mock`'s
independent-agent shape, but keyed by issue number with no milestone lookup) is a
natural follow-up once hand-authoring has enough mileage to know what to automate
— not built by this issue. Explicitly out of scope here, same as the milestone
ceremony above: Gate-A sign-off, stall-protocol automation beyond what already
exists issue-number-keyed (`coord acceptance stall` already takes no milestone
argument either).

## Known-good worked example

The first pass through this loop should be the vimcode extensions-panel help popup,
found by operator smoke on 2026-08-07:

- **Expected**: full box border, title centred in the top border.
- **Actual**: side-bars only, title demoted to a content row.
- **Reproduction**: open vimcode, focus the extensions panel, press `?`.
- **Evidence**: screenshots of both, plus `develop` as a working reference.
- **Root cause**: already filed upstream as quadraui#541.

It is an ideal rehearsal because the expensive part — characterising the bug — is
already done, and it exercises every stage of the lane including the upstream-vs-local
fork. Walked through end to end, in the shape #1964 ships:

1. **Intake.**
   ```
   coord issue create vimcode \
     --title "Extensions-panel help popup: missing top/bottom border" \
     --expected "Full box border, title centred in the top border segment." \
     --actual "Side-bars only (left/right verticals); title demoted to a plain content row." \
     --repro "Open vimcode, focus the extensions panel, press ? to open the help popup." \
     --evidence "Screenshots of both states, plus develop (pre-regression) as a working reference — quadraui#541 has the upstream root cause."
   ```
   This lands as vimcode issue #625, body already split into the four `##`
   sections above — nothing left to re-derive.

2. **Contract authoring (hand, scoped to the one behaviour).** Read #625 directly
   and write `tests/acceptance/issue-625/contract.md` in vimcode's checkout:

   ```markdown
   # Bug-lane contract — issue #625: extensions-panel help popup border

   ## The one behaviour
   `TuiDriver` renders the help popup as a bordered box:
   - Row 0 (top border) is a full horizontal rule using the top-left/top-right
     corner glyphs at both ends — `driver.find(top_left)` and
     `driver.find(top_right)` both return `Some(..)` on the popup's top row.
   - The popup's title text is found on that same top row, horizontally
     centred — not on a separate content row below the border.
   - Left/right columns render the vertical border glyph on every popup row
     (this part already worked pre-regression; asserting it too catches a
     half-fix that adds a top rule but drops the sides).

   ## Done condition (the red gate)
   Before any fix, a test asserting the three bullets above must FAIL,
   specifically on the title-row / top-border assertions. After the fix, the
   same test — unmodified — goes green.
   ```

   Then a matching `tests/acceptance/issue-625/manifest.yml`
   (`tests: {help_popup_border::has_top_and_bottom_rule: 625, ...}`) and the
   actual `TuiDriver` assertions, committed to vimcode's `tests/acceptance/`
   the same way any `ms-NN` slice is.

3. **Red gate.** `coord acceptance run --repo vimcode --issue 625` must FAIL,
   for the title/border reason above, before any fix lands — proves the test
   exercises the reported bug rather than passing vacuously.

4. **Dispatch, fix, converge.** The issue dispatches as ordinary `work` (no
   milestone, no Gate A to wait on). The worker briefing already carries the
   "🔒 Oracle-loop acceptance contract" block pointing at
   `tests/acceptance/issue-625/contract.md`, and iterates with
   `coord acceptance run --repo vimcode --issue 625` until green, same loop as
   the feature lane.

5. **Trust gate + cadence.** `coord acceptance record --repo vimcode --issue 625
   --sha <pushed-sha>` re-runs the sealed slice externally. Because vimcode's real
   test command runs the whole accumulated `tests/acceptance/` tree (#1950), this
   slice re-runs on every later issue forever — coverage ratchets, it does not rot.

## What this program is *not*

- Not a replacement for the feature lane. Multi-issue greenfield work still goes
  through epic → Gate A → oracle loop.
- Not a mandate to backfill vimcode's existing bugs. Coverage **ratchets with churn**:
  a bug gets a test when it gets fixed. Big-bang suite authoring is explicitly
  rejected here for the same reason it is in the repo test policy.
- Not pixel comparison. There is no pixel oracle in this design, on any backend.
