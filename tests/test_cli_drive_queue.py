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
import time
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from coord import state
from coord.cli import main
from coord.drive_queue import (
    DRIVE_STARTUP_GRACE_SECONDS,
    QUEUE_ALERT_ISSUE,
    QUEUE_ALERT_REPO,
)

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
        "add", "list", "remove", "move", "status", "tick", "resume",
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


# ── tick: the startup grace window (#1794) ───────────────────────────────────
#
# The `no_tmux` fixture above IS the incident's false negative: it makes
# `list_drive_sessions()` return `[]`, exactly as the real one does for
# "tmux unavailable" / "no server running" / "the call timed out" — and
# exactly as it did on 2026-08-03, 40s after a healthy launch.


def _backdate(issue: int, seconds: float) -> None:
    """Age a queued entry's `launched_at` by *seconds*, as if time had passed."""
    state._update_drive_queue_entry_local(
        REPO, issue, launched_at=time.time() - seconds
    )


def test_back_to_back_ticks_launch_exactly_one_drive(cli, seed, launches):
    """THE regression for #1794.

    This is `docs/DRIVE_QUEUE.md` §2's install sequence in miniature:
    `systemctl --user enable --now …timer` fires one tick, and the runbook's
    own verification step (`systemctl --user start …service`) fires another
    seconds later. Asserted by COUNTING launches, not by the absence of an
    error message — the 2026-08-03 duplicate exited 0.
    """
    seed(issues={1762: "open"})
    cli("add", REPO, "1762", "--machine", "dellserver")

    first = cli("tick")
    assert first.exit_code == 0, first.output
    second = cli("tick")
    assert second.exit_code == 0, second.output

    assert len(launches) == 1, launches
    entry = queued(1762)
    assert entry["state"] == "running"
    assert entry["attempts"] == 0
    # The exact failure signature from the journal must be gone.
    assert "died without landing the work" not in second.output
    assert "retry" not in second.output
    assert "starting" in second.output
    assert "1/1 occupied" in second.output


def test_a_still_starting_drive_is_reported_not_escalated(cli, seed, launches):
    seed(issues={1762: "open"})
    cli("add", REPO, "1762")
    cli("tick")

    result = cli("tick")
    assert result.exit_code == 0, result.output
    # Occupying a slot is the queue working — no queue-level alert for it.
    queue_alert = state._get_drive_escalation_local(
        QUEUE_ALERT_REPO, QUEUE_ALERT_ISSUE
    )
    assert queue_alert is None
    assert state._get_drive_escalation_local(REPO, 1762) is None
    assert "still starting" in queued(1762)["last_reason"]


def test_a_drive_genuinely_dead_past_the_window_still_retries(cli, seed, launches):
    """The window delays death detection by one interval; it never removes it."""
    seed(issues={1762: "open"})
    cli("add", REPO, "1762")
    cli("tick")
    _backdate(1762, DRIVE_STARTUP_GRACE_SECONDS + 60)

    result = cli("tick")
    assert result.exit_code == 0, result.output
    assert "died without landing the work" in result.output
    # It relaunches on the same tick, as it did before #1794.
    assert len(launches) == 2, launches
    entry = queued(1762)
    assert entry["state"] == "running"
    assert entry["attempts"] == 1


def test_a_repeatedly_dead_drive_still_reaches_blocked_and_escalates(
    cli, seed, launches
):
    seed(issues={1762: "open"})
    cli("add", REPO, "1762")
    cli("tick")
    _backdate(1762, DRIVE_STARTUP_GRACE_SECONDS + 60)
    cli("tick")
    _backdate(1762, DRIVE_STARTUP_GRACE_SECONDS + 60)

    result = cli("tick")
    assert result.exit_code == 0, result.output
    entry = queued(1762)
    assert entry["state"] == "blocked"
    assert entry["attempts"] == 2
    assert state._get_drive_escalation_local(REPO, 1762) is not None


# ── tick: the cross-host guard (#1870) ───────────────────────────────────────
#
# 2026-08-06: a drive launched by hand on `elitebook` was 47 minutes into a
# healthy run when the timer's own tick, on `dellserver`, checked its LOCAL
# tmux, found nothing, and launched a duplicate. `launch_host` is stamped at
# launch time and compared against the ticking host's own identity before a
# `running` entry is ever allowed to reconcile to `retry`.


