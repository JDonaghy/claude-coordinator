"""Tests for coord.models — Board state operations."""

from __future__ import annotations

from coord.models import Assignment, Board, Machine, Repo


def _board() -> Board:
    return Board(
        repos=[
            Repo(name="api", github="acme/api"),
            Repo(name="shared", github="acme/shared"),
        ],
        machines=[
            Machine(name="laptop", host="laptop.tailnet", repos=["api", "shared"]),
            Machine(name="server", host="server.tailnet", repos=["api"]),
        ],
    )


def test_idle_machines_all_idle() -> None:
    b = _board()
    assert [m.name for m in b.idle_machines()] == ["laptop", "server"]


def test_idle_machines_one_busy() -> None:
    b = _board()
    b.active.append(
        Assignment(
            machine_name="laptop",
            repo_name="api",
            issue_number=1,
            issue_title="x",
            status="running",
        )
    )
    assert [m.name for m in b.idle_machines()] == ["server"]


def test_mark_done_moves_assignment_to_completed() -> None:
    b = _board()
    b.active.append(
        Assignment(
            machine_name="laptop",
            repo_name="api",
            issue_number=42,
            issue_title="x",
            status="running",
        )
    )
    done = b.mark_done("laptop", branch="feat/x", pr_url="http://pr/1")
    assert done is not None
    assert done.status == "done"
    assert done.branch == "feat/x"
    assert b.active == []
    assert b.completed[0].issue_number == 42


def test_mark_failed_moves_assignment_to_completed() -> None:
    b = _board()
    b.active.append(
        Assignment(
            machine_name="server",
            repo_name="api",
            issue_number=7,
            issue_title="y",
            status="running",
        )
    )
    failed = b.mark_failed("server")
    assert failed is not None
    assert failed.status == "failed"
    assert b.active == []
    assert b.completed[0].status == "failed"


def test_active_files_by_repo_groups_by_repo() -> None:
    b = _board()
    b.active.extend(
        [
            Assignment(
                machine_name="laptop",
                repo_name="api",
                issue_number=1,
                issue_title="x",
                files_allowed=["a.py"],
                status="running",
            ),
            Assignment(
                machine_name="server",
                repo_name="api",
                issue_number=2,
                issue_title="y",
                files_allowed=["b.py"],
                status="running",
            ),
        ]
    )
    files = b.active_files_by_repo()
    assert set(files["api"]) == {"a.py", "b.py"}


def test_machine_can_work_on() -> None:
    m = Machine(name="laptop", host="h", repos=["api"])
    assert m.can_work_on("api")
    assert not m.can_work_on("other")
