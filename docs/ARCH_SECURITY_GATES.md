# Architecture & Security Gates — a living arch doc + two new review lenses

> **Status:** design, 2026-07-10. Extends [`PIPELINE_V2.md`](PIPELINE_V2.md)'s milestone tier
> (Gate A / Gate B) and complements [`ORACLE_LOOP.md`](ORACLE_LOOP.md). Where the oracle loop made
> *acceptance* an executable check, this doc does the same move for *architecture* (a graph-queryable
> conformance lint) and adds a *security* lens to the post-work audit. Nothing here changes the
> merge-bounce, observability, or git-model parts of Pipeline v2. Issue map at the bottom.

## Why

The gates we have catch three things well and two things not at all.

**What's covered.** Correctness (adversarial code review), the external/UX surface (Gate A's
mock-first `contract.md`), and behavior (the oracle-loop acceptance suite). Each issue is graded by
an agent with zero shared context with the worker — the independence principle holds.

**The two gaps.**

1. **Nothing gates *internal* architecture before work begins.** Gate A pins the *black-box* surface
   — CLI names, screen text, API field shapes. It says nothing about how the feature fits the system:
   which modules change, what the new boundaries are, which invariants must survive, which
   cross-language seams (the #632 `/board` wire contract class) are touched. A worker can pass every
   existing gate and still land code that is correct, matches the contract, and is *architecturally
   wrong* — a boundary crossed, a god-file grown, platform code in a shared path. We catch that (if at
   all) after merge, which is a whole round-trip too late.

2. **Security is nobody's explicit job.** The adversarial reviewer reads CLAUDE.md + a generic
   checklist, so security is one diffuse concern among many, not a focused pass. This repo has real
   surface: it shells out to `claude -p`, `ssh`, and `gh`; it builds worktree paths from issue
   metadata; the daemon is the *sole* holder of gh/gitlab credentials (#584). Command injection, path
   traversal, secrets leaking into briefings/logs — none of that has a dedicated lens.

## The shape — two concerns × two levels

Architecture and security are orthogonal *concerns*, and each wants to appear at **two levels**: an
up-front milestone gate (paid once, amortized across the milestone's issues — Pipeline v2 Principle 5)
and a post-work audit lens (per issue, cheap, checking *against* the milestone-level design).

|                  | Milestone gate (once, amortized)                          | Post-work audit (per issue)                          |
| ---------------- | --------------------------------------------------------- | ---------------------------------------------------- |
| **Architecture** | **A-arch** — epic architecture guide, approved before dispatch | **`review:architecture`** — conformance vs the guide + living doc (largely graph-linted) |
| **Security**     | **threat-model note** in the guide (focuses the lens)     | **`review:security`** — independent lens on the diff |

The expensive *design* thinking is paid once per epic; the per-issue passes are cheap checks against
that design. This is the same economics as the oracle loop: author the hard artifact once, reuse it
across the issues.

## Principles

Inherits Pipeline v2's five; these are the ones this doc leans on or adds.

1. **Intended map vs actual territory.** The living `ARCHITECTURE.md` is the *intended* structure;
   the graphify graph (`graphify-out/`) is the *actual* structure, auto-derived. Every architecture
   gate reads **both** — the doc says what should be, the graph says what is, and the delta is the
   finding.
2. **The one who builds it does not grade it.** The architecture guide is reviewed by an independent
   agent; the per-issue lenses are independent passes. Same rule as the code reviewer.
3. **Amortize at the milestone.** Architecture design is a per-milestone artifact reused across its
   issues — not re-derived per issue. Security *surface* is per-diff, but the threat-model that
   *focuses* the security lens is milestone-level.
4. **Amendable, not frozen.** The architecture guide is versioned and amendable mid-milestone exactly
   like the oracle-loop `contract.md`. Freezing it up front would re-introduce the waterfall the
   oracle loop exists to kill.
5. **Proportional to milestone size.** The same tuning knob ORACLE_LOOP uses for mocks. A 1-issue
   milestone's "guide" is a paragraph; a cross-repo one gets a full design. Shrink milestones instead
   of adding fixed ceremony.
6. **Mechanize what you can.** Boundary / dependency / layering rules are *graph-queryable*. Turn them
   into a runnable lint (the "architecture oracle") so the agent only judges what doesn't mechanize —
   cheaper, independent, and deterministic, same spirit as the test oracle.

## The keystone — a living per-repo `ARCHITECTURE.md`

This is the one genuinely new artifact. It is to *structure* what [`GOAL.md`](../GOAL.md) is to
*intent*: a short, living, human-and-agent-editable statement of how the repo is *meant* to be built.
coord already ships `docs/ARCHITECTURE.md`; this formalizes that role and replicates it per repo.

**What it contains** (target: one page, not a wiki):

- **Module map + responsibilities** — the intended decomposition (not the file listing; the graph has
  that).
- **Allowed dependencies / layering** — "tui must not import daemon internals," "cli.py routes board
  writes through the daemon (#584, no local DB)," expressed as rules a lint can check.
- **Cross-language / cross-process seams** — the `/board` wire contract (#632), the agent HTTP API,
  the coord↔quadraui path dep. Where two sides must stay hand-mirrored, say so.
- **Standing invariants** — "no platform-specific code in shared paths," "only the coordinator writes
  docs," "no Anthropic SDK — `claude -p` only," "state lives in `~/.coord/`."
- **Known debt + direction** — the god-file decomposition direction (#751/#19), so a diff fighting it
  is a finding.

**How it pairs with the graph.** The doc is the *intended map*; `graphify-out/graph.json` is the
*actual territory*, auto-refreshed by the post-commit/post-checkout hooks. The arch gate reads both.
Anything the doc asserts as a boundary can be checked against the graph's edges — drift becomes
*detectable*, not a matter of someone remembering.

**How it stays alive** (the failure mode for every architecture doc is rot):

- **Gate B folds decisions back in.** The post-milestone architecture review's *output* includes any
  boundary/invariant the milestone established — appended to `ARCHITECTURE.md` as part of closing the
  milestone, not as a separate chore. The doc grows with the system.
- **A drift lint flags divergence.** `coord arch lint` diffs the doc's declared rules against the
  graph and surfaces "doc says boundary X; graph shows an edge crossing it." Run it periodically and
  at Gate B so rot is caught, not accumulated.
- **Only the coordinator writes it** (existing rule — parallel worker doc edits conflict).

## Milestone gate — the epic architecture guide (pre-work)

Today Gate A is **external/UX only**. Split it: keep **A-ext** (the mock + `contract.md`, unchanged)
and add **A-arch**, the internal-architecture half. A-arch has three steps, mirroring the mock-first
flow:

1. **[human + agent] Author (chat).** A `coord milestone chat`-style session, seeded with the living
   `ARCHITECTURE.md` + the relevant graph slice, produces an `## Architecture` section on the epic
   (or `docs/design/ms-NN.md`). It covers: modules touched, new boundaries/interfaces, data flow,
   invariants to preserve, cross-repo/cross-language seams, migration/rollout, **and a threat-model
   note** (what new attack surface this epic opens — the thing the per-issue security lens then hunts
   for). Collaborative on purpose: this is where UX-of-the-architecture discovery happens cheaply,
   before implementation.
2. **[indep] Adversarial arch review.** A fresh agent with zero worker context checks the guide
   against `ARCHITECTURE.md` + the graph: boundary violations, unstated dependencies, missing seams,
   direction fights (god-files). Same independence as the code reviewer.
3. **[human] Approve → unblock dispatch.** `coord milestone dispatch` will not drain the frontier
   until the epic has an approved guide — exactly as Gate A blocks issue dispatch until `contract.md`
   exists.

**Anti-waterfall guardrails** (Principles 4 + 5): the guide is **amendable + versioned** like the
contract — a shipped issue that reveals a better structure amends the guide and re-syncs the
conformance rules — and its depth is **proportional to milestone size**. Don't add ceremony; shrink
milestones.

## Post-work audit — Review becomes independent lenses

Don't fatten the single reviewer. Fan out **independent, single-lens passes** in parallel — the same
diverse-lens pattern the design elsewhere already favors — each **relevance-gated** to control cost:

| Lens | What it checks | Skip when |
| --- | --- | --- |
| **`review:correctness`** | Today's adversarial pass, unchanged. | never |
| **`review:security`** | Injection (shells out to `claude -p`/`ssh`/`gh`), path traversal on worktree paths, secrets in briefings/logs, the daemon's auth/creds surface (#584), authz on new endpoints. Repo-tuned via `reviews.repo_overrides`; the `security-review` skill is the base prompt. | pure-doc / test-only diffs |
| **`review:architecture`** | Diff respects the Gate-A-arch guide + `ARCHITECTURE.md`: no boundary crossed, no `/board` wire field added on only one side of the seam (#632 class), no platform code in shared paths, no fight with the decomposition direction. **Largely graph-linted** (below). | trivial diffs |

Parallel fan-out means no added wall-clock; the oracle loop already cut fix-loop churn, which frees
the budget the extra lenses spend. The expensive design is milestone-amortized.

## The architecture oracle — conformance as a runnable lint

The oracle loop's lever was making acceptance *executable* instead of a subjective read. The same
lever applies to architecture: **boundary, dependency, and layering rules are graph-queryable.**

`coord arch lint` reads the machine-checkable rules declared in `ARCHITECTURE.md` (a small rule block
— allowed/forbidden dependency edges, "these two files must both change together," "no imports from X
in layer Y") and evaluates them against the graph for a given diff or the whole repo. A violation is a
deterministic, independent finding — no agent judgment, no shared context, near-zero cost. The
`review:architecture` agent then handles only the residue that *doesn't* mechanize (is this the right
abstraction? does this belong here?).

This makes the architecture gate cheap enough to run **twice**, oracle-style: as a milestone gate
(Gate B) and as a per-issue lens — the same suite, two jobs, same as `acceptance run` vs
`acceptance record`.

## Rule grammar — `coord arch lint` (settled 2026-07-10)

Grounded in the real graph (`graphify-out/graph.json`: ~11.5k nodes, ~32k links). Two facts fix the
grammar: every **node carries `source_file`**, and every **link carries a `relation`** — the
dependency-bearing ones (`imports`, `imports_from`, `calls`, `uses`, `references`, `inherits`,
`implements`) are all present with resolvable `source`/`target` node ids. So a **layer is a glob over
`source_file`**, and a dependency rule classifies both endpoints of an edge into layers.

**Four rule kinds across two evaluators** — the oracle is graph-based for dep/layering (the clean 80%)
and diff-based for the rest:

| Kind | Evaluator | Example |
|---|---|---|
| `forbidden-dep` / layering | **graph** edges | cli must not import/call `state` directly (#584) |
| `forbidden-content` | **diff text** (regex) | no platform code in shared paths |
| `co-change` / seam-mirror | **diff file-set** (heuristic, not proof) | #632 — one side of the `/board` wire changed, the other didn't |
| `metric-delta` | git line/symbol counts vs base | `app.rs` no-growth (#751) |

**Strawman** (lives under `## Rules` in each repo's `ARCHITECTURE.md`, coordinator-authored):

```yaml
layers:                                # globs over graph node.source_file
  tui:    ["tui/src/**"]
  cli:    ["coord/cli.py"]
  daemon: ["coord/serve_app.py", "coord/agent_app.py"]
  state:  ["coord/state.py", "coord/db.py"]
  brain:  ["coord/brain.py", "coord/dispatch.py", "coord/review.py", "coord/merge_queue.py"]
  wire:   ["coord/_board_mapping.py", "coord/models.py"]

rules:
  - id: cli-no-direct-state
    kind: forbidden-dep
    edges: [imports, imports_from, calls]
    from: cli
    to:   state
    why:  "#584 — board writes route through the daemon seam, not a local DB."
    severity: error
  - id: tui-only-through-wire
    kind: forbidden-dep
    from: tui
    to:   [daemon, state, brain]
    except-to: wire
    severity: error
  # forbidden-content / co-change / metric-delta — Slice 1b
```

**Five settled decisions:**

1. **Selector = glob over `source_file`** — not graphify `community` (unstable integers) or the
   `rationale` god-nodes (explanation layer, not code).
2. **Deny-list first, allow-list later** — enumerate *forbidden* edges to start (cheap adoption);
   graduate a mature layer to default-deny (declare its whole allowed adjacency) once its boundary is
   well understood. Proportional-to-maturity.
3. **Net-new semantics (make-or-break)** — fail only on violations the diff *introduces* vs the base
   SHA; grandfather existing debt. Without this the lint red-flags every issue on legacy coupling.
4. **Run on the pushed SHA, not the dirty worktree** — graphify rebuilds per-commit (git hooks), so a
   fresh graph exists only for committed state. Runs externally, `acceptance record`-style; ToS-clean
   (git + a computed check, never the TTY).
5. **Visible waivers, never silent** — `severity: error|warn` + an `exceptions:` allowlist (rule-id +
   reason, logged) for a deliberate, reviewed boundary change. Mirrors the `--force-merge` ethos.

**Build order:** Slice 1 ships **only** the graph-based `forbidden-dep` evaluator with net-new
semantics + deny-list — highest value, cleanest, no diff-text or heuristics. The `forbidden-content`,
`co-change` (incl. the #632 seam-mirror), and `metric-delta` kinds are **Slice 1b**.

## `coord` command surface

Mirrors the `coord acceptance …` family.

| Subcommand | Who runs it | What it does |
| --- | --- | --- |
| `coord arch guide --milestone NN` | Gate A-arch dispatch | Dispatch the chat author: write the `## Architecture` guide + threat-model note, seeded with `ARCHITECTURE.md` + the graph slice. |
| `coord arch review --milestone NN` | Gate A-arch (indep) | Dispatch the adversarial arch review of the guide. |
| `coord arch approve --milestone NN` | **[human]** | Record sign-off. Until set, `coord milestone dispatch` refuses to drain the frontier. |
| `coord arch lint [--repo R] [--base SHA]` | coord / per-issue lens | The architecture oracle: evaluate the doc's declared rules against the graph for a diff (or whole repo). Deterministic findings. |
| `coord arch sync --milestone NN` | coord (Gate B) | Built-to-spec review **and** fold the milestone's new boundaries/invariants back into `ARCHITECTURE.md`. |

Post-work review fans out to lenses via the existing review dispatch (`review:security`,
`review:architecture`), not new top-level commands.

## Config (`coordinator.yml`)

```yaml
reviews:
  lenses: [correctness, security, architecture]   # which post-work passes fan out
  relevance:
    security: { skip_paths: ["**/*.md", "tests/**"] }
    architecture: { skip_trivial: true }
  repo_overrides:
    claude-coordinator:
      security: |
        - Any new subprocess/ssh/gh call: are args shell-safe and not attacker-influenced?
        - Worktree paths built from issue metadata: path traversal?
        - Does anything but the daemon touch gh/gitlab creds (#584)?
architecture:
  living_doc: docs/ARCHITECTURE.md          # per-repo intended-structure doc
  lint_rules: docs/ARCHITECTURE.md#rules    # the machine-checkable block the oracle reads
```

## Order of operations

Legend (from ORACLE_LOOP): **[indep]** zero-worker-context agent · **[worker]** implementer ·
**[coord]** external · **[human]** you.

**Phase 0 — Milestone kickoff (Gate A), once:**
1. **[indep] `coord acceptance mock`** → the viewable mock + `contract.md` (A-ext, unchanged).
2. **[human+agent] `coord arch guide`** → the architecture guide + threat-model note (A-arch).
3. **[indep] `coord arch review`** → adversarial check of the guide vs `ARCHITECTURE.md` + graph.
4. **[human] `coord arch approve`** → unblocks dispatch. **A-arch blocks issue dispatch** like A-ext
   blocks it on the contract.
5. **[indep] `coord acceptance author`** → the red acceptance suite (unchanged).

**Phase 1 — per issue** (Work → Test → Acceptance → **Review-fan-out** → Merge):
6. Work / Test / Acceptance exactly as ORACLE_LOOP.
7. **[indep] Review fan-out:** `review:correctness` ∥ `review:security` (relevance-gated) ∥
   `review:architecture` (`coord arch lint` + residual agent judgment). Any request-changes → Fix.
8. Merge as Pipeline v2 (rebase re-gates the delta — including re-running the lenses on the rebased
   SHA).

**Phase 2 — milestone close:**
9. **[indep] Gate B (`coord arch sync`):** built-to-spec architecture review **and** fold decisions
   back into `ARCHITECTURE.md`.
10. **[coord] Gate C** (full acceptance suite) + **Gate D** (ship), unchanged.

## Tensions, and how they're answered

- **Cost** (+2 agents/issue): parallel fan-out (no wall-clock), relevance-gating, `coord arch lint`
  handling most arch findings for free, and the oracle loop's churn reduction funding the rest. Design
  is milestone-amortized.
- **Drift** of the living doc: the graph↔doc lint makes it detectable; Gate B folds decisions back in
  as an *output*, so the doc grows with the system instead of rotting.
- **Waterfall**: the guide is amendable + versioned and size-proportional — the same anti-waterfall
  posture the oracle loop already proved.

## What's built vs net-new

**Built / reusable:** `coord milestone chat` (author substrate) · epic holds `## Work order`/meta (add
`## Architecture`) · Gate A/B in the design (#930 / #933 — A-arch **extends** #930, built-to-spec **is**
#933) · `reviews.repo_overrides` (per-repo checklists) · the `security-review` skill · the graphify
graph (the arch-oracle half) · `docs/ARCHITECTURE.md` (coord's living doc, to be formalized +
replicated).

**Net-new:** the per-repo living-`ARCHITECTURE.md` discipline + template · the `coord arch lint`
graph↔doc conformance oracle · the A-arch guide gate that blocks dispatch · the `review:security` and
`review:architecture` lenses + relevance-gating.

## Build slice — mapped to existing issues

**Slice 1 — the living doc + the graph-only lint (the arch oracle, unblocks everything):**
- Formalize `docs/ARCHITECTURE.md` as the per-repo living doc + a `## Rules` block (`layers:` +
  `forbidden-dep` rules). → *new issue (coord first; template for other repos).*
- `coord arch lint` — the **graph-based `forbidden-dep` evaluator only**, with **net-new-vs-base-SHA**
  semantics + deny-list, run on the **pushed SHA**. Deterministic findings. → *new issue (reads
  `graphify-out/`; coord-live).*

**Slice 1b — the remaining rule kinds** (after Slice 1 proves the engine):
- `forbidden-content` (diff-text regex), `co-change`/seam-mirror (#632, heuristic), `metric-delta`
  (god-file no-growth) — each a distinct evaluator under the same `coord arch lint`. → *new issue(s).*

**Slice 2 — the post-work lenses (immediate audit value, no git-model change):**
- `review:security` lens + `reviews.repo_overrides` security block + relevance-gating. → *new issue
  (review dispatch fan-out).*
- `review:architecture` lens = `coord arch lint` + residual agent judgment, wired into the Review
  stage. → *new issue.*

**Slice 3 — the milestone A-arch gate (extends #930):**
- `coord arch guide` (chat author + threat-model note) + `coord arch review` (adversarial) +
  `coord arch approve` blocking `coord milestone dispatch`. → *refits **#930** (adds the
  internal-architecture half to the external contract).* **Needs a release** if it touches worker
  briefings.

**Slice 4 — Gate B fold-back (extends #933):**
- `coord arch sync` — built-to-spec review **and** append the milestone's decisions to
  `ARCHITECTURE.md`. → *refits **#933**.*

**Standalone:** Slices 1–2 run on the *current* flat pipeline with no git-model dependency (immediate
audit value). Slices 3–4 ride the Pipeline v2 milestone tier (epic #929) and land with it.

## Non-goals / open questions

- **Not** per-issue architecture *design* — design is milestone-amortized; per-issue is conformance
  only.
- **Not** a security scanner replacement — the `review:security` lens is an adversarial *reader*, not
  SAST/dependency-CVE tooling; those can feed it but are out of scope here.
- **Open:** how much of architecture genuinely mechanizes into `coord arch lint` rules vs needs agent
  judgment — start with dependency/layering/seam rules (clearly mechanical) and let the residual set
  tell us where the line is.
- **Open:** cross-repo architecture (a milestone spanning coord + quadraui + vimcode) — start
  single-repo; revisit once the single-repo living-doc discipline holds.
