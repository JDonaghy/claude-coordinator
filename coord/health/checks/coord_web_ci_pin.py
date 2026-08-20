"""Is `coord-web`'s CI still installing a `coord` that can boot `coord web`?

**The coupling this measures.** `coord-web` (epic #2002) is nominally a pure
HTTP client of the coord daemon, but its Playwright suites are not: the
fixture spec, and the sealed acceptance config
(`playwright.acceptance.config.ts`), boot a **real** ``coord web --fixture
<file> --dist dist`` process as their ``webServer`` (#1818). That means
`coord-web`'s CI has to install *this* repo's package on the runner, and
`coord-web`'s CI therefore encodes a cross-repo contract: which `coord` its
frontend is proven against.

**Why that needs a check at all (#2006).** A version spec sitting in another
repo's workflow YAML is exactly the shape of thing this fleet has already
been burned by, twice:

* ``~/.coord-cli-venv`` was found **three releases stale** on 2026-07-29 —
  silently, because nothing measured it. That incident is why
  ``deploy_lane_facts.probe_cli_venv`` exists.
* vimcode#615 (#1629): CI built on rustc 1.97.1 while every fleet machine
  was months behind, and six snapshot tests were green everywhere and red in
  CI. That is why :mod:`coord.health.checks.toolchain` learned to read a
  repo's workflow YAML at all.

`coord-web`'s `coord` spec is the same class of fact, one repo further away.
:doc:`docs/ADR_COORD_WEB_CI` records the decision it is graded against —
**track latest, never exact-pin** — and this check is the "visible and
assertable" half that ADR promises. Without it the decision is a paragraph
in a document; with it, ``coord health`` prints the actual spec string, from
the actual file, on every tick.

**Annotate, don't gate**, exactly like ``toolchain``/``cli_venv``: nothing
here blocks a dispatch, a routing decision, or a merge. The severities say
"go look at coord-web's ci.yml", never "stop".

**Local-filesystem only.** The workflow YAML is read from a `coord-web`
checkout already on this machine (``repo_paths.coord-web`` in
coordinator.yml). No ``gh`` call, no network — cheap enough for the health
poll tick, and absent on a machine with no `coord-web` checkout, which is
reported as OK ("not present on this machine"), the same way every other
checkout-derived lane reports absence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from coord import __version__ as LOCAL_COORD_VERSION
from coord.health.models import CheckResult, HealthContext, Severity
from coord.health.registry import check
from coord.health.units import expand, shorten_path

CHECK_ID = "coord_web_ci_pin"

#: The repo name (``repos[].name`` / ``repo_paths`` key in coordinator.yml)
#: whose CI this check reads.
COORD_WEB_REPO_NAME = "coord-web"

#: A file at a checkout's root that identifies it as `coord-web` even if the
#: repo is renamed or checked out under a different directory name: it is the
#: config whose ``webServer`` boots ``coord web --fixture`` (#1818), i.e. the
#: literal reason this cross-repo coupling exists.
COORD_WEB_MARKER = "playwright.acceptance.config.ts"

#: The extra that carries uvicorn/Starlette. ``coord web`` is a **server**
#: command; since the base/`[server]` split (#1237) the bare distribution
#: installs a client-only coord that cannot serve anything.
SERVER_EXTRA = "server"

#: PyPI cannot rename a project, so ``claude-coordinator`` is a permanent
#: tombstone that will never gain another release (#2106). CI still asking
#: for it does not get a stale coord — it gets whatever ancient version that
#: tombstone last published, forever.
TOMBSTONE_DIST = "claude-coordinator"

_INSTALL_RE = re.compile(r"\b(?:pip3?|uv\s+pip|pipx)\s+install\b", re.IGNORECASE)

# `code-coordinator[server]>=0.4.90` and friends. Deliberately tolerant of
# quoting (`'code-coordinator[server]'`) and of the underscore spelling pip
# normalises away, because this is reading someone else's hand-written YAML.
_REQ_RE = re.compile(
    r"(?P<dist>code[-_]coordinator|claude[-_]coordinator)"
    r"(?:\s*\[(?P<extras>[^\]]*)\])?"
    r"(?P<constraint>"
    r"(?:\s*(?:===|[<>!~=]=|[<>])\s*[^\s'\",;\\]+)"
    r"(?:\s*,\s*(?:===|[<>!~=]=|[<>])\s*[^\s'\",;\\]+)*"
    r")?",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CoordPin:
    """One ``pip install <coord>`` requirement found in `coord-web`'s CI."""

    workflow: str  # file name, e.g. "ci.yml"
    job: str  # job id, e.g. "e2e"
    spec: str  # the requirement as written, e.g. "code-coordinator[server]"
    dist: str  # normalised distribution name
    extras: tuple[str, ...] = ()
    constraint: str | None = None  # ">=0.4.90", "==0.4.90", or None

    @property
    def has_server_extra(self) -> bool:
        return SERVER_EXTRA in self.extras

    @property
    def is_tombstone(self) -> bool:
        return self.dist == TOMBSTONE_DIST

    @property
    def is_exact_pin(self) -> bool:
        """True for ``==``/``===``, the spec shape that rots silently.

        ``~=`` is deliberately included: ``~=0.4.90`` freezes the minor
        series just as effectively for a project on a single ``0.x`` line.
        """
        c = (self.constraint or "").lstrip()
        return c.startswith(("==", "===", "~="))

    @property
    def floor(self) -> str | None:
        """The ``>=`` version, when the spec declares one."""
        for part in (self.constraint or "").split(","):
            part = part.strip()
            if part.startswith(">="):
                return part[2:].strip()
        return None


