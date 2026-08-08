"""Systemd unit-file drift against `deploy/` (#1831).

`deploy/*.service`/`*.timer` is version-controlled, reviewed, and merged
like code. **Nothing ever installs it.** The release path is bump -> PR ->
merge -> tag push -> `publish.yml` -> PyPI, then `coord agent update` for the
venvs — no step copies `deploy/*.service` into `~/.config/systemd/user/`.
Unit files are hand-installed once at machine setup and drift forever after.

The 2026-08-04 incident this closes: dellserver's `coord-serve.service` was
three weeks stale, its `Environment=PATH=` still starting with an **editable**
checkout of this repo (`~/src/claude-coordinator/.venv/bin`). `coord_argv()`
(`coord/drive.py`) resolves subprocesses via `shutil.which("coord")` — i.e.
from that PATH — so the daemon itself ran the pinned release while
everything it spawned ran whatever stale branch that checkout happened to be
on. Two failure modes, one probe:

`unit_drift`
    Per deploy-lane unit: does the installed copy under
    `~/.config/systemd/user/` match `deploy/<name>`? Absence is the common
    case (most machines don't run every lane) and is reported OK, not a
    fault — same convention as `cli_venv`/`tui_binary`
    (:mod:`coord.health.checks.deploy_lane_facts`).

`_path_shadow_risk`
    Independent of content drift: does the installed unit's
    `Environment=PATH=` put an editable checkout's `.venv/bin` ahead of the
    release entry points (`~/.local/bin`, `~/.coord-venv/bin`)? This is what
    made the drift above *harmful* rather than merely untidy, and it can
    exist even on a unit whose content otherwise matches `deploy/` bit for
    bit if `deploy/` itself regresses — which is exactly what happened to
    `coord-serve.service`'s v0.4.105 cut (it dropped the #1117 PATH entry
    that had also fixed a real bug). CRIT regardless of the content-diff
    verdict — a shadowed release is the split-brain, not a cosmetic
    difference.

#1927 — where the reference comes from
--------------------------------------
The original cut of this check diffed the installed unit against
``<checkout>/deploy/<name>``: a file in the host's own git working copy that
nothing verifies is at the released tag, or current at all. Installed units
and checkouts go stale for the *same* reason (nobody pulled), so they go
stale *together* — and when they do the comparison reports clean. The check
was least reliable in exactly the case it exists to catch, and the remedy it
printed (``cp <checkout>/deploy/... ~/.config/systemd/user/...``) sourced
from the same unverified working copy, cementing the stale unit.

So the reference is now the *packaged* unit set — ``coord/deploy/`` inside
the installed distribution (see :func:`packaged_unit_dir`). That is the
released artifact for the version this process is running, and it cannot
drift with the host. When the reference is NOT a released artifact (an
editable/source checkout, a configured directory, or an old wheel that ships
no units) the verdict is annotated and a *match* grades UNKNOWN rather than
OK: an un-annotated green from an unverified reference is worse than no
check at all.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from pathlib import Path

from coord.health.models import CheckResult, HealthContext, Severity
from coord.health.registry import check
from coord.health.units import expand, human_hours

_UNIT_GLOBS = ("*.service", "*.timer")
_SYSTEMD_USER_DIR = "~/.config/systemd/user"

# Entry points that resolve to the pinned release. A `.venv/bin` entry ahead
# of ALL of these on a unit's PATH can shadow it (#1831's dellserver case).
_RELEASE_MARKERS = ("/.local/bin", "/.coord-venv/bin")

_PATH_LINE_RE = re.compile(r"^Environment\s*=\s*PATH=(.*)$", re.MULTILINE)


def packaged_unit_dir():
    """`coord/deploy/` inside *this* installed distribution, or None.

    Shipped as package data (see `pyproject.toml`), so on a pip-installed
    host it is the unit set as of the installed version — the released
    artifact, which cannot drift with the host's git checkout (#1927).
    Returns None on a wheel old enough to predate #1927, which is why the
    working-copy fallbacks below still exist.
    """
    candidate = Path(__file__).resolve().parent.parent.parent / "deploy"
    if candidate.is_dir() and _unit_files(candidate):
        return candidate
    return None


def in_git_worktree(path: Path) -> bool:
    """Is `path` inside a git working copy?

    The discriminator between "released artifact" and "working copy" for
    :func:`packaged_unit_dir`: an editable/source install puts the package
    under a checkout, where `coord/deploy/` is as unverified as any other
    tracked file. A pip-installed wheel lands in `site-packages`, which has
    no `.git` above it.
    """
    try:
        resolved = path.resolve()
    except OSError:  # pragma: no cover - resolve() is effectively total here
        return False
    for parent in (resolved, *resolved.parents):
        try:
            if (parent / ".git").exists():
                return True
        except OSError:  # pragma: no cover - unreadable ancestor
            continue
    return False


def installed_version() -> str | None:
    """The version of the installed `claude-coordinator`, or None."""
    try:
        from importlib.metadata import version

        return version("claude-coordinator")
    except Exception:  # pragma: no cover - metadata missing in odd installs
        return None


@dataclass(frozen=True)
class UnitReference:
    """The thing installed units are diffed against, and how much it's worth.

    `verified` is the whole point of #1927: only a reference that is the
    released artifact for the installed version can turn a match into a
    green. Everything else is a working copy whose own currency is unknown,
    so its match is reported as UNKNOWN with `label` naming what was
    actually compared.
    """

    path: Path
    source: str  # "package" | "configured" | "checkout"
    verified: bool
    version: str | None = None

    @property
    def label(self) -> str:
        if self.source == "package":
            ver = f" {self.version}" if self.version else ""
            if self.verified:
                return f"the packaged units of installed coord{ver}"
            return f"coord{ver}'s packaged units (SOURCE CHECKOUT, unverified)"
        if self.source == "configured":
            return f"configured reference {self.path} (unverified working copy)"
        return f"{self.path} (unverified working copy)"

    @property
    def short_label(self) -> str:
        """The `headroom` half of :attr:`label` — one line, no path."""
        if self.source == "package" and self.verified:
            return f"packaged coord{' ' + self.version if self.version else ''}"
        return f"{self.path}"


def resolve_reference(ctx: HealthContext) -> UnitReference | None:
    """Where to read the reference units from, and whether it's trustworthy.

    Order (#1927): the packaged units of the running distribution first —
    they are the released artifact and are the only reference that cannot go
    stale with the host. `health.deploy_dir` and then the first local
    checkout's `deploy/` remain as fallbacks for wheels that predate #1927
    (and for operators who deliberately point the check elsewhere), but both
    are working copies and are flagged as such.
    """
    packaged = packaged_unit_dir()
    if packaged is not None:
        return UnitReference(
            path=packaged,
            source="package",
            verified=not in_git_worktree(packaged),
            version=installed_version(),
        )
    fallback = resolve_deploy_dir(ctx)
    if fallback is None:
        return None
    configured = getattr(ctx.thresholds, "deploy_dir", None)
    return UnitReference(
        path=fallback,
        source="configured" if configured else "checkout",
        verified=False,
    )


def resolve_deploy_dir(ctx: HealthContext):
    """The checked-in `deploy/` this machine can diff installed units against.

    The #1927 *fallback* reference, used only when the installed
    distribution ships no `coord/deploy/` of its own — see
    :func:`resolve_reference`, which is what the probe calls.

    Configured `health.deploy_dir` wins outright; otherwise the first local
    checkout (see `coord.health.context.local_checkouts`) that has one —
    normally the `claude-coordinator` entry in `repo_paths`.
    """
    configured = getattr(ctx.thresholds, "deploy_dir", None)
    if configured:
        return expand(configured, ctx.home)
    for checkout in ctx.checkouts:
        candidate = checkout.path / "deploy"
        if candidate.is_dir():
            return candidate
    return None


def resolve_systemd_user_dir(ctx: HealthContext):
    """Where installed systemd *user* units actually live on this machine."""
    configured = getattr(ctx.thresholds, "systemd_user_dir", None)
    if configured:
        return expand(configured, ctx.home)
    return expand(_SYSTEMD_USER_DIR, ctx.home)


def _unit_files(deploy_dir):
    seen = set()
    out = []
    for pattern in _UNIT_GLOBS:
        for path in sorted(deploy_dir.glob(pattern)):
            if path.name in seen:
                continue
            seen.add(path.name)
            out.append(path)
    return sorted(out, key=lambda p: p.name)


def _diff_summary(installed_text: str, deploy_text: str) -> tuple[int, int | None]:
    """(changed line count, first differing line number in the installed
    file) between two unit files — a cheap stand-in for a full diff in a
    one-line `headroom` string."""
    diff = list(
        difflib.unified_diff(
            installed_text.splitlines(), deploy_text.splitlines(), lineterm=""
        )
    )
    changed = sum(
        1 for line in diff if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
    )
    first_line = None
    for line in diff:
        m = re.match(r"@@ -(\d+)", line)
        if m:
            first_line = int(m.group(1))
            break
    return changed, first_line


def find_path_shadow(installed_text: str) -> str | None:
    """The PATH entry that shadows the release, or None if the installed
    unit's PATH is safe (no editable checkout ahead of a release marker).

    A "release marker" is `~/.local/bin` or `~/.coord-venv/bin` — either
    resolves `coord` to the pinned install (`~/.local/bin/coord` is a
    symlink onto `~/.coord-venv/bin/coord`). An entry whose LAST path
    component is `.venv/bin` (a project-local dev venv, as opposed to the
    dot-prefixed-but-distinct `.coord-venv`/`.coord-cli-venv`) ahead of that
    marker is exactly the #1831 split-brain: `shutil.which("coord")`
    (`coord_argv()`, `coord/drive.py`) resolves it first.

    Only the LAST `Environment=PATH=` directive is read — systemd unit files
    may repeat `Environment=`, and later directives for the same key are
    what actually take effect.
    """
    matches = _PATH_LINE_RE.findall(installed_text)
    if not matches:
        return None
    entries = [e for e in matches[-1].split(":") if e]

    release_idx = None
    for idx, entry in enumerate(entries):
        stripped = entry.rstrip("/")
        if any(stripped.endswith(marker) for marker in _RELEASE_MARKERS):
            release_idx = idx
            break

    for idx, entry in enumerate(entries):
        if release_idx is not None and idx >= release_idx:
            break
        if entry.rstrip("/").endswith("/.venv/bin"):
            return entry
    return None


@check(
    id="unit_drift",
    scope="machine",
    title="unit drift",
    order=44,
    description=(
        "Installed systemd user units (~/.config/systemd/user/) match the "
        "units packaged with the installed release, and no unit's PATH lets "
        "an editable checkout shadow that release (#1831, #1927)."
    ),
)
def probe_unit_drift(ctx: HealthContext) -> list[CheckResult]:
    reference = resolve_reference(ctx)
    if reference is None:
        return [
            CheckResult(
                check_id="unit_drift",
                scope="machine",
                severity=Severity.OK,
                headroom="no deploy/ checkout found on this machine",
                values={"deploy_dir": None, "reference_source": None},
            )
        ]

    deploy_dir = reference.path
    installed_dir = resolve_systemd_user_dir(ctx)
    results: list[CheckResult] = []
    for deploy_path in _unit_files(deploy_dir):
        name = deploy_path.name
        installed_path = installed_dir / name
        values: dict = {
            "deploy_path": str(deploy_path),
            "installed_path": str(installed_path),
            # #1927: a green is only interpretable alongside what produced
            # it, so every result carries its reference — and whether that
            # reference is the released artifact or a working copy.
            "reference_dir": str(reference.path),
            "reference_source": reference.source,
            "reference_verified": reference.verified,
            "reference_version": reference.version,
        }

        if not installed_path.exists():
            results.append(
                CheckResult(
                    check_id="unit_drift",
                    scope="machine",
                    subject=name,
                    severity=Severity.OK,
                    headroom="not installed on this machine",
                    values={**values, "installed": False},
                )
            )
            continue

        try:
            deploy_text = deploy_path.read_text()
            installed_text = installed_path.read_text()
            installed_mtime = installed_path.stat().st_mtime
        except OSError as exc:
            results.append(
                CheckResult(
                    check_id="unit_drift",
                    scope="machine",
                    subject=name,
                    severity=Severity.UNKNOWN,
                    headroom=f"could not read unit: {exc}",
                    error=str(exc),
                    values={**values, "installed": True},
                )
            )
            continue

        values["installed"] = True
        values["installed_mtime"] = installed_mtime
        matches = deploy_text == installed_text
        values["matches"] = matches
        shadow_entry = find_path_shadow(installed_text)
        values["shadow_entry"] = shadow_entry

        if shadow_entry:
            age = ctx.now - installed_mtime
            detail = (
                f"editable checkout '{shadow_entry}' precedes the release "
                "entry point on this unit's PATH — shutil.which(\"coord\") in "
                "subprocesses this unit spawns resolves the checkout instead "
                "of the pinned release (#1831). Reorder PATH= so ~/.local/bin "
                "or ~/.coord-venv/bin comes first."
            )
            results.append(
                CheckResult(
                    check_id="unit_drift",
                    scope="machine",
                    subject=name,
                    severity=Severity.CRIT,
                    headroom=f"PATH shadow risk ({human_hours(age)} since install)",
                    detail=detail,
                    threshold="crit when a .venv/bin entry precedes ~/.local/bin or ~/.coord-venv/bin",
                    values=values,
                )
            )
            continue

        if not matches:
            changed, first_line = _diff_summary(installed_text, deploy_text)
            age = ctx.now - installed_mtime
            values["diff_lines"] = changed
            values["first_diff_line"] = first_line
            where = f", first differing at line {first_line}" if first_line else ""
            # The remedy sources from the SAME file the diff read (#1927) —
            # a `cp` out of an unverified checkout is how a stale unit got
            # cemented in the first place.
            results.append(
                CheckResult(
                    check_id="unit_drift",
                    scope="machine",
                    subject=name,
                    severity=Severity.WARN,
                    headroom=(
                        f"stale — installed {human_hours(age)} ago, {changed} "
                        f"line(s) differ from {reference.short_label}"
                        f"{where}"
                    ),
                    detail=(
                        f"cp {deploy_path} {installed_path} && systemctl --user "
                        f"daemon-reload && systemctl --user restart "
                        f"{name.rsplit('.', 1)[0]}   # reference: {reference.label}"
                    ),
                    threshold=f"warn when installed content != {reference.short_label}",
                    values=values,
                )
            )
            continue

        if not reference.verified:
            # Content matches, but the reference is a working copy nothing
            # verified is current (#1927). Reporting OK here is the exact
            # false green this check was rebuilt to stop emitting: a stale
            # checkout and a stale installed unit agree with each other.
            results.append(
                CheckResult(
                    check_id="unit_drift",
                    scope="machine",
                    subject=name,
                    severity=Severity.UNKNOWN,
                    headroom=(
                        f"matches {reference.short_label}, but that reference "
                        "is an unverified working copy — cannot confirm this "
                        "is the released unit"
                    ),
                    detail=(
                        f"diffed against {reference.label}. Nothing checks "
                        "that copy is at the released tag, and an installed "
                        "unit drifts for the same reason a checkout does, so "
                        "the two go stale together and agree (#1927). Install "
                        "a release wheel on this host (it ships coord/deploy/) "
                        "to make this comparison meaningful."
                    ),
                    values=values,
                )
            )
            continue

        results.append(
            CheckResult(
                check_id="unit_drift",
                scope="machine",
                subject=name,
                severity=Severity.OK,
                headroom=f"matches {reference.short_label}",
                values=values,
            )
        )

    return results