def test_tick_stamps_the_launching_hosts_identity(cli, seed, launches, monkeypatch):
    monkeypatch.setattr("socket.gethostname", lambda: "dellserver")
    seed(issues={1811: "open"})
    cli("add", REPO, "1811")

    result = cli("tick")
    assert result.exit_code == 0, result.output
    assert queued(1811)["launch_host"] == "dellserver"


def test_a_tick_on_a_different_host_never_reaps_a_healthy_remote_drive(
    cli, seed, launches, monkeypatch
):
    """THE regression for #1870 — the elitebook/dellserver duplicate launch."""
    monkeypatch.setattr("socket.gethostname", lambda: "elitebook")
    seed(issues={1811: "open"})
    cli("add", REPO, "1811")
    cli("tick")
    assert len(launches) == 1
    assert queued(1811)["launch_host"] == "elitebook"
    _backdate(1811, DRIVE_STARTUP_GRACE_SECONDS + 2841)

    # The timer's own tick, on a DIFFERENT machine.
    monkeypatch.setattr("socket.gethostname", lambda: "dellserver")
    result = cli("tick")

    assert result.exit_code == 0, result.output
    assert "unknown" in result.output
    assert "elitebook" in result.output
    assert "died without landing the work" not in result.output
    # No second drive, no consumed attempt, no escalation — a healthy remote
    # drive must come out of this tick exactly as it went in.
    assert len(launches) == 1, launches
    entry = queued(1811)
    assert entry["state"] == "running"
    assert entry["attempts"] == 0
    assert state._get_drive_escalation_local(REPO, 1811) is None
    assert state._get_drive_escalation_local(QUEUE_ALERT_REPO, QUEUE_ALERT_ISSUE) is None


def test_a_tick_on_the_launching_host_still_detects_a_genuine_death(
    cli, seed, launches, monkeypatch
):
    """The guard must not swallow a REAL death on the entry's own host."""
    monkeypatch.setattr("socket.gethostname", lambda: "dellserver")
    seed(issues={1811: "open"})
    cli("add", REPO, "1811")
    cli("tick")
    _backdate(1811, DRIVE_STARTUP_GRACE_SECONDS + 60)

    result = cli("tick")
    assert result.exit_code == 0, result.output
    assert "died without landing the work" in result.output
    assert len(launches) == 2, launches
    assert queued(1811)["attempts"] == 1


def test_an_entry_launched_before_1870_keeps_the_pre_1870_behaviour(
    cli, seed, launches, monkeypatch
):
    """A row with no recorded `launch_host` degrades to today's behaviour."""
    monkeypatch.setattr("socket.gethostname", lambda: "dellserver")
    seed(issues={1811: "open"})
    cli("add", REPO, "1811")
    state._update_drive_queue_entry_local(
        REPO, 1811, state="running", launched_at=time.time()
    )
    assert queued(1811)["launch_host"] in (None, "")
    _backdate(1811, DRIVE_STARTUP_GRACE_SECONDS + 60)

    result = cli("tick")
    assert result.exit_code == 0, result.output
    assert "died without landing the work" in result.output
    assert queued(1811)["attempts"] == 1


def test_group_help_no_longer_claims_bare_host_independence():
    result = CliRunner().invoke(main, ["drive-queue", "--help"])
    assert result.exit_code == 0, result.output
    assert "safe to run at any time and from any machine that can reach" not in (
        result.output
    )
    assert "1870" in result.output


def test_tick_help_no_longer_claims_bare_host_independence():
    result = CliRunner().invoke(main, ["drive-queue", "tick", "--help"])
    assert result.exit_code == 0, result.output
    assert "safe to run at any time and from any machine that can reach" not in (
        result.output
    )
    assert "1870" in result.output


def test_a_requeued_entry_is_never_relaunched_inside_the_window(
    cli, seed, launches
):
    """The launch-side guard, driven through the CLI.

    Whatever puts a just-launched entry back in `waiting` — a stale retry, a
    hand edit, a launch whose exit code lied — the tick must not start a
    second `coord drive` for it. `coord drive`'s per-issue flock stays the
    last line of defence; the queue must not need it.
    """
    seed(issues={1762: "open"})
    cli("add", REPO, "1762")
    cli("tick")
    assert len(launches) == 1
    state._update_drive_queue_entry_local(REPO, 1762, state="waiting")

    result = cli("tick")
    assert result.exit_code == 0, result.output
    assert len(launches) == 1, launches
    assert "second `coord drive` is refused" in result.output


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


