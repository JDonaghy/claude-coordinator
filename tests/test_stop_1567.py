"""Regression tests for #1567 — `coord stop` must not force-push (or push at
all, by default) a WIP rescue commit onto the branch it is protecting.

Two layers are covered:

* **CLI** (`coord stop` / `coord stop --rescue`) — the `--rescue` flag must
  thread through to the agent's `/cancel/{id}` endpoint as `?rescue=1`, and
  the CLI must print what happened to the worktree/branch rather than stay
  silent about it.
* **Agent** (`AgentServer.cancel`) — the actual git behaviour: stopping an
  assignment with a dirty worktree must leave the worker's remote branch
  tip unchanged by default, and `--rescue` must publish the WIP commit to a
  disposable `rescue/<assignment_id>` ref, never to the worker's own branch.
"""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from coord.agent import AgentAssignment, AgentServer, AssignmentSpec
from coord.cli import main
from coord.models import Assignment, Board, Machine, Repo


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    ).stdout.strip()


def _init_local_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "t@t.com")
    _git(path, "config", "user.name", "Test")
    (path / "README").write_text("init\n")
    _git(path, "add", "README")
    _git(path, "commit", "-m", "initial")
    return path


@pytest.fixture
def repo_with_remote(tmp_path: Path) -> tuple[Path, Path]:
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(remote)],
        check=True, capture_output=True,
    )
    clone = tmp_path / "clone"
    clone.mkdir()
    _git(clone, "init", "-b", "main")
    _git(clone, "config", "user.email", "t@t.com")
    _git(clone, "config", "user.name", "Test")
    (clone / "README").write_text("v1\n")
    _git(clone, "add", "README")
    _git(clone, "commit", "-m", "initial")
    _git(clone, "remote", "add", "origin", str(remote))
    _git(clone, "push", "-u", "origin", "main")
    return clone, remote


def _make_assignment(
    repo: Path, wt: Path, *, branch: str, status: str = "running",
) -> AgentAssignment:
    spec = AssignmentSpec(
        repo_name="api", repo_path=str(repo),
        issue_number=1567, issue_title="dirty", briefing="b",
        branch="main", type="work",
    )
    return AgentAssignment(
        id=uuid.uuid4().hex[:12],
        spec=spec,
        status=status,
        branch=branch,
        worktree_path=str(wt),
    )


def _server(tmp_path: Path, repo: Path) -> AgentServer:
    return AgentServer(
        machine_name="t", repos=["api"],
        repo_paths={"api": str(repo)},
        state_dir=tmp_path / "state",
        worker_command=lambda spec: ["/bin/true"],
    )


# ── Agent-level: the actual git behaviour ────────────────────────────────────

def test_stop_default_leaves_remote_branch_tip_unchanged(
    tmp_path: Path, repo_with_remote: tuple[Path, Path]
) -> None:
    """Acceptance #1: stop an assignment with dirty worktree state — the
    remote branch tip must be unchanged (the branch shouldn't even exist on
    the remote, since it was never pushed there in the first place)."""
    clone, remote = repo_with_remote
    wt = tmp_path / "wt"
    branch = "issue-1567-stop"
    _git(clone, "worktree", "add", "-b", branch, str(wt), "HEAD")
    (wt / "half_done.py").write_text("dangerous in-progress deletion\n")

    remote_main_before = _git(remote, "rev-parse", "main")

    server = _server(tmp_path, clone)
    a = _make_assignment(clone, wt, branch=branch)
    server._assignments[a.id] = a

    server.cancel(a.id)

    # Remote never even heard of this branch — nothing was pushed.
    remote_branches = _git(remote, "branch", "-a")
    assert branch not in remote_branches, (
        f"worker branch {branch!r} reached the remote on a bare `coord stop`: "
        f"{remote_branches!r}"
    )
    # And nothing else on the remote moved either.
    assert _git(remote, "rev-parse", "main") == remote_main_before

    # The work is not lost, just not published: it's on the local branch ref,
    # which survives the worktree being torn down.
    assert _git(clone, "show", f"{branch}:half_done.py") == "dangerous in-progress deletion"
    reason = a.dirty_worktree_reason or ""
    assert "NOT pushed" in reason
    assert "#1567" in reason or "coord stop default" in reason


