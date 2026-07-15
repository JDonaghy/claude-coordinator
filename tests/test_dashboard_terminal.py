"""Tests for the /ws/terminal PTY<->WebSocket bridge (#1065).

Uses Starlette's in-process TestClient.websocket_connect (no real socket) and
a fake SessionAttacher (no real ssh/tmux) per the issue's acceptance bar:
"Keep the ssh/tmux attach behind an injectable seam ... so tests need no real
ssh."
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from coord.config import Config
from coord.dashboard.server import build_app
from coord.dashboard.terminal import (
    WEB_TOKEN_ENV,
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

    def test_missing_token_rejects_upgrade(self) -> None:
        attacher = _FakeSessionAttacher()
        client = _client(token="s3cret", attacher=attacher)
        with patch("coord.dashboard.server.read_board", return_value=_board()):
            with pytest.raises(WebSocketDisconnect) as exc_info:
                with client.websocket_connect("/ws/terminal/abc123"):
                    pass
        assert exc_info.value.code == 4401
        assert attacher.attach_calls == []

    def test_wrong_token_rejects_upgrade(self) -> None:
        attacher = _FakeSessionAttacher()
        client = _client(token="s3cret", attacher=attacher)
        with patch("coord.dashboard.server.read_board", return_value=_board()):
            with pytest.raises(WebSocketDisconnect) as exc_info:
                with client.websocket_connect("/ws/terminal/abc123?token=wrong"):
                    pass
        assert exc_info.value.code == 4401
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

    def test_unknown_session_rejects_upgrade(self) -> None:
        attacher = _FakeSessionAttacher()
        client = _client(attacher=attacher)
        with patch("coord.dashboard.server.read_board", return_value=_board()):
            with pytest.raises(WebSocketDisconnect) as exc_info:
                with client.websocket_connect("/ws/terminal/does-not-exist"):
                    pass
        assert exc_info.value.code == 4404
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