# ── deploy gates (#1757) ─────────────────────────────────────────────────────
#
# The acceptance bar for `--hold-after`.  `merged != live`: a queue that
# launches the next entry the moment the previous one merges fires work that
# depends on a PyPI release / a `coord-serve` restart / a rebuilt binary well
# before any of those exist.  Every test below drives the REAL CLI; the only
# things stubbed remain the two process boundaries the module docstring names,
# plus (here) the `resume_when` probe, which is itself a subprocess.


@pytest.fixture
def probes(monkeypatch):
    """Fake `resume_when` outcomes without spawning a shell.

    Keyed by entry key so a test can hold one gate and pass another. Records
    every invocation so "the tick ran the probe" is assertable, not assumed.
    """

    calls: list[str] = []
    outcomes: dict[str, bool] = {}

    def fake_probe(entry):
        from coord.drive_queue import ProbeResult

        calls.append(entry.resume_when)
        ok = outcomes.get(entry.key, False)
        return ProbeResult(entry.key, ok, "exit 0" if ok else "exit 7: not deployed yet")

    monkeypatch.setattr("coord.commands.drive_queue._run_resume_probe", fake_probe)
    return type("P", (), {"calls": calls, "outcomes": outcomes})()


def _land(seed, issue: int) -> None:
    """Make the board say `issue` merged, the way a finished drive would."""
    seed(
        issues={issue: "closed"},
        assignments=[{"issue_number": issue, "status": "merged"}],
    )


# ── add: flags and validation ────────────────────────────────────────────────


def test_add_hold_after_stores_the_flag_and_reason_and_list_renders_both(cli):
    reason = "release + upgrade ~/.coord-venv on dellserver + restart coord-serve"
    result = cli("add", REPO, "1753", "--hold-after", "--hold-reason", reason)
    assert result.exit_code == 0, result.output

    entry = queued(1753)
    assert entry["hold_after"] == 1
    assert entry["hold_reason"] == reason
    # Armed at enqueue — the gate exists before the entry ever runs.
    assert entry["hold_state"] == "armed"

    listed = cli("list")
    assert listed.exit_code == 0, listed.output
    assert "hold=armed" in listed.output
    assert reason in listed.output


def test_add_stores_the_resume_when_probe(cli):
    probe = "curl -sf http://dellserver:7435/drive-queue"
    assert cli("add", REPO, "1753", "--hold-after", "--resume-when", probe).exit_code == 0
    assert queued(1753)["resume_when"] == probe
    assert probe in cli("list").output


def test_resume_when_without_hold_after_is_a_usage_error_not_a_silent_noop(cli):
    result = cli("add", REPO, "1753", "--resume-when", "true")
    assert result.exit_code != 0
    assert "--resume-when" in result.output
    assert "--hold-after" in result.output
    assert state._list_drive_queue_local() == []


def test_hold_reason_without_hold_after_is_also_refused(cli):
    result = cli("add", REPO, "1753", "--hold-reason", "deploy first")
    assert result.exit_code != 0
    assert state._list_drive_queue_local() == []


def test_re_adding_without_hold_after_withdraws_the_gate(cli):
    cli("add", REPO, "1753", "--hold-after", "--hold-reason", "deploy")
    assert cli("add", REPO, "1753").exit_code == 0
    entry = queued(1753)
    assert entry["hold_after"] == 0
    assert entry["hold_state"] == ""


# ── tick: the gate fires and blocks an eligible successor ────────────────────


def test_a_fired_gate_launches_nothing_even_with_an_eligible_successor(
    cli, seed, launches
):
    """THE test. Free capacity, a fully eligible #1754 — and still no launch."""
    cli("add", REPO, "1753", "--hold-after", "--hold-reason", "restart coord-serve")
    cli("add", REPO, "1754")
    state._update_drive_queue_entry_local(REPO, 1753, state="running")
    seed(
        issues={1753: "closed", 1754: "open"},
        assignments=[{"issue_number": 1753, "status": "merged"}],
    )

    result = cli("tick", "--max-parallel", "1")
    assert result.exit_code == 0, result.output
    assert launches == []
    assert queued(1753)["state"] == "done"
    assert queued(1753)["hold_state"] == "fired"
    # 1754 was NOT touched — not launched, not deferred, not blocked.
    assert queued(1754)["state"] == "waiting"
    assert queued(1754)["deferrals"] == 0

    # Exactly one alert, and it carries the operator's own reason verbatim.
    alert = state._get_drive_escalation_local(QUEUE_ALERT_REPO, QUEUE_ALERT_ISSUE)
    assert alert is not None
    assert "restart coord-serve" in alert["reason"]
    assert "resume" in alert["proposed_command"]
    assert state._get_drive_escalation_local(REPO, 1753) is None


