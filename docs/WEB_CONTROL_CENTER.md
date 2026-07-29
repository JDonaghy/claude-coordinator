# coord web — the browser control center (program RFC)

> **Status:** planning, 2026-07-28. Nothing dispatched.
> **Scope:** a multi-milestone program, not one epic.
> **Two theses, deliberately coupled** — see below.

## Why this program exists

**Product thesis.** `coord-tui` is a good operator cockpit for *one* operator who
built it. A browser app is the right surface for everyone else: no local install,
no Rust rebuild after every merge, works on a phone and a 32" monitor from the same
codebase, and it's the only surface that can plausibly be shown to a client. The
existing phone PWA (milestone #16, epics #700–#703 + #1064) proved the backend can
already feed a React client; it is phone-shaped by design and is not that app.

**Process thesis — this is the point.** The `epic → oracle → drive` machinery has
been built and validated on *small, self-referential* stories inside the tool that
implements it. Before offering to build software for clients on top of it, it has to
be proven on a **long, real, multi-epic project with a genuine user-visible surface**.
This program is that trial. The web app is the deliverable; the *confidence* is the
outcome. Concretely: can a fleet of `claude -p` workers, driven unattended against a
sealed acceptance oracle, produce a modern web application that a human would ship?

These are coupled on purpose. A dogfood vehicle that is merely a toy proves nothing;
a product built without instrumentation teaches nothing.

## Locked decisions (2026-07-28, with the user)

| Decision | Choice | Consequence |
|---|---|---|
| **Codebase** | **Evolve `coord/dashboard/webapp/` in place** | Reuses Playwright config, react-query, Tailwind, generated API types, PWA build, `coord web` serving. The phone Home becomes the *mobile layout* of one responsive app, not a separate app. Accepts churn on a tool that is live on the user's phone. |
| **Oracle sequencing** | **Web acceptance driver first, in parallel with the first web epic** | M-W0 (coord backend) and M-W1 (web shell) run concurrently — near-zero file overlap. M-W1 runs through plain `coord drive` and gives a **non-oracle baseline**; M-W2 onward run the full oracle loop. Same project, both data points. |
| **Parity ambition** | **Full parity; web becomes the primary surface** | Needs a real design system and ~12 panels. Web replaces the phone app, and eventually displaces `coord-tui` as the default surface for anyone who is not the author. This is the *honest* dogfood: a long program is the test. |

## What already exists (do not rebuild)

| Piece | Where | State |
|---|---|---|
| React 18 + Vite + TS + Tailwind + PWA build | `coord/dashboard/webapp/` | shipped |
| `@tanstack/react-query`, `react-router-dom`, `lucide-react` | `package.json` | shipped |
| Vitest unit tests (6 suites) | `src/components/__tests__/` | shipped |
| **Playwright config + 2 E2E specs** | `playwright.config.ts`, `e2e/` | shipped |
| OpenAPI spec generator → TS types | `coord/openapi.py` → `src/api/generated.ts` | shipped, **hand-synced** |
| `GET /api/pipeline` (full `PipelineView`) | `coord/dashboard/server.py:955` | shipped |
| `POST /api/pipeline/action` | `server.py:997` | **only 4 actions** (`dispatch_review`, `dispatch_smoke`, `enqueue`, `merge`) |
| SSE `/events` live bus | `server.py` `EventSource` | shipped |
| WS PTY terminal bridge (ssh+tmux, cross-machine, bearer-auth) | `/ws/terminal/{session_id}` | shipped (#1065) |
| `browser` capability routing for `coord/dashboard/webapp/**` | `smoke_tests.capability_rules` | shipped |
| Deployed as `coord-web.service` on dellserver:7434 | `deploy/coord-web.service` | live |

## The blocking gap: there is no web acceptance driver

`coord/acceptance_drivers.py:36`:

```python
SUPPORTED_KINDS = ("tui-tuidriver", "cli-pytest")
```

`run_driver()` raises `DriverError("...not implemented yet...web-playwright / native
adapters land in later oracle-loop issues")` for anything else. **`CLAUDE.md` currently
describes `web-playwright` as if it ships — it does not.** Until M-W0 lands, `coord
acceptance run/record` cannot gate a single line of this web app, which means the
oracle loop — the exact thing being trialled — is unavailable for the exact project
chosen to trial it. M-W0 is therefore the true start of the program.

## Architecture decisions

**1. One board contract, generated — not hand-mirrored.**
`coord-tui` reads the daemon's `/board` (7435); the webapp reads the dashboard's
`/api/pipeline` (7434). Two hand-maintained cross-language contracts is the #632
blank-board failure class, twice. `src/api/generated.ts` already exists but is
hand-synced. **Fix: generate `generated.ts` from `coord/openapi.py` in CI and fail
the build on drift.** One story, kills a whole bug class for the program's lifetime.

**2. `coord web` stays co-located with the daemon (for now).**
The dashboard calls `read_board()` in-process against `~/.coord/coord.db`, so it is
pinned to dellserver. That is *fine* — dellserver is the daemon host — and daemon-
routing is already tracked as #749. Do not fold that refactor into this program;
just do not add new direct-DB reads outside the `board_service` seam.

**3. The keystone: a deterministic seeded-board fixture server.**
`coord-tui`'s black-box tests work because `make_test_app(BoardData)` builds a whole
app from in-memory state with no live daemon. The web app has **no equivalent** —
today's E2E specs run against whatever the fleet happens to be doing. Acceptance
tests that depend on live fleet state are not an oracle; they are a flake generator.
**`coord web --fixture <board.json>`** (serving a seeded board through the *real*
REST + SSE surface) is the single highest-leverage story in the program. Everything
downstream — every acceptance slice, every Gate-A mock — depends on it.

**4. Gate-A mock shape for web — DECIDED: hand-authored HTML mock pages.**
`tui-tuidriver` mocks are `*.screen` grids; `cli-pytest` mocks are `*.out` stdout.
For web, Gate A produces **static HTML mock pages** (`tests/acceptance/ms-NN/mocks/*.html`)
— one per screen state, hand-authored, self-contained, and **able to carry CSS** so a
mock reads as a real screen rather than a DOM skeleton. The test-author writes DOM
assertions against the mock; the real app must then satisfy them. Alternative
considered and rejected as a *contract*: Playwright screenshot baselines — too
brittle, and unreadable as a spec.

*Known limit, accepted for now:* a static HTML page is a fine **engineering** contract
but a weak **client-facing** artifact. If these mocks ever need to double as something
shown to a client, that wants a different tool (a real design surface, or a
Storybook-style interactive mock). Revisit then; do not over-build it now.

**5. `browser` capability must not live on one machine.**
Today only **elitebook** advertises `browser`, and elitebook is the dev box with a
documented history of gh-blind workers (#1483) and dropped review verdicts. Every
web smoke and every web acceptance run would funnel through it. **Add `browser` to
dellserver** — Playwright headless Chromium needs no display, and dellserver is
always-on and co-located with the app under test.

**6. Deployment friction is a program-level tax — and it is currently a stale config,
not a missing feature.**
A merged web PR is *not* live until someone ssh'es to dellserver and runs `git pull &&
npm run build && systemctl --user restart coord-web`. Over a multi-epic program that
is dozens of manual round-trips on the very surface being dogfooded.

**#758 already landed**: `pyproject.toml` package-data + `MANIFEST.in` ship
`coord/dashboard/webapp/dist/**`, `publish.yml` runs `npm ci && npm run build` before
`python -m build`, and `~/.coord-venv` (0.4.84) on dellserver *does* contain a built
`dist/index.html`. The friction survives only because `coord-web.service` still has
`ExecStart=%h/src/claude-coordinator/.venv/bin/coord web` — the pre-#758 editable
checkout — and was never repointed.

Two ways out, and the choice matters for dogfood cadence:
- **(a) Repoint to `~/.coord-venv`** — deploy becomes release + `pip install --upgrade`
  + restart. Clean, matches every other service, but ties every web change to the
  PyPI release cadence.
- **(b) Keep the editable checkout, automate it** — a post-merge hook that pulls,
  `npm run build`s, and restarts. Gets *merged main* live within a minute.

**Recommend (b) for the duration of this program** — a dogfood loop wants the latest
merged commit live, not the latest release — then switch to (a) when the program ends.

**7. Auth + ToS.** Tailnet-only HTTP, optional bearer (as the terminal bridge already
does). No user accounts, no public exposure in this program. The web terminal stays
**human-attended takeover only** (ToS §3.7, #437) — reviewers must reject any
autonomous read/drive path through the PTY bridge. Unchanged from #1064.

**8. Responsive model.** One route tree, breakpoint-driven layout:
- **Wide (≥1024px):** activity rail (the `SidebarView` twin) + list panel + detail panel.
- **Narrow:** bottom nav + stacked views, i.e. today's phone app, preserved.
Not two codebases, not two component sets — one set with responsive composition.

## Program shape

Ordered. `M-W0` and `M-W1` run concurrently; everything after is sequential.

### M-W0 — Web acceptance oracle *(coord backend, unblocks everything)*
**GitHub milestone #51 · epic #1537** — authored 2026-07-28, not dispatched.

| | Story | DAG |
|---|---|---|
| **#1538** | `coord web --fixture <board.json>` — seeded board through the real REST+SSE surface. The web twin of `make_test_app(BoardData)`. *Blocks every later acceptance slice.* | A |
| **#1539** | `web-playwright` driver — add to `SUPPORTED_KINDS`; parse Playwright's report into the normalized `{id,status,message}` list; tests over **recorded** reporter output | A |
| **#1541** | `browser` capability on dellserver (headless Chromium) — stop funnelling all web testing through elitebook | A |
| **#1543** | Deploy cadence — `coord-web.service` still runs the pre-#758 editable checkout (see decision 6) | A |
| **#1540** | `acceptance.drivers` route for `coord/dashboard/webapp/**` + route-precedence over `coord/**` | B ← 1539 |
| **#1542** | Gate-A mock shape: hand-authored HTML mock pages | C ← 1540 |
| **#1544** | **Exit gate** — one sealed slice proven end-to-end, including a deliberate red run | D ← 1538,1540,1541,1542 |

Docs (`ORACLE_LOOP.md` driver table + the `CLAUDE.md` correction) are **coordinator-owned
close-out**, not a dispatched story — workers must not edit shared docs.

### M-W1 — Responsive shell + design system
**GitHub milestone #52 · epic #1545** — authored 2026-07-28, not dispatched.

| | Story | DAG |
|---|---|---|
| **#1546** | Design tokens, dark/light theme, component primitive baseline (shadcn/ui decision) + dev-only gallery route | A |
| **#1549** | Live data layer — react-query + SSE invalidation, honest connection state, retire ad-hoc polling | A |
| **#1550** | Generate `src/api/generated.ts` from the OpenAPI spec + **fail CI on drift** (#632 class) | A |
| **#1547** | Responsive shell — rail + list + detail on wide, phone layout preserved on narrow | B ← 1546 |
| **#1548** | Route tree + deep links — every view addressable and restorable | C ← 1547 |
| **#1551** | **Exit gate** — shell acceptance slice: both breakpoints, both themes, live updates, phone regression net | D ← 1547,1548,1549 |

### M-W2 — Pipeline panel, read path *(first full oracle-loop epic)*
Issue list with repo/milestone/state filters + search; per-row stage strip (Work→Test→Review→Merge with verdicts and gate reasons); detail tabs **Overview / Issue / Log / Summary** (the `PipelineDetailTab` set minus Terminal); live log streaming; real empty/loading/error states.

### M-W3 — Pipeline panel, actions
The ~20-action surface from `dialogs.rs::context_menu_items_for_pipeline_row`, as a desktop context menu and a mobile action sheet: Start (interactive/automated) Work·Plan·Review·Testing·Merge, Drive + attach/stop, Address review findings, Record acceptance, View Gate-A mock, Open PR, Drop to backlog, Mark/unmark ready, Stop, Watch. Requires **extending `POST /api/pipeline/action` well beyond today's four**. Confirmation + irreversibility guards on anything that spends money.

### M-W4 — Attended sessions on desktop
The #1064 terminal, laid out for a large screen (multi-pane, session switcher) rather than phone-half-screen. Human-attended only.

### M-W5+ — Remaining panels, in rough value order
Board/Kanban · Merge Queue · Milestone DAG · Sessions roster · Audit trail · Spend & Time · Settings · Plans.

### M-W9 — Retire the phone app
Once M-W1–M-W4 land, the phone routes *are* the mobile layout. Close out milestone #16.

## The dogfood scorecard

Measured per issue, reported per epic. Without this the program produces an app and
no evidence, which defeats half its purpose.

| Metric | Source |
|---|---|
| **First-pass acceptance rate** — merged with zero human fix dispatch | board / audit trail (ms #33) |
| **Human interventions per issue** — count + kind (nudge / fix / rescue / abandon) | audit trail |
| **Cost + wall-clock per issue** | Spend & Time (ms #37) |
| **Escaped defects** — caught at review vs Gate-B vs after merge vs by the human in the live app | manual tag |
| **Process bugs surfaced** — coord bugs the program exposed, and whether each got a regression test | issue label |

**Protocol (standing):** a dogfood story is a *test vehicle*. When it surfaces a
process bug in coord itself, **halt the story**, file the bug, fix it **with a test**,
then resume. Shipping the story around a known process bug forfeits the data.

## Known risks

1. **The oracle is unproven on this medium.** `web-playwright` will have its own
   failure modes (flake, timing, StrictMode double-mount — see the existing note that
   webapp E2E runs against Vite **dev**). Expect M-W0 and M-W2 to teach us things.
2. **Milestone-level unattended sequencing is not done** (#1440 open). Driving a whole
   epic end-to-end with nobody watching is still partly manual — this program will
   press on exactly that gap, which is useful but should not be a surprise.
3. **Churn on a live tool.** The phone app is in daily use; M-W1 restructures its shell.
4. **Single browser machine** until W0-4 lands.
5. **Scope.** "Full parity" is ~12 panels against a 7.7k-line Pipeline panel alone.
   The parity goal is a *direction*, and each milestone must stand on its own value.

## Dispatch gate

**Nothing in this program is driven until #1440 lands** (sequence the oracle gates for
a whole milestone A→work→B→C→D unattended). Decided 2026-07-28. M-W0 and M-W1 are
authored and ready so the program can start the moment that gate opens — the point of
this trial is to exercise *unattended milestone driving*, and starting before #1440
would just be manual driving with extra steps.

## Open questions

- **Component library** — keep the hand-rolled `ui/` primitives, or adopt shadcn/ui
  properly? Lean: **adopt shadcn/ui**, since "modern-looking" is an explicit goal and
  hand-rolling a design system across ~12 panels is undifferentiated work. Decided in
  W1-1; cheap to revisit before dispatch, expensive after.
- **Is M-W1's oracle-off run deliberate?** Because of the #1440 gate, M-W0 will likely
  be done before anything is driven — so the "plain `coord drive` baseline" is now a
  *choice*, not a consequence. Decide at dispatch time: run M-W1 oracle-off on purpose
  to get a comparison point, or skip the baseline and put everything through the oracle.
