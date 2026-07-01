"""Tests for the BoardService facade (#749): one place that decides
local-vs-daemon for board reads/writes, replacing the hand-rolled
``resolve_board_service()`` + local-fallback dance duplicated across
coord/commands/*.py, coord/dashboard/server.py and coord/auto_loop.py.
"""

from __future__ import annotations

import pytest

from coord import board_service
from coord.models import Assignment, Board


def _assignment(**kw) -> Assignment:
    base = dict(
        machine_name="laptop", repo_name="api", issue_number=1,
        issue_title="An issue", status="done",
    )
    base.update(kw)
    return Assignment(**base)


class TestResolveAndIsRemote:
    def test_resolve_none_when_unset(self):
        assert board_service.resolve() is None
        assert board_service.is_remote() is False

    def test_resolve_returns_service_when_set(self, monkeypatch):
        from coord import client as cc

        monkeypatch.setattr(
            cc, "resolve_board_service", lambda *a, **k: cc.ServiceConfig("http://d:7435")
        )
        svc = board_service.resolve()
        assert svc is not None and svc.url == "http://d:7435"
        assert board_service.is_remote() is True


class TestReadBoardLocal:
    def test_read_board_falls_back_to_build_when_unsaved(self, coord_db):
        # Nothing saved yet — read_board() must not raise / return None.
        board = board_service.read_board()
        assert isinstance(board, Board)
        assert board.active == [] and board.completed == []

    def test_read_board_returns_saved_board(self, coord_db):
        from coord.state import save_board

        save_board(Board(round_number=3, completed=[_assignment(assignment_id="w1")]))
        board = board_service.read_board()
        assert board.round_number == 3
        assert board.find_by_id("w1") is not None


class TestWriteBoardLocal:
    def test_write_board_persists_locally(self, coord_db):
        board = Board(round_number=1, completed=[_assignment(assignment_id="w2")])
        board_service.write_board(board)

        row = coord_db.execute(
            "SELECT value FROM board_meta WHERE key='round_number'"
        ).fetchone()
        assert row["value"] == "1"
        assert coord_db.execute(
            "SELECT COUNT(*) c FROM assignments WHERE assignment_id='w2'"
        ).fetchone()["c"] == 1


class TestReadWriteRemote:
    def _set_service(self, monkeypatch):
        from coord import client as cc

        monkeypatch.setattr(
            cc, "resolve_board_service", lambda *a, **k: cc.ServiceConfig("http://d:7435")
        )

    def test_read_board_fetches_remote(self, monkeypatch):
        self._set_service(monkeypatch)
        from coord import client as cc

        payload = {
            "assignments": [
                {
                    "assignment_id": "r1", "machine_name": "laptop",
                    "repo_name": "api", "issue_number": 9,
                    "issue_title": "remote", "status": "done", "type": "work",
                },
            ],
            "round_number": 5,
        }
        monkeypatch.setattr(cc, "fetch_board_payload", lambda svc, **kw: payload)
        board = board_service.read_board()
        assert board.round_number == 5
        assert board.find_by_id("r1") is not None

    def test_write_board_posts_to_daemon(self, monkeypatch):
        self._set_service(monkeypatch)
        from coord import client as cc

        captured: dict = {}
        monkeypatch.setattr(
            cc, "post_record",
            lambda svc, path, payload, **kw: captured.update(
                path=path, payload=payload
            ) or {"ok": True},
        )
        board = Board(round_number=2, completed=[_assignment(assignment_id="w3")])
        board_service.write_board(board)
        assert captured["path"] == "/board"
        assert captured["payload"]["round_number"] == 2
        assert captured["payload"]["assignments"][0]["assignment_id"] == "w3"

    def test_write_board_never_touches_local_db(self, monkeypatch, coord_db):
        # Regression: on a thin client, write_board must not fall through to
        # save_board — that would resurrect a non-canonical local DB.
        self._set_service(monkeypatch)
        from coord import client as cc

        monkeypatch.setattr(
            cc, "post_record", lambda svc, path, payload, **kw: {"ok": True}
        )
        board = Board(round_number=9, completed=[_assignment(assignment_id="w4")])
        board_service.write_board(board)
        assert coord_db.execute(
            "SELECT COUNT(*) c FROM assignments WHERE assignment_id='w4'"
        ).fetchone()["c"] == 0


class TestDaemonRerouteTarget:
    def test_none_when_unset(self):
        assert board_service.daemon_reroute_target("COORD_MERGE_ON_DAEMON") is None

    def test_svc_when_set(self, monkeypatch):
        from coord import client as cc

        monkeypatch.setattr(
            cc, "resolve_board_service", lambda *a, **k: cc.ServiceConfig("http://d:7435")
        )
        svc = board_service.daemon_reroute_target("COORD_MERGE_ON_DAEMON")
        assert svc is not None and svc.url == "http://d:7435"

    def test_none_when_env_guard_set(self, monkeypatch):
        from coord import client as cc

        monkeypatch.setattr(
            cc, "resolve_board_service", lambda *a, **k: cc.ServiceConfig("http://d:7435")
        )
        monkeypatch.setenv("COORD_MERGE_ON_DAEMON", "1")
        # We ARE the daemon re-executing the reroute target — must run locally.
        assert board_service.daemon_reroute_target("COORD_MERGE_ON_DAEMON") is None


class TestRouteWrite:
    def test_none_when_svc_none(self):
        assert board_service.route_write(None, "/x", {}) is None

    def test_posts_when_svc_set(self, monkeypatch):
        from coord import client as cc

        captured: dict = {}
        monkeypatch.setattr(
            cc, "post_record",
            lambda svc, path, payload, **kw: captured.update(
                path=path, payload=payload
            ) or {"ok": True},
        )
        svc = cc.ServiceConfig("http://d:7435")
        resp = board_service.route_write(svc, "/assignment-usage", {"a": 1})
        assert resp == {"ok": True}
        assert captured == {"path": "/assignment-usage", "payload": {"a": 1}}
