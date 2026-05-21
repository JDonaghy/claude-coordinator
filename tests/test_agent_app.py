"""Tests for the Starlette HTTP layer over AgentServer."""

from __future__ import annotations

import subprocess
from pathlib import Path

from starlette.testclient import TestClient

from coord.agent import AgentServer
from coord.agent_app import build_app


def _init_repo(path: Path) -> Path:
    """Create a minimal git repo with one commit so worktrees can be created."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(path), check=True, capture_output=True)
    (path / "README").write_text("init\n")
    subprocess.run(["git", "add", "README"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=str(path), check=True, capture_output=True)
    return path


def _client(
    tmp_path: Path,
    *,
    argv: list[str] | None = None,
    repo_paths: dict[str, str] | None = None,
    repo_path: Path | None = None,
) -> tuple[TestClient, AgentServer]:
    rp = repo_path or _init_repo(tmp_path / "repo")
    server = AgentServer(
        machine_name="test",
        capabilities=["python"],
        repos=["api"],
        state_dir=tmp_path / "state",
        worker_command=lambda spec: argv or ["/bin/sh", "-c", "echo ok"],
        repo_paths=repo_paths if repo_paths is not None else {"api": str(rp)},
    )
    app = build_app(server)
    return TestClient(app), server


def _payload(tmp_path: Path, repo_path: Path | None = None, **overrides) -> dict:
    base = {
        "repo_name": "api",
        "repo_path": str(repo_path or tmp_path),
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
    repo = _init_repo(tmp_path / "repo")
    client, server = _client(tmp_path, repo_path=repo)
    r = client.post("/assign", json=_payload(tmp_path, repo_path=repo))
    assert r.status_code == 202
    aid = r.json()["id"]

    server.wait_for(aid)

    r = client.get("/status")
    assert r.status_code == 200
    body = r.json()
    assert len(body["completed"]) == 1
    assert body["completed"][0]["id"] == aid
    assert body["completed"][0]["status"] == "done"
    # worktree_path should be present in status response
    assert body["completed"][0]["worktree_path"] is not None
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
    repo = _init_repo(tmp_path / "repo")
    client, _ = _client(tmp_path, repo_path=repo)
    r = client.post("/assign", json=_payload(tmp_path, repo_path=repo, repo_name="ghost"))
    assert r.status_code == 400


def test_cancel_endpoint(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    client, server = _client(tmp_path, argv=["/bin/sh", "-c", "sleep 30"], repo_path=repo)
    r = client.post("/assign", json=_payload(tmp_path, repo_path=repo))
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


def test_logs_endpoint_returns_log_content(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    client, server = _client(tmp_path, argv=["/bin/sh", "-c", "echo hello-from-worker"], repo_path=repo)
    r = client.post("/assign", json=_payload(tmp_path, repo_path=repo))
    aid = r.json()["id"]
    server.wait_for(aid)

    r = client.get(f"/logs/{aid}")
    assert r.status_code == 200
    assert "hello-from-worker" in r.text
    assert "X-Coord-Log-Total" in r.headers
    assert r.headers["X-Coord-Log-Status"] == "done"
    server.shutdown()


def test_logs_endpoint_supports_since(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    client, server = _client(tmp_path, argv=["/bin/sh", "-c", "echo line"], repo_path=repo)
    r = client.post("/assign", json=_payload(tmp_path, repo_path=repo))
    aid = r.json()["id"]
    server.wait_for(aid)

    full = client.get(f"/logs/{aid}").text
    head = client.get(f"/logs/{aid}", params={"since": 0}).text
    tail = client.get(f"/logs/{aid}", params={"since": len(full) - 5}).text
    assert head == full
    assert len(tail) == 5
    server.shutdown()


def test_logs_endpoint_unknown_id_returns_404(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    r = client.get("/logs/missing")
    assert r.status_code == 404


def test_repos_endpoint_reports_per_repo_state(tmp_path: Path) -> None:
    not_a_repo = tmp_path / "fakerepo"
    not_a_repo.mkdir()
    client, _ = _client(tmp_path, repo_paths={"api": str(not_a_repo)})
    r = client.get("/repos")
    assert r.status_code == 200
    body = r.json()
    # api exists but isn't a git repo → returns an error field, not a 500
    assert "api" in body
    assert "error" in body["api"]


def test_logs_endpoint_bad_since_returns_400(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    client, server = _client(tmp_path, repo_path=repo)
    r = client.post("/assign", json=_payload(tmp_path, repo_path=repo))
    aid = r.json()["id"]
    server.wait_for(aid)

    r = client.get(f"/logs/{aid}", params={"since": "not-an-int"})
    assert r.status_code == 400
    server.shutdown()


def test_status_returns_200_with_truncated_log(tmp_path: Path) -> None:
    """GET /status must return HTTP 200 even when the worker log ends mid-line.

    Race condition: /status is polled while the worker is actively writing an
    event to its stream-json log.  The last line is incomplete JSON.  The
    endpoint must never 500.
    """
    import json as _json
    repo = _init_repo(tmp_path / "repo")
    server = AgentServer(
        machine_name="test",
        repos=["api"],
        state_dir=tmp_path / "state",
        repo_paths={"api": str(repo)},
        # Worker writes one complete stream-json event then sleeps so the
        # assignment stays RUNNING while we poll /status.
        worker_command=lambda spec: [
            "/bin/sh", "-c",
            "printf '%s\\n' '{\"type\":\"system\",\"subtype\":\"init\",\"model\":\"x\",\"session_id\":\"s\"}'; "
            "printf '%s' '{\"type\":\"assistant\",\"partial'; "  # truncated last line
            "sleep 30",
        ],
    )
    app = build_app(server)
    from coord.agent import AssignmentSpec
    spec = AssignmentSpec(
        repo_name="api", repo_path=str(repo),
        issue_number=1, issue_title="t", briefing="b",
    )
    a = server.assign(spec)

    import time
    for _ in range(50):
        if server.get(a.id).status == "running":
            break
        time.sleep(0.02)
    time.sleep(0.15)  # let the worker write its partial line

    from starlette.testclient import TestClient
    client = TestClient(app)
    r = client.get("/status")
    assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"
    body = r.json()
    assert "active" in body

    server.shutdown(kill_running=True)
