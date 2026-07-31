"""How the agent's ``claude-coordinator`` is installed, and how current (#1628).

Two checks, one shared ``pip show`` call:

``agent_venv``
    The agent venv must be a **PyPI install**, not an editable one.  An
    editable agent runs whatever is checked out in someone's source tree —
    including a half-finished feature branch — which makes the machine's
    behaviour untraceable to any release.  #1182 is the recorded version of
    this going wrong (a stale non-editable install silently evaluating
    retired logic and producing a false merge-gate block); an *editable*
    agent is the same failure with no version number to blame it on.

``agent_version``
    How many released versions the install is behind.  The comparison is
    against **PyPI's simple index**, not the JSON API — see
    ``coord.health.pypi`` for why that distinction is load-bearing rather
    than pedantic.

Editable detection uses the ``Editable project location:`` line ``pip show``
prints only for editable installs; a PyPI install has just ``Location:``
pointing into site-packages.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from coord.health.models import CheckResult, HealthContext, Severity
from coord.health.registry import COST_NETWORK, check
from coord.health.units import expand, shorten_path

PROJECT = "claude-coordinator"

# Where install-agent.sh puts the agent's venv.  Overridable via
# `health.agent_venv_python`.
_DEFAULT_AGENT_VENV = "~/.coord-venv/bin/python3"


def resolve_agent_python(ctx: HealthContext) -> Path:
    """The interpreter whose environment we're reporting on.

    Configured value wins; otherwise the standard agent venv when it exists;
    otherwise the running interpreter (which is the honest answer on a
    coordinator-only box that never installed an agent venv).
    """
    configured = getattr(ctx.thresholds, "agent_venv_python", None)
    if configured:
        return expand(configured, ctx.home)
    candidate = expand(_DEFAULT_AGENT_VENV, ctx.home)
    if candidate.exists():
        return candidate
    return Path(sys.executable)


def pip_show(python: Path, *, timeout: float = 8.0) -> dict[str, str]:
    """Parse ``<python> -m pip show claude-coordinator`` into a field dict.

    Returns ``{}`` when pip isn't there or the package isn't installed.
    Raises only for genuinely unexpected conditions — the probe wrapping this
    fails soft either way.
    """
    result = subprocess.run(
        [str(python), "-m", "pip", "show", PROJECT],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        return {}
    fields: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            fields[key.strip()] = value.strip()
    return fields


@check(
    id="agent_venv",
    scope="machine",
    title="agent venv",
    order=40,
    description="The agent's claude-coordinator install is a PyPI install, not editable.",
)
def probe_agent_venv(ctx: HealthContext) -> CheckResult:
    python = resolve_agent_python(ctx)
    try:
        fields = pip_show(python)
    except (OSError, subprocess.SubprocessError) as exc:
        return CheckResult(
            check_id="agent_venv",
            scope="machine",
            severity=Severity.UNKNOWN,
            headroom=f"could not run pip show ({type(exc).__name__})",
            error=str(exc),
            values={"python": str(python)},
        )

    if not fields:
        return CheckResult(
            check_id="agent_venv",
            scope="machine",
            severity=Severity.UNKNOWN,
            headroom=f"{PROJECT} not installed for {shorten_path(str(python), str(ctx.home))}",
            error="pip show returned nothing",
            values={"python": str(python)},
        )

    editable_location = fields.get("Editable project location") or ""
    version = fields.get("Version", "")
    location = fields.get("Location", "")

    if editable_location:
        return CheckResult(
            check_id="agent_venv",
            scope="machine",
            severity=Severity.CRIT,
            headroom=f"editable {version or '?'} from {shorten_path(editable_location, str(ctx.home))}",
            detail=(
                "an editable agent runs whatever is checked out in that tree — "
                "its behaviour is not traceable to any release"
            ),
            threshold="crit when editable",
            values={
                "python": str(python),
                "version": version,
                "editable": True,
                "editable_location": editable_location,
                "location": location,
            },
        )

    return CheckResult(
        check_id="agent_venv",
        scope="machine",
        severity=Severity.OK,
        headroom=f"pypi {version or '?'}",
        detail=shorten_path(location, str(ctx.home)) if location else "",
        values={
            "python": str(python),
            "version": version,
            "editable": False,
            "editable_location": None,
            "location": location,
        },
    )


@check(
    id="agent_version",
    scope="machine",
    title="agent version",
    order=41,
    cost=COST_NETWORK,
    description="Installed claude-coordinator vs the latest release on PyPI's simple index.",
)
def probe_agent_version(ctx: HealthContext) -> CheckResult:
    from coord.health.pypi import latest_release, parse_version  # noqa: PLC0415

    th = ctx.thresholds
    python = resolve_agent_python(ctx)
    try:
        fields = pip_show(python)
    except (OSError, subprocess.SubprocessError) as exc:
        fields = {}
        installed_raw = ""
        pip_error: str | None = f"{type(exc).__name__}: {exc}"
    else:
        installed_raw = fields.get("Version", "")
        pip_error = None

    if not installed_raw:
        return CheckResult(
            check_id="agent_version",
            scope="machine",
            severity=Severity.UNKNOWN,
            headroom="installed version unknown",
            error=pip_error or "pip show reported no Version",
            values={"python": str(python)},
        )

    try:
        latest, finals = latest_release(
            PROJECT,
            index_url=th.pypi_index_url,
            timeout=th.network_timeout_secs,
        )
    except Exception as exc:  # noqa: BLE001 — any network/parse failure is "unknown"
        return CheckResult(
            check_id="agent_version",
            scope="machine",
            severity=Severity.UNKNOWN,
            headroom=f"installed {installed_raw}, PyPI index unreachable",
            error=f"{type(exc).__name__}: {exc}",
            values={"python": str(python), "installed": installed_raw},
        )

    installed = parse_version(installed_raw)
    if installed is None or latest is None:
        return CheckResult(
            check_id="agent_version",
            scope="machine",
            severity=Severity.UNKNOWN,
            headroom=f"installed {installed_raw}, could not compare against the index",
            error="unparseable version",
            values={"python": str(python), "installed": installed_raw},
        )

    behind = sum(1 for v in finals if v > installed)
    if behind >= th.agent_version_crit_behind:
        severity = Severity.CRIT
    elif behind >= th.agent_version_warn_behind:
        severity = Severity.WARN
    else:
        severity = Severity.OK

    if behind == 0:
        headroom = f"{installed_raw} (latest {latest.raw})"
    else:
        headroom = (
            f"{installed_raw}, {behind} release{'s' if behind != 1 else ''} behind "
            f"(latest {latest.raw})"
        )

    return CheckResult(
        check_id="agent_version",
        scope="machine",
        severity=severity,
        headroom=headroom,
        threshold=f"crit at {th.agent_version_crit_behind} behind",
        values={
            "python": str(python),
            "installed": installed_raw,
            "latest": latest.raw,
            "releases_behind": behind,
            "index_url": th.pypi_index_url,
            "warn_behind": th.agent_version_warn_behind,
            "crit_behind": th.agent_version_crit_behind,
        },
    )
