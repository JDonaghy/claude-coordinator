"""Tests for `coord pull-artifact` CLI command (#305)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from click.testing import CliRunner

from coord.cli import main


# ── Helpers ──────────────────────────────────────────────────────────────────


def _write_config(tmp_path: Path) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n"
        "  - name: myrepo\n"
        "    github: acme/myrepo\n"
        "    artifact_paths:\n"
        "      - target/debug/mybinary\n"
        "machines:\n"
        "  - name: builder\n"
        "    host: builder.tailnet\n"
        "    capabilities: [rust]\n"
        "    repos: [myrepo]\n"
    )
    return p


def _insert_assignment(
    coord_db,
    *,
    assignment_id: str = "asgn-abc123",
    machine_name: str = "builder",
    repo_name: str = "myrepo",
    branch: str | None = "issue-42-my-feature",
    issue_number: int = 42,
    issue_title: str = "my feature",
    status: str = "done",
) -> None:
    coord_db.execute(
        """INSERT INTO assignments
           (assignment_id, machine_name, repo_name, issue_number, issue_title,
            status, type, branch)
           VALUES (?, ?, ?, ?, ?, ?, 'work', ?)""",
        (assignment_id, machine_name, repo_name, issue_number, issue_title,
         status, branch),
    )
    coord_db.commit()


# ── Tests ────────────────────────────────────────────────────────────────────


def test_pull_artifact_not_found_in_db(tmp_path: Path, coord_db) -> None:
    """Non-existent assignment_id should produce a clear error."""
    cfg = _write_config(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["pull-artifact", "notanid", "--config", str(cfg)],
    )
    assert result.exit_code != 0
    assert "not found" in (result.output + (result.exception and "" or "")).lower() or \
           result.exit_code == 1


def test_pull_artifact_machine_not_in_config(tmp_path: Path, coord_db) -> None:
    """Assignment on an unknown machine should error."""
    cfg = _write_config(tmp_path)
    _insert_assignment(coord_db, assignment_id="asgn-1", machine_name="ghost-machine")

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["pull-artifact", "asgn-1", "--config", str(cfg)],
    )
    assert result.exit_code != 0


def test_pull_artifact_404_from_agent(tmp_path: Path, coord_db) -> None:
    """HTTP 404 from agent manifest endpoint → clear 'stash gone' message."""
    cfg = _write_config(tmp_path)
    _insert_assignment(coord_db)

    mock_resp = MagicMock()
    mock_resp.status_code = 404
    mock_resp.text = "not found"

    runner = CliRunner()
    with patch("httpx.get", return_value=mock_resp):
        result = runner.invoke(
            main,
            ["pull-artifact", "asgn-abc123", "--config", str(cfg)],
        )

    assert result.exit_code != 0
    output = result.output
    assert "no artifacts" in output.lower() or "gc" in output.lower() or \
           "not found" in output.lower() or "stash" in output.lower()


def test_pull_artifact_thin_client_resolves_from_daemon(
    tmp_path: Path, coord_db, monkeypatch
) -> None:
    """#601: a thin client's local DB is empty — pull-artifact must resolve the
    assignment from the daemon's board (then pull from the agent as usual)."""
    from coord import client as cc

    cfg = _write_config(tmp_path)
    # NOTE: no _insert_assignment — the local DB is intentionally empty.
    monkeypatch.setattr(
        cc, "resolve_board_service", lambda *a, **k: cc.ServiceConfig("http://d:7435")
    )
    monkeypatch.setattr(
        cc, "fetch_board_payload",
        lambda svc, **k: {
            "assignments": [{
                "assignment_id": "asgn-abc123", "machine_name": "builder",
                "repo_name": "myrepo", "branch": "issue-42-my-feature",
                "issue_number": 42, "issue_title": "my feature",
            }]
        },
    )
    mock_resp = MagicMock(status_code=404, text="not found")
    with patch("httpx.get", return_value=mock_resp):
        result = CliRunner().invoke(
            main, ["pull-artifact", "asgn-abc123", "--config", str(cfg)]
        )
    # Resolved from the daemon → reached the agent query (404), NOT the
    # "not found in database" local-DB failure.
    assert "not found in database" not in result.output
    assert "no artifacts" in result.output.lower()


def test_pull_artifact_agent_unreachable(tmp_path: Path, coord_db) -> None:
    """Network error reaching agent should exit non-zero with message."""
    import httpx

    cfg = _write_config(tmp_path)
    _insert_assignment(coord_db)

    runner = CliRunner()
    with patch("httpx.get", side_effect=httpx.ConnectError("connection refused")):
        result = runner.invoke(
            main,
            ["pull-artifact", "asgn-abc123", "--config", str(cfg)],
        )

    assert result.exit_code != 0
    assert "could not reach" in result.output.lower() or \
           "error" in result.output.lower()


