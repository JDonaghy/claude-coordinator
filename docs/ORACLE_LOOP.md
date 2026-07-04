# The Oracle Loop — a tight, framework-pluggable acceptance loop

> **Status:** design + build slice, 2026-07-04. Refines [`PIPELINE_V2.md`](PIPELINE_V2.md)'s
> "Independent acceptance testing" and Gate A. Pipeline v2's merge-bounce, observability, and
> git-model parts are unchanged; this doc replaces *how acceptance testing works* and *how Gate A
> is run*. Build slice + issue map at the bottom.

## The problem this fixes

The fix/test loop today crosses stage boundaries with **cold starts**. Work implements and
*releases*; a fresh Test agent spins up cold and records a fail; a fresh Fix worker spins up cold,
re-reads the issue + diff + failure to re-acquire context, attempts a fix, *releases*; repeat. Every
round is 2–3 fresh `claude` sessions each paying full context re-acquisition — re-reading the big
files every time. The intelligence resets to zero between attempts. That is the token bleed with no
visible progress, and it is what drives the operator to bail out and drive the branch by hand.

## The core idea — an oracle

An **oracle** is a test suite an agent can **RUN but cannot READ or EDIT**. It answers one question
— *"does the behavior match the contract? yes/no, and which cases fail?"* — and leaks nothing about
its internals. The worker iterates against it **in its own warm session** until green, then
releases. The loop collapses from "cross-stage, cold, 2–3 sessions per round" to "in-session, warm,
near-zero marginal cost per round." Context never resets.

**Independence is preserved by separating *authoring* from *running*.** Independence is a property
of *who wrote the test and when* — **not** of who runs it. A suite authored by a different agent
(`test-author`), from the Gate-A contract, *before the code existed*, is exactly as independent
whether the worker executes it or a separate stage does. So:

- **`test-author` writes** the acceptance suite from the contract, red, before any work.
- **The worker gets it *sealed*** — read-only, run-only. It can call `coord acceptance run` and see
  `2/5 green · test_x expected A got B`, but it cannot open or edit the test files. It iterates to
  green against a suite it did not write and **cannot game because it cannot see inside it**.

