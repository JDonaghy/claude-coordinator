"""Tests for coord.network — health checks and error classification."""

from __future__ import annotations

import socket
from unittest.mock import MagicMock, patch

import httpx
import pytest

from coord import network
from coord.models import Machine


def _m(name: str = "laptop", host: str = "laptop.tailnet") -> Machine:
    return Machine(name=name, host=host, repos=["api"])


class TestClassifyError:
    def test_connect_timeout(self) -> None:
        state, reason = network.classify_error(httpx.ConnectTimeout("timed out"))
        assert state == network.TIMEOUT
        assert "timed out" in reason

    def test_read_timeout(self) -> None:
        state, _ = network.classify_error(httpx.ReadTimeout("slow"))
        assert state == network.TIMEOUT

    def test_dns_error_via_message(self) -> None:
        state, reason = network.classify_error(
            httpx.ConnectError("[Errno -2] Name or service not known")
        )
        assert state == network.DNS_ERROR
        assert "resolvable" in reason

    def test_dns_error_via_socket(self) -> None:
        state, _ = network.classify_error(socket.gaierror("nodename"))
        assert state == network.DNS_ERROR

    def test_connection_refused(self) -> None:
        state, reason = network.classify_error(
            httpx.ConnectError("[Errno 111] Connection refused")
        )
        assert state == network.OFFLINE
        assert "refused" in reason

    def test_generic_connect_error(self) -> None:
        state, _ = network.classify_error(httpx.ConnectError("weird"))
        assert state == network.OFFLINE

    def test_other_http_error(self) -> None:
        state, _ = network.classify_error(httpx.HTTPError("hmm"))
        assert state == network.HTTP_ERROR

    def test_unknown_exception(self) -> None:
        state, _ = network.classify_error(RuntimeError("???"))
        assert state == network.UNKNOWN


class TestCheckMachine:
    def test_online_path(self) -> None:
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"machine": "laptop", "active": 0}
        with patch.object(network.httpx, "get", return_value=resp):
            s = network.check_machine(_m())
        assert s.is_online
        assert s.state == network.ONLINE
        assert s.health == {"machine": "laptop", "active": 0}
        assert s.latency_ms is not None and s.latency_ms >= 0

    def test_timeout(self) -> None:
        with patch.object(
            network.httpx, "get", side_effect=httpx.ConnectTimeout("slow")
        ):
            s = network.check_machine(_m())
        assert s.state == network.TIMEOUT
        assert not s.is_online

    def test_connection_refused(self) -> None:
        with patch.object(
            network.httpx,
            "get",
            side_effect=httpx.ConnectError("[Errno 111] Connection refused"),
        ):
            s = network.check_machine(_m())
        assert s.state == network.OFFLINE
        assert "refused" in s.reason

    def test_dns_error(self) -> None:
        with patch.object(
            network.httpx,
            "get",
            side_effect=httpx.ConnectError("Name or service not known"),
        ):
            s = network.check_machine(_m(host="ghost.tailnet"))
        assert s.state == network.DNS_ERROR

    def test_non_200_status(self) -> None:
        resp = MagicMock()
        resp.status_code = 500
        with patch.object(network.httpx, "get", return_value=resp):
            s = network.check_machine(_m())
        assert s.state == network.HTTP_ERROR
        assert "500" in s.reason

    def test_invalid_json(self) -> None:
        resp = MagicMock()
        resp.status_code = 200
        resp.json.side_effect = ValueError("nope")
        with patch.object(network.httpx, "get", return_value=resp):
            s = network.check_machine(_m())
        assert s.state == network.HTTP_ERROR
        assert "invalid JSON" in s.reason


