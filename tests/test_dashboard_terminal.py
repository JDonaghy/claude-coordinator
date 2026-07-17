"""Tests for the /ws/terminal PTY<->WebSocket bridge (#1065).

Uses Starlette's in-process TestClient.websocket_connect (no real socket) and
a fake SessionAttacher (no real ssh/tmux) per the issue's acceptance bar:
"Keep the ssh/tmux attach behind an injectable seam ... so tests need no real
ssh."

Mind the TestClient's blind spot (#1071): ``websocket_connect`` reports a close
code whether or not the app ``accept()``ed the handshake first, so a rejection
test written against it passes even when a real browser gets a bare HTTP 403
with no code attached -- which is exactly how the 4404 "session gone" signal
shipped broken. Rejection paths therefore assert on the raw ASGI message
sequence via ``_raw_ws_messages`` instead; see
``test_unknown_session_accepts_before_closing_4404``.
"""
# #1229 regression: TmuxSessionAttacher must always pass TERM to its subprocess

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from starlette.testclient import TestClient

from coord.config import Config
from coord.dashboard.server import build_app
from coord.dashboard.terminal import (
    WEB_TOKEN_ENV,
    TmuxSessionAttacher,
    resolve_session_target,
    resolve_web_token,
)
from coord.models import Assignment, Board, Machine, Repo


@pytest.fixture(autouse=True)
def _no_spa_dist(monkeypatch: pytest.MonkeyPatch) -> None:
    """See tests/test_dashboard.py::_no_spa_dist -- same isolation, this
    module doesn't touch "/" but keeps behaviour consistent regardless."""
    monkeypatch.setattr(
        "coord.dashboard.server.WEBAPP_DIST",
        Path("/nonexistent/dist"),
    )


def _config() -> Config:
    return Config(
        repos=[Repo(name="api", github="acme/api")],
        machines=[Machine(
            name="laptop", host="laptop.tailnet", repos=["api"],
            repo_paths={"api": "/tmp/api"},
        )],
    )


def _board() -> Board:
    return Board(
        active=[
            Assignment(
                machine_name="laptop", repo_name="api",
                issue_number=42, issue_title="Fix auth",
                assignment_id="abc123", status="running",
            ),
        ],
    )


class _FakeAttachedPty:
    """Fakes one client's attach to a session that lives independently in
    ``_FakeSessionAttacher.live_sessions`` -- lets tests distinguish "this
    client's relay ended" (detach) from "the underlying session died" (kill),
    which is exactly the distinction #1065 requires the bridge to preserve.
    """

    def __init__(self, attacher: "_FakeSessionAttacher", session_name: str) -> None:
        self._attacher = attacher
        self._session_name = session_name
        self.written = bytearray()
        self.resizes: list[tuple[int, int]] = []
        self.detach_called = False
        self._outbox: asyncio.Queue[bytes] = asyncio.Queue()

    def push_output(self, data: bytes) -> None:
        self._outbox.put_nowait(data)

    async def read(self) -> bytes:
        return await self._outbox.get()

    def write(self, data: bytes) -> None:
        self.written.extend(data)

    def resize(self, cols: int, rows: int) -> None:
        self.resizes.append((cols, rows))

    def detach(self) -> None:
        self.detach_called = True
        # Crucially: detaching a client does NOT touch live_sessions.


class _FakeSessionAttacher:
    def __init__(self) -> None:
        self.live_sessions: dict[str, bool] = {}
        self.attach_calls: list[tuple[str | None, str]] = []
        self.last_pty: _FakeAttachedPty | None = None

    async def attach(self, host: str | None, session_name: str) -> _FakeAttachedPty:
        self.attach_calls.append((host, session_name))
        self.live_sessions[session_name] = True
        pty = _FakeAttachedPty(self, session_name)
        self.last_pty = pty
        return pty


def _client(*, token: str | None = None, attacher: _FakeSessionAttacher | None = None) -> TestClient:
    return TestClient(build_app(_config(), token=token, session_attacher=attacher))


