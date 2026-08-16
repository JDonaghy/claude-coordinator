"""Resolve which distribution name this install landed under (#2103).

Part of the `claude-coordinator` -> `code-coordinator` rename (epic #2096).
**This module renames nothing** — `pyproject.toml` owns that, and since
#2104 it says `code-coordinator`.

The problem this originally closed: an agent reports its own version by
reading its distribution metadata *by name* (`coord/agent_app.py`'s
`_installed_version`, `AGENT_PKG_NAME`, `_detect_install_mode`;
`coord/agent_update.py`'s smoke check; `coord/cli.py`'s upgrade hint). Every
one of those hardcoded `"claude-coordinator"`. During the #2096 cutover,
some machines had `code-coordinator` installed instead, and a hardcoded
lookup raised/returned nothing for them, reporting an unknown version and
surfacing `coord agent update`'s `✗ did not come back` false negative even
though the agent was online and fully updated. #2103 fixed that by
resolving tolerantly against both names, in one place, instead of five
independent `try/except ImportError` blocks that could each drift.

**#2106 (R-4): the fallback is gone.** The fleet-wide cutover (#2105) is
done — every venv resolves `code-coordinator` and nothing resolves
`claude-coordinator` anymore, so a lookup that still needs the old name to
succeed is exactly the failure this module should now surface loudly rather
than mask. `claude-coordinator` is also a permanent PyPI tombstone (PyPI
cannot rename a project) that will never gain another release, so keeping
it as a live fallback target forever would only ever hide a genuinely
broken install. What #2103 still buys, and this keeps: every call site
below resolves through one place, and a miss is a named
:class:`DistributionNotFoundError`, never a bare ``None`` that downstream
renders as an unqualified "did not come back".
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

#: The one distribution name coord now ships under. Still a tuple — every
#: call site below is written against "however many candidates there are"
#: rather than a single hardcoded string, so a future rename only has to
#: change this line (see #2103's history for why that property matters).
CANDIDATE_NAMES: tuple[str, ...] = ("code-coordinator",)


class DistributionNotFoundError(LookupError):
    """Neither name in :data:`CANDIDATE_NAMES` is an installed distribution.

    #2103 acceptance #4: callers that need to *act* on the resolved name
    (not just report it) should let this propagate rather than silently
    coercing it to ``None`` — a bare ``None`` is exactly what downstream
    renders as an unqualified "did not come back", the false negative this
    module exists to kill. The message names every candidate tried.
    """


@dataclass(frozen=True)
class ResolvedDist:
    """Which of :data:`CANDIDATE_NAMES` is actually installed, and its
    version — the pair, not just the version, so a caller can *report*
    which name was found rather than silently picking one."""

    name: str
    version: str


def resolve_installed(names: tuple[str, ...] = CANDIDATE_NAMES) -> ResolvedDist:
    """Return the first of *names* that ``importlib.metadata`` knows about,
    paired with its installed version.

    Raises :class:`DistributionNotFoundError`, naming every candidate
    tried, when none of them is installed. Callers that want a tolerant
    "not installed" signal instead of an exception should catch that
    explicitly (see :func:`resolve_installed_name`) — it is not swallowed
    here.
    """
    tried: list[str] = []
    for name in names:
        try:
            return ResolvedDist(name=name, version=_pkg_version(name))
        except PackageNotFoundError:
            tried.append(name)
            continue
    raise DistributionNotFoundError(
        "no coordinator distribution installed — tried: " + ", ".join(tried)
    )


def resolve_installed_name(names: tuple[str, ...] = CANDIDATE_NAMES) -> str | None:
    """Convenience wrapper over :func:`resolve_installed` for callers that
    only need the name and are fine treating "neither installed" as
    ``None`` (best-effort reporting sites — see module docstring for the
    one site, the pip install target, that deliberately does NOT use this
    and lets :class:`DistributionNotFoundError` propagate instead)."""
    try:
        return resolve_installed(names).name
    except DistributionNotFoundError:
        return None


def pkg_spec(extra: str | None = None, names: tuple[str, ...] = CANDIDATE_NAMES) -> str:
    """Return the pip install target for whichever distribution is
    currently installed, with *extra* (e.g. ``"server"``) appended as
    ``name[extra]`` when given.

    #2103: the ``[server]`` extra must survive under whichever name
    resolves — a bare upgrade without it leaves a fresh venv without
    starlette/uvicorn and the agent dead on its next restart.

    Deliberately raises :class:`DistributionNotFoundError` rather than
    guessing when neither name is installed: this is the one call site
    (``coord/agent_app.py``'s ``/update`` handler) that decides what pip
    actually installs next, and silently defaulting there risks installing
    the wrong (possibly no-longer-published) name instead of failing loudly
    with both names named. The caller already has a "report failure"
    lane (``/update``'s ``last_update.json``) for exactly this.
    """
    name = resolve_installed(names).name
    return f"{name}[{extra}]" if extra else name
