# Platform Evolution — Component Boundaries & Migration Sequence

> **Status:** design draft / RFC (2026-06-29). Captures the target architecture for evolving
> claude-coordinator from a single-fleet local tool into a cloud-coordinated, multi-engineer
> platform with an eventual non-technical **customer portal**. Not committed direction — a
> thinking artifact to react to.

## Intent

A **cloud coordination API over Postgres**, served to engineer clients (coord-tui/CLI) and an
eventual customer web GUI; a **dial-out fleet of runners** (evolved `coord agent`) that bring
their own Claude subscription + machines and execute work locally. Engineers are a small,
**trusted + contactable** pool (people you know, or a company's employees/contractors).
Customers describe features in plain language and never see git. Single-org per deployment;
self-hostable as the endgame.

## The load-bearing principle: two planes

| Plane | Lives | Holds |
|---|---|---|
| **Control / coordination** | cloud-hostable | board state, gates, merge *sequencing*, forge-metadata seam, auth, RBAC, brain/planning |
| **Execution + interactive I/O** | fleet-local | git working tree, `claude -p`, the interactive **PTY** |

Today's daemon (`coord/serve_app.py` + `coord/agent.py`) **fuses both**. The entire migration is
the work of *separating* them. The interactive PTY is **never** in the API path — it always was,
and stays, a direct operator↔own-machine tmux/ssh stream.

## Target components

1. **Cloud Coordination API** *(extracted from today's daemon)* — HTTP service over Postgres.
   - **Owns:** board state (assignments, issues, merge_plan/queue, plans, proposals, ownership/claims);
     brain/planning; merge-queue **gate evaluation + sequencing** (the *decisions*); the **forge-metadata
     seam** (`IForgeProvider`: issues, comments=bus, PR open/merge-button, CI-status read); auth (OIDC);
     authz (RBAC: customer / engineer / admin + row-level ownership).
   - **Does NOT own:** git working-tree ops, `claude -p`, the interactive PTY.
   - Shape: API-over-SQL coordination service. The holder of the **coordination token** for the forge.
   - **Agent-facing surface (MCP) is a generated view of this API, not a new component.** The daemon's
     OpenAPI 3 spec (`coord/openapi.py`, #757 — introspected from the dataclasses + SQLite DDL, so it can't
     drift) is the machine-readable contract for both the TS/Rust client codegen (#750) **and** an eventual
     **MCP server** exposing `board`/`assign`/`merge`/`report_result` as tools to external Claude/agent
     clients. This is the end-state #478 anticipated and #590 folded in: MCP is the *agent-facing form* of
     the write path, slotting into the `IssueStore`/daemon seam (`coord/issue_store.py:92` —
     "backend can be swapped to MCP without changing the call sites") — not a parallel service. Build it
     when external agents need to drive the Cloud API; until then it stays parked behind the seam.
2. **Postgres** — the store. Local on dellserver first → Azure PG Flexible Server later.
3. **Fleet Runner** *(evolved `coord agent`, flipped push→pull)*.
   - **Owns:** lease/claim authorized work; worktree setup; run `claude -p` (headless) **and** launch/manage
     interactive tmux sessions; git working-tree ops (clone/fetch/rebase/resolve/push); STATUS/STUCK capture;
     **checkpoint-push cadence**; report verdicts/results up.
   - **Holds locally:** git **push** creds (per-machine/per-engineer), the Claude subscription OAuth, toolchains.
   - **Talks to:** the Cloud API, **outbound only** (no inbound port — kills the NAT/multi-engineer problem).
4. **Operator clients** — `coord-tui`, `coord` CLI.
   - Render board state from the Cloud API (OIDC instead of today's bearer token).
   - Launch/attach interactive sessions **directly** to the operator's **own** fleet (tmux/ssh); record
     verdicts via the API (`report-result`, never TTY-scrape — ToS §3.7).
5. **Customer Web GUI** *(later)* — a **separate** OIDC-authed client of the Cloud API, RBAC-scoped to the
   customer plane (projects/features, intake chat). **Never** reaches the engineer/execution plane.

## Seams

| Seam | Transport / auth | Carries |
|---|---|---|
| Operator / Customer ↔ Cloud API | HTTPS + **OIDC** (humans), RBAC enforced | state reads, claims, intake, verdicts |
| **Runner ↔ Cloud API** | outbound HTTPS, **machine/enrollment auth** | lease work, report status, checkpoint, stream STATUS |
| Cloud API ↔ Forge | `IForgeProvider` (GitHub/GitLab), coordination token **here** | issues, comments, PR open/merge, CI status — **GitHub↔GitLab swap point** |
| Runner ↔ Forge (git) | plain git, **local push creds** | clone/fetch/push — forge-agnostic, needs no abstraction |
| Cloud API ↔ Postgres | SQL (managed identity later) | persistence |
| **Operator ↔ own fleet (PTY)** | **direct tmux/ssh** — *not through the API* | live interactive keystrokes + render |

## What moves out of the daemon (today → target)

| Today (`serve_app.py` / `agent.py`) | Target |
|---|---|
| `/board` read | Cloud API read endpoint (stays) |
| `/cmd` board mutations | Cloud API endpoints (stays) |
| `post_merge` → `coord merge` (**git ops + gh, on daemon host**) | **SPLIT:** gate-eval + sequencing + PR-merge-button → Cloud API; **git rebase/conflict/push → Runner task** |
| `_tick_loop` (reconcile, auto-drain, issues-sync) | Cloud API background workers; git-needing parts dispatch to runners |
| dispatch (`POST` to agent:7433) | **inverted:** runners **pull/lease** from the Cloud API |
| `coord agent` (:7433 server, spawns `claude -p`) | the dial-out **Runner** |

**Keystone extraction:** peel **git/merge execution** off the daemon into a runner task, and invert
dispatch to a **pull/lease** model. Everything else (cloud-hosting, multi-engineer, no-inbound-port)
depends on this one seam. A thin "DB API" alone unlocks nothing.

## Concurrency model (per-path — not blanket-optimistic)

- **Today = single writer** (SQLite, the #584 daemon-is-sole-writer design). **No concurrency control is
  needed until a second writer exists** (multiple API instances, or runners leasing). Building it earlier =
  speculative code with nothing to exercise it.
- **Cheap & early:** add `version` / `updated_at` columns in the SQLite→PG migration. Defer the control
  *logic* until the second writer lands.
- **Work-claim / lease path:** `SELECT … FOR UPDATE SKIP LOCKED` + **lease-with-heartbeat**. This is the
  idiomatic Postgres job-queue claim (contention-free; each runner grabs a different row). An expired lease →
  work returns to claimable = **micro-AWOL recovery** for free.
- **General board mutations:** **optimistic** (version check, retry on conflict). Low contention, simple.
- **Merge:** keep **one logical merge-driver** (centralized merge authority — already your stance). No fancy
  concurrency; serialized by design.

## Interactive sessions — preserved by construction

The API never carried keystrokes; it held the assignment record + verdict only. So:
- **Coordination plane → Cloud API:** launch request, session metadata (machine, `coord-<aid>`, type),
  liveness, finalize backstop, verdict.
- **Interactive I/O plane → direct & local:** embedded terminal (quadraui engine), live `claude` PTY,
  `coord reattach` (`ssh -t`), typing. **Unchanged.** Engineers attach only to their **own** fleet, so no
  cross-boundary inbound is ever needed (cross-engineer collaboration = screen share, by decision).
- Bonus: runner-reported liveness can **replace** the fragile `coord sessions --remote` ssh-probe
  (the `ConnectTimeout=4` false-negative), while attach stays the same direct experience.

## AWOL handoff (engineer goes dark)

Durable unit of work = **pushed branch (platform-owned origin) + cloud context digest (#603)** — *not* the
engineer's machine. Recovery: revoke claim (cloud) → reassign → new fleet fetches the branch → inherits the
context digest → resumes (the existing `--fix-of`/`--rework-of`/reattach-to-branch capability). Residual loss
= un-pushed work since last checkpoint, bounded by **checkpoint-push cadence**. Same machinery hardens the
**single-engineer** case (machine dies → recover elsewhere).

## Phased sequence (reordered from the infra-first draft)

1. **Postgres behind the existing daemon, local on dellserver.** Migrate SQLite→PG, *no topology change*.
   Add `version`/`updated_at` columns; **no concurrency logic yet**. Safety net = existing suite + black-box
   tests. Budget for SQLite-isms (bool-as-int #546/#632, upserts, dialect). *(low-regret — do first)*
2. **Customer-loop spike on the now-Postgres stack.** Project/Feature model over issues; intake chat →
   milestones (reuse the refinement chat / brain, #319); design the **up-mapping status vocabulary**. Prove
   the bridge with you as both customer and engineer. **Validates the product hypothesis on cheap infra —
   before sinking weeks into extraction.**
3. **Extract the Cloud API + invert dispatch to pull/lease + peel git/merge execution into a runner task.**
   Concurrency control lands **here** (skip-locked leases + heartbeat for claims; optimistic for board
   mutations; single merge-driver). *(the real architectural work)*
4. **Multi-engineer:** runner enrollment, RBAC (customer/engineer/admin + ownership), AWOL handoff
   (lease expiry + auto-fetch).
5. **Cloud hosting** (Azure PG + API; Entra managed identity → secrets out of config; private endpoint) +
   **customer web GUI** as a first-class client.
6. **Company-deployable:** config-driven IdP (Entra External ID / arbitrary OIDC), `IForgeProvider` for
   GitLab/Bitbucket, install/upgrade story.

Policy discipline throughout: **commit narrow in policy, stay general in the data model + seams** — every
later relaxation (milestone-claiming, open pool, self-host) becomes a config/policy change, not a rewrite.

## Where the customer portal sits

The infra sequence builds the **foundation** the portal needs — it is **not** the portal. The portal is a
**layer above**: customer domain model (Project/Feature, git hidden) + intake LLM chat + up-mapping
vocabulary + the web GUI + the customer/engineer **wall** (RBAC). Much of its *hard substrate* arrives en
route or already exists — OIDC auth (built for all clients), intake chat (refinement #319), per-issue context
(#603), RBAC (built for multi-engineer; customer is just another role). So the portal ≈ a new client + a
domain-model layer + a status-translation vocabulary on a foundation you'll have built — **but the
product-risky bridge (does plain-language → milestones → shippable actually work?) is deferred to the very
end unless the Phase-2 spike pulls it forward.**

## Phase-2 spike: the customer loop, concretely

The spike exists to answer one question cheaply: **does plain-language → decomposition →
engineer-executable issues → git-free progress actually work?** Build the bridge, nothing else.

### Domain model (and how it maps to today)

| Level | Visible to | New / existing | Maps to |
|---|---|---|---|
| **Project** | customer | new (cloud DB) | named container scoping features → one or more repos |
| **Feature** | customer | new (cloud DB) | the unit of customer intent (one NL request) |
| **Milestone** | engineer | new (cloud DB) | the chat's structured decomposition; the engineer-fleshing input |
| **Issue** | engineer | **existing** (GitHub + `issues` table) | the executable work unit the current pipeline runs |

**Key decision:** the Project/Feature/Milestone hierarchy lives in the **cloud DB**, not GitHub. GitHub
milestones are per-repo and flat — a customer Feature can span repos, so the grouping must be ours. Issues
stay in GitHub (the leaves), each carrying a link upward.

Minimal spike tables:
```
projects(id, name, owner, repos[])
features(id, project_id, title, description, spec, status, created_by)
milestones(id, feature_id, title, scope, status)             -- chat output
issue_links(issue_number, repo, milestone_id)                -- leaf → hierarchy
feature_questions(id, feature_id, question, answer, status)  -- the "needs your input" channel
```

### Intake → decomposition → accept (reuses propose/approve)

1. Customer opens a Feature, describes it in NL chat.
2. The chat (reuse the **refinement chat / brain**, #319) does **requirements elicitation** — acceptance
   criteria, examples, scope boundaries — then emits a **Feature spec + a proposed decomposition**
   (milestones → draft issues: titles, scope, target repo(s), dependencies, rough size). It gets
   `coordinator.yml` (repo topology + deps) as context so it targets the right repos.
3. **Engineer gate** — the decomposition is a *proposal*, not a fait accompli; an engineer reviews/edits/
   accepts. **This is the existing `coord plan → coord approve` propose/approve pattern** — customer intake
   just becomes another producer of `proposals`, and the acceptance step *is* "the engineer fleshes it out."
4. On accept: issues are created in GitHub via the forge seam, labeled (`status:ready`), linked to the
   milestone, dependencies set. **The existing pipeline (plan/assign → work → test → review → merge) takes
   over unchanged.**

So the customer plane is a *front-end that emits proposals*; the existing approve→dispatch machinery is the
bridge, and the engineer-acceptance gate is exactly where customer-language becomes engineer-executable.

### Up-mapping: engineer state → customer vocabulary

Leaf/assignment states roll **up** (issue → milestone → feature → project) and collapse into a git-free
vocabulary. Only **customer-actionable** or **terminal** states cross the wall:

| Engineer-side reality | Customer sees |
|---|---|
| intake not yet accepted | **Describing** |
| accepted, issues ready, not started | **Planned** |
| work in progress (incl. request-changes, rebase, CI churn) | **In progress** (+ % = items shipped / total) |
| testing + review | **Quality check** |
| engineer raised a `feature_question` | **Needs your input** ⟵ the only state that *demands* the customer |
| merged / done | **Shipped** |
| stalled — no movement past the On-hold threshold (below) | **On hold** (engineer contactability kicks in) |

Progress % = "N of M work-items shipped" — honest, git-free, GitHub-milestone-style. Request-changes,
conflicts, CI failures **never** surface; they live inside *In progress* / *Quality check*.

**Precedence (mixed-state features — first match wins).** A Feature's items are usually in several states at
once; the single customer-facing word is chosen by salience:

1. any open customer question → **Needs your input**
2. any item stalled past the On-hold threshold → **On hold**
3. all items merged → **Shipped**
4. all remaining items in testing/review → **Quality check**
5. any item in progress/ready → **In progress** (+ %)
6. not yet accepted → **Describing** (chatting) / **Planned** (accepted, unstarted)

**On-hold threshold — business-time, not wall-clock.** An engineer-side stall (worker failed, conflict needs
a human) is hidden inside *In progress* until it has had **no movement for ~1 business day**, then surfaces as
**On hold**. The clock ticks during working hours only and **pauses nights, weekends, and holidays** — a
Friday-evening hiccup must not flip to "On hold" on Saturday when no one is working. For the spike, a
weekday-aware elapsed-working-time check (skip Sat/Sun) is enough; a per-deployment working calendar +
holidays is a config refinement (each org differs). The threshold value, and whether **On hold** surfaces to
customers at all, are product calls — this is the vocabulary's most opinionated knob.

### The one genuinely new primitive: the customer-question channel

"Needs your input" needs a way for an engineer (or a worker via `STUCK:`) to **raise a customer-facing
question mid-execution** and pause that thread until answered — the `feature_questions` table + a surfacing
hook. It's the only piece with no existing analogue (the issue-comment bus + `STUCK:` lines are the raw
material). It's the inverse of contactability: the platform reaching the *customer*, not the engineer.

### Spike scope — build the bridge, nothing else

- **Build:** the minimal tables; `coord feature describe` (intake chat → proposal); reuse `coord approve` to
  accept → create linked issues; `coord feature status` (or a TUI panel) rendering the up-mapped customer
  view. Drive it from CLI/TUI with **you playing both roles**.
- **Do NOT build yet:** auth, OIDC, RBAC, web GUI, multi-tenancy — that's foundation/portal work, not the bridge.
- **Dogfood target:** pick a real thing to build in one of your repos, describe it as a "customer," accept the
  decomposition, let the pipeline run, watch the customer view track it to **Shipped**.

**Success criteria**
- Decomposition needed only *light* engineer editing (down-mapping is good enough).
- The acceptance step felt *worth it* vs. hand-writing issues (value proposition holds).
- The customer view tracked progress *accurately and without leaking git* (up-mapping works).

**Expected failure modes (learning, not surprises):** chat over/under-decomposes (prompt iteration);
cross-repo targeting needs topology in context; intra-feature dependency ordering (lean on the brain's
existing conflict/dep inference); the question-channel surfacing UX.
