"""Tests for the Starlette HTTP layer over AgentServer."""

from __future__ import annotations

from pathlib import Path

from starlette.testclient import TestClient

from coord.agent import AgentServer
from coord.agent_app import build_app


def _client(tmp_path: Path, *, argv: list[str] | None = None) -> tuple[TestClient, AgentServer]:
    server = AgentServer(
        machine_name="test",
        capabilities=["python"],
        repos=["api"],
        state_dir=tmp_path / "state",
        worker_command=lambda spec: argv or ["/bin/sh", "-c", "echo ok"],
    )
    app = build_app(server)
    return TestClient(app), server


def _payload(tmp_path: Path, **overrides) -> dict:
    base = {
        "repo_name": "api",
        "repo_path": str(tmp_path),
        "issue_number": 1,
        "issue_title": "do thing",
        "briefing": "fix the bug",
        "files_allowed": [],
        "files_forbidden": [],
        "branch": "main",
    }
    base.update(overrides)
    return base


def test_health_endpoint(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["machine"] == "test"
    assert body["repos"] == ["api"]


def test_assign_then_status(tmp_path: Path) -> None:
    client, server = _client(tmp_path)
    r = client.post("/assign", json=_payload(tmp_path))
    assert r.status_code == 202
    aid = r.json()["id"]

    server.wait_for(aid)

    r = client.get("/status")
    assert r.status_code == 200
    body = r.json()
    assert len(body["completed"]) == 1
    assert body["completed"][0]["id"] == aid
    assert body["completed"][0]["status"] == "done"
    server.shutdown()


def test_assign_invalid_json(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    r = client.post("/assign", content="not json", headers={"content-type": "application/json"})
    assert r.status_code == 400


def test_assign_bad_payload_shape(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    r = client.post("/assign", json={"unexpected": "fields only"})
    assert r.status_code == 400
    assert "bad assignment payload" in r.json()["error"]


def test_assign_unknown_repo(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    r = client.post("/assign", json=_payload(tmp_path, repo_name="ghost"))
    assert r.status_code == 400


def test_cancel_endpoint(tmp_path: Path) -> None:
    client, server = _client(tmp_path, argv=["/bin/sh", "-c", "sleep 30"])
    r = client.post("/assign", json=_payload(tmp_path))
    aid = r.json()["id"]

    # Wait until it's running before cancelling
    import time
    for _ in range(50):
        if server.get(aid).status == "running":
            break
        time.sleep(0.02)

    r = client.post(f"/cancel/{aid}")
    assert r.status_code == 200
    assert r.json()["status"] == "cancelled"
    server.shutdown()


def test_cancel_unknown_id_returns_404(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    r = client.post("/cancel/missing")
    assert r.status_code == 404