def test_pull_artifact_success_rsync(tmp_path: Path, coord_db) -> None:
    """Happy path: manifest returns files, rsync succeeds → 0 exit, path printed."""
    cfg = _write_config(tmp_path)
    _insert_assignment(coord_db)

    manifest_payload = {
        "files": [{"name": "mybinary", "size": 204800, "mtime": 1700000000.0}],
        "total_bytes": 204800,
        "built_by_assignment_id": "asgn-abc123",
    }
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = manifest_payload

    dest = tmp_path / "out"
    mock_proc = MagicMock()
    mock_proc.returncode = 0

    runner = CliRunner()
    with patch("httpx.get", return_value=mock_resp), \
         patch("subprocess.run", return_value=mock_proc) as mock_run:
        result = runner.invoke(
            main,
            ["pull-artifact", "asgn-abc123", "--into", str(dest), "--config", str(cfg)],
        )

    assert result.exit_code == 0, result.output
    # rsync should have been called
    mock_run.assert_called_once()
    rsync_cmd = mock_run.call_args[0][0]
    assert rsync_cmd[0] == "rsync"
    assert "-az" in rsync_cmd
    # ssh must be non-interactive: BatchMode=yes so an auth/host-key prompt
    # can never open /dev/tty and hijack the TUI's terminal (screen
    # corruption, unresponsive to 'q').
    ssh_opt = rsync_cmd[rsync_cmd.index("-e") + 1]
    assert "BatchMode=yes" in ssh_opt, ssh_opt
    # Belt-and-braces: detach from the controlling terminal and null stdin so
    # no descendant (ssh) can claim the TUI's tty even if BatchMode is bypassed.
    assert mock_run.call_args.kwargs.get("stdin") is subprocess.DEVNULL
    assert mock_run.call_args.kwargs.get("start_new_session") is True
    # Path should be printed
    assert str(dest) in result.output


def test_pull_artifact_local_machine_skips_ssh(tmp_path: Path, coord_db) -> None:
    """Artifact built on the local host: copy locally, never rsync/ssh.

    rsync-over-ssh to our own hostname fails ("Permission denied" — no
    self-ssh key), which surfaced as a meaningless pull error in the TUI.
    """
    from coord.agent import _sanitize_branch

    cfg = _write_config(tmp_path)
    _insert_assignment(coord_db)

    manifest_payload = {
        "files": [{"name": "mybinary", "size": 10, "mtime": 1.0}],
        "total_bytes": 10,
        "built_by_assignment_id": "asgn-abc123",
    }
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = manifest_payload

    # Simulate the agent's local stash under a patched HOME.
    home = tmp_path / "home"
    src = home / ".coord" / "artifacts" / "myrepo" / _sanitize_branch("issue-42-my-feature")
    src.mkdir(parents=True)
    (src / "mybinary").write_bytes(b"BINARYDATA")

    dest = tmp_path / "out"
    runner = CliRunner()
    with patch("httpx.get", return_value=mock_resp), \
         patch("socket.gethostname", return_value="builder"), \
         patch("pathlib.Path.home", return_value=home), \
         patch("subprocess.run") as mock_run:
        result = runner.invoke(
            main,
            ["pull-artifact", "asgn-abc123", "--into", str(dest), "--config", str(cfg)],
        )

    assert result.exit_code == 0, result.output
    # The whole point: a local pull must NOT shell out to rsync/ssh.
    mock_run.assert_not_called()
    assert (dest / "mybinary").read_bytes() == b"BINARYDATA"


def test_pull_artifact_rsync_failure(tmp_path: Path, coord_db) -> None:
    """When rsync fails, exit non-zero with clear message."""
    cfg = _write_config(tmp_path)
    _insert_assignment(coord_db)

    manifest_payload = {
        "files": [{"name": "mybinary", "size": 204800, "mtime": 1700000000.0}],
        "total_bytes": 204800,
        "built_by_assignment_id": "asgn-abc123",
    }
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = manifest_payload

    mock_proc = MagicMock()
    mock_proc.returncode = 23  # rsync partial failure

    runner = CliRunner()
    with patch("httpx.get", return_value=mock_resp), \
         patch("subprocess.run", return_value=mock_proc):
        result = runner.invoke(
            main,
            ["pull-artifact", "asgn-abc123", "--into", str(tmp_path / "out"),
             "--config", str(cfg)],
        )

    assert result.exit_code != 0
    assert "rsync" in result.output.lower() or "error" in result.output.lower()


def test_pull_artifact_uses_sanitized_branch_in_url(tmp_path: Path, coord_db) -> None:
    """Slashes in branch names should be sanitized in the URL."""
    cfg = _write_config(tmp_path)
    # Branch name with a slash
    _insert_assignment(coord_db, branch="feature/cool-thing")

    captured_url: list[str] = []

    def fake_get(url: str, **kwargs):
        captured_url.append(url)
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.text = "not found"
        return mock_resp

    runner = CliRunner()
    with patch("httpx.get", side_effect=fake_get):
        runner.invoke(
            main,
            ["pull-artifact", "asgn-abc123", "--config", str(cfg)],
        )

    assert captured_url, "httpx.get was never called"
    url = captured_url[0]
    # Slash should have been replaced with dash
    assert "/artifact/myrepo/feature-cool-thing" in url


def test_pull_artifact_branch_fallback_when_db_null(tmp_path: Path, coord_db) -> None:
    """When DB branch is NULL, CLI should compute it from issue_number+title."""
    cfg = _write_config(tmp_path)
    _insert_assignment(
        coord_db,
        branch=None,  # branch not yet recorded
        issue_number=99,
        issue_title="do the thing",
    )

    captured_url: list[str] = []

    def fake_get(url: str, **kwargs):
        captured_url.append(url)
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.text = "not found"
        return mock_resp

    runner = CliRunner()
    with patch("httpx.get", side_effect=fake_get):
        runner.invoke(
            main,
            ["pull-artifact", "asgn-abc123", "--config", str(cfg)],
        )

    assert captured_url, "httpx.get was never called"
    url = captured_url[0]
    # Should derive branch as issue-99-do-the-thing (slugified)
    assert "/artifact/myrepo/issue-99-do-the-thing" in url
