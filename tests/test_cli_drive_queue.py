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
import re
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from coord import state
from coord.cli import main
from coord.drive_queue import (
    DRIVE_STARTUP_GRACE_SECONDS,
    PARK_STALE_SECONDS,
    QUEUE_ALERT_ISSUE,
    QUEUE_ALERT_REPO,
)

REPO = "claude-coordinator"
# A SECOND repo, so #1972's per-repo capacity has something to be per-repo
# about: the whole point is that a quadraui entry can ride alongside an
# in-progress claude-coordinator one.
OTHER_REPO = "quadraui"

_CONFIG_YAML = f"""\
repos:
  - name: {REPO}
    github: john/claude-coordinator
    default_branch: main
  - name: {OTHER_REPO}
    github: john/quadraui
    default_branch: main
machines:
  - name: dellserver
    host: dellserver
    repos: [{REPO}, {OTHER_REPO}]
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
        repo: str = REPO,
    ) -> None:
        for number, issue_state in (issues or {}).items():
            coord_db.execute(
                "INSERT OR REPLACE INTO issues (repo_name, number, title, state) "
                "VALUES (?, ?, ?, ?)",
                (repo, number, f"issue {number}", issue_state),
            )
        for index, row in enumerate(assignments or []):
            coord_db.execute(
                "INSERT INTO assignments "
                "(assignment_id, repo_name, issue_number, issue_title, "
                " machine_name, type, status, dispatched_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    # Repo-qualified so a test can seed BOTH repos without the
                    # second call colliding on the assignment_id primary key.
                    row.get("assignment_id", f"a-{repo}-{index}"),
                    repo,
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


# ── #2133: `last_reason` is a snapshot, never re-validated — its rendering
# must carry its own age so it can never be mistaken for a current diagnosis.
# Reproduces the shape of the #2104 incident: a `blocked` entry's reason was
# captured once and then read, ~3 hours later, as if it still described the
# present.


def _backdate_reason(coord_db, issue: int, seconds: float) -> None:
    """Age a queued entry's `reason_at` by *seconds* directly in SQLite —
    simulating a `last_reason` snapshot captured that long ago. Bypasses
    `update_drive_queue_entry` (which always stamps "now") on purpose: this
    is standing in for the wall-clock time that has genuinely elapsed since
    a real tick wrote the reason, exactly as `_backdate` does for
    `launched_at` above.
    """
    coord_db.execute(
        "UPDATE drive_queue SET reason_at = ? WHERE repo_name = ? AND issue_number = ?",
        (time.time() - seconds, REPO, issue),
    )
    coord_db.commit()


def test_list_shows_no_age_for_a_freshly_written_reason(cli):
    cli("add", REPO, "1650")
    state._update_drive_queue_entry_local(
        REPO, 1650, state="blocked", last_reason="checks_failed: test (3.12)"
    )
    result = cli("list")
    assert result.exit_code == 0, result.output
    assert "checks_failed: test (3.12)" in result.output
    assert "0s ago" in result.output, (
        "a reason written this instant must still carry SOME age marker — "
        "the point is that a reader never has to guess whether an age is "
        "being shown at all:\n" + result.output
    )


def test_list_ages_a_stale_park_reason_instead_of_showing_it_bare(cli, coord_db):
    """The #2104 reproduction: a `blocked` reason captured hours ago must
    read as history, not as a live diagnosis of the current blocker."""
    cli("add", REPO, "1650")
    state._update_drive_queue_entry_local(
        REPO, 1650, state="blocked", last_reason="checks_failed: test (3.12)"
    )
    _backdate_reason(coord_db, 1650, 3 * 3600 + 60)  # ~3h ago, clear of rounding

    result = cli("list")
    assert result.exit_code == 0, result.output
    assert "checks_failed: test (3.12)" in result.output, (
        "the reason text itself must still be legible:\n" + result.output
    )
    assert "(3h ago)" in result.output, (
        "#2133: a stale `last_reason` rendered without its age is exactly "
        "the trap that misdirected the #2104 diagnosis — the queue text "
        "pointed at CI while the real, later blocker (a review verdict) "
        "went unmentioned:\n" + result.output
    )


def test_list_json_carries_reason_at_for_a_client_to_render_its_own_age(cli, coord_db):
    cli("add", REPO, "1650")
    state._update_drive_queue_entry_local(
        REPO, 1650, state="blocked", last_reason="checks_failed"
    )
    _backdate_reason(coord_db, 1650, 90.0)

    rows = json.loads(cli("list", "--json").output)
    assert rows[0]["last_reason"] == "checks_failed"
    assert rows[0]["reason_at"] == pytest.approx(time.time() - 90.0, abs=5.0)


def test_list_omits_age_for_a_reason_with_no_capture_time(cli, coord_db):
    """A row predating #2133's migration (or written straight to the table by
    hand) has `reason_at IS NULL` — the renderer must not fabricate an age
    for it, just show the bare reason exactly as it always has."""
    cli("add", REPO, "1650")
    coord_db.execute(
        "UPDATE drive_queue SET state = 'blocked', last_reason = 'legacy reason', "
        "reason_at = NULL WHERE repo_name = ? AND issue_number = ?",
        (REPO, 1650),
    )
    coord_db.commit()

    result = cli("list")
    assert result.exit_code == 0, result.output
    assert "legacy reason" in result.output
    assert "ago" not in result.output, (
        "no `reason_at` means no age can be computed — inventing one would "
        "be worse than showing nothing:\n" + result.output
    )


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


# ── tick: --reconcile-only (#2110) ───────────────────────────────────────────
#
# The missing primitive the stop-the-timer-to-roll-the-fleet sequence needed:
# update the queue's view of reality (a finished `running` row moves to
# `done`) without ever starting a new `coord drive`. Both halves need a test,
# since launching is the failure mode a bug here would reintroduce.


def test_reconcile_only_marks_a_finished_entry_done_and_launches_nothing(
    cli, seed, launches
):
    seed(
        issues={1650: "closed", 1654: "open"},
        assignments=[{"issue_number": 1650, "status": "merged"}],
    )
    cli("add", REPO, "1650")
    cli("add", REPO, "1654")
    state._update_drive_queue_entry_local(REPO, 1650, state="running")

    result = cli("tick", "--reconcile-only")
    assert result.exit_code == 0, result.output
    assert "--reconcile-only" in result.output
    assert queued(1650)["state"] == "done"
    # The eligible successor is NOT launched — that is the entire point.
    assert launches == []
    assert queued(1654)["state"] == "waiting"


def test_max_parallel_zero_behaves_exactly_like_reconcile_only(
    cli, seed, launches
):
    seed(
        issues={1650: "closed", 1654: "open"},
        assignments=[{"issue_number": 1650, "status": "merged"}],
    )
    cli("add", REPO, "1650")
    cli("add", REPO, "1654")
    state._update_drive_queue_entry_local(REPO, 1650, state="running")

    result = cli("tick", "--max-parallel", "0")
    assert result.exit_code == 0, result.output
    assert queued(1650)["state"] == "done"
    assert launches == []


def test_reconcile_only_leaves_a_genuinely_live_drive_running(
    cli, seed, launches, live_sessions
):
    """A healthy in-flight drive must not be disturbed by a reconcile-only
    run — this is "update the view of reality", not "clear every row"."""
    seed(issues={1650: "open"})
    cli("add", REPO, "1650")
    state._update_drive_queue_entry_local(REPO, 1650, state="running")
    live_sessions(1650)

    result = cli("tick", "--reconcile-only")
    assert result.exit_code == 0, result.output
    assert queued(1650)["state"] == "running"
    assert launches == []


def test_reconcile_only_raises_no_queue_level_alert(cli, seed, launches):
    """A stalled-looking queue under a normal tick would escalate (#1754);
    a reconcile-only run must not, since it never even attempts the capacity
    walk that decides whether anything is eligible to launch."""
    seed(issues={1650: "open"})
    cli("add", REPO, "1650")

    result = cli("tick", "--reconcile-only")
    assert result.exit_code == 0, result.output
    assert (
        state._get_drive_escalation_local(QUEUE_ALERT_REPO, QUEUE_ALERT_ISSUE)
        is None
    )


# ── tick: per-repo capacity (#1972) ──────────────────────────────────────────


def test_a_second_repo_launches_alongside_an_in_progress_repo(
    cli, seed, launches, live_sessions
):
    """#1972's acceptance scenario, through the real CLI.

    Capacity 3. One claude-coordinator drive is in progress, several more
    claude-coordinator entries are queued behind it, and a quadraui entry sits
    at the BACK. The tick must ride the quadraui entry alongside the running
    one instead of launching a same-repo neighbour (which could stale its Test
    verdict) or idling behind the queue.
    """
    seed(issues={1650: "open", 1654: "open", 1655: "open"})
    seed(issues={302: "open"}, repo=OTHER_REPO)
    cli("add", REPO, "1650")
    cli("add", REPO, "1654")
    cli("add", REPO, "1655")
    cli("add", OTHER_REPO, "302")
    state._update_drive_queue_entry_local(REPO, 1650, state="running")
    live_sessions(1650)

    result = cli("tick", "--max-parallel", "3")
    assert result.exit_code == 0, result.output
    assert len(launches) == 1, launches
    assert OTHER_REPO in launches[0] and "302" in launches[0]
    assert f"{OTHER_REPO}#302" in result.output

    # The passed-over same-repo entries DEFERRED: still waiting, still in
    # position, no attempt spent, and the reason names the per-repo limit.
    for issue, position in ((1654, 1), (1655, 2)):
        row = queued(issue)
        assert row["state"] == "waiting"
        assert row["position"] == position
        assert row["attempts"] == 0
        assert "at its limit (1/1)" in row["last_reason"]

    # No escalation: a repo waiting on its own in-flight drive is the queue
    # working, not a stall.
    assert (
        state._get_drive_escalation_local(QUEUE_ALERT_REPO, QUEUE_ALERT_ISSUE)
        is None
    )


def test_dry_run_explains_the_per_repo_occupancy(
    cli, seed, launches, live_sessions
):
    seed(issues={1650: "open", 1654: "open"})
    cli("add", REPO, "1650")
    cli("add", REPO, "1654")
    state._update_drive_queue_entry_local(REPO, 1650, state="running")
    live_sessions(1650)

    result = cli("tick", "--max-parallel", "3", "--dry-run")
    assert result.exit_code == 0, result.output
    assert f"per-repo: {REPO} 1/1" in result.output
    assert "counted from board state" in result.output
    assert "at its limit (1/1)" in result.output
    assert launches == []
    assert queued(1654)["deferrals"] == 0  # --dry-run mutates nothing


def test_the_per_repo_ceiling_is_configurable_from_the_cli(
    cli, seed, launches, live_sessions
):
    seed(issues={1650: "open", 1654: "open"})
    cli("add", REPO, "1650")
    cli("add", REPO, "1654")
    state._update_drive_queue_entry_local(REPO, 1650, state="running")
    live_sessions(1650)

    result = cli("tick", "--max-parallel", "3", "--max-parallel-per-repo", "2")
    assert result.exit_code == 0, result.output
    assert len(launches) == 1, launches
    assert "1654" in " ".join(launches[0])


def test_a_negative_per_repo_ceiling_is_refused(cli):
    result = cli("tick", "--max-parallel-per-repo", "-1")
    assert result.exit_code != 0
    assert "--max-parallel-per-repo" in result.output


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


def test_a_dead_drives_own_exit_reason_reaches_the_queue_row(cli, seed, launches):
    """#1845/#1844, end-to-end: when the drive itself recorded why it
    stopped — a `drive_exited` audit row written before `coord drive`
    returned — the tick must carry THAT reason forward instead of
    overwriting it with "drive session died". The audit row is exactly
    what `coord.drive.Driver.run` already writes on every exit; the tick
    just wasn't reading it.

    Checked on the retry tick's OUTPUT (a successful relaunch blanks
    `last_reason` back to "" a moment later, same as any other retry — see
    `test_back_to_back_ticks_launch_exactly_one_drive`) and on the FINAL
    blocked row's `last_reason`, which is the one an operator actually goes
    looking at afterwards.
    """
    from coord.audit import record_audit

    seed(issues={1762: "open"})
    cli("add", REPO, "1762")
    cli("tick")

    def _own_reason(n: int) -> str:
        return (
            f"drive exited for claude-coordinator#1762 (exit_code=1): "
            f"merge attempted 3 times without landing (attempt {n})."
        )

    launched_at = queued(1762)["launched_at"]
    _backdate(1762, DRIVE_STARTUP_GRACE_SECONDS + 60)
    record_audit(
        tier="business", category="drive", event_type="drive_exited",
        actor="drive", summary=_own_reason(1), repo=REPO, issue=1762,
        ts=launched_at + 5,
    )
    first_retry = cli("tick")
    assert first_retry.exit_code == 0, first_retry.output
    assert _own_reason(1) in first_retry.output
    assert "died without landing the work" not in first_retry.output
    assert queued(1762)["attempts"] == 1

    launched_at = queued(1762)["launched_at"]
    _backdate(1762, DRIVE_STARTUP_GRACE_SECONDS + 60)
    record_audit(
        tier="business", category="drive", event_type="drive_exited",
        actor="drive", summary=_own_reason(2), repo=REPO, issue=1762,
        ts=launched_at + 5,
    )
    second_retry = cli("tick")
    assert second_retry.exit_code == 0, second_retry.output

    entry = queued(1762)
    assert entry["state"] == "blocked"
    assert entry["attempts"] == 2
    assert _own_reason(2) in entry["last_reason"]
    assert "died without landing the work" not in entry["last_reason"]
    assert state._get_drive_escalation_local(REPO, 1762) is not None


def test_a_permanent_dispatch_refusal_blocks_on_the_first_tick_no_attempt_spent(
    cli, seed, launches,
):
    """#1844, end-to-end. The regression test built from the exact #1817
    shape: `coord drive` refused a dispatch on a deterministic pre-dispatch
    guard (`enforce_oracle_readiness`) and exited `EXIT_DISPATCH_REFUSED`,
    recorded as `details.exit_code` on its own `drive_exited` audit row
    (exactly what `coord.drive.Driver._drive_exit_summary` writes after this
    issue's `coord/drive.py` fix). The tick must NOT treat this like the
    genuine-death case (`test_a_dead_drives_own_exit_reason_reaches_the_
    queue_row` above): straight to `blocked`, `attempts` UNCHANGED — not
    incremented once and then reset, literally never touched — and
    `last_reason` must carry the guard's own remedy.
    """
    from coord.audit import record_audit
    from coord.drive import EXIT_DISPATCH_REFUSED

    seed(issues={1762: "open"})
    cli("add", REPO, "1762")
    cli("tick")
    assert queued(1762)["attempts"] == 0

    refusal = (
        "drive exited for claude-coordinator#1762 (exit_code="
        f"{EXIT_DISPATCH_REFUSED}): dispatch failed: Issue #1762 is part of "
        "oracle-opted-in milestone ms-51 (Gate A satisfied) but has no "
        "acceptance slice yet — run `coord acceptance author "
        "claude-coordinator <tracking_issue> --issue 1762` first."
    )
    launched_at = queued(1762)["launched_at"]
    _backdate(1762, DRIVE_STARTUP_GRACE_SECONDS + 60)
    record_audit(
        tier="business", category="drive", event_type="drive_exited",
        actor="drive", summary=refusal, repo=REPO, issue=1762,
        ts=launched_at + 5,
        details={"exit_code": EXIT_DISPATCH_REFUSED, "error": refusal},
    )

    result = cli("tick")
    assert result.exit_code == 0, result.output
    assert refusal in result.output
    # NOT the retry path: no second launch, no requeue.
    assert len(launches) == 1, launches
    assert "died without landing the work" not in result.output

    entry = queued(1762)
    assert entry["state"] == "blocked"
    assert entry["attempts"] == 0
    assert refusal in entry["last_reason"]
    assert "coord acceptance author" in entry["last_reason"]
    assert state._get_drive_escalation_local(REPO, 1762) is not None


# ── #1891: `parked` — a missing CI verdict must not consume merge budget ────
#
# Black-box per this repo's CLAUDE.md: drives the queue through a simulated
# pending-CI window (a `merge_queue` row persisted the SAME way a real `coord
# merge` attempt would leave it after observing `checks_pending`) and asserts
# on `coord drive-queue list`/`status`'s RENDERED output, not just internal
# counters — a held queue must not look like an idle one.


def _seed_ci_pending_merge_row(coord_db, issue: int, *, reason: str = "CI running: build") -> None:
    """A `merge_queue` row shaped the way a live `coord merge --only` attempt
    leaves it after observing `checks_pending` — `entry.state` stays
    ``pending`` (unchanged, per `merge_queue.process()`), only `error`
    carries the `CI_PENDING_PREFIX`-tagged reason. This is exactly what
    `_local_merge_queue_rows()` reads back for a standalone/local-DB tick
    (this test suite's environment — no daemon `board_service` configured).
    """
    coord_db.execute(
        "INSERT INTO merge_queue "
        "(assignment_id, repo_name, repo_github, branch, target_branch, "
        " issue_number, issue_title, state, error, enqueued_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
        (
            f"w{issue}", REPO, "john/claude-coordinator", f"work-{issue}", "main",
            issue, f"issue {issue}", reason, time.time(),
        ),
    )
    coord_db.commit()


def test_a_parked_entry_renders_distinctly_from_waiting_and_blocked(
    cli, seed, launches, coord_db,
):
    seed(issues={1762: "open"})
    cli("add", REPO, "1762")
    cli("tick")
    assert queued(1762)["state"] == "running"

    _seed_ci_pending_merge_row(coord_db, 1762)
    _backdate(1762, DRIVE_STARTUP_GRACE_SECONDS + 60)

    result = cli("tick")
    assert result.exit_code == 0, result.output
    assert "parked" in result.output
    assert "CI running: build" in result.output

    entry = queued(1762)
    assert entry["state"] == "parked"
    assert entry["attempts"] == 0  # the whole point: never spent

    listing = cli("list")
    assert re.search(r"claude-coordinator#1762\s+parked", listing.output)
    assert "waiting" not in listing.output
    assert "blocked" not in listing.output

    status = cli("status")
    assert "1 parked" in status.output
    assert "blocked" not in status.output
    # No escalation of any kind — parked is quiet by design, unlike blocked.
    assert state._get_drive_escalation_local(REPO, 1762) is None
    assert (
        state._get_drive_escalation_local(QUEUE_ALERT_REPO, QUEUE_ALERT_ISSUE)
        is None
    )


def test_a_parked_entry_resumes_and_launches_once_ci_reports_no_operator(
    cli, seed, launches, coord_db,
):
    seed(issues={1762: "open"})
    cli("add", REPO, "1762")
    cli("tick")
    _seed_ci_pending_merge_row(coord_db, 1762)
    _backdate(1762, DRIVE_STARTUP_GRACE_SECONDS + 60)
    cli("tick")
    assert queued(1762)["state"] == "parked"

    # Checks report — clear the persisted signal exactly as the NEXT live
    # `coord merge` attempt (or a fresh `_gate_refresher` pass) would once
    # GitHub reports a real conclusion. No `coord drive-queue` command is
    # involved — the very next tick is the only thing that runs.
    coord_db.execute(
        "UPDATE merge_queue SET error = NULL WHERE issue_number = ?", (1762,)
    )
    coord_db.commit()

    result = cli("tick")
    assert result.exit_code == 0, result.output
    entry = queued(1762)
    assert entry["state"] == "running"  # resumed straight into a fresh launch
    assert entry["attempts"] == 0
    assert len(launches) == 2, launches

    status = cli("status")
    assert "parked" not in status.output
    assert "1 running" in status.output


def test_a_still_parked_entry_stays_parked_across_a_quiet_tick(
    cli, seed, launches, coord_db,
):
    """No regression: a tick that fires while CI is STILL pending leaves the
    entry exactly where it was — no relaunch, no write, no drift."""
    seed(issues={1762: "open"})
    cli("add", REPO, "1762")
    cli("tick")
    _seed_ci_pending_merge_row(coord_db, 1762)
    _backdate(1762, DRIVE_STARTUP_GRACE_SECONDS + 60)
    cli("tick")
    assert queued(1762)["state"] == "parked"
    assert len(launches) == 1, launches

    result = cli("tick")
    assert result.exit_code == 0, result.output
    entry = queued(1762)
    assert entry["state"] == "parked"
    assert entry["attempts"] == 0
    assert len(launches) == 1, launches  # no second launch attempt


# ── #2158: a park must have an exit that is not the merge it blocks ────────
#
# #1891's resume predicate was refreshable ONLY by a live `coord merge`
# attempt — the raw `merge_queue.error` string above is written by
# `merge_queue.process()` and by nothing else — and a parked entry by
# definition runs none. So the predicate that RELEASES the park was refreshed
# only by the action the park WITHHOLDS.
#
# claude-coordinator#2138, 2026-08-12 (UTC): CI run 31570947900 completed
# green at 06:48:51; the park below was written at 06:49:32 quoting "CI
# running: no-gh-on-path, test (3.13), test (3.12)"; the entry then held that
# 41-second-stale reading for 7h25m over a fully satisfied gate, invisible in
# every tick's output, until an unrelated merge happened to rewrite the board.
# Both tests here are that entry — one per exit the fix gives it.


@pytest.fixture
def board_merge_plan(monkeypatch):
    """Put a `merge_plan` section into the tick's `/board` payload.

    This suite's lane is the standalone/local-DB one, which has none: the
    plan is computed, not stored, so `_local_merge_queue_rows()` backfills the
    raw table only (see its docstring). This stands in for the DAEMON lane,
    where `GET /board` ships a `merge_plan[]` carrying `_entry_gate_status`'s
    fresh re-derivation plus `summarize_counts`'s CI rollup on every build.

    What it deliberately does NOT touch is `merge_queue.error` — that stays
    exactly as the parking `coord merge` attempt left it, which is the whole
    point: the entry must resume with no live merge having run in between.
    """
    from coord.drive_state import BoardFetcher

    real_fetch = BoardFetcher.fetch
    rows: list[dict] = []

    def fetch_with_plan(self, *args, **kwargs):
        payload = real_fetch(self, *args, **kwargs)
        if isinstance(payload, dict):
            payload = {**payload, "merge_plan": list(rows)}
        return payload

    monkeypatch.setattr(BoardFetcher, "fetch", fetch_with_plan, raising=True)

    def _set(*plan_rows: dict) -> None:
        rows[:] = list(plan_rows)

    return _set


def _plan_row(issue: int, *, reason=None, passed=0, failed=0, running=0) -> dict:
    """One `/board` `merge_plan` row as `serve_app` ships it — `asdict` of a
    `PlannedMerge`, so `ci_summary` is a nested `CiCheckSummary` dict."""
    return {
        "repo_name": REPO,
        "issue_number": issue,
        "reason": reason,
        "ci_summary": {
            "passed": passed,
            "failed": failed,
            "running": running,
            "failed_names": [],
            "first_failed_url": None,
        },
    }


def _park(cli, seed, coord_db, issue: int = 2138) -> None:
    """Drive a fresh entry all the way into `parked` on pending CI."""
    seed(issues={issue: "open"})
    cli("add", REPO, str(issue))
    cli("tick")
    _seed_ci_pending_merge_row(
        coord_db,
        issue,
        reason="CI running: no-gh-on-path, test (3.13), test (3.12)",
    )
    _backdate(issue, DRIVE_STARTUP_GRACE_SECONDS + 60)
    cli("tick")
    assert queued(issue)["state"] == "parked"


def test_a_parked_entry_resumes_when_the_boards_own_ci_rollup_reports_green(
    cli, seed, launches, coord_db, board_merge_plan,
):
    """THE #2158 regression.

    CI finishes and the board's next build sees it: the plan re-derives this
    entry clean and its `ci_summary` shows all 8 checks green. The raw
    `merge_queue.error` is UNCHANGED — no `coord merge` has run, and none can,
    because the entry is parked. The very next `drive-queue tick` must resume
    it anyway.
    """
    _park(cli, seed, coord_db)
    assert len(launches) == 1, launches

    # 06:48:51 — the run completes, all green. Nothing else happens: no
    # operator, no merge, no other command. The raw row still says "CI
    # running: ..." and will say so forever.
    board_merge_plan(_plan_row(2138, reason=None, passed=8))
    persisted = coord_db.execute(
        "SELECT error FROM merge_queue WHERE issue_number = ?", (2138,)
    ).fetchone()
    assert persisted["error"].startswith("CI running:")

    result = cli("tick")
    assert result.exit_code == 0, result.output
    entry = queued(2138)
    assert entry["state"] == "running"  # resumed straight into a fresh launch
    assert entry["attempts"] == 0  # …and still free, per #1891
    assert len(launches) == 2, launches

    status = cli("status")
    assert "parked" not in status.output


def test_a_park_the_boards_rollup_still_calls_pending_does_not_hot_loop(
    cli, seed, launches, coord_db, board_merge_plan,
):
    """The other half: CI genuinely still running is not evidence against the
    park — it agrees with it. No resume, no relaunch, whatever the clock says
    (this park is backdated 30h, far past `PARK_STALE_SECONDS`, and a reading
    the board re-derives every build is never stale)."""
    _park(cli, seed, coord_db)
    board_merge_plan(_plan_row(2138, reason="CI running: test (3.12)", running=1))
    _backdate_reason(coord_db, 2138, 30 * 3600)

    result = cli("tick")
    assert result.exit_code == 0, result.output
    assert queued(2138)["state"] == "parked"
    assert len(launches) == 1, launches
    assert "1 parked" in cli("status").output


def test_a_park_that_can_never_refresh_itself_ages_out_and_relaunches(
    cli, seed, launches, coord_db,
):
    """The second exit, for the lane where no rollup can ever arrive.

    No `merge_plan` section at all — the daemon-host tick, this suite's own
    default lane. The reading holding this park has no read-path writer in
    existence, so past `PARK_STALE_SECONDS` the tick stops believing it rather
    than holding a possibly-mergeable entry forever.
    """
    _park(cli, seed, coord_db)
    _backdate_reason(coord_db, 2138, PARK_STALE_SECONDS + 60)

    result = cli("tick")
    assert result.exit_code == 0, result.output
    assert "#2158" in result.output
    entry = queued(2138)
    assert entry["state"] == "running"
    assert entry["attempts"] == 0  # ageing out spends nothing either
    assert len(launches) == 2, launches


def test_a_park_younger_than_the_ceiling_is_left_alone(
    cli, seed, launches, coord_db,
):
    """The ceiling is a backstop, not a second CI timeout — a park inside it
    behaves exactly as it did before #2158."""
    _park(cli, seed, coord_db)
    _backdate_reason(coord_db, 2138, PARK_STALE_SECONDS - 120)

    result = cli("tick")
    assert result.exit_code == 0, result.output
    assert queued(2138)["state"] == "parked"
    assert len(launches) == 1, launches


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


@pytest.mark.posix_only
@pytest.mark.skipif(
    sys.platform == "win32",
    reason="FileLock is backed by fcntl.flock() (coord/filelock.py) — POSIX-only "
    "advisory locking, no Windows lock backend implemented yet",
)
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


# ── #2159: a transient board-read lock retries instead of failing the tick ──


def test_a_transient_locked_board_read_retries_and_the_tick_still_launches(
    cli, seed, launches, monkeypatch
):
    """Two `database is locked` reads followed by a real one must not abort
    the tick — the read is idempotent, so the bounded retry recovers it and
    the tick completes exactly as an unretried, first-try success would."""
    import sqlite3

    from coord.commands import drive_queue as drive_queue_cmd

    seed(issues={1650: "open"})
    cli("add", REPO, "1650", "--machine", "dellserver")

    real_fetch = drive_queue_cmd._fetch_board_view
    calls = {"n": 0}

    def flaky() -> drive_queue_cmd.BoardView:
        calls["n"] += 1
        if calls["n"] <= 2:
            raise sqlite3.OperationalError("database is locked")
        return real_fetch()

    monkeypatch.setattr(drive_queue_cmd, "_fetch_board_view", flaky)
    slept: list[float] = []
    monkeypatch.setattr(drive_queue_cmd.time, "sleep", slept.append)

    result = cli("tick")
    assert result.exit_code == 0, result.output
    assert calls["n"] == 3
    assert len(slept) == 2  # backed off between attempts 1→2 and 2→3
    assert "recovered after 2 retry" in result.output

    # The tick did real work — not a silent no-op — exactly like an unretried
    # success would.
    assert "launched" in result.output
    assert launches and "1650" in " ".join(launches[0])
    assert queued(1650)["state"] == "running"


def test_a_board_read_still_locked_past_the_retry_budget_aborts_as_before(
    cli, seed, launches, monkeypatch
):
    """The retry budget is bounded — a lock that never clears must still
    abort the tick with the pre-#2159 message, not spin forever or no-op."""
    import sqlite3

    from coord.commands import drive_queue as drive_queue_cmd

    seed(issues={1650: "open"})
    cli("add", REPO, "1650")
    before = state._list_drive_queue_local()

    calls = {"n": 0}

    def always_locked() -> drive_queue_cmd.BoardView:
        calls["n"] += 1
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(drive_queue_cmd, "_fetch_board_view", always_locked)
    monkeypatch.setattr(drive_queue_cmd.time, "sleep", lambda _s: None)

    result = cli("tick")
    assert result.exit_code != 0
    assert "aborting without launching" in result.output
    assert "database is locked" in result.output
    assert calls["n"] == drive_queue_cmd._BOARD_READ_RETRY_ATTEMPTS
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


# ── add: --scope (#2186) ──────────────────────────────────────────────────────


def test_add_hold_after_defaults_to_entry_scope(cli):
    result = cli("add", REPO, "1753", "--hold-after", "--hold-reason", "deploy")
    assert result.exit_code == 0, result.output
    assert queued(1753)["hold_scope"] == "entry"
    # The default scope is quiet in `list` — only a non-default one is worth
    # a word (see the fleet-scope test below).
    assert "fleet" not in cli("list").output


def test_add_hold_after_can_declare_fleet_scope(cli):
    result = cli(
        "add", REPO, "1753",
        "--hold-after", "--hold-reason", "deploy", "--scope", "fleet",
    )
    assert result.exit_code == 0, result.output
    assert "fleet-wide" in result.output
    assert queued(1753)["hold_scope"] == "fleet"

    listed = cli("list")
    assert listed.exit_code == 0, listed.output
    assert "scope=fleet" in listed.output
    assert "fleet-wide" in listed.output


def test_scope_fleet_without_hold_after_is_refused(cli):
    result = cli("add", REPO, "1753", "--scope", "fleet")
    assert result.exit_code != 0
    assert "--hold-after" in result.output
    assert state._list_drive_queue_local() == []


def test_scope_rejects_a_value_other_than_entry_or_fleet(cli):
    result = cli(
        "add", REPO, "1753", "--hold-after", "--hold-reason", "d", "--scope", "queue",
    )
    assert result.exit_code != 0
    assert state._list_drive_queue_local() == []


# ── tick: the gate fires, and #2186 scopes what it blocks ────────────────────


def test_2186_a_fired_gate_does_not_block_an_unrelated_eligible_entry(
    cli, seed, launches
):
    """THE #2186 acceptance test: default scope is `entry`, not `fleet`.

    Free capacity, a fully eligible #1754 that has NO `--after` relationship
    to #1753 — it launches in the same tick, even though #1753's gate just
    fired. This is the exact incident: one issue's deploy dependency must not
    idle the rest of the fleet.
    """
    cli("add", REPO, "1753", "--hold-after", "--hold-reason", "restart coord-serve")
    cli("add", REPO, "1754")
    state._update_drive_queue_entry_local(REPO, 1753, state="running")
    seed(
        issues={1753: "closed", 1754: "open"},
        assignments=[{"issue_number": 1753, "status": "merged"}],
    )

    result = cli("tick", "--max-parallel", "1")
    assert result.exit_code == 0, result.output
    assert "1754" in " ".join(launches[0])
    assert queued(1753)["state"] == "done"
    assert queued(1753)["hold_state"] == "fired"
    assert queued(1753)["hold_scope"] == "entry"
    assert queued(1754)["state"] == "running"

    # The gate fired, but scoped — no fleet-wide "QUEUE HELD" alert.
    alert = state._get_drive_escalation_local(QUEUE_ALERT_REPO, QUEUE_ALERT_ISSUE)
    assert alert is None
    assert state._get_drive_escalation_local(REPO, 1753) is None


def test_a_fired_gate_still_blocks_its_own_after_dependent(cli, seed, launches):
    """The other half of #2186: scoping the hold must not remove it."""
    cli("add", REPO, "1753", "--hold-after", "--hold-reason", "restart coord-serve")
    cli("add", REPO, "1754", "--after", "1753")
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
    # #1754 WAS touched — deferred, with a live reason, not silently frozen.
    assert queued(1754)["state"] == "waiting"
    assert queued(1754)["deferrals"] == 1
    assert "restart coord-serve" in queued(1754)["last_reason"]

    # Deferring #1754 is not itself the fleet-wide "QUEUE HELD" alert — it IS
    # the queue's ordinary "nothing eligible" alert, since #1754 is the only
    # entry left waiting.
    alert = state._get_drive_escalation_local(QUEUE_ALERT_REPO, QUEUE_ALERT_ISSUE)
    assert alert is not None
    assert "restart coord-serve" in alert["gate_readings"]


def test_a_fleet_scoped_gate_launches_nothing_even_with_an_eligible_successor(
    cli, seed, launches
):
    """The pre-#2186 whole-queue stop, preserved for an explicit --scope=fleet."""
    cli(
        "add", REPO, "1753",
        "--hold-after", "--hold-reason", "restart coord-serve", "--scope", "fleet",
    )
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
    assert queued(1753)["hold_scope"] == "fleet"
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
    cli("add", REPO, "1754", "--after", "1753")
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
    cli("add", REPO, "1754", "--after", "1753")
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
    cli("add", REPO, "1754", "--after", "1753")
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
        # #2186: the gate is entry-scoped by default, so the queue-level
        # alert is the ordinary "nothing eligible" one raised by #1754's own
        # deferral (the only entry left waiting) — not a fleet-wide
        # `QUEUE HELD`. Either way it carries the rising attempt count.
        alert = state._get_drive_escalation_local(QUEUE_ALERT_REPO, QUEUE_ALERT_ISSUE)
        assert f"attempt {expected} failed" in alert["gate_readings"]
        assert f"attempt {expected} failed" in queued(1754)["last_reason"]

    assert launches == []
    assert len(probes.calls) == 3
    # #2186: #1754 was re-evaluated (and its reason re-written) EVERY tick —
    # the fix for the incident's stale, hours-old `last:` text.
    assert queued(1754)["deferrals"] >= 3
    assert "failed 3×" in cli("status").output


def test_a_passing_probe_releases_and_launches_in_the_same_tick(
    cli, seed, launches, probes
):
    cli(
        "add", REPO, "1753",
        "--hold-after", "--hold-reason", "deploy",
        "--resume-when", "curl -sf http://dellserver:7435/drive-queue",
    )
    cli("add", REPO, "1754", "--after", "1753")
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
