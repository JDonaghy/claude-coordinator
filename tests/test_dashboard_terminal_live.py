"""Live regression test for #1216: uvicorn needs a real WebSocket library
(``websockets`` or ``wsproto``) installed to perform an actual protocol
upgrade (101 Switching Protocols).

``tests/test_dashboard_terminal.py`` exercises ``/ws/terminal/{session_id}``
through Starlette's ``TestClient``, which drives the ASGI app in-process via
direct ``receive``/``send`` callables -- it never negotiates a real HTTP
Upgrade handshake, so it passes whether or not a WS library is installed.
That's exactly why the shipped suite didn't catch #1216: with no
``websockets``/``wsproto`` in the venv, a live ``uvicorn`` process answers a
real WS upgrade request with a plain HTTP 200 instead of 101, and every real
browser connection fails silently while this mocked suite stays green.

This test boots the real ASGI app under a real ``uvicorn.Server`` bound to an
actual TCP socket and connects with the real ``websockets`` client library
(not TestClient) -- proving the handshake genuinely upgrades. Without the
``websockets`` dependency, the ``connect()`` call below fails outright
(``InvalidStatus``/``InvalidMessage``) instead of yielding the expected
accept-then-close-4404 sequence for an unresolvable session_id (#1071).
"""

from __future__ import annotations

import socket
import threading
import time
from unittest.mock import patch

import pytest
import uvicorn
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

from coord.config import Config
from coord.dashboard.server import build_app
from coord.models import Board, Machine, Repo


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _UnusedSessionAttacher:
    """Every case here hits the accept-then-close-4404 unknown-session path,
    so ``attach()`` should never be invoked -- guard against that regressing
    into a real tmux/ssh spawn."""

    async def attach(self, host: str | None, session_name: str):
        raise AssertionError("attach() should not be called for an unknown session")


def _config() -> Config:
    return Config(
        repos=[Repo(name="api", github="acme/api")],
        machines=[Machine(
            name="laptop", host="laptop.tailnet", repos=["api"],
            repo_paths={"api": "/tmp/api"},
        )],
    )


class TestLiveUvicornWebSocketUpgrade:
    def test_ws_terminal_route_actually_upgrades(self) -> None:
        app = build_app(_config(), session_attacher=_UnusedSessionAttacher())
        port = _free_port()
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host="127.0.0.1",
                port=port,
                log_level="warning",
                # uvicorn's "auto" (production default, unchanged here) picks
                # its legacy websockets_impl when the `websockets` package is
                # installed, which only emits noisy DeprecationWarnings on
                # newer websockets releases -- harmless but distracting in
                # test output, so pin the modern sans-io implementation here.
                ws="websockets-sansio",
            )
        )

        # Patch read_board() to an empty in-memory Board rather than hitting
        # the real sqlite-backed board: (a) sqlite3 connections can't cross
        # threads, and the app runs in a background thread here, and (b) an
        # empty board is all this test needs -- session_id "does-not-exist"
        # resolves to nothing regardless of board content.
        with patch("coord.dashboard.server.read_board", return_value=Board()):
            thread = threading.Thread(target=server.run, daemon=True)
            thread.start()
            try:
                deadline = time.monotonic() + 5
                while not server.started and time.monotonic() < deadline:
                    time.sleep(0.05)
                assert server.started, "uvicorn server did not start in time"

                # The server accepts the WS upgrade (a real 101 -- the thing
                # #1216 broke) then closes with 4404 for the unresolvable
                # session_id. If the WS library were missing, this connect()
                # would raise instead of ever reaching the close frame.
                with connect(f"ws://127.0.0.1:{port}/ws/terminal/does-not-exist") as ws:
                    with pytest.raises(ConnectionClosed) as exc_info:
                        ws.recv()
                    assert exc_info.value.rcvd is not None
                    assert exc_info.value.rcvd.code == 4404
            finally:
                server.should_exit = True
                thread.join(timeout=5)
