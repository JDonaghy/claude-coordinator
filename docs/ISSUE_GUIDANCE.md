# Issue guidance for claude-coordinator

This file is the source of truth for what a refined issue in this repo should contain. It is read at `new-issue-chat` time (per `Repo.resolve_new_issue_guidance` in `coord/models.py`) and used to shape the agent's questions and the finalised issue body.

When in doubt, keep sections short and observable. A reviewer who has never seen this conversation should be able to read the issue and know what passing looks like.

## Title

- Active voice. Imperative for fixes ("Fix X"), descriptive noun phrase for features ("Per-repo issue templates").
- ≤ 80 characters.
- For phased work, include the phase: `(Phase A of #N)`.

## Required sections

The agent should ask focused questions to populate each of the following. Sections without enough information yet should be marked `(TBD — to refine before dispatch)` rather than omitted.

### What

One paragraph describing what the change is. Concrete enough that a reader who only reads this section understands the surface area.

### Why now

What is broken / missing / blocked without this change? Include any deadlines, blocking work, or recent failures that motivate it. If the answer is "nice to have, no pressure," say so — that calibrates priority correctly.

### Design

How the change works mechanically. For new features: the API shape, the data flow, the new state. For bugs: the root cause and the fix direction. Reference the existing patterns or files the worker should follow.

### Acceptance criteria

Bulleted list of observable, testable conditions. Each line should be falsifiable — a reviewer can run something or read something and decide "yes" or "no".

Examples:
- ✅ "Running `coord pull-artifact <id>` against a completed assignment populates `~/.coord/artifacts/<repo>/<branch>/` with the configured globs."
- ❌ "Artifacts work correctly." (Not observable.)
- ✅ "TUI Test stage shows the artifact badge within one frame of the manifest arriving."
- ❌ "Performance is acceptable." (Not falsifiable.)

### Smoke tests

What the user should run / look at locally to confirm the change works in practice. Include:
- The exact command(s) — `cd ~/src/quadraui && cargo run --example tui_diff_view`
- What to look for — "side-by-side panes align, gutter colours visible, j/k scroll moves both panes together"
- Any setup needed — "must have `artifact_paths` set in `coordinator.yml`"

If the change can't be smoke-tested without infrastructure (e.g. depends on a not-yet-configured remote), say so explicitly. That's a `coord test --skipped` candidate.

### Acceptance contract (oracle-loop milestones)

For an issue inside an **oracle-loop milestone** ([`ORACLE_LOOP.md`](ORACLE_LOOP.md)), the black-box
acceptance tests are **authored independently** (`type=test-author`), not by the worker — so **do
not** ask the worker to write them. Instead, point the issue at its slice of the milestone's
`tests/acceptance/ms-NN/contract.md`, and phrase the acceptance criteria as the **contract surface**
the worker must make green (CLI names, key screen text, API field shapes). The worker still writes
its own unit/internal tests and must not edit `tests/acceptance/**`.

### Out of scope

What this issue intentionally does NOT cover. Link to follow-up issues if the boundary is "Phase A vs Phase B".

## Recommended sections (optional but useful)

### Depends on

Other issues / PRs that must land first. Cross-link explicitly so the dispatcher can sequence work.

### Pointers

File paths + line numbers for the relevant existing code. Saves the worker grepping. Format as `coord/agent.py:823` (clickable in many UIs).

### Tier

Model-by-label routing is driven by `models.labels` in `coordinator.yml` (empty by default — these labels only change the model where you configure the mapping; otherwise everything uses `models.default`). A common convention:

- `tier:small` — single-file mechanical change. 1-3 line predicate/classifier edits. Map to haiku.
- `tier:large` — multi-module refactor, algorithm-heavy work. Map to opus.
- No matching label — falls through to `models.default` (sonnet).

Configure it under `models.labels` (e.g. `tier:small: haiku`, `tier:large: opus`). The dispatcher (human or AI coordinator) sets the label when filing; you can override at refinement time.

## Anti-patterns to avoid

- **"And also fix X"** — bundle scope creep into a separate follow-up.
- **Reviewer notes for phased work** — if a PR covers only Phase B, the issue body must say so, otherwise reviewers correctly flag missing Phase C work as a `🔴` finding (see #334).
- **Vague acceptance** — "works as expected", "performance is good", "feels right". Replace with something a reviewer can run or measure.
- **Hidden dependencies** — if the work requires a quadraui branch that isn't merged yet, name the branch explicitly. Path-dep cascades cost real money in fix iterations.

## Style

- Markdown, no HTML except for the occasional `<details>` summary.
- Use code fences with language hints (` ```rust `, ` ```yaml `).
- Prefer fully-qualified file paths over relative when referring to the codebase from the issue body.

## After refinement

The user marks refinement complete by closing the chat with "Done". The agent's finaliser step posts the issue body to GitHub. The user then:

1. Reviews the posted issue.
2. Applies tier and any other domain labels.
3. Adds `status:ready` when the issue is ready to dispatch.
4. Dispatches via `coord plan` (label-based) or `coord assign` (direct).