def _norm_dist(raw: str) -> str:
    return raw.strip().lower().replace("_", "-")


def _version_tuple(s: str) -> tuple[int, ...] | None:
    parts = re.findall(r"\d+", s or "")
    return tuple(int(p) for p in parts) if parts else None


def _release_tuple(s: str) -> tuple[int, ...] | None:
    """A comparable tuple, but only for a version that is a real release.

    ``coord.__version__`` falls back to ``0+unknown`` when it cannot resolve
    a distribution (a source checkout with no install, a wheel-less CI box).
    Comparing a CI floor against *that* would report every floor as "ahead of
    this machine" — a fabricated finding, and fabricating findings is how a
    check earns the right to be ignored. Unknown stays unknown.
    """
    if not s or "unknown" in s.lower():
        return None
    t = _version_tuple(s)
    return t if t and len(t) >= 2 else None


def _floor_exceeds(floor: str, local: str) -> bool:
    """True iff *floor* is strictly newer than *local*, both being releases.

    Compares at equal precision — a floor of ``0.4`` is satisfied by a local
    ``0.4.91``, so the shorter tuple is padded rather than compared as-is
    (``(0, 4, 91) > (0, 4)`` would otherwise read as "ahead").
    """
    f, loc = _release_tuple(floor), _release_tuple(local)
    if not f or not loc:
        return False
    width = max(len(f), len(loc))

    def pad(t: tuple[int, ...]) -> tuple[int, ...]:
        return t + (0,) * (width - len(t))

    return pad(f) > pad(loc)


def resolve_coord_web_checkout(ctx: HealthContext) -> Path | None:
    """This machine's `coord-web` checkout, if it has one.

    Same convention as every other path in :class:`coord.config.HealthConfig`:
    a configured ``health.coord_web_checkout`` wins outright, ``None`` means
    "discover it", never "disable the lane".
    """
    configured = getattr(ctx.thresholds, "coord_web_checkout", None)
    if configured:
        return expand(configured, ctx.home)
    for checkout in ctx.checkouts:
        if _norm_dist(checkout.name) == COORD_WEB_REPO_NAME:
            return checkout.path
    # Fall back to the structural marker so a rename of the repo doesn't
    # silently turn this lane off — an off lane is indistinguishable from a
    # healthy one, which is the whole failure mode being guarded against.
    for checkout in ctx.checkouts:
        if (checkout.path / COORD_WEB_MARKER).is_file():
            return checkout.path
    return None


def _iter_run_steps(checkout_path: Path):
    """Yield ``(workflow_file_name, job_id, run_script)`` for every step.

    A sibling of :func:`coord.health.checks.toolchain._iter_workflow_steps`,
    but keeping the workflow/job labels: a finding here has to name *where*
    in someone else's repo to go fix it, or it is just as much of a mystery
    as the thing it replaced.
    """
    workflows_dir = checkout_path / ".github" / "workflows"
    if not workflows_dir.is_dir():
        return
    try:
        files = sorted(workflows_dir.glob("*.yml")) + sorted(workflows_dir.glob("*.yaml"))
    except OSError:
        return
    for wf_file in files:
        try:
            doc = yaml.safe_load(wf_file.read_text(encoding="utf-8", errors="replace"))
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(doc, dict):
            continue
        jobs = doc.get("jobs")
        if not isinstance(jobs, dict):
            continue
        for job_id, job in jobs.items():
            if not isinstance(job, dict):
                continue
            for step in job.get("steps") or []:
                if not isinstance(step, dict):
                    continue
                run = step.get("run")
                if isinstance(run, str) and run.strip():
                    yield wf_file.name, str(job_id), run


def find_coord_pins(checkout_path: Path) -> list[CoordPin]:
    """Every ``pip install <coord>`` requirement in the checkout's workflows.

    Only ``run:`` scripts that actually invoke an installer are scanned — a
    comment or an echo naming the package is not a pin, and treating it as
    one would grade prose.
    """
    found: list[CoordPin] = []
    seen: set[tuple[str, str, str]] = set()
    for workflow, job, run in _iter_run_steps(checkout_path):
        for line in run.splitlines():
            if not _INSTALL_RE.search(line):
                continue
            for m in _REQ_RE.finditer(line):
                extras = tuple(
                    e.strip().lower() for e in (m.group("extras") or "").split(",") if e.strip()
                )
                constraint = (m.group("constraint") or "").strip() or None
                spec = m.group(0).strip()
                key = (workflow, job, spec)
                if key in seen:
                    continue
                seen.add(key)
                found.append(
                    CoordPin(
                        workflow=workflow,
                        job=job,
                        spec=spec,
                        dist=_norm_dist(m.group("dist")),
                        extras=extras,
                        constraint=constraint,
                    )
                )
    return found


