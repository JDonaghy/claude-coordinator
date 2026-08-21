# Forge Independence

**Milestone:** `Forge Independence` (#58) · **Tracking epic:** #1902 · **Status:** planning, Phases 0–2 approved, Phases 3–6 gated

This document is the plan of record for decoupling `coord` from the assumption that
GitHub is simultaneously our issue tracker, our PR host, and our CI provider. It exists
because that assumption became visible on 2026-08-06, and because the cost of removing
it is very unevenly distributed — some of it is nearly free and some of it is a rewrite.

Read this before starting any issue in milestone #58.

---

## 1. Two goals, and why conflating them is the main risk

**Goal 1 — survive a forge outage.** When the forge is unavailable, `coord` should stop
making progress *gracefully* and resume by itself. Today it converts an outage into
manual work.

**Goal 2 — be able to leave a forge.** Today we cannot, at any price short of a rewrite,
because GitHub Issues is not a ticket tracker in this system. It is the message bus.

These have wildly different costs. Goal 1 is a handful of issues and is worth doing
whether or not we ever move. Goal 2 is 20–40 issues and its value is entirely contingent
on a claim we have not yet tested — that some other forge is meaningfully more reliable
for us.

**The failure mode this document is designed to prevent is doing Goal 2's work while
believing we are doing Goal 1's.** Hence the decision gate in §6.

---

## 2. What happened on 2026-08-06

GitHub Actions was in `major_outage` for 7+ hours (incident opened 15:22:49Z). Two
distinct failure signatures, neither of which ran a line of our code:

1. **Never assigned a runner** — jobs cancelled at exactly the 15-minute queue timeout,
   `runner_name` empty, `steps` array empty.
2. **Got a runner, died before checkout** — one step, `Set up job`, failing with
   `Failed to resolve action download info. Error: Service Unavailable`.

**The gates behaved correctly.** Nothing merged untested; the #240 CI gate and the #1525
fail-closed allow-list held all day. The damage was entirely in *retry accounting*:
#1547 walked to `blocked` at `attempts=2/2` with a healthy PR and an approved review,
wedging #1548 and #1551 behind it, and the queue was only saved by an operator stopping
the timer by hand. `vimcode` #609 hit the identical failure in the same window.

> **That defect is epic #1894 (#1891/#1892/#1893), and it is forge-independent.** A GitLab
> pipeline outage wedges the queue through exactly the same code path. **Nothing in this
> document is a substitute for those fixes, and none of them should wait on it.**

### The external precedent

Mitchell Hashimoto announced Ghostty leaving GitHub for the same reason — reliability,
*"almost every day has an X"* — and made the same distinction we are making here:
*"the issue isn't Git, it's the infrastructure we rely on around it: issues, PRs,
Actions, etc."*

Two commitments in that post are worth copying directly, and are reflected in §5:

- the move is **incremental**, dependencies removed one at a time;
- GitHub keeps a **read-only mirror**.

Note that the post deliberately does **not** name a destination. It is evidence that the
problem is real. It is not an endorsement of any particular replacement.

---

## 3. What is actually coupled

Measured against the tree on 2026-08-06. Numbers, not impressions.

| Layer | Where | State | Cost |
|---|---|---|---|
| CI status + merge gate | `coord/ci_store.py` (265 lines), `coord/ci_github.py` (276) | **Already a protocol.** `ci_store: {type: none}` ships today as a working second implementation | **Small** |
| Issues + PRs | `coord/github_ops.py` — 1901 lines, 71 public fns, 31 `gh api` calls, imported by 54 modules | Single chokepoint. `_gh()` is **private**; **zero** raw-argv leaks elsewhere in the tree | Medium |
| Parentage / epics | `coord/parentage_github.py`, `coord/parentage.py`, `coord/milestone_order.py`, `coord/commands/milestone.py` | GitHub **sub-issues GraphQL** | **Hard — may force a design change** |
| Worker + reviewer prompts | `coord/agent.py` — prompt prose and deny-list argv patterns | GitHub verbs hardcoded in prompts; ships on the **agent deploy lane** | Medium |
| CI workflows | `test.yml`, `cargo-test.yml`, `publish.yml`; `vimcode/ci.yml`; `quadraui/ci.yml` | Actions syntax | Small |
| Release | `publish.yml`, `v*` tag trigger, `PYPI_API_TOKEN` repo secret | GitHub-specific | Small but safety-critical |
| Branch protection | required status checks (#1525) | forge settings | Config, not code |
| Docs | `CLAUDE.md`, `docs/**` — "GitHub issue comments as message bus" throughout | prose | Ongoing tax |

**The headline finding is favourable.** `gh` is *referenced* in 41 files, but every
`["gh", ...]` argv construction in the tree lives inside `github_ops.py`, behind 71
named domain functions, and `_gh()` is private with no passthrough callers. The
abstraction point that #21 filed in 2025 as *"a reminder that the abstraction point
exists"* effectively exists. That single fact is what makes Phase 3 a refactor rather
than a rewrite.

---

## 4. Three hazards that shape the plan

### 4.1 Issues are the message bus *and* the audit trail

Every briefing, completion notice, failure report, review verdict and test verdict is an
issue comment carrying `<!-- coord:event=... assignment=... -->` markers that `coord
notify`, reconciliation and verdict capture parse. Migrating issues means migrating the
machine-readable history of every dispatch ever run — and we have already been bitten by
marker parsing that looked fine and was not (#617; the bolded-marker verdict drop).

### 4.2 Issue numbers are load-bearing

Issue numbers appear in branch names (`issue-{N}-*`), commit subjects (`fix(#N)`),
`verify-merge`'s subject heuristic, PR bodies, the drive queue, and every historical
comment. GitLab IIDs will not match GitHub's. **This is the single largest hazard in the
migration and needs an explicit decision before anything moves** (see #1900).

### 4.3 Labels gate the Pipeline

Board and Pipeline membership is label-driven — `coord`, `status:ready`, `tier:*`. These
must survive a move byte-identical or the board silently shows the wrong thing, and
`status:ready` limbo (#359) is already a known failure mode.

---

## 5. The phases

| Phase | Issue | What | Gated? |
|---|---|---|---|
| **P0** | #1896 | Measure forge + CI availability from the seams we already have | No |
| **P1** | #1897 (design: #239) | `GitLabCi` behind the existing `CiStore` | No |
| **P2** | #1898 | Push-mirror all three repos; one runner, one pipeline | No |
| **P3** | #185 → #187, #186 (design: #183) | `IssueStore`/`ForgeStore` split out of `github_ops.py` | **Yes** |
| **P4** | #1899 | Decide whether `coord` owns parentage instead of GitHub GraphQL | **Yes** |
| **P5** | #1900 | Issue + comment history cutover | **Yes** |
| **P6** | #1901 | Worker/reviewer prompts, release pipeline, docs | **Yes** |

### P0 — measure

Instrument three existing seams: `CiStore` reachability, `github_ops._gh()` exit status
and duration, and merge-gate refusals by reason. No new network calls; capture is
strictly best-effort and must never raise or delay a caller.

**Start this first and let it run.** Its value is a function of elapsed wall-clock, so
every week it is not collecting is a week the Phase 3–6 decision stays uninformed.

### P1 — decouple CI

The layer that failed is the layer that is already abstracted. Adding a third `CiStore`
backend is additive, touches no caller, and needs no migration. It also unlocks the
genuinely useful intermediate state: **code on GitHub, CI on GitLab**, which is
Hashimoto's "as incrementally as possible" applied to our actual failure mode.

The hard constraint: GitLab's job statuses must be **mapped into** the existing
fail-closed vocabulary, never used to widen it. `manual` and `created` are not passing.
An unrecognised future status maps to *blocking*. A false block costs one operator
action; a false pass costs a bad merge.

### P2 — mirror, don't move

Git hosting is simultaneously the easiest thing to make redundant and the least valuable
to migrate — git is distributed, every fleet machine has full history, and a git-hosting
outage is survivable. Mirroring buys the insurance without the disruption and gives P1
something real to test against. GitHub stays canonical.

### P3 — the protocol split

`IssueStore` / `ForgeStore` out of `github_ops.py`, per the design already written in
#183. The sleeper asset in that epic is the **local SQLite canonical read view**: it
decouples the board and TUI from any forge, cuts the token waste in `coord plan`, and
gives migrated history somewhere to land. It is worth most of its cost even standing
still.

### P4 — parentage

GitLab has no equivalent of GitHub's sub-issues API with the same shape — epics are
group-level and paid-tier, linked issues are weaker. So the adapter may not be available
at an acceptable price, and the alternative is better anyway: **`coord` owns the
relationship and projects it to the forge, rather than reading it back.** We already
half-own this — `## Work order` is a coord-parsed representation of ordering.

**This is a decision before it is an implementation.**

### P5 — cutover

The point of no return. Numbering decision first, dry-run to a scratch project second,
full marker-parse verification third, one repo at a time — `quadraui` first,
`code-coordinator` last, and never while a drive queue is live.

### P6 — the long tail

Prompts hardcode `gh` verbs and the deny-list is a **security control** whose patterns
fail *open* when they no longer match the tool in use. This ships on the agent deploy
lane: merged is not live until a release plus `coord agent update`.

---

## 6. The decision gate

**Phases 0–2 are unconditional.** Cheap, individually valuable even if we never leave,
fully reversible.

**Phases 3–6 are gated on #1896's data**, and the gate is mechanical rather than a matter
of remembering:

- every gated issue carries `after: #1896` in #1902's `## Work order`, so
  `coord milestone order` reports them as blocked and the ready frontier contains only
  P0–P2;
- none of them carry the `coord` label, so they do not appear in the Pipeline.

**Open the gate** when #1896 has ≥4 weeks of data showing forge unavailability materially
above what a second CI backend already mitigates. **Close it — and archive P3–P6** if it
does not. Either way, record the decision in this document.

The asymmetry justifying the gate: the expensive part of this program (issues, parentage,
prompts, every doc) is *not the part that failed*. If P0 says 2026-08-06 was an outlier,
we will have spent three cheap issues and gained real redundancy. If it says otherwise,
we will already have built the two pieces that are hardest to retrofit later.

---

## 7. What this document does not claim

It does not claim that GitLab, or any other forge, is more reliable than GitHub. We have
one bad day and no baseline. That claim is what #1896 exists to test, and this program is
deliberately structured so that it does not need to be true for Phases 0–2 to pay off.

It also does not claim that leaving GitHub would have prevented 2026-08-06's damage. It
would not have. The queue's retry accounting (#1894) was the proximate cause, it is
forge-independent, and it ships regardless.

---

## 8. P2 runbook — push-mirror, runner, pipeline

**Status: procedure below, not yet executed.** Creating the GitLab group, wiring
push mirroring, registering a runner and provisioning `GITLAB_TOKEN` onto the
daemon host and agent machines all require a real GitLab account plus shell
access to those machines — neither is available to an automated worker session.
This section is the exact procedure an operator (or a future session that *has*
those credentials) runs to close out #1898's acceptance criteria. Nothing here
changes `coordinator.yml` or any coord code path, per §5's P2 scope.

### 8.1 Namespace mapping

`coordinator.yml` keys each repo as `github: <owner>/<name>` (see
`coordinator.example.yml`) — GitHub's `owner/name` slug. GitLab's equivalent is
`namespace/project`, and namespaces are a real hierarchy (user, group, or
nested group), not just an owner string. Map 1:1 by name under a single group
so the three mirrors read the same as their GitHub counterparts:

| `coordinator.yml` `github:` | GitLab project |
|---|---|
| `JDonaghy/claude-coordinator` | `jdonaghy-mirrors/claude-coordinator` |
| `JDonaghy/vimcode` | `jdonaghy-mirrors/vimcode` |
| `JDonaghy/quadraui` | `jdonaghy-mirrors/quadraui` |

`jdonaghy-mirrors` is a dedicated GitLab group, not the personal namespace —
that keeps mirror projects visibly separate from anything hand-authored on
GitLab, and scopes the runner registration and `GITLAB_TOKEN` (§8.5) to one
group instead of an entire account. This mapping is documentation only; it
does not touch `coordinator.yml`, which has no GitLab-side field to add (P2 is
explicitly out of scope for that — see the issue's "Explicitly out of scope").

### 8.2 Create the projects and push mirror

Per repo, for all three (`claude-coordinator`, `vimcode`, `quadraui`):

1. GitLab UI → `jdonaghy-mirrors` group → **New project** → **Import project**
   → **Repository by URL**, source
   `https://github.com/JDonaghy/<name>.git`. This does the initial clone; the
   next step turns it into a *standing* mirror rather than a one-time import.
2. Project → **Settings → Repository → Mirroring repositories**:
   - Git repository URL: `https://github.com/JDonaghy/<name>.git`
   - Mirror direction: **Pull** — GitLab pulls from GitHub, not the reverse.
     This is what "GitHub stays canonical" means mechanically: GitLab only
     ever reads from GitHub, so the mirror cannot diverge and a GitLab-side
     push/merge is a no-op from GitHub's perspective (and should be disabled
     entirely — protect all mirrored branches on the GitLab side too).
   - Authentication: a GitHub PAT with `repo` read scope (public repos, so
     `public_repo` is sufficient), entered once at setup time, not stored in
     any file this repo tracks.
   - Trigger: **Mirror repository periodically** (GitLab's default poll,
     currently 5 min minimum on gitlab.com) — sufficient for a proving ground;
     revisit if P5 ever needs push-triggered freshness.
3. Confirm the mirror ran once (Settings → Repository → Mirroring shows a
   green "Successfully updated" with a timestamp) before moving on.

### 8.3 Verify mirror freshness

The acceptance criterion is a *documented command*, not a dashboard click.
Compare `HEAD` on both remotes directly — no GitLab API token needed, since
both repos are public:

```bash
# For each of claude-coordinator, vimcode, quadraui:
diff <(git ls-remote https://github.com/JDonaghy/<name>.git HEAD) \
     <(git ls-remote https://gitlab.com/jdonaghy-mirrors/<name>.git HEAD)
# Empty output = mirror is current. A diff means the poll interval hasn't
# caught up yet (expect up to ~5 min lag) or the mirror job is failing —
# check Settings → Repository → Mirroring on the GitLab project in that case.
```

Wrap the three-repo loop in a one-liner for routine checks:

```bash
for name in claude-coordinator vimcode quadraui; do
  echo -n "$name: "
  diff <(git ls-remote https://github.com/JDonaghy/$name.git HEAD | cut -f1) \
       <(git ls-remote https://gitlab.com/jdonaghy-mirrors/$name.git HEAD | cut -f1) \
       >/dev/null && echo "in sync" || echo "STALE or failing — check GitLab mirror settings"
done
```

### 8.4 Runner + pipeline — `quadraui` as the proving ground

`quadraui` is the right first repo per the issue: a single `ci.yml`, no PyPI
publish step (unlike `code-coordinator`'s `publish.yml`), no webapp/browser
capability requirement (unlike `coord web`'s Playwright suite). Register a
GitLab Runner scoped to the `jdonaghy-mirrors` group (Settings → CI/CD →
Runners → group runner, not shared — the security note in §8.6 is why),
tagged `coord-proving-ground`, then add `.gitlab-ci.yml` at the root of the
`quadraui` mirror project (not the GitHub repo — this is GitLab-side only,
consistent with "no coord changes"):

```yaml
# .gitlab-ci.yml — proving-ground pipeline, mirrors quadraui/ci.yml's cargo
# test step. Kept deliberately minimal: this validates that GitLab CI *works*
# against our mirror, not full CI parity with GitHub Actions.
stages:
  - test

cargo-test:
  stage: test
  image: rust:1-slim
  tags:
    - coord-proving-ground
  cache:
    key: "$CI_COMMIT_REF_SLUG"
    paths:
      - .cargo/registry
      - target/
  variables:
    CARGO_HOME: "$CI_PROJECT_DIR/.cargo"
  script:
    - cargo build --workspace
    - cargo test --workspace
  rules:
    - if: '$CI_PIPELINE_SOURCE == "push"'
```

Push a trivial commit to the GitHub repo, let the mirror pull it in (§8.3),
and confirm a green pipeline appears under the GitLab project's **CI/CD →
Pipelines**. That satisfies "one repo has a working `.gitlab-ci.yml`
producing green pipelines on push" — the push is to GitHub, mirrored to
GitLab, which then runs its own pipeline; there is no push directly to
GitLab in this model, matching "GitHub stays canonical."

### 8.5 `GITLAB_TOKEN` provisioning

Same posture as every other fleet secret (`PYPI_API_TOKEN` in
`.github/workflows/publish.yml`, `github-token` in Key Vault per
`docs/EPHEMERAL_WORKERS.md` §"One-time setup") — **environment variable,
never a config file, never a log line**:

- Scope: a GitLab Personal Access Token with `read_api` + `read_repository`
  on the `jdonaghy-mirrors` group only — enough to query mirror status and
  pipeline results, nothing to write with. This phase does not need coord to
  push or trigger anything, so a broader scope is unjustified.
- Where it's needed: the daemon host, if/when a future phase (P1's
  `GitLabCi` backend) queries pipeline status from `coord`; not needed by P2
  itself, since mirror and pipeline verification here are done by hand (§8.3,
  §8.4). Provision ahead of P1 landing so that phase isn't blocked on a
  separate secrets round-trip: `export GITLAB_TOKEN=...` in the daemon host's
  shell profile, same mechanism as any other fleet env var — **not** added to
  `coordinator.yml`, which has no secret fields by design.
- Verification: `curl -sf -H "PRIVATE-TOKEN: $GITLAB_TOKEN" \
  https://gitlab.com/api/v4/groups/jdonaghy-mirrors" >/dev/null && echo ok` —
  confirms the token resolves and is scoped correctly, without ever echoing
  the token itself into a log.

### 8.6 Security note

Per the issue: all three repos are public, so this stands up a second public
surface (the GitLab mirror) plus a CI runner that executes arbitrary
repo-defined pipeline code. The same reasoning `docs/EPHEMERAL_WORKERS.md`
applies to the tailnet ACL applies here — **scope the runner to what it
actually needs and nothing else**:

- Group runner, not shared/instance runner — confines it to
  `jdonaghy-mirrors`, so a pipeline in an unrelated GitLab project can never
  pick it up.
- The runner host has **no** access to the coord fleet's tailnet, Key Vault,
  or `~/.coord/` on any machine — it only needs to clone the mirror and run
  `cargo build`/`cargo test`. Provision it as an isolated host or ephemeral
  container, not a machine that's already a fleet member.
- `GITLAB_TOKEN` (§8.5) is read-only and group-scoped; it cannot register or
  reconfigure runners even if leaked.
- Because push mirroring is **pull**-direction only (§8.2), a compromised
  GitLab project cannot write back to GitHub — the worst case is a bad
  pipeline result or resource abuse on the runner host, not corrupted source.

### 8.7 Teardown

If the proving ground is abandoned or the decision gate (§6) closes without
opening:

1. GitLab project → Settings → Repository → Mirroring → remove the mirror
   entry (stops the pull; the project itself keeps its last-synced content
   until deleted).
2. GitLab group → Settings → CI/CD → Runners → remove the
   `coord-proving-ground` runner registration; deprovision its host.
3. Revoke the `GITLAB_TOKEN` PAT in GitLab → Preferences → Access Tokens, then
   `unset GITLAB_TOKEN` / remove it from the daemon host's shell profile.
4. Optionally delete the `jdonaghy-mirrors` group once all three projects are
   confirmed unneeded — not required immediately, since an unused mirror with
   no runner and a revoked token is inert.

No step here touches GitHub, `coordinator.yml`, or any coord code path —
teardown is as reversible as setup was additive.

---

## 9. References

- **#1902** — tracking epic, work order, decision gate
- **#1894** (#1891/#1892/#1893) — queue resilience; forge-independent, ships regardless
- **#183** — `IssueStore`/`ForgeStore` design; **#239** — `CiStore` design; **#21** — the 2025 origin note
- **#1525** — the fail-closed CI allow-list every phase must preserve
- **#1200** / milestone 41 — the sub-issues model P4 reconsiders
- [`docs/OPERATING_GOTCHAS.md`](OPERATING_GOTCHAS.md) — the deploy-lane matrix P6 depends on
- [`docs/ARCHITECTURE.md`](ARCHITECTURE.md) — where each surface named in §3 actually runs
- Mitchell Hashimoto, *Ghostty is Leaving GitHub* — https://mitchellh.com/writing/ghostty-leaving-github
