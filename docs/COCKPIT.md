# The AI-Engineering Cockpit — Product Thesis & Program

> **A trust surface for AI software engineering: a cross-platform, VSCode-feel cockpit where a
> human developer stays genuinely in the loop — reading the code, driving the gates — instead of
> an unattended autopilot no one reviews.**
>
> _Status: RFC / proposed — 2026-07-23; reconciled with the web control-center program 2026-07-28
> (see §7)._ This is the **intent** layer (cf. [`GOAL.md`](../GOAL.md)) — the standing argument for
> *why* a trust surface wins. It is deliberately **surface-independent**: GOAL.md's near-term slot
> now belongs to the web control center, and §7 explains why that does not change this thesis.

## 1. The wedge

Risk-averse engineering orgs will not adopt "vibe coding" — a flow where an AI writes code and
**nobody reads it** before it merges. The liability, the audit gap, and the loss of engineering
judgment are non-starters for them. Yet the productivity ceiling of AI engineering is enormous.

The opening is the **opposite posture**: a tool where a human developer is *demonstrably* in the
loop — the AI does the work, but a person **reads the actual diffs and approves each gate** before
anything lands. If we build that tool well *and* developers are comfortable living in it, we own a
segment the autopilots structurally cannot serve.

## 2. Why this is defensible

The nearest competitors (Devin-class desktops) sell **unattended, flat-rate** autonomy. That is the
exact thing a cautious org distrusts, and it is a licensing/positioning line they can't easily cross
without abandoning their pitch. Our moat is the inverse and is already half-built:

- **Gates.** The pipeline already enforces `Work → Test → Review → Merge` with an **adversarial AI
  reviewer** on a *different* machine with zero shared context, plus a merge gate on CI/tests.
- **A human in the seat.** The board already *launches and drives* human-attended interactive
  sessions across the fleet (the GOAL north star).
- **What's missing to close the story:** the human's oversight of the *code itself* is still
  out-of-app (they go read the PR on GitHub). Bring **reading and approving the code inside the
  cockpit**, and "someone reviewed this" becomes literally, provably true at the gate.

## 3. Three pillars

### Pillar 1 — A project-scoped cockpit (legibility)

Today the TUI is already one-*view*-at-a-time (a VSCode-style ActivityBar switches Board / Pipeline /
Sessions / …), but every view **unions and stacks all repos at once** — the source of the "too much
on screen" feeling. A cockpit for *reading code* has to let a developer **focus on one codebase** and
push the rest of the fleet out of view.

The model, borrowing VSCode's vocabulary: a **project = a repo**; you **open** several and **view one
at a time** via a top **tab strip** (VSCode editor tabs); the active project **scopes every
repo-partitioned view**. This is the *chassis* — not the feature — but it's the precondition for
everything else feeling calm enough to review code in.

### Pillar 2 — In-TUI code review at the gate (the trust feature)

A first-class human review seat, in-app, at the Review gate:

- **Read the change** — the branch/PR diff rendered in a review pane (quadraui already ships a
  `DiffView` primitive), scoped to the active project.
- **Human confirms the AI review.** The adversarial reviewer pre-screens and surfaces findings; the
  developer reads the diff *plus* those findings and **confirms or overrides** — low friction, AI
  does the heavy lifting, the human is accountable. The verdict flows through the existing
  `report-result` / review pipeline.
