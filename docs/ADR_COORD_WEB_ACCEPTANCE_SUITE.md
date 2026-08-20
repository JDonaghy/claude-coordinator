# ADR: Where the sealed ms-51 acceptance suite lives once the webapp is its own repo

**Status:** Accepted
**Date:** 2026-08-20
**Issue:** #2007 (UX-5, Phase 1 of milestone #62, epic #2002). Depends on #2005
(closed). UX-6 (#2008) depends on this decision; UX-7 (#2009) is gated on it
too.

## Context

`tests/acceptance/ms-51/**` (`contract.md`, `mocks/`, the derived Playwright
specs, `manifest.yml`) is the webapp's sealed oracle. Today it lives in
`claude-coordinator`, at the repo root, alongside `coord/dashboard/webapp/**`
— the code it pins — *and* alongside the machinery that runs it
(`coord acceptance run/record/author/mock`, `coord/acceptance_drivers.py`,
the `test-author` dispatch + Gate-A flow, `acceptance.drivers` routing in
`coordinator.yml`). Once UX-7 deletes `coord/dashboard/webapp/**` from this
repo, those two things — the code and the machinery — are no longer in the
same place, and the suite has to pick a side.

The invariant that must survive either way, from `CLAUDE.md`: the suite is
delivered to workers **read-only / run-only**; a `type="work"` diff touching
`tests/acceptance/**` (or a declared driver `entrypoint`) is an unconditional,
mandatory `request-changes` in `coord/review.py`. #2007's framing worried that
moving the suite into `coord-web` weakens this from a repo boundary to "just"
review convention.

**That framing doesn't hold up.** No sealed suite in this project has ever
been protected by a repo boundary. `tests/acceptance/**` (the `cli-pytest`
route) already seals `coord/` in the same repo it lives in. `tui/tests/acceptance.rs`
already seals `tui/` in the same repo it lives in — CLAUDE.md calls this out
explicitly: sealed "despite living nowhere near `tests/acceptance/`," enforced
purely by the reviewer's mandatory-request-changes rule. Review-time
enforcement, same-repo, is the only mechanism this project has ever used.
Moving `ms-51` into `coord-web` doesn't introduce a weaker model — it matches
the one every other driver already runs on. `AcceptanceConfig.sealed_paths()`
is derived per `repo_name` for exactly this reason: it was never repo-boundary
shaped to begin with.

## Decision

**Move `tests/acceptance/ms-51/**` into `coord-web`, at its repo root,
alongside the webapp source it pins.** Concretely, once UX-7 lands:

- `coord-web`'s repo root gains `tests/acceptance/ms-51/**`, satisfying
  `AcceptanceConfig.SEALED_ACCEPTANCE_DIR` (`"tests/acceptance/"`,
  repo-root-relative) with zero changes to the sealing code itself — sealing
  activates automatically the moment `acceptance.drivers["coord-web"]` exists
  (UX-6, #2008).
- `acceptance.drivers["coord-web"]` becomes a **flat** entry (`kind:
  web-playwright`, `run: npm run test:acceptance -- {ms}`, `mock: *.html`,
  `capability: browser`) — no `routes:`, since `coord-web` doesn't need
  `claude-coordinator`'s router split (that split exists there only because
  `coord/dashboard/webapp/**` is one of several subpaths in a bigger repo).
- The `cd coord/dashboard/webapp &&` indirection in today's `run`/`setup`
  commands drops out — `coord-web`'s repo root *is* the webapp root, so
  `npm run test:acceptance -- {ms}` runs directly.
- `playwright.acceptance.config.ts` keeps resolving `tests/acceptance/{ms}`
  the same way; the path just stops needing to walk up out of
  `coord/dashboard/webapp/` to find it.
- `claude-coordinator`'s `coord/dashboard/webapp/**` route in
  `acceptance.drivers` is deleted as part of UX-7, not re-pointed — the
  webapp source is gone from this repo, so there's nothing left to route.

## Rejected alternative

**Leave `tests/acceptance/ms-51/**` in `claude-coordinator`, next to the
`coord` CLI machinery that runs it.** This keeps `coord acceptance run` able
to find the suite without any cross-repo reach, but it means `coord-web`'s own
CI (UX-4, #2006) — which needs to run the sealed suite as its `acceptance`
job — can't do so from its own checkout. It would need `claude-coordinator`
checked out alongside `coord-web` on the runner (sparse-checkout or
submodule) just to see files that logically belong to `coord-web`'s own test
tree, for a guarantee (repo-boundary sealing) this project doesn't rely on
anywhere else. Rejected: real, ongoing plumbing cost for a property nothing
else here provides either.

Note the `coord` **package** (the machinery: `coord/acceptance_drivers.py`,
`coord acceptance run`, etc.) is a different axis from the suite **files**
and isn't actually blocked by this decision — UX-4 already has `coord-web`'s
CI install `coord` from PyPI to boot `coord web --fixture` for the
`web-playwright` driver's own web server. That install is needed either way;
what changes is only where the *files* the installed `coord` runs against
live.

## What this doesn't solve

#1950: the ms-51 slice has been red since #1547 and nobody noticed, because
`test:acceptance` runs in no CI job and no Test lane today. Relocating the
suite doesn't fix that by itself — UX-4 (#2006) is where an actual gate
(`coord-web`'s `acceptance` CI job) has to start running it. This ADR settles
*where the files live*, not whether anything executes them; #2006 owns the
latter and should not treat this move as having already solved it.

## Consequences

- Sealing continues to work exactly as it does today for `coord/` and
  `tui/**` — same mechanism, same enforcement point (`coord/review.py`), one
  more repo added to the set it already covers.
- `coord-web`'s CI can run its own acceptance suite from a single checkout,
  no cross-repo fetch.
- `claude-coordinator`'s `acceptance.drivers` config gets simpler after UX-7
  (one less router route), not more complex.
- The open exposure is #1950 (no automatic gate runs the suite yet), owned by
  UX-4, not by this decision.