def test_a_hold_does_not_decay_across_ticks(cli, seed, launches):
    cli("add", REPO, "1753", "--hold-after", "--hold-reason", "deploy")
    cli("add", REPO, "1754")
    state._update_drive_queue_entry_local(REPO, 1753, state="running")
    seed(
        issues={1753: "closed", 1754: "open"},
        assignments=[{"issue_number": 1753, "status": "merged"}],
    )

    for _ in range(3):
        assert cli("tick").exit_code == 0
    assert launches == []
    assert queued(1753)["hold_state"] == "fired"


def test_status_reports_the_hold_and_its_reason(cli, seed, launches):
    cli("add", REPO, "1753", "--hold-after", "--hold-reason", "restart coord-serve")
    state._update_drive_queue_entry_local(REPO, 1753, state="running")
    _land(seed, 1753)
    cli("tick")

    result = cli("status")
    assert result.exit_code == 0, result.output
    assert "HELD" in result.output
    assert "restart coord-serve" in result.output
    assert "coord drive-queue resume" in result.output

    payload = json.loads(cli("status", "--json").output)
    assert [h["key"] for h in payload["held"]] == [f"{REPO}#1753"]


# ── resume ───────────────────────────────────────────────────────────────────


def test_resume_clears_the_hold_and_the_very_next_tick_launches(
    cli, seed, launches
):
    cli("add", REPO, "1753", "--hold-after", "--hold-reason", "deploy")
    cli("add", REPO, "1754")
    state._update_drive_queue_entry_local(REPO, 1753, state="running")
    seed(
        issues={1753: "closed", 1754: "open"},
        assignments=[{"issue_number": 1753, "status": "merged"}],
    )
    cli("tick")
    assert launches == []

    released = cli("resume")
    assert released.exit_code == 0, released.output
    assert f"{REPO}#1753" in released.output
    assert queued(1753)["hold_state"] == "released"
    # The entry stays in the queue as history — the RELEASE is what unblocks.
    assert queued(1753)["state"] == "done"

    result = cli("tick")
    assert result.exit_code == 0, result.output
    assert "1754" in " ".join(launches[0])


def test_resume_with_nothing_held_exits_non_zero(cli):
    cli("add", REPO, "1753", "--hold-after", "--hold-reason", "deploy")
    result = cli("resume")
    assert result.exit_code != 0
    assert "no deploy gate" in result.output
    # An armed-but-unfired gate is untouched — resume must not disarm it.
    assert queued(1753)["hold_state"] == "armed"


def test_resume_can_name_one_entry(cli, seed, launches):
    cli("add", REPO, "1753", "--hold-after", "--hold-reason", "deploy")
    state._update_drive_queue_entry_local(REPO, 1753, state="running")
    _land(seed, 1753)
    cli("tick")

    assert cli("resume", REPO, "9999").exit_code != 0
    assert queued(1753)["hold_state"] == "fired"
    assert cli("resume", REPO, "1753").exit_code == 0
    assert queued(1753)["hold_state"] == "released"


# ── --resume-when auto-release ───────────────────────────────────────────────


def test_a_failing_probe_keeps_the_gate_held_with_a_rising_attempt_count(
    cli, seed, launches, probes
):
    cli(
        "add", REPO, "1753",
        "--hold-after", "--hold-reason", "deploy",
        "--resume-when", "curl -sf http://dellserver:7435/drive-queue",
    )
    cli("add", REPO, "1754")
    state._update_drive_queue_entry_local(REPO, 1753, state="running")
    seed(
        issues={1753: "closed", 1754: "open"},
        assignments=[{"issue_number": 1753, "status": "merged"}],
    )

    # Tick 1 FIRES the gate; per the design the probe does not run yet.
    cli("tick")
    assert probes.calls == []
    assert queued(1753)["hold_probes"] == 0

    for expected in (1, 2, 3):
        assert cli("tick").exit_code == 0
        assert queued(1753)["hold_probes"] == expected
        alert = state._get_drive_escalation_local(QUEUE_ALERT_REPO, QUEUE_ALERT_ISSUE)
        assert f"attempt {expected} failed" in alert["gate_readings"]

    assert launches == []
    assert len(probes.calls) == 3
    assert "failed 3×" in cli("status").output


