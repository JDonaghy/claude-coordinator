# ADR: How a built `coord-web` bundle reaches the daemon host

**Status:** Accepted
**Date:** 2026-08-18
**Issue:** #2004 (UX-2, Phase 1 of milestone #62, epic #2002). Blocks UX-4
("CI must know what it publishes") and UX-7.

## Context

`coord/dashboard/webapp/**` is moving out of this repo into its own
`coord-web` repo. Today, before that split, the same question already has a
shipped answer: **[`docs/PHONE_WEBAPP.md`](PHONE_WEBAPP.md), "Going live
automatically (#1543)."** `deploy/coord-web-dist-build.timer` fires every 10
minutes (retuned from 1 minute by #2122; see that unit's header), fetches
`origin/main` read-only, builds it in a dedicated worktree
(`~/.coord-web-checkout`), health-checks the result on a scratch port before
ever publishing it (#1560), and atomically repoints `~/coord-web-dist` —
which `coord web --dist` serves — at the new release. A one-command rollback
(`coord-web-rollback.sh`) with a rollback-sentinel anti-flap guard exists for
the class of bug the health check can't see. `coord release verify` already
has a lane for it: `webapp_bundle` (#1834 lane 5), which grades the live
bundle's staleness against the `coord/dashboard/webapp/` source tree it
claims to be built from — deliberately *not* against `--expected`/PyPI,
because the bundle is versioned by `origin/main`'s SHA on its own continuous
publish timer, decoupled on purpose from the `~/.coord-venv` release
cadence. `webapp_build_heartbeat` (#2122) catches the timer going silently
dead, independent of whether there's unbuilt source to expose it.

The question #2004 poses is really: **once the thing that timer builds lives
in a different repo, does any of this change in kind, or only in which
repo(s) it points at?**

## Decision

**Extend the existing mechanism to track `coord-web`'s `main`, instead of
`coord/dashboard/webapp/` inside this repo.** This is Option 1 from the
issue, and it is not a new lane so much as a redirection of a lane this
fleet already built, hardened, and has a release-verify check for.

Concretely, once the split lands:

- `coord-web-dist-build.sh`'s `BASE_CHECKOUT`/`WEBAPP_CHECKOUT` point at a
  dedicated clone of `coord-web` (never the operator's own checkout, same
  rule #1543 already enforces) instead of a worktree of this repo.
- The build step becomes `npm ci && npm run build` at `coord-web`'s repo
  root instead of `coord/dashboard/webapp/`.
- The health-check-before-cutover, atomic symlink publish, rollback script,
  and rollback-sentinel guard carry over **unchanged** — none of that logic
  is specific to which repo it's building.
- `coord.health.checks.deploy_lane_facts.resolve_webapp_source_dir` (which
  scans `ctx.checkouts` for a directory that looks like the webapp source)
  gets pointed at a `coord-web` checkout's layout instead of
  `<checkout>/coord/dashboard/webapp/src`. The `webapp_bundle` check's
  *shape* — dist mtime vs. source mtime, WARN on staleness, never a version
  comparison — does not change; only the path it looks under does.
- The PyPI-wheel-bundled fallback path (`coord/dashboard/webapp/dist`
  vendored into `claude-coordinator`'s own release, #758) stays, unchanged
  in role: it is the answer for `pip install code-coordinator` on a machine
  with no `--dist` configured (fresh installs, CI, one-off dev boxes), not
  the primary path to a running daemon host. This is Option 4, kept
  deliberately as the fallback it already is today rather than promoted to
  primary — see "Rejected alternatives" below for why it can't be primary
  post-split.

## Rejected alternatives

**Option 2 — GitHub release artifact (tarball per `coord-web` release).**
Versioned and auditable, but it reintroduces exactly the coupling #1543 was
built to remove: a webapp fix would sit merged-but-not-live until someone
cuts a release, and nothing today gives that step the ~10-minute, unattended
cadence the timer already provides. It would also need the health-check
gate and rollback machinery rebuilt around a fetch/verify/unpack step
instead of a `git fetch` + worktree build — real new surface for no
capability the current mechanism lacks. Revisit if `coord-web` ever needs a
public, pinned distribution channel independent of the daemon host (it
doesn't today).

**Option 3 — npm package.** Same coupling-to-publish-cadence problem as
Option 2, plus it adds a registry-auth and install-on-host step to a
deploy path that currently needs neither. No requirement in this issue
calls for the bundle to be consumable outside this fleet's own daemon host,
which is the only scenario where an npm package would earn its cost back.

**Option 4 — vendored at `claude-coordinator` release time, promoted to
primary.** Rejected as the *primary* path because it re-couples the two
repos' release cadences — the coupling the split (epic #2002) exists to
remove. A `coord/dashboard/webapp/**` merge would go live only on
`claude-coordinator`'s next PyPI release, not within ~10 minutes of merging.
Kept as the fallback, because that's a real, already-solved problem
(bootstrapping a `--dist`-less host, or a fresh install) distinct from
"how does a live daemon host stay current," and downgrading it doesn't cost
anything the fleet needs today.

**Why Option 1 is not "a fifth silent lane."** The issue's core objection —
this fleet already has four deploy lanes that go stale with no signal
(agent venv, `coord-serve`, local `cargo build`, `~/.coord-cli-venv`) — is
real, but those four go stale because going live requires an *operator* to
re-run an install step (`pip install --upgrade`, `cargo build`) and nothing
polls to catch it if they don't. The webapp-dist lane is structurally
different: it already has (a) an autonomous timer instead of an
operator-triggered pull, (b) a pre-cutover health check that refuses to
publish a broken build in the first place, (c) a heartbeat file
`coord health`/`coord release verify` already read to distinguish "up to
date" from "the trigger died silently," and (d) a release-verify lane
(`webapp_bundle`) already wired into the fleet health surface. Extending it
to a second repo carries all four properties across unchanged — it doesn't
have to invent them the way a from-scratch Option 2/3 pipeline would.

## Staleness across the split is a different question than staleness today

Pre-split, `coord-web-dist-build.timer` and `~/.coord-venv` build off the
*same* repo, so "is the live bundle SHA an ancestor of what the daemon was
built from" is at least answerable in principle. Post-split, `coord-serve`
(from `claude-coordinator`, versioned by PyPI release) and the webapp bundle
(from `coord-web`, versioned by its own `main` SHA) are two independently
released artifacts with **no shared version number** — and per the
`webapp_bundle` design already in `release_verify.py`, that's intentional:
grading the bundle against `--expected`/PyPI would "manufacture permanent,
meaningless skew rather than report a real one." That reasoning holds even
harder once the source repos are different.

What that does *not* cover, and what genuinely is new here: a phone hitting
`http://dellserver:7434` can land on a `coord-web` bundle that expects an
API shape `coord-serve` hasn't shipped yet (or vice versa), because nothing
before this ADR pins a compatibility floor between the two repos'
independent cadences. This is a real risk the split introduces — it isn't
solved by choosing Option 1 over 2/3/4, and it isn't this ADR's job to
solve, but it should be named so UX-4/UX-7 pick it up rather than
rediscover it: the daemon already reports its own version
(`coord/dashboard/server.py`'s `version=__version__`); the cheapest fix is
likely a minimum-compatible-API marker the webapp checks against that value
at load and degrades visibly (banner, not a silent break) on mismatch,
rather than trying to keep the two repos' releases in lockstep.

Propagation lag itself (a phone briefly on a bundle one merge behind the
daemon, or vice versa) is not new and not a defect — it's the same ~10-minute
window the timer already runs at for the single-repo case (retuned from the
original 1-minute cadence #1543 documents; see #2122 and
`deploy/coord-web-dist-build.timer`'s header for why). It stays detectable
the same way: the release directory is named after the
`coord-web` SHA it was built from, and `webapp_build_heartbeat` proves the
timer is still alive.

## Consequences

- `coord release verify`'s `webapp_bundle` lane keeps working post-split
  with a path/repo-target change, not a redesign — satisfying this issue's
  "whichever option wins should be assertable by it" requirement at close
  to zero incremental cost.
- The daemon host needs a second dedicated checkout (`coord-web`, alongside
  whatever `claude-coordinator` checkout it already keeps for other lanes)
  and a second instance of the build-timer/rollback pairing pointed at it.
  That's real setup work, tracked under UX-4/UX-7, not a design question.
- A `coord-web`↔`coord-serve` compatibility floor is an explicitly named
  open follow-up, not solved here.
- Options 2 and 3 remain available to revisit if `coord-web` ever needs a
  distribution channel independent of this fleet's own daemon host — no
  requirement in scope today calls for that.