**The oracle runs twice, on purpose:**
- **In-session, by the worker** — for *speed* (the warm loop).
- **Externally, by the coordinator** against the pushed SHA — for *trust* (a headless worker can lie
  about "green"; it cannot fake the coordinator's own run). Same suite, two jobs.

**Why this is the lever for >90% UAT.** The oracle is a *proxy* for user acceptance. If it is a
faithful proxy — a function of contract fidelity — then "worker exits green against the oracle" ≈
"UAT will pass." The worker converges to acceptance behavior *before a human ever looks*. UAT stops
being where you **discover** problems and becomes where you **confirm** them; residual UAT failures
are exactly the cases the contract missed, which is a tight feedback signal on the contract.

## This is not waterfall

The waterfall pathology is *discovering you built the wrong thing after building it*. The oracle loop
front-loads discovery to the cheapest possible point and keeps it amendable:

1. **Mock-first Gate A.** Gate A produces a **viewable mock in the target medium**, not a text spec.
   You react to a rendered screen / wireframe *before* the expensive implementation. The mock **is**
   the contract fixture.
2. **The contract is amendable and versioned**, not frozen. When a shipped issue makes you want
   something different, you amend the contract → the test-author updates the affected slice → work
   re-syncs. A normal, cheap, logged operation.
3. **Only one slice is pinned ahead of the work** (just-in-time authoring). The full suite need only
   exist by Gate C.

Structurally: a fast **inner loop** (per-issue, converge-to-oracle) inside a steering **outer loop**
(amend the contract as the milestone takes shape). Iterative development with an *executable* spec.

**Mock granularity is milestone-only, tuned by milestone size (decided 2026-07-04).** The mock is
rendered once, up front, for the whole milestone — the more waterfall-leaning of the two options
(the alternative was a per-issue slice-mock). We deliberately chose milestone-only and treat
**milestone size as the tuning knob** instead of adding per-issue mock ceremony: a small (1–2 issue)
milestone's up-front mock *approximates* a per-issue mock, so shrinking milestones buys tighter,
later-binding UX discovery, while larger ones amortize Gate A. This is itself an empirical bet — we
can't know the right granularity up front, so we ship milestone-only and let observed behavior
(Gate-A overhead vs. mock-drift) tune the size. Revisit per-issue slice-mocks only if size-tuning
proves insufficient.

## Framework drivers — the oracle is framework-dependent

`coord acceptance` is a thin, framework-agnostic orchestrator over a **driver adapter declared per
repo** — the same shape as `smoke_tests.capability_rules` (files → machine capability). The driver
knows how to launch, drive, and assert on the app; the tests use the driver's API.

```yaml
# coordinator.yml
acceptance:
  drivers:
    coord-tui:
      kind: tui-tuidriver              # quadraui TuiDriver / TestBackend
      run: "cargo test --test acceptance -- --format json"
      mock: "*.screen"                 # text screen-grids: mock == assertion fixture
      capability: rust
    webapp:
      kind: web-playwright             # drives Electron too
      run: "npx playwright test tests/acceptance"
      mock: "*.html"                   # static wireframe: reviewed, then asserted against
      capability: browser
```

| Medium | Mock format (agent-authored, human-reviewed) | Driver / assertion |
|---|---|---|
| **TUI (quadraui)** | `.screen` text grids — **mock == assertion** | `TuiDriver` / `TestBackend` (#690/#691) |
| **Web / Electron** | self-contained `.html` wireframe | Playwright, `browser` capability |
| **Native rich client** | SwiftUI / Compose previews, or a generated image | XCUITest / Espresso / WinAppDriver |

TUIs are the cleanest case: the UI *is* text, so the mock, the contract, and the assertion fixture
are the same artifact. **Known limit (coord-tui):** `TuiDriver` renders to `TestBackend`, so
raw-terminal / ANSI / SGR-mouse / embedded-PTY bugs are out of reach and stay on a thin live smoke
(the quadraui #302 pty+vt100 tier is unbuilt).

## The `coord acceptance` runner — concrete spec

One command, framework-agnostic above the driver:

| Subcommand | Who runs it | What it does |
|---|---|---|
| `coord acceptance mock --milestone NN` | Gate A dispatch (#930) | Dispatch a **mock-author** agent: render the viewable mock + write `contract.md` from it. |
| `coord acceptance author --milestone NN [--issue N]` | Gate A / JIT (#931) | Dispatch the independent **`test-author`**: write/extend the red acceptance suite from the contract. |
| `coord acceptance run --issue N` | **worker, in-session** | Run issue N's slice via the repo driver; return **structured** per-test pass/fail. Sealed: verdicts only, no test source. |
| `coord acceptance run --all` | coordinator (Gate C) | Run the full accumulated suite. |
| `coord acceptance record --issue N --sha <sha>` | coordinator, **external** | Re-run the sealed slice against the pushed SHA; **write the verdict to the board** (the Acceptance box). The trust gate. |
| `coord acceptance stall --issue N --tried … --stuck …` | worker, on non-convergence | Emit the structured stall report + push a WIP snapshot → raises `needs-attention` (#846). |

**Sealing — climb the ladder:**
- **v1 (policy):** the acceptance dir is checked out in the worktree but listed in `files_forbidden`;
  the git-floor / reviewer flags any diff that touches `tests/acceptance/**` (tamper detection).
- **v2 (construction):** the worktree does **not** contain the acceptance source; `coord acceptance
  run` executes against a coordinator/daemon-held copy and returns only structured verdicts.

**Layout (per driver, in-repo, separate target):**
```
tests/acceptance/ms-NN/
  contract.md          # the black-box surface: CLI names, key screen text, API field shapes
  mocks/               # the viewable mocks == the assertion fixtures (*.screen / *.html / …)
  <suite files>        # red at Gate A, extended JIT per issue; SEALED to the worker
  manifest.(yml|json)  # test-id → issue-slice mapping (drives --issue N)
```

## The worker briefing contract

Injected at the top of every Work briefing in an oracle-loop milestone:

- Implement to **`tests/acceptance/ms-NN/contract.md`** (the black-box surface).
- You **may not** edit `tests/acceptance/**`. It is the sealed oracle. Touching it fails the gate.
- Run **`coord acceptance run --issue N`** to check yourself; iterate until your slice is green.
- Write your **own unit / internal tests** (that is still your job).
- If your slice will not converge — the failing set churns rather than shrinks across **2** rounds —
  **stop** and run `coord acceptance stall …` with what you tried and the stuck set. Do **not** grind.

## The stall protocol + convergence detector (#846)

The in-session loop still needs a bound — cheaper thrash is still thrash. Two triggers, one exit:

- **Worker self-report (preferred):** the worker watches its own failing-*set* trajectory. Shrinking
  = converging; churning (same count, different members) = whack-a-mole. On churn ≥2 rounds it calls
  `coord acceptance stall …`.
- **Coordinator backstop (headless):** the daemon watches the `acceptance record` history for a SHA
  series; if the external failing set churns across ≥2 runs, it raises `needs-attention` itself.

`needs-attention` is the **escape-hatch trigger**, not a separate epic — it is the stall-exit of the
loop. Headless auto-re-dispatches (or parks for the operator); interactive surfaces to the operator,
who already has the branch. This is all #846 needs to be; the WIP-snapshot handoff (#847) and
takeover-relaunch (#849) are conveniences, not prerequisites.

## Order of operations

Legend: **[indep]** zero-worker-context agent · **[worker]** implementer · **[coord]** external ·
**[human]** you.

**Phase 0 — Milestone kickoff (Gate A), once:**
1. **[indep] `coord acceptance mock`** renders the viewable mock; **[human]** reacts and signs off
   (UX discovery, against a cheap mock). The approved mock + `contract.md` is the pinned surface.
2. **[indep] `coord acceptance author`** writes the red suite from the contract. Gate A **blocks
   issue dispatch until the contract exists.**

**Phase 1 — per issue:**
3. **[coord]** Dispatch Work with the briefing contract above (issue + #603 digest + contract slice +
   the sealed `coord acceptance run --issue N` command).
4. **[worker]** Implements; writes unit tests (tier 1).
5. **[worker] in-session oracle loop:** `run --issue N` → fix → repeat (warm, no reset) →
   **converge → push + done**, or **stall → `acceptance stall` → WIP snapshot** (§ above).
   - *Headless:* whole loop in one `claude -p`, **zero human interaction.**
   - *Interactive:* identical; **[human]** present, nudges/ends on stall. Tightness comes from the
     agent self-running the oracle.
6. **[coord] Acceptance box (trust gate):** `acceptance record --issue N --sha <pushed>` — the
   coordinator re-runs the sealed slice externally. **ToS-clean: git + a test run, never the TTY.**
   Green → advance; red → bounce to Fix with the external failure.
7. **[coord] Test box:** the repo's full normal suite (regressions outside the slice) on a
   capability-matched machine.
8. **[indep] Review:** adversarial, zero shared context. Approve → advance; request-changes → Fix.
9. **[coord] Merge box:** rebase → if the artifact changed non-trivially, **re-run `acceptance
   record` + Test + Review on the rebased SHA** → merge into `feature/ms-NN` (Pipeline v2 keystone).

**Phase 2 — milestone close:**
10. **[coord] Gate C:** `acceptance run --all` green on `feature/ms-NN` (integration gaps *between*
    issues). **[indep] Gate B:** built-to-spec review. **Gate D:** ship → `develop`, gated on B + C.
11. **[human] UAT:** a **confirmation**, not a discovery. A residual failure → amend the contract
    (feeds Gate A) + file an issue. That delta measures how far below 100% the oracle sits.

## ToS posture — "done" is observed as "green," never scraped

The completion signal is **not** "the session ended" or "the model said done" — it is **"the oracle
passes on the pushed SHA,"** observed by the coordinator running the sealed suite itself (git + a
test run). This is ToS-clean in **both** modes because it never reads the terminal. Headless can also
auto-re-dispatch because `claude -p` is the sanctioned automation path (its stdout is program output,
not a scraped TTY). The **one** thing forbidden ToS-clean is auto-continuing a *stalled interactive*
session (needs a human keystroke) — which is exactly where the human belongs anyway.

## Dogfooding + limits

- **Dogfood target: coord-tui the app** — `TuiDriver` + `make_test_app(BoardData)` are real; the
  `.screen` mock == assertion property holds. Rollout does **not** block on quadraui maturing.
- **Excluded: quadraui the library** (it is the framework, still evolving) and **raw-terminal/PTY
  behavior** (out of `TestBackend` reach; thin live smoke, quadraui #302).
- Most consumers will be web / Electron / native — the driver table above is how they plug in; the
  coord-tui slice proves the machinery on the cleanest medium first.

## Build slice — phased, mapped to existing issues

**Slice 1 — the runner + one driver (unblocks everything):**
- `coord acceptance` command skeleton (`run` / `record` / `run --all`) + the `acceptance.drivers`
  config + the `tui-tuidriver` adapter (wraps `cargo test --test acceptance`, parses structured
  verdicts). Sealing v1 (`files_forbidden` + tamper detection). → *new issue; the plumbing #931/#932
  both assume.*
- Worker briefing contract injection for oracle-loop milestones. → *new issue (agent.py; needs a
  release + `coord agent update`).*

**Slice 2 — the in-session loop + trust gate (the tight loop):**
- Wire `coord acceptance run --issue N` for **in-session** worker use + the external
  `acceptance record` **Acceptance box** on the board. → *refits **#932** (adds the in-session half;
  #932 today is post-work only).*
- Deliver the suite **sealed** to the worker; author it framework-driver-aware. → *refits **#931**.*
- Stall protocol (`acceptance stall`) + coord-side churn detection → `needs-attention`. → *refits
  **#846** as the stall-exit of the loop.*

**Slice 3 — mock-first Gate A + amendable contract (kills waterfall):**
- `coord acceptance mock` (mock-author) + `contract.md` at `tests/acceptance/ms-NN/` + amend flow. →
  *refits **#930** (spec-first → mock-first) and the contract-storage open question.*

**Slice 4 — milestone close:**
- Gate C (`run --all`) + Gate B (built-to-spec) + Gate D (ship). → *refits **#932** (Gate C) + #933.*

**Interaction with per-stage mode (#686):** the in-session oracle loop **is** the headless
"zero-touch" Test/Work mode; interactive is the low-touch supervised variant. #686's per-issue
test-mode policy chooses between them.

## Dispatch order (milestone #25)

The slices resolve to one dependency DAG, encoded as the `## Work order` block in the milestone's
**epic tracking issue #947** (machine-readable by `coord milestone order` / `coord milestone
dispatch`, #768/#769 — `coord milestone dispatch` drains this frontier in order):

```
#944 ─┬─► #932 ─┐
      │         ├─► #945 ─┬─► #931
      └─► #846 ─┘         └─► #930
```

| Step | Issue | After | Runtime |
|---|---|---|---|
| 1 | **#944** runner + `tui-tuidriver` + sealing v1 + the `oracle_loop` milestone marker | — | coord-live |
| 2 | **#932** in-session run + external trust gate (Acceptance box) + Gate C | #944 | coord-live |
| 2 | **#846** stall protocol (`coord acceptance stall`) + churn detector | #944 | coord-live |
| 3 | **#945** worker briefing-contract injection | #944, #846 | **needs release** |
| ▶ | **Dogfood checkpoint** — hand-write a `contract.md` + one red acceptance slice for a small coord-tui issue, run *one* issue through the loop before automating authoring/Gate A | | |
| 4 | **#931** independent sealed authoring (`type=test-author`) | #945, #932 | **needs release** |
| 4 | **#930** mock-first Gate A + amendable contract | #945, #932 | **needs release** |

#932 ∥ #846 run concurrently (both only need #944); #931 ∥ #930 run concurrently after the plumbing.
The DAG **gates #931/#930 behind #945 + #932** so authoring + mock automation only begin once the loop
is dogfoodable — the checkpoint is enforced, not just advisory. **"needs release"** = touches
`agent.py` / worker prompts, so it reaches agents only after a PyPI release + `coord agent update`;
#944/#932/#846 are live from the editable install immediately.

**Standalone:** this milestone runs on the *current* flat pipeline — it does **not** depend on
Pipeline v2's Observability (#925–927) or Merge-bounce (#915) phases. Only **Gate C** rides along
(in #932); Gate B/D + the `develop` git model (#933/#934) stay deferred.

## Open questions

- Contract storage: settled here as checked-in `tests/acceptance/ms-NN/contract.md` (closes the
  #930/PIPELINE_V2 open question). Revisit if milestones need cross-repo contracts.
- Sealing v2 (worktree-absent, daemon-held suite) — deferred until v1 tamper-detection proves
  insufficient.
- Web/native driver adapters — specced, unbuilt; land after the coord-tui slice validates the shape.
