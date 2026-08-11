"""Live regression test for #2095's core mechanism, against a real SSE
client and a real process boundary.

The issue: coord-web serves `text/event-stream` endpoints
(coord/dashboard/server.py -- e.g. `/api/chat`'s `stream()`/`canned()`).
uvicorn's graceful shutdown waits for open connections to drain, and an SSE
stream does not close on its own -- a connected browser tab or the phone
PWA holds it open indefinitely. Before this fix, the ONLY thing standing
between that and a hung restart was `coord/agent_app.py`'s hard 15s
`subprocess.run(["systemctl", "--user", "restart", unit], timeout=15)`,
which fired, abandoned the unit mid-stop, and reported a failure that still
printed a leading `✓` one layer up. The fix has two halves: (1) issue the
restart with `--no-block` and let the pre-existing `is-active` poll decide
the outcome (`_restart_sibling_unit`), and (2) bound the stop AT THE UNIT
with `TimeoutStopSec`/`KillMode=process` (`deploy/coord-web.service`) so
systemd itself escalates to SIGKILL rather than waiting forever.

Every other #2095 test -- `tests/test_agent_update.py`'s
`TestRestartSiblingUnit`/`TestSiblingLivenessProbe`,
`tests/test_cli_release_propagate.py` -- mocks `subprocess`/`urllib` at the
unit level. That proves the CODE THAT WOULD HANDLE a stuck stop behaves as
scripted; it does not prove an actual open SSE connection actually blocks
an actual graceful HTTP server shutdown, which is the entire premise the
fix rests on. This test is the one place that closes that gap.

It cannot drive real `systemctl`/systemd (there is no systemd user session
inside a sandboxed test run), so it reproduces the mechanism one layer
down, at the actual OS boundary the issue is about: a real ASGI app with a
never-closing SSE generator (the same shape as coord-web's real streaming
endpoints) is run under a real `uvicorn` **subprocess** -- not in-process,
specifically so real OS signals can be sent to it exactly as systemd would
-- a real HTTP client opens a real SSE connection and never closes it, and
then:

  * OLD mechanism (a bare SIGTERM, i.e. no unit-level stop bound -- the
    state `deploy/coord-web.service` was in before this issue): the
    process is demonstrated to still be alive well past a deadline that
    would comfortably contain a clean shutdown. This is "the old code
    hangs", reproduced against a live client.
  * NEW mechanism (SIGKILL after that deadline -- exactly what
    `TimeoutStopSec=10`/`KillMode=process` on `coord-web.service` makes
    systemd do on its own once a graceful stop doesn't land in time): the
    process is demonstrated to be gone, regardless of the open connection.
    This is "the new code does not hang".

This does not replace `tests/test_deploy_web_unit.py`'s check that the
packaged unit file actually carries those settings -- it proves *why* the
unit file needs them, against a live SSE client, which no mocked test can.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

# A minimal ASGI app whose one route mirrors the load-bearing property of
# coord-web's real SSE endpoints: a generator that keeps yielding until the
# CLIENT goes away -- it never closes on its own. Run via `python -c` in a
# child process rather than a Starlette TestClient/in-process uvicorn.Server
# so a real SIGTERM/SIGKILL can be sent to it exactly as systemd would.
_CHILD_SCRIPT = """
import asyncio
import os
import uvicorn
from starlette.applications import Starlette
from starlette.responses import StreamingResponse
from starlette.routing import Route


async def sse(request):
    async def gen():
        while True:
            yield "data: tick\\n\\n"
            await asyncio.sleep(0.2)
    return StreamingResponse(gen(), media_type="text/event-stream")


app = Starlette(routes=[Route("/events", sse)])

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ["SSE_TEST_PORT"]),
                log_level="warning")
"""


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_port(port: int, deadline: float) -> None:
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise AssertionError("server did not start listening in time")


def test_an_open_sse_connection_survives_sigterm_but_not_a_bounded_sigkill():
    port = _free_port()
    env = dict(os.environ, SSE_TEST_PORT=str(port))
    proc = subprocess.Popen([sys.executable, "-c", _CHILD_SCRIPT], env=env)
    resp = None
    try:
        _wait_for_port(port, time.monotonic() + 10)

        # A real, live SSE client that never closes -- exactly the shape of
        # the phone PWA / open browser tab that caused the 2026-08-10
        # incident. `readline()` blocks until data actually arrives, so
        # this proves the connection is genuinely open and streaming, not
        # just accepted.
        resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/events", timeout=5)
        assert resp.readline().strip() == b"data: tick"

        # OLD: a bare SIGTERM -- the entire stop mechanism `coord-web`
        # (and every sibling unit) had before #2095's TimeoutStopSec/
        # KillMode. uvicorn's graceful shutdown waits for the open
        # connection to drain; a client that never disconnects means it
        # never does. `_restart_sibling_unit`'s pre-fix hard 15s
        # `subprocess.run` timeout is exactly what this reproduces one
        # layer up: the wait never resolves on its own.
        proc.terminate()  # SIGTERM
        try:
            proc.wait(timeout=3)
            pytest.fail(
                "the server exited on a bare SIGTERM while an SSE "
                "connection was still open -- if this starts passing, "
                "uvicorn's graceful-shutdown behaviour changed and the "
                "#2095 fix (TimeoutStopSec/KillMode on coord-web.service) "
                "may no longer be load-bearing; re-read the issue before "
                "relying on that"
            )
        except subprocess.TimeoutExpired:
            pass  # confirmed: SIGTERM alone does not end this process
                  # while a live SSE client is attached -- "the old code
                  # hangs", against a real client.

        # NEW: exactly what `TimeoutStopSec=10` + `KillMode=process` makes
        # systemd do on its own once the graceful stop above doesn't land
        # in time -- escalate straight to SIGKILL against the main process
        # (not the whole cgroup -- see deploy/coord-web.service's header
        # for why `KillMode=process` specifically, tied to the incident's
        # own recovery failure: `systemctl --user kill -s SIGKILL` refused
        # under the default `KillMode=control-group`).
        proc.kill()  # SIGKILL
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pytest.fail("SIGKILL did not end the process within 5s")
        assert proc.returncode is not None  # "the new code does not hang"
    finally:
        if resp is not None:
            resp.close()
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
