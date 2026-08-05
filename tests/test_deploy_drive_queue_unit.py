"""Regression guard for `deploy/coord-drive-queue.service`'s `KillMode` (#1830).

This is a systemd *property*: whether a cgroup gets torn down when a
`Type=oneshot` unit finishes is enforced by the init system, not by any code
this repo runs, so there is no way to exercise the actual failure mode
(tick's own `tmux new-session` spawning a server that then dies with the
unit's cgroup) from pytest. See docs/DRIVE_QUEUE.md §2a for the honest,
systemd-level verification procedure (and why it can only be done with no
tmux server already running — an attended check cannot reproduce the bug).

What *can* be pinned here is the one line the fix actually consists of:
`KillMode=process` on the `[Service]` section of the shipped unit file. Losing
that line silently reopens #1830 on the next `deploy/` install, and nothing
else in the test suite would notice — this file is that notice.
"""

from __future__ import annotations

import configparser
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
UNIT_PATH = REPO_ROOT / "deploy" / "coord-drive-queue.service"


def _parse_unit(path: Path) -> configparser.RawConfigParser:
    # RawConfigParser (not ConfigParser): systemd's `%h`/`%%` specifiers
    # collide with configparser's default `%`-interpolation syntax and raise
    # otherwise (e.g. the `ExecStart=` line below).
    cp = configparser.RawConfigParser(strict=False)
    cp.read(path)
    return cp


def test_killmode_process_is_set() -> None:
    """The actual #1830 fix. Without it, systemd's default
    KillMode=control-group reaps any tmux server the tick's own
    `coord drive --tmux` had to spawn, the instant the oneshot tick exits —
    invisibly, whenever a tmux server already existed (the attended case)."""
    unit = _parse_unit(UNIT_PATH)
    assert unit.get("Service", "KillMode") == "process"


def test_still_a_oneshot_unit() -> None:
    """The fix assumes `Type=oneshot` (a short-lived launcher whose own exit
    must not take its children down). If this ever changes, the KillMode
    reasoning above needs re-deriving, not silently carrying forward."""
    unit = _parse_unit(UNIT_PATH)
    assert unit.get("Service", "Type") == "oneshot"


def test_killmode_comment_names_the_issue() -> None:
    """Loose guard against a future edit deleting the explanatory comment
    along with (or instead of) the setting — the reasoning for why this line
    must never be removed needs to stay attached to it."""
    text = UNIT_PATH.read_text()
    assert "#1830" in text
    assert "KillMode=process" in text