async def _raw_ws_messages(
    path: str,
    *,
    query_string: bytes = b"",
    token: str | None = None,
    attacher: _FakeSessionAttacher | None = None,
) -> list[dict]:
    """Drive the ASGI app directly and return every ``send`` message, in order.

    ``TestClient.websocket_connect`` surfaces a close code whether or not the
    app accepted the handshake first, so it *cannot* see the #1071 live bug:
    the app closed with 4404 pre-accept, the test read 4404 and passed, but a
    real browser got a plain HTTP 403 with no code and retried forever. Only
    the raw message sequence distinguishes the two, so assert on that.
    """
    app = build_app(_config(), token=token, session_attacher=attacher)
    scope = {
        "type": "websocket",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "scheme": "ws",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query_string,
        "root_path": "",
        "headers": [(b"host", b"testserver")],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "subprotocols": [],
    }
    incoming: list[dict] = [
        {"type": "websocket.connect"},
        {"type": "websocket.disconnect", "code": 1005},
    ]
    sent: list[dict] = []

    async def receive() -> dict:
        return incoming.pop(0) if incoming else {"type": "websocket.disconnect", "code": 1005}

    async def send(message: dict) -> None:
        sent.append(message)

    await app(scope, receive, send)
    return sent


class TestTerminalBridge:
    def test_bytes_round_trip_both_directions(self) -> None:
        attacher = _FakeSessionAttacher()
        client = _client(attacher=attacher)
        with (
            patch("coord.dashboard.server.read_board", return_value=_board()),
            patch("coord.dashboard.terminal._local_short_hostname", return_value="laptop"),
        ):
            with client.websocket_connect("/ws/terminal/abc123") as ws:
                ws.send_bytes(b"echo hi\n")
                attacher.last_pty.push_output(b"hi\r\n")
                assert ws.receive_bytes() == b"hi\r\n"
        assert bytes(attacher.last_pty.written) == b"echo hi\n"
        assert attacher.attach_calls == [(None, "coord-abc123")]

    def test_resize_forwarded_to_pty(self) -> None:
        attacher = _FakeSessionAttacher()
        client = _client(attacher=attacher)
        with patch("coord.dashboard.server.read_board", return_value=_board()):
            with client.websocket_connect("/ws/terminal/abc123") as ws:
                ws.send_text(json.dumps({"type": "resize", "cols": 120, "rows": 40}))
                # Round-trip a byte so we know the resize message was
                # processed before we tear the connection down.
                ws.send_bytes(b"x")
        assert attacher.last_pty.resizes == [(120, 40)]

    def test_disconnect_detaches_but_never_kills_session(self) -> None:
        attacher = _FakeSessionAttacher()
        client = _client(attacher=attacher)
        with patch("coord.dashboard.server.read_board", return_value=_board()):
            with client.websocket_connect("/ws/terminal/abc123") as ws:
                ws.send_bytes(b"hello")
            # `with` block exit closes the client side of the WS.

        pty = attacher.last_pty
        assert pty.detach_called is True
        # The underlying tmux session must still be considered live -- a
        # WebSocket disconnect is a detach, never a kill (#1065 core
        # requirement).
        assert attacher.live_sessions["coord-abc123"] is True

    def test_missing_token_rejects_with_4401(self) -> None:
        attacher = _FakeSessionAttacher()
        client = _client(token="s3cret", attacher=attacher)
        with patch("coord.dashboard.server.read_board", return_value=_board()):
            # The handshake is accepted so the 4401 can actually be delivered
            # (#1071) -- the rejection lands on the first receive, not on
            # connect. See test_bad_token_accepts_before_closing_4401.
            with client.websocket_connect("/ws/terminal/abc123") as ws:
                message = ws.receive()
        assert message["type"] == "websocket.close"
        assert message["code"] == 4401
        assert attacher.attach_calls == []

    def test_wrong_token_rejects_with_4401(self) -> None:
        attacher = _FakeSessionAttacher()
        client = _client(token="s3cret", attacher=attacher)
        with patch("coord.dashboard.server.read_board", return_value=_board()):
            with client.websocket_connect("/ws/terminal/abc123?token=wrong") as ws:
                message = ws.receive()
        assert message["type"] == "websocket.close"
        assert message["code"] == 4401
        assert attacher.attach_calls == []

    def test_correct_token_accepts_upgrade(self) -> None:
        attacher = _FakeSessionAttacher()
        client = _client(token="s3cret", attacher=attacher)
        with (
            patch("coord.dashboard.server.read_board", return_value=_board()),
            patch("coord.dashboard.terminal._local_short_hostname", return_value="laptop"),
        ):
            with client.websocket_connect("/ws/terminal/abc123?token=s3cret") as ws:
                ws.send_bytes(b"x")
        assert attacher.attach_calls == [(None, "coord-abc123")]

    def test_unknown_session_rejects_with_4404(self) -> None:
        attacher = _FakeSessionAttacher()
        client = _client(attacher=attacher)
        with patch("coord.dashboard.server.read_board", return_value=_board()):
            # Accepted first, then closed 4404 -- the code is what tells the
            # client this session is gone for good rather than blipped (#1071).
            with client.websocket_connect("/ws/terminal/does-not-exist") as ws:
                message = ws.receive()
        assert message["type"] == "websocket.close"
        assert message["code"] == 4404
        assert attacher.attach_calls == []

    def test_unknown_session_accepts_before_closing_4404(self) -> None:
        """The 4404 must ride an *accepted* connection (#1071 live-smoke fix).

        Closing pre-accept aborts the HTTP upgrade, so the browser sees a bare
        403 -- no close code -- and `Terminal.tsx`, which keys the terminal
        "session ended" state off code 4404, treats it as a transient drop and
        reconnects forever against a session_id that will never resolve.
        """
        attacher = _FakeSessionAttacher()
        with patch("coord.dashboard.server.read_board", return_value=_board()):
            sent = asyncio.run(
                _raw_ws_messages("/ws/terminal/does-not-exist", attacher=attacher)
            )

        assert [m["type"] for m in sent] == ["websocket.accept", "websocket.close"]
        assert sent[-1]["code"] == 4404
        assert attacher.attach_calls == []

    def test_bad_token_accepts_before_closing_4401(self) -> None:
        """Same accept-then-close shape for the auth rejection: a pre-accept
        close degrades to a bare 403 there too, so the client can never tell
        "your token is wrong" from "the network blipped". No PTY is attached.
        """
        attacher = _FakeSessionAttacher()
        with patch("coord.dashboard.server.read_board", return_value=_board()):
            sent = asyncio.run(
                _raw_ws_messages(
                    "/ws/terminal/abc123",
                    query_string=b"token=wrong",
                    token="s3cret",
                    attacher=attacher,
                )
            )

        assert [m["type"] for m in sent] == ["websocket.accept", "websocket.close"]
        assert sent[-1]["code"] == 4401
        assert attacher.attach_calls == []

    def test_remote_machine_attaches_over_configured_host(self) -> None:
        """A session on a non-local machine must resolve to that machine's
        ssh host (#1065's "cross-machine ssh attach is required")."""
        attacher = _FakeSessionAttacher()
        cfg = Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[Machine(
                name="definitely-not-this-box", host="otherbox.tailnet",
                repos=["api"], repo_paths={"api": "/tmp/api"},
            )],
        )
        client = TestClient(build_app(cfg, session_attacher=attacher))
        board = Board(
            active=[
                Assignment(
                    machine_name="definitely-not-this-box", repo_name="api",
                    issue_number=1, issue_title="x",
                    assignment_id="rem1", status="running",
                ),
            ],
        )
        with patch("coord.dashboard.server.read_board", return_value=board):
            with client.websocket_connect("/ws/terminal/rem1") as ws:
                ws.send_bytes(b"x")
        assert attacher.attach_calls == [("otherbox.tailnet", "coord-rem1")]


