# Pipeline v2 — a two-tier pipeline (issue × milestone)

> **Status:** design, agreed 2026-07-03. Supersedes the flat `Work → Test → Review → Merge`
> model in [`ARCHITECTURE.md`](ARCHITECTURE.md) once the phases below land. This doc is the
> north star the epics point at; it is not yet fully built. Issue map is at the bottom.
>
> **Refined 2026-07-04 by [`ORACLE_LOOP.md`](ORACLE_LOOP.md):** acceptance testing becomes a
> *tight, in-session* loop (the worker iterates against a **sealed, runnable-not-editable** oracle in
> its own warm session, then the coordinator re-runs it externally as a trust gate), Gate A becomes
> **mock-first** with an **amendable, versioned contract** (which is what keeps this iterative, not
> waterfall), and the runner sits above **pluggable framework drivers** (TUI/web/native). Where this
> doc and ORACLE_LOOP.md differ on *how acceptance works* or *how Gate A runs*, **ORACLE_LOOP.md
> wins**; the merge-bounce, observability, and git-model parts below are unchanged.

## Why

Two things broke down and this redesign fixes both.

**1. Yesterday's merge (2026-07-02) was a tangled, hours-long manual untangle.** The milestone
trio **#769 / #645 / #770** — siblings all editing the same new files — were approved, then sat
while ~10 other issues merged ahead of them. By merge time they were 9–11 commits stale and each
rebase collided with its siblings'. The "Start merge" button ran the automated path
(`coord merge` → `gh pr merge --rebase`), which **refuses on conflict and re-marks
`state=conflict` with no feedback** — the operator clicked three times and "nothing happened."
The capable resolvers (`conflict-fix` #241, interactive `--merge-of`) exist but weren't wired to
that button. And a rebase changes the artifact, but nothing re-gated it: we'd have merged code no
reviewer and no CI ever saw, on the *old* approval.

**2. The operator is flying blind.** The pipeline row is three boxes with binary color. There is
no way to see how many worker runs or test runs happened, which stage is *in progress*
(a fix worker running behind a red Test box looks identical to an idle failure), or where exactly
a story is. The only history window — the Summary tab — dies on a transient board fetch and needs
a full TUI restart to recover.

**3. Test integrity.** The *worker* writes its own black-box tests. A worker grading its own
homework lets bugs sneak through — the tests get written to match what was built.

## Principles

1. **A merge can bounce backwards.** The merge stage is not a terminal gate. When it changes the
   artifact (rebase) or finds a gate stale, it kicks the work back to Test/Review — and the merge
   is only re-attempted after those pass again. "Approved" is not permanent.
2. **Never a silent dead-end.** Every stage either advances, blocks *with a visible reason*, or
   escalates to the operator with a next action. No box is ever stuck with no explanation.
3. **The one who builds it does not grade it.** Acceptance tests are authored by an agent with
   zero shared context with the worker — the same independence principle as the adversarial review.
4. **Observability is a feature, not a nicety.** Run counts, in-progress state, and resilient
   history are first-class requirements, not polish.
5. **Amortize expensive gates at the milestone level.** Architecture review and the acceptance
   suite are defined once per milestone and reused across its issues, not paid per issue.

## The two tiers

An **issue pipeline** nested inside a **milestone pipeline**:

```
MILESTONE  feature/ms-NN ──────────────────────────────────────────────────► develop
  │
  ├─ (A) Arch gate      design review + author the feature-level black-box CONTRACT
  │                     (CLI names, key screen text, API field shapes) + red acceptance suite
  │
  ├─ issue  Work → Test(auto) → Acceptance(indep slice) → Review → Merge ─┐
  ├─ issue  Work → Test(auto) → Acceptance(indep slice) → Review → Merge ─┤─► feature/ms-NN
  ├─ issue  …                                                             ─┘
  │
  ├─ (B) Arch review    implemented-to-spec check against the Gate-A design
  ├─ (C) Full suite     the whole accumulated acceptance suite, green
  └─ (D) Ship           merge feature/ms-NN → develop   (gated on B + C)
```

### Issue-level stages

| Stage | What it is | Gate |
|---|---|---|
| **Work** | Worker implements; writes **unit / internal** tests only — **not** the acceptance tests. | commits pushed |
| **Test** | The repo's normal automated suite (`cargo test` / `pytest`), incl. the worker's unit tests. | suite green + a recorded verdict (#923 backstop) |
| **Acceptance** | The **independent, feature-level** black-box suite (a *separate target/dir*). The worker iterates against it **sealed & in-session** during Work (ORACLE_LOOP.md); the coordinator re-runs it externally against the pushed SHA as the trust gate. Partial-green expected until the feature completes. | its slice green (externally verified) |
| **Review** | Adversarial code review, zero shared context (unchanged). | approved / bounce → Fix |
| **Merge** | **Explicit, driven, bounce-capable** box (see below). The merge queue still sequences underneath but is **hidden** — Merge is a first-class stage the operator drives. | rebased-delta re-gated + CI green → merged into `feature/ms-NN` |

### Milestone-level gates

| Gate | What it is |
|---|---|
| **A — Arch gate** | Before any issue work: **mock-first** (ORACLE_LOOP.md) — an independent agent renders a **viewable mock** in the target medium; the operator reacts to *that* (UX discovery against a cheap artifact, not a text spec); the approved mock + an **amendable, versioned `contract.md`** pin the black-box surface; a second independent agent authors the acceptance suite **red** against the contract. |
| **B — Arch review** | After the issues land: an independent review that the milestone was **implemented to the Gate-A spec** (not just that each issue passed). |
| **C — Full acceptance suite** | The whole accumulated acceptance suite must be **green** — catches the integration gaps *between* issues that per-issue runs miss. |
| **D — Ship** | Merge `feature/ms-NN → develop`, gated on B + C. |

## Independent acceptance testing — the details

> **See [`ORACLE_LOOP.md`](ORACLE_LOOP.md) for the built shape** — the sealed `coord acceptance`
> runner, the in-session worker loop, the external trust gate, framework drivers, and the stall
> protocol. The independence + separate-target facts below are unchanged; the key refinement is that
> the same suite runs **twice** (in-session for speed, externally for trust) and is delivered to the
> worker **read-only / run-only** so it can iterate against it without gaming it.

**When authored:** at the **Gate-A arch gate**, from the spec, by an independent `test-author`
agent — *before* the work, so tests can't be rationalized to match what was built. Extended
**just-in-time** as each issue firms up its slice of the surface — still spec-derived, still a
different agent than the worker.

**The contract is what keeps author and worker in sync without a shared session.** Gate A pins the
exact black-box surface — CLI command names, key screen text, API field shapes. The test-author
writes to the contract; the worker implements to the contract *and* to green. Neither reads the
other's session. If the surface must change mid-flight, the contract is amended at the gate, not
silently in one side.

**When run:**
- **Per issue** — that issue's slice + the growing suite as regression, in its **own box**
  (partial-green expected until the feature completes). This is the "an issue can't sneak through"
  gate.
- **In full at Gate C** — the whole suite green before the milestone ships to develop.

**Where they live:** in the repo's normal tree but as a **separate target/dir**
(`tests/acceptance/…`) with its own runner, reported and gated **separately** from the automated
suite — a visibly distinct box, not folded into Test.

**Cost:** feature-level (per-milestone) authoring — not per-issue — is what keeps this affordable:
one suite per milestone amortized over its issues, plus one arch review per milestone.

## Merge as a driven, bounce-capable stage

The merge **queue stays** — it still sequences PRs dependency-aware and prevents concurrent-merge
races — but it is **demoted to a hidden scheduling detail**. **Merge becomes a first-class box** the
operator drives, showing live sub-state: `queued → rebasing → resolving → re-gating → merged`
(or `bounced`). The button routes instead of dead-ending:

- **Trivial replay** (clean rebase, no semantic change) → merge straight through.
- **Mechanical conflict** → auto-dispatch the `conflict-fix` worker (#241).
- **Semantic conflict** → escalate to the interactive `--merge-of` resolver, with visible status.
- **Non-trivial rebase changed the artifact** → **bounce Test + Review to pending for the rebased
  SHA**, re-run CI, re-review the *delta*, then merge the reviewed SHA. (The keystone: merged
  artifact must equal reviewed artifact.)
- **Stale approval** (approved-SHA ≠ current tip) → auto-kick to re-review; surface "approved N
  commits ago / branch moved." Kill the false green.

## Observability requirements

- **Per-stage run counts** — each box shows `×N` iterations (2 worker runs → `Work ×2`;
  fail-then-pass test history → `Test ✗✓`).
- **In-progress / active-stage indicator** — a live assignment on a stage is visibly *running*
  (spinner / highlight), distinct from an idle red failure. A fix worker behind a red Test box
  must be glanceable.
- **Resilient history** — the Summary tab retries/refreshes on a transient board fetch failure and
  **never** requires a TUI restart to recover (the #632-class "one bad fetch nukes the panel").

## Git model

Adopted in two steps to de-risk the big rewire:

- **Now (Phases 1–3): logical gates on `main`.** Prove the milestone gates (A/B/C) and the merge
  loop while issues still target `main` directly. No branch-model change.
- **Phase 4: `develop` + a feature branch per milestone.** Issues branch off `feature/ms-NN`; the
  feature branch merges to `develop` only when Gate C is green; `develop → main` is a release cut.
  This touches config (per-milestone base branch), dispatch (branch-from), review base-diffs, CI,
  freshness, reconcile, and merge targets — hence last.

## Phasing

1. **Observability** — run counts, in-progress indicator, Summary-tab resilience. Cheap, no
   git-model change, immediate daily relief. *Start here.*
2. **Merge = driven / bounce-capable** — epic #915 (the merge stage becomes a router that rebases,
   resolves, re-gates, and bounces) + the interactive verdict backstop #923.
3. **Independent acceptance testing (the oracle loop)** — the sealed `coord acceptance` runner + one
   framework driver (`tui-tuidriver`), the in-session worker loop + external trust gate, mock-first
   Gate A, and the Gate-C full-suite gate. Detailed build slice in [`ORACLE_LOOP.md`](ORACLE_LOOP.md).
4. **Milestone tier + git model** — arch gates A/B, then `develop` + feature-branch-per-milestone.

## Non-goals / open questions

- **Not** per-issue independent test authoring (too expensive) — feature-level only.
- Auto-drain / autonomous merge stays **off** until trust is regained (unchanged).
- ~~Open: how the Gate-A contract is stored and versioned.~~ **Settled** in ORACLE_LOOP.md: a
  checked-in `tests/acceptance/ms-NN/contract.md` alongside the mock fixtures, amendable in place.

## Issue map

**Phase 1 — Observability** (milestone: TUI: Polish & Observability)
- #925 — per-stage run-count badges (`×N`)
- #926 — in-progress / active-stage indicator
- #927 — Summary tab recovers from a transient fetch failure without a TUI restart

**Phase 2 — Merge = driven / bounce-capable** (epic #915, milestone: Merge Queue v2)
- #928 — hide the merge queue; make Merge an explicit driven box
- #916 — re-gate the delta after a non-trivial rebase (keystone) · #917 — route to the resolver on
  conflict · #918 — stale-approval detection · #919 — verify PR-mergeable + branch-fresh ·
  #920 — sequence high-overlap siblings · #923 — interactive Test-verdict backstop *(shipped)*

**Phases 3–4 — Two-tier milestone pipeline** (epic #929)
- #930 — (A) pre-work architecture gate + black-box contract
- #931 — independent feature-level acceptance test authoring (`type=test-author`)
- #932 — acceptance run stage + Gate-C full-suite gate
- #933 — (B) post-milestone architecture review (built-to-spec)
- #934 — (D) `develop` + feature-branch-per-milestone git model *(Phase 4, last)*