def test_a_passing_probe_releases_and_launches_in_the_same_tick(
    cli, seed, launches, probes
):
    cli(
        "add", REPO, "1753",
        "--hold-after", "--hold-reason", "deploy",
        "--resume-when", "curl -sf http://dellserver:7435/drive-queue",
    )
    cli("add", REPO, "1754")
    state._update_drive_queue_entry_local(REPO, 1753, state="running")
    seed(
        issues={1753: "closed", 1754: "open"},
        assignments=[{"issue_number": 1753, "status": "merged"}],
    )
    cli("tick")               # fires
    assert launches == []

    probes.outcomes[f"{REPO}#1753"] = True
    result = cli("tick")      # probes, releases, AND launches — one tick
    assert result.exit_code == 0, result.output
    assert queued(1753)["hold_state"] == "released"
    assert "1754" in " ".join(launches[0])
    # The HELD alert must not survive the release.
    assert state._get_drive_escalation_local(QUEUE_ALERT_REPO, QUEUE_ALERT_ISSUE) is None


def test_a_hanging_probe_is_killed_and_treated_as_a_failure(cli, seed, launches):
    """The REAL `_run_resume_probe`, against a command that never returns.

    A wedged probe must not wedge the tick — a tick that stops ticking is
    indistinguishable from a queue with nothing to do.
    """
    import time as _time

    from coord.commands import drive_queue as dq

    cli(
        "add", REPO, "1753",
        "--hold-after", "--hold-reason", "deploy",
        "--resume-when", "sleep 120",
    )
    state._update_drive_queue_entry_local(REPO, 1753, state="running")
    _land(seed, 1753)
    cli("tick")  # fires

    original = dq.RESUME_PROBE_TIMEOUT_SECONDS
    dq.RESUME_PROBE_TIMEOUT_SECONDS = 0.4
    try:
        started = _time.monotonic()
        result = cli("tick")
        elapsed = _time.monotonic() - started
    finally:
        dq.RESUME_PROBE_TIMEOUT_SECONDS = original

    assert result.exit_code == 0, result.output
    assert elapsed < 20.0, "the probe timeout did not bound the tick"
    assert queued(1753)["hold_state"] == "fired"
    assert queued(1753)["hold_probes"] == 1
    assert launches == []


def test_dry_run_does_not_run_the_probe(cli, seed, launches, probes):
    cli(
        "add", REPO, "1753",
        "--hold-after", "--hold-reason", "deploy", "--resume-when", "true",
    )
    state._update_drive_queue_entry_local(REPO, 1753, state="running")
    _land(seed, 1753)
    cli("tick")

    result = cli("tick", "--dry-run")
    assert result.exit_code == 0, result.output
    assert probes.calls == []
    assert "--dry-run" in result.output


# ── the gate never doubles up with the escalation path ───────────────────────


def test_a_hold_after_entry_that_ends_blocked_raises_no_second_alert(
    cli, seed, launches
):
    """`blocked` already stops the queue — two alerts for one condition is
    how an alert channel gets muted."""
    cli("add", REPO, "1753", "--hold-after", "--hold-reason", "deploy")
    state._update_drive_queue_entry_local(
        REPO, 1753, state="running", attempts=1
    )
    # Board says: no session, no merge, no active work → the drive died.
    seed(issues={1753: "open"})

    result = cli("tick", "--max-parallel", "1")
    assert result.exit_code == 0, result.output
    entry = queued(1753)
    assert entry["state"] == "blocked"
    # The gate never fired: it fires on `done` only.
    assert entry["hold_state"] == "armed"

    # The per-issue escalation exists…
    assert state._get_drive_escalation_local(REPO, 1753) is not None
    # …and the queue-level record is the ordinary stall line, not a HELD one.
    alert = state._get_drive_escalation_local(QUEUE_ALERT_REPO, QUEUE_ALERT_ISSUE)
    assert alert is None or "HELD" not in alert["reason"]
