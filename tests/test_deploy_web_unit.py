"""Regression guard for `deploy/coord-web.service`'s stop bounds (#2095).

The 2026-08-10 0.5.15 -> 0.5.26 fleet roll took the phone dashboard offline
for the length of the run: coord-web serves `text/event-stream` endpoints
(coord/dashboard/server.py), uvicorn's graceful shutdown waits for every open
connection to drain, and an SSE stream does not close on its own — a browser
tab or the phone PWA left open holds systemd's stop open indefinitely. With
no `TimeoutStopSec`/`KillMode` tuning on the unit, systemd just waited.

This is a systemd *property* enforced by the init system, not by any code
this repo runs — same caveat as `test_deploy_drive_queue_unit.py` (#1830),
which this module otherwise mirrors. What CAN be pinned here is the two
lines the fix actually consists of on the `[Service]` section of the shipped
unit file. Losing either silently reopens #2095 on the next `deploy/`
install, and nothing else in the test suite would notice — this file is that
notice. See also `tests/test_agent_restart_services_endpoint.py` and the
`_restart_sibling_unit` tests in `tests/test_agent_update.py`, which pin the
caller-side half of the same fix (`systemctl restart --no-block`, plus a
liveness probe, deciding the outcome instead of a hard subprocess timeout).
"""

from __future__ import annotations

import configparser
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
UNIT_PATH = REPO_ROOT / "deploy" / "coord-web.service"


def _parse_unit(path: Path) -> configparser.RawConfigParser:
    # RawConfigParser (not ConfigParser): systemd's `%h`/`%%` specifiers
    # collide with configparser's default `%`-interpolation syntax and raise
    # otherwise (e.g. the `ExecStart=` line below).
    cp = configparser.RawConfigParser(strict=False)
    cp.read(path)
    return cp


def test_timeout_stop_sec_is_bounded() -> None:
    """The actual #2095 fix. Without this, systemd's default (90s, and
    unbounded in spirit — it only fires SIGKILL after waiting the whole
    window) leaves the unit `deactivating` for as long as an SSE client
    stays connected, which for a browser tab or the phone PWA is
    indefinite."""
    unit = _parse_unit(UNIT_PATH)
    stop_timeout = int(unit.get("Service", "TimeoutStopSec"))
    assert 0 < stop_timeout <= 30, (
        "TimeoutStopSec must be a real, short bound — long enough for a "
        "clean shutdown, nowhere near long enough for an SSE client to "
        "matter"
    )


def test_kill_mode_is_process() -> None:
    """KillMode=process targets only the main uvicorn process on the
    SIGKILL systemd sends once TimeoutStopSec elapses, rather than the
    default `control-group`. This also sidesteps the exact recovery failure
    the 2026-08-10 incident hit by hand afterwards: `systemctl --user kill
    -s SIGKILL coord-web` refused with "Failed to send signal SIGKILL to
    auxiliary processes: Invalid argument"."""
    unit = _parse_unit(UNIT_PATH)
    assert unit.get("Service", "KillMode") == "process"


def test_still_a_simple_unit() -> None:
    """The fix assumes `Type=simple` (a long-running server, not a
    short-lived launcher). If this ever changes, the stop-bound reasoning
    above needs re-deriving, not silently carrying forward."""
    unit = _parse_unit(UNIT_PATH)
    assert unit.get("Service", "Type") == "simple"


def test_fix_comment_names_the_issue() -> None:
    """Loose guard against a future edit deleting the explanatory comment
    along with (or instead of) the settings — the reasoning for why these
    lines must never be removed needs to stay attached to them."""
    text = UNIT_PATH.read_text()
    assert "#2095" in text
    assert "TimeoutStopSec=10" in text
    assert "KillMode=process" in text
