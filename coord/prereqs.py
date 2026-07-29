"""External-tool prereq manifest and version probing (#1570 parts B/D/E).

coord shells out to a handful of external binaries — `git`, `gh`, and
whatever a machine's declared `capabilities:` promise (`cargo` for `rust`,
GTK4 dev libs for `gtk`, ...) — but until now it never checked any of them.
`shutil.which()` presence checks existed for a few binaries; nothing probed
*capability*, and nothing published what it found. #1564 fixed the sharpest
edge of this (the CI merge gate's `gh` floor, `GH_PR_CHECKS_JSON_MIN_VERSION`
in `coord/github_ops.py`) as a one-off inside the seam that needed it most.
This module generalizes that pattern into a manifest so the same probe/floor
machinery backs every tool coord depends on, not just `gh`:

- :func:`probe_all` — probe every baseline prereq plus whatever prereqs back
  a given set of capabilities. Used by `AgentServer.health()` (#1570 B) to
  publish resolved tool versions fleet-wide, and by `coord doctor` (#1570 E)
  to render a per-machine prereq report without SSHing anywhere.
- :func:`unmet_capabilities` — cross-reference a machine's *advertised*
  `capabilities:` claims against what its probes actually found, for the
  dispatcher to refuse routing capability-gated work to a machine that can't
  back its claim (#1570 D) instead of finding out 20 minutes into a worker.

A prereq's `min_version` is `None` until a floor has actually been confirmed
(the way #1564 confirmed gh's) — this module never invents one. `None` means
"probe for presence only," not "no requirement."
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Iterable

from coord.github_ops import GH_PR_CHECKS_JSON_MIN_VERSION

DEFAULT_PROBE_TIMEOUT = 10.0


@dataclass(frozen=True)
class Prereq:
    """One external-tool dependency coord relies on.

    ``capability`` is ``None`` for a baseline prereq — required on every
    machine regardless of its declared `capabilities:` — or the
    `coordinator.yml` capability name (`"rust"`, `"gtk"`, ...) whose promise
    this tool backs.
    """

    tool: str
    binary: str
    version_args: tuple[str, ...]
    version_re: str
    min_version: str | None
    capability: str | None
    what_breaks: str


# Required on every machine, no matter its declared capabilities — coord
# itself doesn't function without these.
BASELINE_PREREQS: tuple[Prereq, ...] = (
    Prereq(
        tool="git", binary="git", version_args=("--version",),
        version_re=r"git version (\S+)", min_version=None, capability=None,
        what_breaks="coord cannot inspect, commit, or push any repo",
    ),
    Prereq(
        tool="gh", binary="gh", version_args=("--version",),
        version_re=r"gh version (\S+)",
        # Single source of truth stays coord.github_ops — imported, not
        # duplicated, so the two never drift (#1564's own comment on the
        # constant flags exactly this risk).
        min_version=GH_PR_CHECKS_JSON_MIN_VERSION, capability=None,
        what_breaks=(
            "the CI merge gate cannot read check status — see "
            "coord.github_ops.GhTooOldForJsonChecks (#1564)"
        ),
    ),
)

# Gate a `coordinator.yml` `capabilities:` entry. Only probed for a machine
# that actually claims the matching capability (see `probe_all`) — a plain
# CLI-only box is never dinged for lacking a browser or GTK4.
CAPABILITY_PREREQS: tuple[Prereq, ...] = (
    Prereq(
        tool="cargo", binary="cargo", version_args=("--version",),
        version_re=r"cargo (\S+)", min_version=None, capability="rust",
        what_breaks="rust-capability work (cargo build/test) cannot run",
    ),
    Prereq(
        tool="python3", binary="python3", version_args=("--version",),
        version_re=r"Python (\S+)", min_version=None, capability="python",
        what_breaks="python-capability work cannot run",
    ),
    # tui/'s `--features gtk` build links against GTK4 via pkg-config
    # (tui/Cargo.toml: "GTK binary requires the `gtk` feature"); probing
    # pkg-config's module lookup is the cheapest real signal that the dev
    # libs (not just a runtime GTK) are actually present.
    Prereq(
        tool="gtk4", binary="pkg-config", version_args=("--modversion", "gtk4"),
        version_re=r"(\S+)", min_version=None, capability="gtk",
        what_breaks="the tui/ `--features gtk` build cannot link against GTK4",
    ),
    # `browser` is a worked example in coordinator.example.yml for
    # Playwright-style acceptance suites in consuming projects — this repo
    # doesn't use it itself. Presence-only: which browser binary a suite
    # expects is project-specific, so this checks the most common default.
    Prereq(
        tool="browser", binary="chromium", version_args=("--version",),
        version_re=r"(\S+)", min_version=None, capability="browser",
        what_breaks="browser-driven acceptance suites (e.g. Playwright) cannot run",
    ),
)

ALL_PREREQS: tuple[Prereq, ...] = BASELINE_PREREQS + CAPABILITY_PREREQS


@dataclass(frozen=True)
class ToolProbe:
    """Result of probing one :class:`Prereq`. Never raises to build one."""

    tool: str
    capability: str | None
    found: bool
    version: str | None
    min_version: str | None
    meets_floor: bool | None  # None: no floor to check, or version unknown
    what_breaks: str

    @property
    def ok(self) -> bool:
        """False when the tool is missing or fails its documented floor.

        A tool found but with an unparsable version (`meets_floor is None`
        with `min_version` set) is treated as ok=True — degrade to
        "unknown, assume fine" rather than false-failing on an output-format
        change, matching `_gh_version()`'s existing best-effort contract.
        """
        if not self.found:
            return False
        if self.min_version is not None and self.meets_floor is False:
            return False
        return True

    def to_dict(self) -> dict:
        return {
            "found": self.found,
            "version": self.version,
            "min_version": self.min_version,
            "meets_floor": self.meets_floor,
            "capability": self.capability,
            "ok": self.ok,
        }


def _parse_version(text: str, pattern: str) -> str | None:
    match = re.search(pattern, text)
    return match.group(1) if match else None


def _version_tuple(version: str) -> tuple[int, ...]:
    """Best-effort numeric tuple for comparison ("2.86.0" -> (2, 86, 0)).

    Non-numeric segments (pre-release suffixes etc.) collapse to 0 rather
    than raising — this only needs to be right for well-formed dotted
    versions, and must never blow up a probe over an odd one.
    """
    parts = []
    for segment in re.split(r"[.\-+]", version):
        match = re.match(r"\d+", segment)
        parts.append(int(match.group(0)) if match else 0)
    return tuple(parts)


def meets_floor(version: str, min_version: str) -> bool:
    """Whether `version` is >= `min_version`, comparing dotted numerics.

    Zero-pads the shorter tuple before comparing — plain tuple comparison
    would otherwise rank "2.86" below "2.86.0" (a shorter-but-equal prefix
    tuple compares as less than a longer one), which is wrong: they're the
    same version.
    """
    v = _version_tuple(version)
    m = _version_tuple(min_version)
    width = max(len(v), len(m))
    v = v + (0,) * (width - len(v))
    m = m + (0,) * (width - len(m))
    return v >= m


def probe(prereq: Prereq, *, timeout: float = DEFAULT_PROBE_TIMEOUT) -> ToolProbe:
    """Run `prereq`'s version probe and classify the result.

    Never raises — a missing binary, a hang, or unparsable output all
    degrade to a `ToolProbe` describing that, rather than blowing up a
    `/health` response or a `coord doctor` sweep over one flaky tool.
    """
    if shutil.which(prereq.binary) is None:
        return ToolProbe(
            tool=prereq.tool, capability=prereq.capability, found=False,
            version=None, min_version=prereq.min_version, meets_floor=None,
            what_breaks=prereq.what_breaks,
        )
    try:
        result = subprocess.run(
            [prereq.binary, *prereq.version_args],
            capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        # Present per `which` but unrunnable/hung — still "found" (the
        # binary exists), version simply couldn't be determined.
        return ToolProbe(
            tool=prereq.tool, capability=prereq.capability, found=True,
            version=None, min_version=prereq.min_version, meets_floor=None,
            what_breaks=prereq.what_breaks,
        )
    version = _parse_version((result.stdout or "") + (result.stderr or ""), prereq.version_re)
    floor_ok = None
    if version is not None and prereq.min_version is not None:
        floor_ok = meets_floor(version, prereq.min_version)
    return ToolProbe(
        tool=prereq.tool, capability=prereq.capability, found=True,
        version=version, min_version=prereq.min_version, meets_floor=floor_ok,
        what_breaks=prereq.what_breaks,
    )


def probe_all(
    capabilities: Iterable[str] = (), *, timeout: float = DEFAULT_PROBE_TIMEOUT
) -> dict[str, ToolProbe]:
    """Probe every baseline prereq plus every prereq gating a capability in
    `capabilities` (typically a machine's declared `capabilities:` list).

    Returns a dict keyed by tool name — JSON-friendly via `ToolProbe.to_dict`
    for embedding in a `/health` response.
    """
    caps = set(capabilities)
    relevant = list(BASELINE_PREREQS) + [
        p for p in CAPABILITY_PREREQS if p.capability in caps
    ]
    return {p.tool: probe(p, timeout=timeout) for p in relevant}


def tool_versions_summary(probes: dict[str, ToolProbe]) -> dict[str, dict]:
    """JSON-friendly form of `probe_all()`'s result."""
    return {tool: p.to_dict() for tool, p in probes.items()}


def unmet_capabilities(
    capabilities: Iterable[str], probes: dict[str, ToolProbe]
) -> dict[str, list[str]]:
    """Cross-reference declared `capabilities` against `probes` (typically
    from a `/health` response's `tool_versions`, already restricted to that
    machine's own advertised capabilities).

    Returns `{capability: [reason, ...]}` for every capability whose backing
    tool(s) failed their probe — empty dict when everything declared checks
    out. A capability with no registered prereq (nothing in
    `CAPABILITY_PREREQS` names it) is silently skipped, not flagged — an
    unprobed claim is not (yet) a *known-broken* one; this only reports
    claims this module can actually verify.
    """
    unmet: dict[str, list[str]] = {}
    for cap in capabilities:
        reasons = []
        for prereq in CAPABILITY_PREREQS:
            if prereq.capability != cap:
                continue
            p = probes.get(prereq.tool)
            if p is None:
                continue  # not probed — nothing to report either way
            if not p.found:
                reasons.append(f"{prereq.tool} not found ({prereq.what_breaks})")
            elif p.min_version is not None and p.meets_floor is False:
                reasons.append(
                    f"{prereq.tool} {p.version} < required {p.min_version} "
                    f"({prereq.what_breaks})"
                )
        if reasons:
            unmet[cap] = reasons
    return unmet
