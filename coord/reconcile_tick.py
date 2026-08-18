"""Shared primitive: run ``coord drive-queue tick --reconcile-only`` as a
subprocess and report ``(ok, detail)``.

Two independent surfaces need to run this exact command — the real CLI path,
with its own file lock (`coord.filelock.drive_queue_lock_path`) against a
concurrently-running timer tick on the same host, and its own ``--config``
resolution:

* ``coord release propagate``'s drain loop (`coord/commands/release.py`,
  `_run_reconcile_tick`, #2110) — polls this on every iteration so a drive
  that finishes *after* `coord-drive-queue.timer` has been stopped for the
  drain still gets reconciled from ``running`` to ``done``.
* `AgentServer.reconcile_drive_queue` (`coord/agent.py`, #2373) — reachable
  over the agent's HTTP API so `coord release propagate`'s drain-deadline
  escalation can ask a non-daemon ``launch_host`` to resolve its own
  cross-host liveness ambiguity (`coord.drive_queue._reconcile_running`'s
  #1870 guard) before escalating loudly. See ``docs/AGENT_OPERATIONS.md``'s
  #2373 section for the full incident writeup.

Per this repo's rule (CLAUDE.md, epic #2096): if two surfaces answer the
same question, they must call the same function. This is that function —
not two hand-maintained copies that quietly drift apart (as #2373's review
found: a hardcoded 120s timeout vs. a caller-supplied one, and a 200-char
detail truncation vs. a 2000-char one).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def run_reconcile_tick(
    config_path: Path | str,
    *,
    timeout: float = 120.0,
    detail_limit: int = 2000,
    runner=None,
) -> tuple[bool, str]:
    """Run ``coord drive-queue tick --reconcile-only --config <config_path>``.

    Never raises: a timeout or any other subprocess failure comes back as
    ``(False, "<ExceptionType>: <message>")`` rather than propagating — both
    callers treat this as a best-effort step inside a larger loop/escalation,
    not a gate that may take the caller down with it.

    *runner* is the `subprocess.run`-shaped seam tests substitute; *timeout*
    and *detail_limit* let each caller keep its own existing behavior
    (`_run_reconcile_tick`'s 120s/200-char vs. `reconcile_drive_queue`'s
    caller-supplied timeout/2000-char) without re-implementing the command
    itself twice.
    """
    run = runner or subprocess.run
    try:
        proc = run(
            [sys.executable, "-m", "coord.cli", "drive-queue", "tick",
             "--reconcile-only", "--config", str(config_path)],
            capture_output=True, text=True, timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001 — best effort, see docstring
        return False, f"{type(exc).__name__}: {exc}"
    ok = getattr(proc, "returncode", 1) == 0
    detail = (getattr(proc, "stderr", "") or getattr(proc, "stdout", "") or "").strip()
    return ok, detail[:detail_limit]
