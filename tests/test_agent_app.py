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


def test_inject_endpoint_delivers_message(tmp_path: Path) -> None:
    """POST /inject/{id} writes the text to the worker's stdin."""
    import time
    repo = _init_repo(tmp_path / "repo")
    client, server = _client(
        tmp_path,
        argv=["/bin/sh", "-c", "read a; echo got1=$a; read b; echo got2=$b"],
        repo_path=repo,
    )
    r = client.post("/assign", json=_payload(tmp_path, repo_path=repo))
    aid = r.json()["id"]
    time.sleep(0.3)  # let stdin wire up + first line drain

    r = client.post(f"/inject/{aid}", json={"text": "injected-content"})
    assert r.status_code == 202, r.text
    assert r.json()["status"] == "delivered"

    final = server.wait_for(aid, timeout=5.0)
    log = Path(final.log_path).read_text()
    assert "injected-content" in log
    assert "# inject: injected-content" in log


def test_inject_endpoint_unknown_id_returns_404(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    r = client.post("/inject/missing", json={"text": "hi"})
    assert r.status_code == 404


def test_inject_endpoint_finished_assignment_returns_409_or_410(tmp_path: Path) -> None:
    """A finished assignment can no longer be injected into."""
    repo = _init_repo(tmp_path / "repo")
    client, server = _client(tmp_path, argv=["/bin/echo", "done"], repo_path=repo)
    r = client.post("/assign", json=_payload(tmp_path, repo_path=repo))
    aid = r.json()["id"]
    server.wait_for(aid)

    r = client.post(f"/inject/{aid}", json={"text": "too late"})
    assert r.status_code in (409, 410)


def test_inject_endpoint_rejects_bad_body(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    r = client.post("/inject/anything", json={"wrong_key": "x"})
    assert r.status_code == 400
    r = client.post("/inject/anything", json={"text": ""})
    assert r.status_code == 400


def test_health_surfaces_version_and_last_update(tmp_path: Path) -> None:
    """/health includes the running version and any persisted last_update
    payload so the CLI can show a clear before/after delta."""
    import json as _json
    client, server = _client(tmp_path)
    # No last_update file → version present, last_update absent.
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert "version" in body
    assert body["version"]
    assert "last_update" not in body

    # Persist a last_update.json — /health should now include it.
    (server.state_dir / "last_update.json").write_text(
        _json.dumps({
            "mode": "pip install --upgrade",
            "version_before": "0.3.0",
            "version_after": "0.4.0",
            "result": "upgraded",
            "error": None,
        })
    )
    r = client.get("/health")
    body = r.json()
    assert body["last_update"]["result"] == "upgraded"
    assert body["last_update"]["version_after"] == "0.4.0"


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


# ── /health includes worktree_bytes ──────────────────────────────────────────

def test_health_includes_worktree_bytes(tmp_path: Path) -> None:
    """GET /health must include worktree_bytes."""
    client, _ = _client(tmp_path)
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert "worktree_bytes" in body
    assert isinstance(body["worktree_bytes"], int)


# ── /worktree-clean endpoint ──────────────────────────────────────────────────

def test_worktree_clean_empty(tmp_path: Path) -> None:
    """POST /worktree-clean returns JSON with cleaned/kept/bytes_freed."""
    client, _ = _client(tmp_path)
    r = client.post("/worktree-clean")
    assert r.status_code == 200
    body = r.json()
    assert body["cleaned"] == 0
    assert body["kept"] == 0
    assert body["bytes_freed"] == 0


def test_worktree_clean_removes_orphan(tmp_path: Path) -> None:
    """POST /worktree-clean removes orphaned worktrees (no matching assignment).

    Uses ``recent_secs=0`` to bypass the race-window mtime guard that
    would otherwise keep this just-created directory.
    """
    client, server = _client(tmp_path)
    orphan = server.state_dir / "worktrees" / "no-such-id"
    orphan.mkdir(parents=True)
    (orphan / "data.txt").write_text("hello")

    r = client.post("/worktree-clean", json={"recent_secs": 0})
    assert r.status_code == 200
    body = r.json()
    assert body["cleaned"] == 1
    assert not orphan.exists()


def test_worktree_clean_respects_recent_secs(tmp_path: Path) -> None:
    """recent_secs body param actually protects fresh orphans (race fix)."""
    client, server = _client(tmp_path)
    fresh = server.state_dir / "worktrees" / "racing-id"
    fresh.mkdir(parents=True)
    (fresh / "data.txt").write_text("partial")

    # Large recent_secs → fresh orphan must NOT be deleted.
    r = client.post("/worktree-clean", json={"recent_secs": 600})
    assert r.status_code == 200
    body = r.json()
    assert body["cleaned"] == 0
    assert body["kept"] == 1
    assert fresh.exists()

    # recent_secs=0 → same orphan IS deleted on the next call.
    r = client.post("/worktree-clean", json={"recent_secs": 0})
    assert r.status_code == 200
    body = r.json()
    assert body["cleaned"] == 1
    assert not fresh.exists()


# ── GET /artifact/{repo}/{branch} ─────────────────────────────────────────────


def test_artifact_manifest_404_when_missing(tmp_path: Path) -> None:
    """GET /artifact/repo/branch returns 404 when no stash exists."""
    client, _ = _client(tmp_path)
    r = client.get("/artifact/myrepo/issue-99-some-feature")
    assert r.status_code == 404
    body = r.json()
    assert "error" in body


def test_artifact_manifest_200_with_stash(tmp_path: Path) -> None:
    """GET /artifact/repo/branch returns 200 with manifest when stash exists."""
    client, server = _client(tmp_path)

    # Create a fake stash with one binary-sized file.
    stash_dir = server.state_dir / "artifacts" / "myrepo" / "issue-99-feature"
    stash_dir.mkdir(parents=True, exist_ok=True)
    artifact = stash_dir / "mybin"
    artifact.write_bytes(b"x" * 512)
    (stash_dir / ".assignment_id").write_text("abc-123")

    r = client.get("/artifact/myrepo/issue-99-feature")
    assert r.status_code == 200
    body = r.json()
    assert body["total_bytes"] == 512
    assert len(body["files"]) == 1
    assert body["files"][0]["name"] == "mybin"
    assert body["files"][0]["size"] == 512
    assert body["built_by_assignment_id"] == "abc-123"


def test_artifact_manifest_rejects_path_traversal(tmp_path: Path) -> None:
    """GET /artifact/../.. (or branch=..) returns 404 — not a real directory leak."""
    client, server = _client(tmp_path)

    # Starlette routing won't even match a literal ".." segment in most cases,
    # but the server-side guard should also reject it.  We test what we can
    # reach via the test client.  A branch that encodes ".." in a safe way
    # (dots only) should be caught by the regex guard.
    r = client.get("/artifact/myrepo/..badname")
    # Could be 404 (guard rejected) or 404 (stash missing); either way NOT 200.
    assert r.status_code == 404

    # Verify the guard in artifact_manifest itself rejects ".." strings.
    manifest = server.artifact_manifest("myrepo", "..")
    assert manifest is None

    manifest = server.artifact_manifest("..", "issue-1-branch")
    assert manifest is None

    # Also verify that a valid pair returns None when the stash is genuinely absent.
    manifest = server.artifact_manifest("myrepo", "issue-1-branch")
    assert manifest is None