class TestCheckAll:
    def test_preserves_order(self) -> None:
        resp = MagicMock(); resp.status_code = 200; resp.json.return_value = {}
        ms = [_m(name=f"m{i}", host=f"m{i}.tailnet") for i in range(3)]
        with patch.object(network.httpx, "get", return_value=resp):
            result = network.check_all(ms)
        assert [s.machine.name for s in result] == ["m0", "m1", "m2"]

    def test_empty_input(self) -> None:
        assert network.check_all([]) == []

    def test_mixed_outcomes(self) -> None:
        def fake_get(url, timeout=None):
            if "m1" in url:
                raise httpx.ConnectError("[Errno 111] Connection refused")
            r = MagicMock(); r.status_code = 200; r.json.return_value = {}
            return r

        ms = [_m(name="m0", host="m0.tailnet"), _m(name="m1", host="m1.tailnet")]
        with patch.object(network.httpx, "get", side_effect=fake_get):
            result = network.check_all(ms)
        assert result[0].is_online
        assert result[1].state == network.OFFLINE


class TestFetchLog:
    def test_returns_status_and_body(self) -> None:
        resp = MagicMock()
        resp.status_code = 200
        resp.content = b"hello"
        with patch.object(network.httpx, "get", return_value=resp) as mock_get:
            status, body = network.fetch_log(_m(), "abc123")
        assert status == 200
        assert body == b"hello"
        mock_get.assert_called_once()
        assert "/logs/abc123" in mock_get.call_args.args[0]

    def test_since_param(self) -> None:
        resp = MagicMock(); resp.status_code = 200; resp.content = b""
        with patch.object(network.httpx, "get", return_value=resp) as mock_get:
            network.fetch_log(_m(), "abc", since=42)
        assert mock_get.call_args.kwargs["params"] == {"since": 42}

    def test_no_since_omits_params(self) -> None:
        resp = MagicMock(); resp.status_code = 200; resp.content = b""
        with patch.object(network.httpx, "get", return_value=resp) as mock_get:
            network.fetch_log(_m(), "abc")
        assert mock_get.call_args.kwargs["params"] is None


class TestCleanWorktrees:
    """#1220: coord.network.clean_worktrees — the per-machine POST /worktree-clean
    helper the daemon's fleet-wide sweep tick uses."""

    def test_success(self) -> None:
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"cleaned": 3, "kept": 1, "bytes_freed": 12345}
        with patch.object(network.httpx, "post", return_value=resp) as mock_post:
            result = network.clean_worktrees(_m())
        assert result == {
            "ok": True,
            "cleaned": 3,
            "kept": 1,
            "bytes_freed": 12345,
            "error": None,
        }
        assert "/worktree-clean" in mock_post.call_args.args[0]
        assert mock_post.call_args.kwargs["json"] == {"recent_secs": 300.0}

    def test_passes_recent_secs(self) -> None:
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"cleaned": 0, "kept": 0, "bytes_freed": 0}
        with patch.object(network.httpx, "post", return_value=resp) as mock_post:
            network.clean_worktrees(_m(), recent_secs=0)
        assert mock_post.call_args.kwargs["json"] == {"recent_secs": 0}

    def test_connection_refused_never_raises(self) -> None:
        with patch.object(
            network.httpx,
            "post",
            side_effect=httpx.ConnectError("[Errno 111] Connection refused"),
        ):
            result = network.clean_worktrees(_m())
        assert result["ok"] is False
        assert result["cleaned"] == 0
        assert "refused" in result["error"]

    def test_timeout_never_raises(self) -> None:
        with patch.object(
            network.httpx, "post", side_effect=httpx.ConnectTimeout("slow")
        ):
            result = network.clean_worktrees(_m())
        assert result["ok"] is False
        assert "timed out" in result["error"]

    def test_non_200_status(self) -> None:
        resp = MagicMock()
        resp.status_code = 500
        with patch.object(network.httpx, "post", return_value=resp):
            result = network.clean_worktrees(_m())
        assert result["ok"] is False
        assert "500" in result["error"]

    def test_invalid_json(self) -> None:
        resp = MagicMock()
        resp.status_code = 200
        resp.json.side_effect = ValueError("nope")
        with patch.object(network.httpx, "post", return_value=resp):
            result = network.clean_worktrees(_m())
        assert result["ok"] is False
        assert "invalid JSON" in result["error"]
