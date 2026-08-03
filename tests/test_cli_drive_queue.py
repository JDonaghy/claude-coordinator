"""Black-box CLI tests for `coord drive-queue` (#1754, DQ-2) — the acceptance bar.

Drives the REAL Click CLI against a seeded board + queue and asserts on its
rendered output, per this repo's CLAUDE.md ("every PR that changes user-visible
behavior must ship a black-box test that drives the running app"). The
``cli-pytest`` shape: seed, invoke `coord drive-queue ...`, assert on stdout.

WHAT IS AND IS NOT MOCKED. The queue and the board are REAL — rows go into the
same `coord.db` schema `coord serve` uses, and the tick reads them back through
DQ-1's routed accessors and `coord.drive_state.BoardFetcher`. Exactly two
process boundaries are stubbed, both of which are "the world", not logic:

* `coord.drive.list_drive_sessions` — a `tmux list-sessions` subprocess;
* the `coord drive --tmux` launch subprocess itself.

`plan_tick` is never called directly here — every assertion goes through the
CLI, so a broken flag, a bad render, or an unrouted write fails these tests.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from coord import state
from coord.cli import main
from coord.drive_queue import QUEUE_ALERT_ISSUE, QUEUE_ALERT_REPO

REPO = "claude-coordinator"

_CONFIG_YAML = f"""\
repos:
  - name: {REPO}
    github: john/claude-coordinator
    default_branch: main
machines:
  - name: dellserver
    host: dellserver
    repos: [{REPO}]