- **A code-grounded review chat** *(the differentiator)* — an AI chat **embedded in the review
  pane, grounded in the change under review**: the diff, the issue, the repo's CLAUDE.md, the
  per-issue context store (#603), and the graphify code graph. The developer *interrogates* the
  change ("why this approach?", "does this break callers of X?", "is this input validated?") instead
  of squinting at raw diff. This is what makes reading AI code **tractable** — and it's the feature a
  skeptical senior engineer will actually value.

### Pillar 3 — Cross-platform, quadraui-native (feels like VSCode everywhere)

The cockpit ships as **TUI, GTK, macOS, and Windows** builds that feel *somewhat like VSCode*
(activity bar, tabs, sidebar, panels). This is a hard constraint — and it's the decisive argument for
putting the **framework-generic logic in `quadraui`**, which already has **four backends**, a
`GtkDriver`/`TuiDriver` black-box test story, and the primitives we need (`ActivityBar`, `TabBar`,
`TabGroupController`, `SplitTree`, `DiffView`, `Editor`, `ChatController`, `SidebarSystem`). A
cross-platform port is already tracked (mac epic #1160, Windows epic #1165). **Every new cockpit
component must be a backend-neutral quadraui primitive/controller so it rides that port for free;**
coord-tui stays thin domain wiring on the `ShellApp` seam.

## 4. The program

Two coord-tui epics riding two quadraui additions. Chassis first, because review lives *inside* it.

| | quadraui (backend-neutral) | coord-tui (domain wiring) |
|---|---|---|
| **Chassis** | `WorkspaceController` + AppShell top tab-strip slot (open-set of documents, one active, open/close/activate/reorder, keyboard+mouse, `TabBar`-rendered) | **Epic A** — project model + persistence; scope every view to the active project; wire the tab strip; open/close UX |
| **Review** | Review surface: `DiffView`-backed multi-file diff panel + embedded code-grounded `ChatController` seat | **Epic B** — render the branch/PR diff for the active project; human-confirms-AI verdict; ground the review chat in diff+issue+CLAUDE.md+#603+graph |

The chassis (Epic A) delivers the density relief on its own — defaulting to a single active repo with
a cycle key ships value *before* the polished tab strip and the whole review epic.

## 5. Non-goals / deferred

- **Split view** (two projects side-by-side) — quadraui's `TabGroupController` already supports it,
  but it reintroduces density; deferred until asked for.
- **Named workspaces** (saved sets of repos, e.g. "coord stack" = coordinator + quadraui) — a later
  layer on top of the repo-as-project atom.
- **Full IDE-grade editing** (edit code in place in the review pane) — the target is *review-grade*
  (read + interrogate + approve), not an editor. Editing is a much larger program if ever pursued.

## 6. Open questions (for Epic B decomposition)

- What exactly grounds the review chat, and how much context is injected vs. tool-fetched on demand
  (diff always; graph/graphify and CLAUDE.md via retrieval)?
- Comment model: inline diff comments in v1, or approve/request-changes + chat only?
- How the "human confirms AI review" verdict is recorded so it's auditable (ties to the Audit Trail
  epic #1041) — the gate should log *that a human read it*, not just the outcome.
- Does the review chat get read-only code tools (grep/graph/read) so it can answer beyond the diff?

## 7. Relationship to the web control center (added 2026-07-28)

Five days after this thesis was written, the near-term direction was repointed to the **web control
center** ([`WEB_CONTROL_CENTER.md`](WEB_CONTROL_CENTER.md)): a responsive browser app growing toward
full `coord-tui` parity, and eventually the primary surface for anyone who is not the author. Read
alongside §3's Pillar 1 and Pillar 3, that looks like a contradiction. It mostly isn't — but one part
of it genuinely is, and that part is now a decision rather than an assumption.

**What survives unchanged.** The wedge (§1) and the moat (§2) are arguments about *posture*, not
about pixels. "A human demonstrably read the code before it merged" is equally true in a terminal, a
GTK window, or a browser tab. Nothing in §1–§2 depends on the seat being a TUI.

**What the web program actually helps.** Pillar 3's real requirement is *reach* — the cockpit has to
be usable wherever the developer is. A browser delivers that more completely than four native builds
do: zero install, any machine, and — the point that matters for the commercial thesis — **it is the
only surface you can put in front of a client without asking them to install anything.** A
risk-averse org evaluating a trust surface will not start by building a Rust binary.

**What genuinely conflicts.** Pillar 3's engineering argument was *leverage*: put the review surface
in quadraui as backend-neutral primitives and it rides the TUI/GTK/mac/Windows port for free. **A web
client does not ride that port.** `DiffView`, the review pane, the embedded chat — none of it
transfers to React. So Pillar 2, the trust feature itself, now faces a real fork: build the review
seat in quadraui (four native surfaces, not the browser), in the web app (the browser and nothing
else), or accept building it twice. This thesis assumed the first without knowing the third option
would exist.

**How to keep the decision cheap.** The trust guarantee does not live in the UI. It lives in the
*record* — that a human read the diff, at which gate, with what verdict, auditable afterwards
(§6's fourth open question, tying to the Audit Trail epic #1041). **Build that record
surface-neutrally, in the coord backend, before either review seat is built.** Then "which surface
gets the review pane first" becomes a UX scheduling question rather than an architectural
commitment, and whichever surface loses the race can be added later without re-litigating the trust
model.

**Sequencing, as of 2026-07-28.** Neither program is dispatched. The web program (milestones #51/#52)
is authored and gated on #1440; the cockpit program (#45/#46) is a stub. GOAL.md's near-term priority
is the web program, on the grounds that it is simultaneously a product and the real-world trial of
`epic → oracle → drive`. This thesis stays the intent layer for the trust wedge and should be
revisited — not rewritten — when the cockpit program is scheduled.