class TestResolveSessionTarget:
    def test_local_machine_resolves_to_none_host(self) -> None:
        # "laptop" won't match this test process's real hostname, but the
        # config's machine.host also won't match -- exercised via the
        # dedicated local-hostname patch below for a deterministic assertion.
        with patch(
            "coord.dashboard.terminal._local_short_hostname",
            return_value="laptop",
        ):
            result = resolve_session_target("abc123", _board(), _config())
        assert result == (None, "coord-abc123")

    def test_remote_machine_resolves_to_its_host(self) -> None:
        with patch(
            "coord.dashboard.terminal._local_short_hostname",
            return_value="some-other-box",
        ):
            result = resolve_session_target("abc123", _board(), _config())
        assert result == ("laptop.tailnet", "coord-abc123")

    def test_unknown_session_id_returns_none(self) -> None:
        assert resolve_session_target("nope", _board(), _config()) is None


class TestResolveWebToken:
    def test_flag_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(WEB_TOKEN_ENV, "from-env")
        assert resolve_web_token("from-flag") == "from-flag"

    def test_env_wins_over_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        fake_file = tmp_path / "web_token"
        fake_file.write_text("from-file")
        monkeypatch.setattr(
            "coord.dashboard.terminal.WEB_TOKEN_FILE", fake_file
        )
        monkeypatch.setenv(WEB_TOKEN_ENV, "from-env")
        assert resolve_web_token(None) == "from-env"

    def test_file_used_when_no_flag_or_env(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        fake_file = tmp_path / "web_token"
        fake_file.write_text("  from-file  \n")
        monkeypatch.setattr(
            "coord.dashboard.terminal.WEB_TOKEN_FILE", fake_file
        )
        monkeypatch.delenv(WEB_TOKEN_ENV, raising=False)
        assert resolve_web_token(None) == "from-file"

    def test_none_when_nothing_configured(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(
            "coord.dashboard.terminal.WEB_TOKEN_FILE", tmp_path / "missing"
        )
        monkeypatch.delenv(WEB_TOKEN_ENV, raising=False)
        assert resolve_web_token(None) is None

    def test_blank_token_treated_as_unset(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(
            "coord.dashboard.terminal.WEB_TOKEN_FILE", tmp_path / "missing"
        )
        monkeypatch.delenv(WEB_TOKEN_ENV, raising=False)
        assert resolve_web_token("   ") is None


class TestTmuxSessionAttacherTermEnv:
    """Regression tests for #1229: ``TERM`` must always reach the subprocess.

    The key scenario: ``coord web`` runs as a systemd user service with no
    controlling TTY, so ``os.environ`` has no ``TERM`` at all.  The spawned
    ``tmux attach-session`` (or its ssh wrapper) then inherits no ``TERM``,
    and anything inside the pane that probes terminal capabilities (e.g.
    ``claude``) immediately fails with "terminal does not support clear".

    We reproduce that condition by clearing ``TERM`` from ``os.environ`` in
    the test and assert that ``subprocess.Popen`` still receives a non-empty
    ``TERM`` via the explicit ``env=`` argument -- proving the code no longer
    silently inherits the process environment.
    """

    def test_term_injected_when_os_environ_has_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Simulate the systemd-service condition: no TERM in the environment."""
        monkeypatch.delenv("TERM", raising=False)

        captured_env: dict[str, str] | None = None

        def fake_popen(argv, *, stdin, stdout, stderr, preexec_fn, close_fds, env):
            nonlocal captured_env
            captured_env = env
            mock = MagicMock()
            mock.pid = 99999
            return mock

        fake_master, fake_slave = 10, 11

        with (
            patch("coord.dashboard.terminal.subprocess.Popen", side_effect=fake_popen),
            patch("coord.dashboard.terminal.os.close"),
            patch("pty.openpty", return_value=(fake_master, fake_slave)),
        ):
            import asyncio

            attacher = TmuxSessionAttacher()
            asyncio.run(attacher.attach(None, "coord-abc123"))

        assert captured_env is not None, "Popen was not called"
        assert "TERM" in captured_env, "TERM was not present in env passed to Popen"
        assert captured_env["TERM"], "TERM was empty"

    def test_term_not_overridden_when_caller_sets_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the caller's environment already has TERM, preserve it."""
        monkeypatch.setenv("TERM", "rxvt-unicode-256color")

        captured_env: dict[str, str] | None = None

        def fake_popen(argv, *, stdin, stdout, stderr, preexec_fn, close_fds, env):
            nonlocal captured_env
            captured_env = env
            mock = MagicMock()
            mock.pid = 99999
            return mock

        fake_master, fake_slave = 10, 11

        with (
            patch("coord.dashboard.terminal.subprocess.Popen", side_effect=fake_popen),
            patch("coord.dashboard.terminal.os.close"),
            patch("pty.openpty", return_value=(fake_master, fake_slave)),
        ):
            attacher = TmuxSessionAttacher()
            asyncio.run(attacher.attach(None, "coord-abc123"))

        assert captured_env is not None
        # setdefault must not overwrite a caller-supplied TERM.
        assert captured_env["TERM"] == "rxvt-unicode-256color"