def _where(pin: CoordPin) -> str:
    return f"{pin.workflow}:{pin.job}"


def _result(
    severity: Severity,
    headroom: str,
    values: dict,
    *,
    detail: str = "",
) -> CheckResult:
    return CheckResult(
        check_id=CHECK_ID,
        scope="machine",
        severity=severity,
        headroom=headroom,
        detail=detail,
        values=values,
    )


@check(
    id=CHECK_ID,
    scope="machine",
    title="coord-web ci pin",
    order=46,
    description=(
        "The coord spec coord-web's CI installs to boot `coord web --fixture` "
        "(#2006) — must carry the [server] extra and must not exact-pin."
    ),
)
def probe_coord_web_ci_pin(ctx: HealthContext) -> CheckResult:
    checkout = resolve_coord_web_checkout(ctx)
    if checkout is None or not checkout.is_dir():
        # The overwhelming common case: only the machines that list coord-web
        # in `repo_paths` have one. Absence is not a fault.
        return _result(
            Severity.OK,
            "not present on this machine",
            {"present": False, "checkout": str(checkout) if checkout else None, "pins": []},
        )

    short = shorten_path(str(checkout), str(ctx.home))
    pins = find_coord_pins(checkout)
    values: dict = {
        "present": True,
        "checkout": str(checkout),
        "local_version": LOCAL_COORD_VERSION,
        "pins": [
            {
                "workflow": p.workflow,
                "job": p.job,
                "spec": p.spec,
                "dist": p.dist,
                "extras": list(p.extras),
                "constraint": p.constraint,
            }
            for p in pins
        ],
    }

    if not pins:
        # coord-web's e2e/acceptance jobs shell out to `coord web --fixture`
        # as their Playwright webServer. No install step means either the
        # job was deleted or it is about to fail with "coord: not found".
        return _result(
            Severity.WARN,
            "coord-web CI installs no coord CLI",
            values,
            detail=(
                f"{short}/.github/workflows has no `pip install code-coordinator[server]` — "
                "the Playwright webServer that boots `coord web --fixture` (#1818) "
                "cannot start. See docs/ADR_COORD_WEB_CI.md."
            ),
        )

    specs = ", ".join(f"{p.spec} ({_where(p)})" for p in pins)

    tombstoned = [p for p in pins if p.is_tombstone]
    if tombstoned:
        return _result(
            Severity.CRIT,
            f"coord-web CI installs the dead distribution name: {specs}",
            values,
            detail=(
                f"`{TOMBSTONE_DIST}` is a permanent PyPI tombstone that will never gain "
                f"another release (#2106) — CI is frozen on its last-ever version. "
                f"Rename to `code-coordinator[{SERVER_EXTRA}]` in "
                f"{', '.join(sorted({_where(p) for p in tombstoned}))}."
            ),
        )

    clientonly = [p for p in pins if not p.has_server_extra]
    if clientonly:
        return _result(
            Severity.CRIT,
            f"coord-web CI installs a client-only coord: {specs}",
            values,
            detail=(
                f"`coord web` is a server command; uvicorn/Starlette live behind the "
                f"[{SERVER_EXTRA}] extra since the base/server split (#1237). "
                f"Add it in {', '.join(sorted({_where(p) for p in clientonly}))}."
            ),
        )

    exact = [p for p in pins if p.is_exact_pin]
    if exact:
        return _result(
            Severity.WARN,
            f"coord-web CI exact-pins coord: {specs}",
            values,
            detail=(
                "docs/ADR_COORD_WEB_CI.md decided track-latest precisely because an "
                "exact pin rots silently — the same failure as ~/.coord-cli-venv found "
                "three releases stale on 2026-07-29. An exact-pinned coord-web stays "
                "green while the coord its users actually run has already broken the "
                "contract. Relax to a `>=` floor."
            ),
        )

    floors = {p.floor for p in pins if p.floor}
    unsatisfiable = sorted(f for f in floors if _floor_exceeds(f, LOCAL_COORD_VERSION))
    if unsatisfiable:
        return _result(
            Severity.WARN,
            f"coord-web CI floor is ahead of this machine's coord: {specs}",
            values,
            detail=(
                f"floor {', '.join(unsatisfiable)} > local coord {LOCAL_COORD_VERSION} — "
                "either the floor names a release that is not out yet, or this machine "
                "is behind the coord coord-web's CI proves its frontend against."
            ),
        )

    floor_note = f" (floor {', '.join(sorted(floors))})" if floors else ""
    return _result(
        Severity.OK,
        f"tracks latest{floor_note}: {specs}",
        values,
        detail=f"local coord {LOCAL_COORD_VERSION}; source {short}",
    )