def test_stop_rescue_pushes_to_dedicated_ref_never_worker_branch(
    tmp_path: Path, repo_with_remote: tuple[Path, Path]
) -> None:
    """Acceptance #2: `coord stop --rescue` (cancel(rescue=True)) publishes
    the WIP commit to `rescue/<assignment_id>` and leaves the worker's own
    branch alone on the remote."""
    clone, remote = repo_with_remote
    wt = tmp_path / "wt"
    branch = "issue-1567-rescue"
    _git(clone, "worktree", "add", "-b", branch, str(wt), "HEAD")
    (wt / "half_done.py").write_text("rescued edit\n")

    server = _server(tmp_path, clone)
    a = _make_assignment(clone, wt, branch=branch)
    server._assignments[a.id] = a

    server.cancel(a.id, rescue=True)

    remote_branches = _git(remote, "branch", "-a")
    assert branch not in remote_branches, (
        "rescue push landed on the worker's own branch instead of a "
        f"dedicated rescue ref: {remote_branches!r}"
    )
    rescue_ref = f"rescue/{a.id}"
    assert rescue_ref in remote_branches
    assert _git(remote, "show", f"{rescue_ref}:half_done.py") == "rescued edit"
    assert rescue_ref in (a.dirty_worktree_reason or "")


def test_stop_rescue_push_never_uses_force(
    tmp_path: Path, repo_with_remote: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rescue push must never pass --force / --force-with-lease, even
    though it's pushing to a disposable ref."""
    clone, remote = repo_with_remote
    wt = tmp_path / "wt"
    branch = "issue-1567-noforce"
    _git(clone, "worktree", "add", "-b", branch, str(wt), "HEAD")
    (wt / "half_done.py").write_text("rescued edit\n")

    server = _server(tmp_path, clone)
    a = _make_assignment(clone, wt, branch=branch)
    server._assignments[a.id] = a

    import coord.agent as agent_mod
    real_git = agent_mod._git
    seen_push_args: list[tuple] = []

    def _spy_git(cwd, *args, **kwargs):
        if args and args[0] == "push":
            seen_push_args.append(args)
        return real_git(cwd, *args, **kwargs)

    monkeypatch.setattr(agent_mod, "_git", _spy_git)

    server.cancel(a.id, rescue=True)

    assert seen_push_args, "expected a push during rescue"
    for push_args in seen_push_args:
        assert "--force" not in push_args
        assert "--force-with-lease" not in push_args


# ── CLI: --rescue flag threads through, and output is not silent ────────────

CONFIG_YAML = """\
repos:
  - name: api
    github: acme/api
    default_branch: main
machines:
  - name: laptop
    host: laptop.tailnet
    repos: [api]
    repo_paths:
      api: /tmp/api
"""


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(CONFIG_YAML)
    return p


def _board_with_assignment(assignment_id: str) -> Board:
    return Board(
        repos=[Repo(name="api", github="acme/api")],
        machines=[Machine(name="laptop", host="laptop.tailnet", repos=["api"])],
        active=[
            Assignment(
                machine_name="laptop", repo_name="api", issue_number=1567,
                issue_title="dirty", assignment_id=assignment_id, status="running",
            )
        ],
    )


def test_cli_stop_default_does_not_send_rescue_param(config_file: Path) -> None:
    board = _board_with_assignment("abc123")
    fake_resp = MagicMock()
    fake_resp.raise_for_status.return_value = None
    fake_resp.json.return_value = {
        "dirty_worktree_reason": (
            "worker left 1 uncommitted file(s); committed to local branch "
            "issue-1567-x only — NOT pushed (coord stop default, #1567)"
        )
    }
    with patch("coord.board_service.read_board", return_value=board), \
         patch("coord.board_service.write_board"), \
         patch("coord.commands.dispatch.httpx.post", return_value=fake_resp) as post:
        result = CliRunner().invoke(
            main, ["stop", "abc123", "--config", str(config_file)]
        )

    assert result.exit_code == 0, result.output
    _, kwargs = post.call_args
    assert kwargs.get("params") is None
    assert "NOT pushed" in result.output


def test_cli_stop_rescue_flag_sends_rescue_query_param(config_file: Path) -> None:
    board = _board_with_assignment("abc123")
    fake_resp = MagicMock()
    fake_resp.raise_for_status.return_value = None
    fake_resp.json.return_value = {
        "dirty_worktree_reason": (
            "worker left 1 uncommitted file(s); committed to issue-1567-x "
            "and pushed to rescue/abc123 (worker branch issue-1567-x left "
            "untouched)"
        )
    }
    with patch("coord.board_service.read_board", return_value=board), \
         patch("coord.board_service.write_board"), \
         patch("coord.commands.dispatch.httpx.post", return_value=fake_resp) as post:
        result = CliRunner().invoke(
            main, ["stop", "abc123", "--rescue", "--config", str(config_file)]
        )

    assert result.exit_code == 0, result.output
    _, kwargs = post.call_args
    assert kwargs.get("params") == {"rescue": "1"}
    assert "rescue/abc123" in result.output
