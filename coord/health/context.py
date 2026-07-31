"""Build the :class:`~coord.health.models.HealthContext` a run needs (#1628).

Kept out of the registry so probes never reach for ``coord.config.load()``
or ``Path.home()`` themselves — a probe that only reads its context is one
that a unit test can drive with a fake filesystem and no ``coordinator.yml``.
"""

from __future__ import annotations

import socket
import time
from pathlib import Path
from typing import Any

from coord.health.models import Checkout, HealthContext


def local_checkouts(config: Any) -> tuple[Checkout, ...]:
    """The checkouts named in ``coordinator.yml`` that actually exist here.

    Same scope rule as ``coord diagnose --graph`` / ``--orphan-worktrees``:
    local machine only.  Machines whose name/host matches this hostname are
    considered first so a repo configured on several machines resolves to
    *this* box's path; any other machine's path is a fallback for the common
    single-user case where every machine shares a layout.
    """
    if config is None:
        return ()
    try:
        local_hostname = socket.gethostname().split(".")[0]
    except OSError:  # pragma: no cover — gethostname basically cannot fail
        local_hostname = ""

    seen: set[Path] = set()
    # Also deduped by repo NAME, not just path: a repo listed on several
    # machines with different paths, more than one of which happens to exist
    # here, must still be one row in the report. Without this the fallback
    # pass re-adds another machine's path for a repo the hostname-matched
    # pass already resolved, and every checkout-scope check reports it twice
    # — with the *other* machine's answer.
    seen_names: set[str] = set()
    out: list[Checkout] = []
    for pass_no in range(2):
        for machine in getattr(config, "machines", ()) or ():
            on_this_machine = (
                machine.name == local_hostname
                or machine.host.split(".")[0] == local_hostname
            )
            if pass_no == 0 and not on_this_machine:
                continue
            if pass_no == 1 and on_this_machine:
                continue
            for repo in getattr(config, "repos", ()) or ():
                raw = machine.repo_path(repo.name)
                if not raw:
                    continue
                if repo.name in seen_names:
                    continue
                path = Path(raw).expanduser()
                if path in seen or not (path / ".git").exists():
                    continue
                seen.add(path)
                seen_names.add(repo.name)
                out.append(
                    Checkout(
                        name=repo.name,
                        path=path,
                        default_branch=getattr(repo, "default_branch", "main") or "main",
                        develop_branch=getattr(repo, "develop_branch", None),
                    )
                )
    return tuple(out)


def build_context(
    config: Any = None,
    *,
    thresholds: Any = None,
    home: Path | None = None,
    coord_dir: Path | None = None,
    allow_network: bool = True,
    now: float | None = None,
) -> HealthContext:
    """Assemble a context from a loaded config (or from nothing at all).

    ``config=None`` is a supported mode: ``coord health`` on a machine with
    no ``coordinator.yml`` still reports every ``machine``-scope check, it
    just has no checkouts to sweep.
    """
    if thresholds is None:
        if config is not None and getattr(config, "health", None) is not None:
            thresholds = config.health
        else:
            from coord.config import HealthConfig  # noqa: PLC0415 — avoid import cycle

            thresholds = HealthConfig()
    resolved_home = home or Path.home()
    return HealthContext(
        thresholds=thresholds,
        home=resolved_home,
        coord_dir=coord_dir or (resolved_home / ".coord"),
        now=time.time() if now is None else now,
        checkouts=local_checkouts(config),
        config=config,
        allow_network=allow_network,
    )