"""


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "coordinator.yml"
    path.write_text(_CONFIG_YAML)
    return path


@pytest.fixture
def cli(config_file: Path):
    """Invoke `coord drive-queue <args...>` with the seeded config."""

    def run(*args: str):
        return CliRunner().invoke(
            main, ["drive-queue", *args, "--config", str(config_file)]
        )

    return run


# ── board seeding (real rows in the real schema) ─────────────────────────────


@pytest.fixture
def seed(coord_db):
    """Write `assignments` / `issues` rows the tick will actually read back."""

    def _seed(
        *,
        issues: dict[int, str] | None = None,
        assignments: list[dict[str, Any]] | None = None,
    ) -> None:
        for number, issue_state in (issues or {}).items():
            coord_db.execute(
                "INSERT OR REPLACE INTO issues (repo_name, number, title, state) "
                "VALUES (?, ?, ?, ?)",
                (REPO, number, f"issue {number}", issue_state),
            )
        for index, row in enumerate(assignments or []):
            coord_db.execute(
                "INSERT INTO assignments "
                "(assignment_id, repo_name, issue_number, issue_title, "
                " machine_name, type, status, dispatched_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row.get("assignment_id", f"a{index}"),
                    REPO,
                    row["issue_number"],
                    f"issue {row['issue_number']}",
                    "dellserver",
                    row.get("type", "work"),
                    row["status"],
                    100.0 + index,
                ),
            )
        coord_db.commit()

    return _seed


@pytest.fixture(autouse=True)
def no_tmux(monkeypatch):
    """No live drive sessions unless a test says otherwise."""
    monkeypatch.setattr("coord.drive.list_drive_sessions", lambda *a, **k: [])


@pytest.fixture(autouse=True)
def tick_lock(monkeypatch, tmp_path) -> Path:
    """Give every test its own tick lock.

    `drive_queue_lock_path()` resolves under the REAL `~/.coord`, so without
    this the suite (a) writes into the developer's home and (b) — worse — two
    concurrent pytest runs on one machine would contend for the same flock and
    one of them would take the "another tick is running" early return, turning
    every tick assertion into a coin flip.
    """
    path = tmp_path / "drive-queue.lock"
    monkeypatch.setattr("coord.filelock.drive_queue_lock_path", lambda: path)
    return path


@pytest.fixture
def live_sessions(monkeypatch):
    def _set(*issues: int) -> None:
        monkeypatch.setattr(
            "coord.drive.list_drive_sessions",
            lambda *a, **k: [{"repo": REPO, "issue": n} for n in issues],
        )

    return _set


class _Launches(list):
    """Captured `coord drive --tmux` argvs, plus the exit the stub should fake."""

    outcome: dict[str, Any]


@pytest.fixture
def launches(monkeypatch) -> _Launches:
    """Capture the `coord drive --tmux` argv instead of running it."""
    captured = _Launches()
    captured.outcome = {"returncode": 0, "stderr": ""}

    class _Result:
        def __init__(self) -> None:
            self.returncode = captured.outcome["returncode"]
            self.stdout = ""
            self.stderr = captured.outcome["stderr"]

    def fake_run(argv, **_kw):
        captured.append(list(argv))
        return _Result()

    monkeypatch.setattr("coord.commands.drive_queue.subprocess.run", fake_run)
    return captured


def queued(issue: int) -> dict | None:
    return state._get_drive_queue_entry_local(REPO, issue)


# ── add ──────────────────────────────────────────────────────────────────────


def test_drive_queue_is_registered_with_every_verb():
    assert "drive-queue" in main.commands
    assert set(main.commands["drive-queue"].commands) == {
        "add", "list", "remove", "move", "status", "tick",
    }


def test_add_writes_a_row_visible_in_list(cli):
    result = cli("add", REPO, "1650", "--machine", "dellserver", "--after", "1645")
    assert result.exit_code == 0, result.output
    assert f"{REPO}#1650" in result.output

    listed = cli("list")
    assert listed.exit_code == 0, listed.output
    assert f"{REPO}#1650" in listed.output
    assert "machine=dellserver" in listed.output
    assert f"after={REPO}#1645" in listed.output


def test_add_resolves_a_bare_after_against_the_entrys_repo(cli):
    assert cli("add", REPO, "1654", "--after", "1650").exit_code == 0
    assert queued(1654)["after_json"] == [f"{REPO}#1650"]


def test_add_accepts_a_qualified_cross_repo_after(cli):
    assert cli("add", REPO, "1654", "--after", "quadraui#302").exit_code == 0
    assert queued(1654)["after_json"] == ["quadraui#302"]


def test_add_with_a_cycle_exits_non_zero_and_writes_nothing(cli):
    assert cli("add", REPO, "1650", "--after", "1654").exit_code == 0
    before = state._list_drive_queue_local()

    result = cli("add", REPO, "1654", "--after", "1650")
    assert result.exit_code != 0
    assert "cycle" in result.output
    assert state._list_drive_queue_local() == before


def test_add_refuses_a_self_edge(cli):
    result = cli("add", REPO, "1650", "--after", "1650")
    assert result.exit_code != 0
    assert "itself" in result.output
    assert state._list_drive_queue_local() == []


def test_add_refuses_a_repo_coordinator_yml_never_heard_of(cli):
    result = cli("add", "not-a-repo", "1")
    assert result.exit_code != 0
    assert "not in coordinator.yml" in result.output
    assert state._list_drive_queue_local() == []


def test_add_refuses_a_malformed_after_entry(cli):
    result = cli("add", REPO, "1654", "--after", "nonsense")
    assert result.exit_code != 0
    assert "malformed" in result.output
    assert state._list_drive_queue_local() == []


def test_list_is_empty_before_anything_is_queued(cli):
    result = cli("list")
    assert result.exit_code == 0
    assert "empty" in result.output


def test_list_json_emits_the_raw_rows(cli):
    cli("add", REPO, "1650")
    result = cli("list", "--json")
    assert result.exit_code == 0
    rows = json.loads(result.output)
    assert [r["issue_number"] for r in rows] == [1650]
    assert rows[0]["after_json"] == []  # a real list on the wire, never a string


# ── remove / move ────────────────────────────────────────────────────────────


def test_remove_drops_the_row_and_renumbers(cli):
    cli("add", REPO, "1650")
    cli("add", REPO, "1654")
    assert cli("remove", REPO, "1650").exit_code == 0
    rows = state._list_drive_queue_local()
    assert [(r["issue_number"], r["position"]) for r in rows] == [(1654, 0)]


def test_remove_of_an_unqueued_issue_exits_non_zero(cli):
    result = cli("remove", REPO, "9999")
    assert result.exit_code != 0
    assert "not in the drive queue" in result.output


def test_move_reorders_the_queue(cli):
    cli("add", REPO, "1650")
    cli("add", REPO, "1654")
    assert cli("move", REPO, "1654", "--to", "0").exit_code == 0
    rows = state._list_drive_queue_local()
    assert [r["issue_number"] for r in rows] == [1654, 1650]


# ── tick: the launch decision ────────────────────────────────────────────────


def test_dry_run_names_the_launch_and_the_defer_reason_and_mutates_nothing(
    cli, seed, launches
):
    seed(issues={1650: "open", 1654: "open"})
    cli("add", REPO, "1650", "--machine", "dellserver")
    cli("add", REPO, "1654", "--after", "1650")
    before = state._list_drive_queue_local()

    result = cli("tick", "--dry-run")
    assert result.exit_code == 0, result.output
    assert f"would launch {REPO}#1650 on dellserver" in result.output
    # …and the defer reason for the tail names the pre-req by number.
    assert f"defer {REPO}#1654" in result.output
    assert f"waiting on {REPO}#1650" in result.output
    assert launches == []
    assert state._list_drive_queue_local() == before


def test_tick_launches_the_head_and_marks_it_running(cli, seed, launches):
    seed(issues={1650: "open"})
    cli("add", REPO, "1650", "--machine", "dellserver")

    result = cli("tick")
    assert result.exit_code == 0, result.output
    assert "launched" in result.output

    argv = launches[0]
    assert argv[-6:] == ["drive", REPO, "1650", "--tmux", "--machine", "dellserver"] or (
        "--tmux" in argv and "--machine" in argv
    )
    assert "drive" in argv and "1650" in argv and "--tmux" in argv

    entry = queued(1650)
    assert entry["state"] == "running"
    assert entry["session_name"] == f"coord-drive-{REPO}-1650"
    assert entry["launched_at"]


def test_tick_launches_the_later_entry_when_the_head_is_unsatisfied(
    cli, seed, launches
):
    seed(issues={1650: "open", 1654: "open"})
    cli("add", REPO, "1650", "--after", "1654")
    cli("add", REPO, "1654")
    # 1654 is queued and waiting, so 1650 defers; 1654 itself is eligible.
    result = cli("tick")
    assert result.exit_code == 0, result.output
    assert "1654" in " ".join(launches[0])

    listed = cli("list")
    # Original order preserved — a deferral never reorders (#1750 design note).
    positions = [r["issue_number"] for r in state._list_drive_queue_local()]
    assert positions == [1650, 1654]
    assert "deferrals=1" in listed.output
    assert f"waiting on {REPO}#1654" in listed.output


def test_tick_with_nothing_eligible_exits_zero_and_records_one_alert(
    cli, seed, launches
):
    seed(issues={})
    cli("add", REPO, "1650", "--after", "quadraui#302")
    cli("add", REPO, "1654", "--after", "quadraui#303")

    result = cli("tick")
    assert result.exit_code == 0, result.output
    assert launches == []
    assert "no launch" in result.output

    status = cli("status")
    assert "alert:" in status.output
    assert "nothing eligible" in status.output
    # Exactly one queue-level record, not one per entry.
    assert state._get_drive_escalation_local(QUEUE_ALERT_REPO, QUEUE_ALERT_ISSUE)


def test_an_unsatisfiable_prereq_blocks_without_consuming_an_attempt(
    cli, seed, launches
):
    seed(issues={})
    cli("add", REPO, "1654", "--after", "quadraui#302")

    result = cli("tick")
    assert result.exit_code == 0, result.output
    entry = queued(1654)
    assert entry["state"] == "blocked"
    assert entry["attempts"] == 0
    assert "quadraui#302" in entry["last_reason"]
    # …and it escalates against its OWN issue, not the synthetic queue key.
    escalation = state._get_drive_escalation_local(REPO, 1654)
    assert escalation is not None
    assert "drive-queue remove" in escalation["proposed_command"]


def test_a_merged_prereq_unblocks_the_dependent_entry(cli, seed, launches):
    seed(
        issues={1650: "closed", 1654: "open"},
        assignments=[{"issue_number": 1650, "status": "merged"}],
    )
    cli("add", REPO, "1654", "--after", "1650")

    result = cli("tick")
    assert result.exit_code == 0, result.output
    assert "1654" in " ".join(launches[0])


# ── tick: capacity ───────────────────────────────────────────────────────────


def test_tick_at_capacity_launches_nothing(cli, seed, launches, live_sessions):
    seed(issues={1650: "open", 1654: "open"})
    cli("add", REPO, "1650")
    cli("add", REPO, "1654")
    state._update_drive_queue_entry_local(REPO, 1650, state="running")
    live_sessions(1650)

    result = cli("tick", "--max-parallel", "1")
    assert result.exit_code == 0, result.output
    assert launches == []
    assert "1/1 occupied" in result.output


def test_a_deadline_expired_drive_still_counts_against_capacity(
    cli, seed, launches
):
    """#1660 / the 2026-08-01 incident.

    `coord drive` exited EXIT_DEADLINE, so its tmux session is gone — but the
    work is still running on the fleet. A session count says "free"; the board
    says "occupied", and the board is what this must believe.
    """
    seed(
        issues={1650: "open", 1654: "open"},
        assignments=[{"issue_number": 1650, "status": "running"}],
    )
    cli("add", REPO, "1650")
    cli("add", REPO, "1654")
    state._update_drive_queue_entry_local(REPO, 1650, state="running")
    # no live_sessions() call — the tmux session is GONE.

    result = cli("tick", "--max-parallel", "1")
    assert result.exit_code == 0, result.output
    assert launches == []
    assert "1/1 occupied" in result.output
    assert "still ACTIVE on the board" in result.output
    # The row is held as `running`, not requeued behind an attempt.
    entry = queued(1650)
    assert entry["state"] == "running"
    assert entry["attempts"] == 0


def test_a_finished_drive_is_reconciled_done_and_frees_its_slot(
    cli, seed, launches
):
    seed(
        issues={1650: "closed", 1654: "open"},
        assignments=[{"issue_number": 1650, "status": "merged"}],
    )
    cli("add", REPO, "1650")
    cli("add", REPO, "1654")
    state._update_drive_queue_entry_local(REPO, 1650, state="running")

    result = cli("tick", "--max-parallel", "1")
    assert result.exit_code == 0, result.output
    assert queued(1650)["state"] == "done"
    assert "1654" in " ".join(launches[0])


# ── tick: the lock and the fail-closed board ─────────────────────────────────


def test_tick_with_a_held_flock_exits_zero_without_touching_the_queue(
    cli, seed, launches, tick_lock
):
    seed(issues={1650: "open"})
    cli("add", REPO, "1650")
    before = state._list_drive_queue_local()

    from coord.filelock import FileLock

    lock = FileLock(tick_lock)
    lock.acquire(timeout=0.0)
    try:
        result = cli("tick")
    finally:
        lock.release()

    assert result.exit_code == 0, result.output
    assert "another drive-queue tick is running" in result.output
    assert launches == []
    assert state._list_drive_queue_local() == before


def test_an_unreadable_board_aborts_without_launching(
    cli, seed, launches, monkeypatch
):
    seed(issues={1650: "open"})
    cli("add", REPO, "1650")
    before = state._list_drive_queue_local()

    def boom(self):
        raise RuntimeError("board daemon unreachable")

    monkeypatch.setattr("coord.drive_state.BoardFetcher.fetch", boom)

    result = cli("tick")
    assert result.exit_code != 0
    assert "aborting without launching" in result.output
    assert launches == []
    assert state._list_drive_queue_local() == before


def test_a_failed_launch_is_a_consumed_attempt_not_a_running_entry(
    cli, seed, launches
):
    # #1606: `--tmux` exits 0 only once the session is live, so a non-zero exit
    # means nothing is running.
    seed(issues={1650: "open"})
    cli("add", REPO, "1650")
    launches.outcome["returncode"] = 1
    launches.outcome["stderr"] = "tmux: no server running"

    result = cli("tick")
    assert result.exit_code != 0
    entry = queued(1650)
    assert entry["state"] == "waiting"
    assert entry["attempts"] == 1
    assert not entry["session_name"]
    assert "tmux: no server running" in entry["last_reason"]


# ── status ───────────────────────────────────────────────────────────────────


def test_status_counts_by_state_after_a_real_tick(cli, seed, launches):
    seed(issues={1650: "open", 1654: "open"})
    cli("add", REPO, "1650")
    cli("add", REPO, "1654", "--after", "1650")

    assert cli("tick").exit_code == 0
    result = cli("status")
    assert result.exit_code == 0, result.output
    assert "1 running · 1 waiting" in result.output


def test_status_json_carries_the_counts_and_the_alert(cli, seed, launches):
    seed(issues={})
    cli("add", REPO, "1650", "--after", "quadraui#302")
    cli("tick")

    result = cli("status", "--json")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["total"] == 1
    assert payload["counts"]["blocked"] == 1
    assert "nothing eligible" in payload["alert"]["reason"]


def test_status_on_an_empty_queue_says_so(cli):
    result = cli("status")
    assert result.exit_code == 0
    assert "empty" in result.output
    assert "alert: (none)" in result.output
