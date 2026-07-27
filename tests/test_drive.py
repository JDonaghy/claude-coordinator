"""Tests for coord/drive.py — the `coord drive` state machine (#1392).

Every bug ``scripts/drive-issue.sh`` shipped was in decision logic, not in
subprocess orchestration:

- an ``advisory`` work row fell through to a bare ``sleep; continue`` — a silent
  240-minute spin (PR #1386)
- merge verification used ``merge-base --is-ancestor``, which is **always**
  wrong under ``coord merge --method rebase``
- unbounded merge retries until the deadline
- interactive work parked on a review that was never coming (#555)

In bash those were untestable. Here they are :func:`coord.drive.decide` /
:func:`coord.drive.preflight` calls, and this file is the "behaviour that must
survive the port" list from #1392, one test per item.

The other invariant under test is the CLI boundary: every board mutation must
go out as a ``coord`` subcommand argv, never a direct internal call. Calling
``record_test_verdict()`` instead of ``coord test --passed`` silently
reintroduces #1384 (the CLI mirrors ``test_state`` → the legacy ``smoke_test``
field; the function alone does not), which makes ``coord fix`` refuse to
dispatch. So the assertions below are on ``action.command``.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from coord.config import Config
from coord.drive import (
    EXIT_DEADLINE,
    EXIT_OK,
    EXIT_TERMINAL_FAILURE,
    EXIT_USAGE,
    RUN,
    WAIT,
    Action,
    DriveCounters,
    DriveError,
    DriveOptions,
    Driver,
    FileLock,
    GitMergeVerifier,
    LockBusy,
    coord_argv,
    decide,
    preflight,
)
from coord.drive_state import IssueState
from coord.models import Machine, Repo


REPO = "claude-coordinator"
ISSUE = 1392


def make_config() -> Config:
    return Config(
        repos=[Repo(name=REPO, github="john/claude-coordinator", test_command="pytest -q")],
        machines=[Machine(name="precision", host="precision", repos=[REPO])],
    )


def state(**kw) -> IssueState:
    base = dict(repo=REPO, issue=ISSUE, repo_github="john/claude-coordinator")
    base.update(kw)
    return IssueState(**base)


class FakeVerifier:
    """Stands in for git/gh so decision tests never touch the network."""

    def __init__(self, *, has_commits: bool = True, merged: bool = True) -> None:
        self._has_commits = has_commits
        self._merged = merged
        self.commits_calls = 0
        self.merged_calls = 0

    def branch_has_commits(self, s: IssueState) -> bool:
        self.commits_calls += 1
        return self._has_commits

    def verify_merged(self, s: IssueState) -> bool:
        self.merged_calls += 1
        return self._merged


def step(s: IssueState, opts: DriveOptions | None = None, **kw) -> Action:
    """One decide() call with sensible defaults."""
    verifier = kw.pop("verifier", None) or FakeVerifier()
    counters = kw.pop("counters", None) or DriveCounters()
    return decide(
        s,
        opts or DriveOptions(machine="precision"),
        counters,
        verifier,
        machine=kw.pop("machine", "precision"),
    )


# ═══════════════════════════════════════════════════════════════════════════
# preflight
# ═══════════════════════════════════════════════════════════════════════════


def test_preflight_resolves_the_least_loaded_machine_when_none_given():
    pre = preflight(state(picked_machine="dellserver"), DriveOptions())
    assert pre.machine == "dellserver"


def test_preflight_prefers_an_explicit_machine():
    pre = preflight(
        state(picked_machine="dellserver"), DriveOptions(machine="precision")
    )
    assert pre.machine == "precision"


def test_preflight_refuses_when_no_machine_hosts_the_repo():
    with pytest.raises(DriveError) as exc:
        preflight(state(), DriveOptions())
    assert "no unpaused machine hosts" in str(exc.value)
    assert exc.value.exit_code == EXIT_USAGE


def test_preflight_warns_when_the_auto_loop_is_off():
    pre = preflight(state(picked_machine="m", auto_loop=False), DriveOptions())
    assert any("auto_loop is OFF" in w for w in pre.warnings)


# ── #555: interactive work is refused at PREFLIGHT, not at the review gate ──


def test_interactive_work_is_refused_at_preflight_not_after_the_test_suite():
    """#555 + the #1357 drive: a run must not burn ~6min of tests then park.

    `dispatch_pending_reviews` carries `provider_name != "claude-pty"`, so for
    interactive work the review is not late — it is never coming.
    """
    s = state(
        picked_machine="m",
        work_aid="w1",
        work_provider="claude-pty",
        work_status="done",
        work_branch="b",
    )
    with pytest.raises(DriveError) as exc:
        preflight(s, DriveOptions())
    assert "INTERACTIVELY" in str(exc.value)
    assert "--force-review" in str(exc.value)
    assert exc.value.exit_code == EXIT_USAGE


def test_force_review_turns_the_preflight_refusal_into_a_warning():
    s = state(
        picked_machine="m",
        work_aid="w1",
        work_provider="claude-pty",
        work_status="done",
    )
    pre = preflight(s, DriveOptions(force_review=True))
    assert any("--force-review set" in w for w in pre.warnings)


def test_preflight_allows_interactive_work_that_already_has_a_review():
    s = state(
        picked_machine="m",
        work_aid="w1",
        work_provider="claude-pty",
        review_aid="r1",
    )
    assert preflight(s, DriveOptions()).machine == "m"


def test_preflight_allows_headless_work():
    s = state(picked_machine="m", work_aid="w1", work_provider="claude-code")
    assert preflight(s, DriveOptions()).machine == "m"


# ═══════════════════════════════════════════════════════════════════════════
# no work yet: plan / dispatch
# ═══════════════════════════════════════════════════════════════════════════


def test_no_work_row_dispatches_work_through_the_cli():
    action = step(state())
    assert action.kind == RUN
    assert action.command == ("assign", "precision", REPO, "1392")


def test_dispatch_work_passes_model_and_briefing_file():
    opts = DriveOptions(machine="precision", model="opus", briefing_file="/tmp/b.md")
    action = step(state(), opts)
    assert action.command == (
        "assign", "precision", REPO, "1392",
        "--model", "opus",
        "--briefing-file", "/tmp/b.md",
    )


def test_plan_flag_dispatches_a_plan_only_assignment_first():
    action = step(state(), DriveOptions(machine="precision", do_plan=True))
    assert action.command == ("assign", "--plan-only", "precision", REPO, "1392")


def test_a_done_plan_is_auto_approved():
    action = step(
        state(plan_aid="p1", plan_status="done"),
        DriveOptions(machine="precision", do_plan=True),
    )
    assert action.command == ("approve-plan", "p1")


def test_a_failed_plan_is_terminal():
    action = step(
        state(plan_aid="p1", plan_status="failed"),
        DriveOptions(machine="precision", do_plan=True),
    )
    assert action.is_exit
    assert action.exit_code == EXIT_TERMINAL_FAILURE
    assert "plan assignment p1 failed" in action.message


def test_a_running_plan_just_waits():
    action = step(
        state(plan_aid="p1", plan_status="running"),
        DriveOptions(machine="precision", do_plan=True),
    )
    assert action.kind == WAIT


def test_anything_active_just_waits():
    action = step(state(active_count=1, active_types=("smoke",)))
    assert action.kind == WAIT


# ═══════════════════════════════════════════════════════════════════════════
# work-stage terminal states
# ═══════════════════════════════════════════════════════════════════════════


def test_failed_work_retries_through_the_cli_then_stops_at_the_cap():
    counters = DriveCounters()
    opts = DriveOptions(machine="precision", max_work_retries=1)
    s = state(work_aid="w1", work_status="failed", work_failure_reason="boom")

    first = step(s, opts, counters=counters)
    assert first.command == ("retry", "w1")
    assert counters.work_retries == 1

    second = step(s, opts, counters=counters)
    assert second.is_exit
    assert second.exit_code == EXIT_TERMINAL_FAILURE
    assert "boom" in second.message


def test_usage_limit_kill_waits_instead_of_retrying_or_dying():
    """#1461: a usage-limit kill must WAIT (not retry, not die/escalate) —
    retrying before the reset just burns the same exhausted budget and fails
    again for no diagnostic reason. Must not consume the retry budget."""
    counters = DriveCounters()
    opts = DriveOptions(machine="precision", max_work_retries=1)
    s = state(
        work_aid="w1", work_status="failed",
        work_failure_reason="usage limit — resets 8:30pm (America/Chicago)",
    )
    action = step(s, opts, counters=counters)
    assert action.kind == WAIT
    assert counters.work_retries == 0
    assert any("usage-limit" in w for w in action.warnings)
    assert "8:30pm (America/Chicago)" in action.warnings[0]

    # And it keeps waiting — never escalates into the retry-cap die either,
    # even across repeated polls.
    action2 = step(s, opts, counters=counters)
    assert action2.kind == WAIT
    assert counters.work_retries == 0


def test_usage_limit_kill_on_advisory_also_waits():
    """A kill has been observed landing ADVISORY (clean exit, 0 commits) just
    as often as FAILED — must be recognised regardless of which terminal
    status the agent's own reap chose."""
    action = step(
        state(
            work_aid="w1", work_status="advisory",
            work_failure_reason="usage limit — resets 8:30pm (America/Chicago)",
        ),
        verifier=FakeVerifier(has_commits=False),
    )
    assert action.kind == WAIT


def test_normal_advisory_failure_reason_is_not_mistaken_for_a_usage_limit():
    """A failure_reason that doesn't carry the exact stamped prefix must fall
    through to the ordinary retry-or-die path unchanged."""
    counters = DriveCounters()
    opts = DriveOptions(machine="precision", max_work_retries=1)
    s = state(
        work_aid="w1", work_status="failed",
        work_failure_reason="usage limit exceeded on some unrelated API call",
    )
    action = step(s, opts, counters=counters)
    assert action.command == ("retry", "w1")
    assert counters.work_retries == 1


def test_cancelled_work_is_terminal_and_says_how_to_re_dispatch():
    action = step(state(work_aid="w1", work_status="cancelled"))
    assert action.is_exit
    assert "--force" in action.message


def test_an_unknown_terminal_status_refuses_to_guess():
    """No terminal status may fall through to a bare wait (PR #1386)."""
    action = step(state(work_aid="w1", work_status="wat"))
    assert action.is_exit
    assert action.exit_code == EXIT_TERMINAL_FAILURE
    assert "refusing to guess" in action.message


def test_done_work_with_no_branch_is_terminal():
    action = step(state(work_aid="w1", work_status="done", work_branch=""))
    assert action.is_exit
    assert "no branch" in action.message


# ── advisory: distinguished by whether the branch actually carries commits ───


def test_advisory_with_no_commits_on_the_branch_is_terminal():
    verifier = FakeVerifier(has_commits=False)
    action = step(
        state(work_aid="w1", work_status="advisory", work_branch="b"),
        verifier=verifier,
    )
    assert action.is_exit
    assert "no commits on its branch" in action.message
    assert verifier.commits_calls == 1


def test_advisory_with_commits_stops_and_names_1357_without_accept_advisory():
    action = step(
        state(work_aid="w1", work_status="advisory", work_branch="b"),
        verifier=FakeVerifier(has_commits=True),
    )
    assert action.is_exit
    assert "#1357" in action.message
    assert "--accept-advisory" in action.message


def test_advisory_with_commits_proceeds_under_accept_advisory():
    """PR #1386: this arm used to fall through to a bare sleep — a silent
    240-minute spin. It must reach the Test gate and warn while doing so."""
    action = step(
        state(work_aid="w1", work_status="advisory", work_branch="b", work_test_state=""),
        DriveOptions(machine="precision", accept_advisory=True),
        verifier=FakeVerifier(has_commits=True),
    )
    assert action.kind == WAIT  # waiting on coord to dispatch the Test stage
    assert any("--accept-advisory" in w for w in action.warnings)


def test_advisory_with_commits_and_a_passed_test_reaches_the_merge_stage():
    action = step(
        state(
            work_aid="w1",
            work_status="advisory",
            work_branch="b",
            work_test_state="passed",
            review_verdict="approve",
        ),
        DriveOptions(machine="precision", accept_advisory=True),
        verifier=FakeVerifier(has_commits=True),
    )
    assert action.command[:2] == ("merge", "--only")
    assert any("--accept-advisory" in w for w in action.warnings)


def test_advisory_with_no_branch_at_all_is_terminal():
    action = step(state(work_aid="w1", work_status="advisory", work_branch=""))
    assert action.is_exit
    assert "no commits on its branch" in action.message


# ═══════════════════════════════════════════════════════════════════════════
# the TEST gate
# ═══════════════════════════════════════════════════════════════════════════


def done_work(**kw) -> IssueState:
    base = dict(work_aid="w1", work_status="done", work_branch="issue-1392-x")
    base.update(kw)
    return state(**base)


def test_no_test_verdict_yet_waits_for_coord_to_dispatch_the_stage():
    """#1426: coord's own dispatch_pending_smoke runs the Test stage. Two
    drivers racing to dispatch the same thing is the #476/#477 incident."""
    action = step(done_work(work_test_state=""))
    assert action.kind == WAIT
    assert action.command == ()


def test_skip_test_records_the_verdict_through_the_test_cli():
    """The CLI, never record_test_verdict() — see #1384."""
    action = step(done_work(), DriveOptions(machine="precision", skip_test=True))
    assert action.command == (
        "test", "--skipped", "--reason", "coord drive --skip-test", "w1",
    )
    assert action.sleep_after == 5.0


def test_a_running_test_stage_just_waits():
    action = step(done_work(work_test_state="running"))
    assert action.kind == WAIT


def test_a_failed_test_loops_through_coord_fix_on_the_same_branch():
    """`coord fix` gates on the legacy smoke_test field and dispatches with
    inherit_branch=True — the same branch, model escalated (#1445)."""
    counters = DriveCounters()
    action = step(done_work(work_test_state="failed"), counters=counters)
    assert action.command == ("fix", "w1")
    assert counters.fix_rounds == 1
    assert "smoke_test" in action.error_message  # the diagnosis if it won't dispatch
    assert action.on_error == "die"


def test_the_test_fix_loop_is_bounded_by_max_fix_rounds():
    counters = DriveCounters()
    opts = DriveOptions(machine="precision", max_fix_rounds=2)
    s = done_work(work_test_state="failed", work_test_reason="3 failed")

    assert step(s, opts, counters=counters).command == ("fix", "w1")
    assert step(s, opts, counters=counters).command == ("fix", "w1")
    exhausted = step(s, opts, counters=counters)
    assert exhausted.is_exit
    assert exhausted.exit_code == EXIT_TERMINAL_FAILURE
    assert "after 2 fix round(s)" in exhausted.message
    assert "3 failed" in exhausted.message


def test_max_fix_rounds_zero_never_dispatches_a_fix():
    action = step(
        done_work(work_test_state="failed"),
        DriveOptions(machine="precision", max_fix_rounds=0),
    )
    assert action.is_exit


def test_an_unexpected_test_state_warns_and_waits_rather_than_guessing():
    action = step(done_work(work_test_state="weird"))
    assert action.kind == WAIT
    assert any("weird" in w for w in action.warnings)


@pytest.mark.parametrize("verdict", ["passed", "skipped"])
def test_a_passed_or_skipped_test_falls_through_to_the_review_gate(verdict):
    action = step(done_work(work_test_state=verdict))
    assert action.kind == WAIT  # no review row yet
    assert action.command == ()


# ═══════════════════════════════════════════════════════════════════════════
# the REVIEW gate
# ═══════════════════════════════════════════════════════════════════════════


def work_tested(**kw) -> IssueState:
    return done_work(work_test_state="passed", **kw)


def test_no_review_row_yet_waits_for_coords_auto_dispatch():
    action = step(work_tested())
    assert action.kind == WAIT
    assert action.command == ()


def test_a_review_that_finished_with_no_verdict_is_terminal():
    action = step(work_tested(review_aid="r1", work_review_state="done"))
    assert action.is_exit
    assert "NO verdict" in action.message


def test_request_changes_waits_for_coords_auto_loop_to_dispatch_the_fix():
    """This driver must NOT dispatch a review fix itself — #476/#477."""
    action = step(
        work_tested(
            review_aid="r1",
            review_verdict="request-changes",
            work_review_iter=1,
            max_review_iterations=5,
        )
    )
    assert action.kind == WAIT
    assert action.command == ()


def test_request_changes_stops_when_the_review_fix_loop_is_exhausted():
    action = step(
        work_tested(
            review_aid="r1",
            review_verdict="request-changes",
            work_review_iter=5,
            max_review_iterations=5,
        )
    )
    assert action.is_exit
    assert "fix loop is exhausted" in action.message


def test_interactive_work_requests_its_review_exactly_once_under_force_review():
    counters = DriveCounters()
    opts = DriveOptions(machine="precision", force_review=True)
    s = work_tested(work_provider="claude-pty")

    first = step(s, opts, counters=counters)
    assert first.command == ("review", "w1")
    assert counters.review_dispatches == 1

    second = step(s, opts, counters=counters)
    assert second.is_exit
    assert "none appeared on the board" in second.message


def test_interactive_work_without_force_review_is_terminal_at_the_review_gate():
    action = step(work_tested(work_provider="claude-pty"))
    assert action.is_exit
    assert "#555" in action.message


def test_an_unexpected_review_verdict_warns_and_waits():
    action = step(work_tested(review_aid="r1", review_verdict="maybe"))
    assert action.kind == WAIT
    assert any("maybe" in w for w in action.warnings)


# ═══════════════════════════════════════════════════════════════════════════
# the MERGE stage
# ═══════════════════════════════════════════════════════════════════════════


def approved_work(**kw) -> IssueState:
    return work_tested(review_aid="r1", review_verdict="approve", **kw)


def test_no_merge_stops_after_the_review_approves():
    action = step(approved_work(), DriveOptions(machine="precision", do_merge=False))
    assert action.is_exit
    assert action.exit_code == EXIT_OK
    assert "coord merge --only w1" in action.message


def test_an_approved_review_merges_through_the_cli_under_the_merge_lock():
    action = step(approved_work())
    assert action.command == ("merge", "--only", "w1", "--method", "rebase")
    assert action.serialize_merge is True
    # Tolerant: the first attempt often precedes enqueue_approved_work.
    assert action.on_error == "warn"


def test_the_merge_method_is_honoured():
    action = step(
        approved_work(), DriveOptions(machine="precision", merge_method="squash")
    )
    assert action.command[-1] == "squash"


def test_merge_uses_the_queue_entrys_assignment_id_when_it_differs():
    """A fix chain enqueues under an earlier work row; --only must match it."""
    action = step(approved_work(merge_aid="w0", merge_status=""))
    assert action.command == ("merge", "--only", "w0", "--method", "rebase")


def test_merge_retries_are_bounded():
    """Unbounded merge retries until the deadline was a real bug."""
    counters = DriveCounters()
    opts = DriveOptions(machine="precision", max_merge_attempts=2)
    s = approved_work()

    assert step(s, opts, counters=counters).kind == RUN
    assert step(s, opts, counters=counters).kind == RUN
    exhausted = step(s, opts, counters=counters)
    assert exhausted.is_exit
    assert "merge attempted 2 times without landing" in exhausted.message


def test_human_required_merge_is_terminal_with_the_override_recipe():
    action = step(approved_work(merge_status="HUMAN_REQUIRED", merge_reason="semantic"))
    assert action.is_exit
    assert "--override-human-required" in action.message
    assert "semantic" in action.message


@pytest.mark.parametrize("status", ["HUMAN_REQUIRED", "human_required"])
def test_human_required_is_matched_case_insensitively(status):
    assert step(approved_work(merge_status=status)).is_exit


def test_a_conflict_waits_for_coords_conflict_fix_worker():
    action = step(approved_work(merge_status="CONFLICT"))
    assert action.kind == WAIT
    assert "conflict-fix" in action.label


def test_a_blocked_merge_waits_and_reports_the_gate():
    action = step(approved_work(merge_status="BLOCKED", merge_reason="CI running"))
    assert action.kind == WAIT
    assert "CI running" in action.label


# ── terminal: merged, verified ───────────────────────────────────────────────


def test_a_merged_board_row_is_verified_before_reporting_success():
    verifier = FakeVerifier(merged=True)
    action = step(approved_work(work_status="merged"), verifier=verifier)
    assert action.is_exit
    assert action.exit_code == EXIT_OK
    assert "MERGED" in action.message
    assert verifier.merged_calls == 1


def test_a_merged_board_row_that_did_not_actually_land_fails_loudly():
    action = step(
        approved_work(work_status="merged"), verifier=FakeVerifier(merged=False)
    )
    assert action.is_exit
    assert action.exit_code == EXIT_TERMINAL_FAILURE
    assert "has NOT landed" in action.message


def test_merge_status_merged_is_also_a_terminal_success():
    action = step(approved_work(merge_status="MERGED"), verifier=FakeVerifier(merged=True))
    assert action.exit_code == EXIT_OK


def test_a_merged_row_with_no_branch_cannot_be_verified():
    action = step(approved_work(work_status="merged", work_branch=""))
    assert action.exit_code == EXIT_TERMINAL_FAILURE


# ═══════════════════════════════════════════════════════════════════════════
# GitMergeVerifier — never `merge-base --is-ancestor`
# ═══════════════════════════════════════════════════════════════════════════


@pytest.fixture
def recorded_git(monkeypatch):
    """Capture every subprocess argv and script the return values."""
    calls: list[list[str]] = []
    scripted: dict[tuple[str, ...], tuple[int, str]] = {}

    def fake_run(argv, **kw):
        calls.append(list(argv))
        for needle, (rc, out) in scripted.items():
            if all(token in argv for token in needle):
                return subprocess.CompletedProcess(argv, rc, out, "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr("coord.drive.subprocess.run", fake_run)
    return calls, scripted


def test_verify_merged_never_uses_merge_base_is_ancestor(recorded_git, tmp_path):
    """`--is-ancestor` is ALWAYS wrong under --method rebase/squash: both
    rewrite the commits, so a landed branch's tip is never an ancestor of the
    target. Verified against #1344 (merged via PR #1355, still says no)."""
    calls, scripted = recorded_git
    (tmp_path / ".git").mkdir()
    scripted[("cherry",)] = (0, "- abc123\n- def456\n")

    verifier = GitMergeVerifier(repo_path=str(tmp_path))
    s = state(work_branch="issue-1392-x", repo_github="", repo_default_branch="main")
    assert verifier.verify_merged(s) is True

    flat = [" ".join(c) for c in calls]
    assert not any("--is-ancestor" in c for c in flat), flat
    assert any("cherry" in c for c in flat), flat


def test_verify_merged_reports_unmerged_when_cherry_shows_a_plus(recorded_git, tmp_path):
    _calls, scripted = recorded_git
    (tmp_path / ".git").mkdir()
    scripted[("cherry",)] = (0, "- abc123\n+ def456\n")
    s = state(work_branch="b", repo_github="", repo_default_branch="main")
    assert GitMergeVerifier(repo_path=str(tmp_path)).verify_merged(s) is False


def test_verify_merged_prefers_the_github_pr_state(recorded_git, tmp_path, monkeypatch):
    calls, scripted = recorded_git
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr("coord.drive.shutil.which", lambda name: "/usr/bin/gh")
    scripted[("pr", "view")] = (0, "MERGED\n")

    s = state(work_branch="b", repo_github="john/x")
    assert GitMergeVerifier(repo_path=str(tmp_path)).verify_merged(s) is True
    # Authoritative: no git fallback needed.
    assert not any("cherry" in " ".join(c) for c in calls)


def test_verify_merged_rejects_a_closed_pr(recorded_git, tmp_path, monkeypatch):
    _calls, scripted = recorded_git
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr("coord.drive.shutil.which", lambda name: "/usr/bin/gh")
    scripted[("pr", "view")] = (0, "CLOSED\n")

    warned: list[str] = []
    verifier = GitMergeVerifier(repo_path=str(tmp_path), warn=warned.append)
    assert verifier.verify_merged(state(work_branch="b", repo_github="john/x")) is False
    assert any("CLOSED" in w for w in warned)


def test_verify_merged_falls_back_to_cherry_when_gh_finds_no_pr(
    recorded_git, tmp_path, monkeypatch
):
    calls, scripted = recorded_git
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr("coord.drive.shutil.which", lambda name: "/usr/bin/gh")
    scripted[("pr", "view")] = (1, "")
    scripted[("cherry",)] = (0, "- abc\n")
    s = state(work_branch="b", repo_github="john/x")
    assert GitMergeVerifier(repo_path=str(tmp_path)).verify_merged(s) is True
    assert any("cherry" in " ".join(c) for c in calls)


def test_verify_merged_cleans_up_its_verify_ref(recorded_git, tmp_path):
    calls, scripted = recorded_git
    (tmp_path / ".git").mkdir()
    scripted[("cherry",)] = (0, "- abc\n")
    s = state(work_branch="b", repo_github="", repo_default_branch="main")
    GitMergeVerifier(repo_path=str(tmp_path)).verify_merged(s)
    assert any("update-ref -d" in " ".join(c) for c in calls)


def test_branch_has_commits_counts_commits_the_default_branch_lacks(
    recorded_git, tmp_path
):
    calls, scripted = recorded_git
    (tmp_path / ".git").mkdir()
    scripted[("rev-list",)] = (0, "3\n")
    s = state(work_branch="b", repo_default_branch="main")
    assert GitMergeVerifier(repo_path=str(tmp_path)).branch_has_commits(s) is True
    assert any("origin/main..FETCH_HEAD" in " ".join(c) for c in calls)


def test_branch_has_commits_is_false_for_zero(recorded_git, tmp_path):
    _calls, scripted = recorded_git
    (tmp_path / ".git").mkdir()
    scripted[("rev-list",)] = (0, "0\n")
    s = state(work_branch="b")
    assert GitMergeVerifier(repo_path=str(tmp_path)).branch_has_commits(s) is False


def test_branch_has_commits_is_false_when_the_remote_branch_is_missing(
    recorded_git, tmp_path
):
    _calls, scripted = recorded_git
    (tmp_path / ".git").mkdir()
    scripted[("fetch", "b")] = (128, "")
    s = state(work_branch="b")
    assert GitMergeVerifier(repo_path=str(tmp_path)).branch_has_commits(s) is False


def test_the_verifiers_are_inert_without_a_local_checkout(tmp_path):
    verifier = GitMergeVerifier(repo_path=str(tmp_path / "nope"))
    s = state(work_branch="b", repo_github="")
    assert verifier.branch_has_commits(s) is False
    assert verifier.verify_merged(s) is False


def test_branch_has_commits_is_false_with_no_branch(tmp_path):
    assert GitMergeVerifier(repo_path=str(tmp_path)).branch_has_commits(state()) is False


# ═══════════════════════════════════════════════════════════════════════════
# locking
# ═══════════════════════════════════════════════════════════════════════════


def test_a_second_lock_holder_is_refused_immediately(tmp_path):
    first = FileLock(tmp_path / "l")
    first.acquire(timeout=0.0)
    try:
        with pytest.raises(LockBusy):
            FileLock(tmp_path / "l").acquire(timeout=0.0)
    finally:
        first.release()
    # Released: it can be taken again.
    second = FileLock(tmp_path / "l")
    second.acquire(timeout=0.0)
    second.release()


def test_lock_is_released_on_context_exit(tmp_path):
    with FileLock(tmp_path / "l"):
        pass
    FileLock(tmp_path / "l").acquire(timeout=0.0)


# ═══════════════════════════════════════════════════════════════════════════
# the Driver loop (I/O shell)
# ═══════════════════════════════════════════════════════════════════════════


class FakeFetcher:
    """Serves a scripted sequence of board payloads, repeating the last."""

    def __init__(
        self,
        payloads: list[dict] | None = None,
        error: Exception | None = None,
        error_on_call: int = 1,
    ):
        self.payloads = payloads or []
        self.error = error
        self.error_on_call = error_on_call
        self.calls = 0

    def fetch(self) -> dict:
        self.calls += 1
        if self.error is not None and self.calls == self.error_on_call:
            raise self.error
        if not self.payloads:
            return {"assignments": []}
        idx = min(self.calls - 1, len(self.payloads) - 1)
        return self.payloads[idx]


def board(**kw) -> dict:
    a = {
        "repo_name": REPO,
        "issue_number": ISSUE,
        "type": "work",
        "assignment_id": "w1",
        "dispatched_at": 1.0,
        "status": "done",
        "branch": "issue-1392-x",
        "machine_name": "precision",
    }
    a.update(kw)
    return {"assignments": [a]}


@pytest.fixture
def driver_factory(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr("coord.drive_state.scratch_dir", lambda: tmp_path)
    monkeypatch.setattr("coord.drive.scratch_dir", lambda: tmp_path)

    def make(payloads, *, opts=None, verifier=None, ticks=200):
        clock = {"t": 0.0}
        recorded: list[list[str]] = []

        def fake_run(argv, **kw):
            recorded.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, "ok\n", "")

        monkeypatch.setattr("coord.drive.subprocess.run", fake_run)

        driver = Driver(
            repo=REPO,
            issue=ISSUE,
            opts=opts or DriveOptions(machine="precision", poll=1.0),
            config=make_config(),
            fetcher=FakeFetcher(payloads),
            verifier=verifier or FakeVerifier(),
            sleeper=lambda secs: clock.__setitem__("t", clock["t"] + secs),
            clock=lambda: clock["t"],
        )
        driver.recorded = recorded  # type: ignore[attr-defined]
        return driver

    return make


def test_driver_exits_zero_on_a_verified_merge(driver_factory, capsys):
    driver = driver_factory([board(status="merged")])
    assert driver.run() == EXIT_OK
    assert "MERGED" in capsys.readouterr().out


def test_driver_shells_out_to_coord_and_never_calls_internals(driver_factory):
    """The CLI boundary, end to end: the merge really is a `coord` argv."""
    payload = board(
        status="done", test_state="passed", review_state="done", review_iteration=0
    )
    payload["assignments"].append(
        {
            "repo_name": REPO,
            "issue_number": ISSUE,
            "type": "review",
            "assignment_id": "r1",
            "dispatched_at": 2.0,
            "status": "done",
            "review_of_assignment_id": "w1",
            "review_verdict": "approve",
        }
    )
    driver = driver_factory(
        [payload],
        opts=DriveOptions(
            machine="precision", poll=1.0, max_merge_attempts=1, deadline_mins=1.0
        ),
    )
    assert driver.run() == EXIT_TERMINAL_FAILURE  # cap reached, never landed
    argvs = [" ".join(a) for a in driver.recorded]  # type: ignore[attr-defined]
    assert any("merge --only w1 --method rebase" in a for a in argvs), argvs


def test_driver_returns_the_deadline_code_when_time_runs_out(driver_factory, capsys):
    driver = driver_factory(
        [board(status="done", test_state="")],
        opts=DriveOptions(machine="precision", poll=30.0, deadline_mins=1.0),
    )
    assert driver.run() == EXIT_DEADLINE
    assert "deadline" in capsys.readouterr().err


def test_driver_tolerates_a_transport_blip_mid_loop_and_retries(driver_factory, capsys):
    """A daemon blip must be a retry next poll, never a traceback."""
    driver = driver_factory([board(status="merged")])
    driver.fetcher = FakeFetcher(
        [board(status="merged")],
        error=RuntimeError("connection reset"),
        error_on_call=2,  # call 1 is the preflight read
    )
    assert driver.run() == EXIT_OK
    assert "state read failed" in capsys.readouterr().err


def test_a_blip_on_the_preflight_read_is_a_usage_error(driver_factory):
    """Preflight has nothing to resume from — mirrors the bash `die ... 2`."""
    driver = driver_factory([board(status="merged")])
    driver.fetcher = FakeFetcher([board()], error=RuntimeError("no route to host"))
    with pytest.raises(DriveError) as exc:
        driver.run()
    assert exc.value.exit_code == EXIT_USAGE


def test_driver_refuses_a_second_run_on_the_same_issue(driver_factory, tmp_path):
    """A per-issue lock: two drivers on the same issue would double-dispatch."""
    held = FileLock(tmp_path / f"lock-{REPO}-{ISSUE}")
    held.acquire(timeout=0.0)
    (tmp_path / f"holder-{REPO}-{ISSUE}").write_text("someone else (pid 1)\n")
    try:
        driver = driver_factory([board(status="merged")])
        with pytest.raises(DriveError) as exc:
            driver.run()
        assert "already driving" in str(exc.value)
        assert "someone else" in str(exc.value)
        assert exc.value.exit_code == EXIT_USAGE
    finally:
        held.release()


def test_driver_allows_a_concurrent_run_on_a_different_issue(driver_factory, tmp_path):
    held = FileLock(tmp_path / f"lock-{REPO}-999")
    held.acquire(timeout=0.0)
    try:
        driver = driver_factory([board(status="merged")])
        assert driver.run() == EXIT_OK
    finally:
        held.release()


def test_driver_releases_the_lock_and_removes_the_holder_file(driver_factory, tmp_path):
    driver = driver_factory([board(status="merged")])
    assert driver.run() == EXIT_OK
    assert not (tmp_path / f"holder-{REPO}-{ISSUE}").exists()
    FileLock(tmp_path / f"lock-{REPO}-{ISSUE}").acquire(timeout=0.0)


def test_dry_run_prints_the_state_and_exits_without_dispatching(driver_factory, capsys):
    driver = driver_factory(
        [board(status="done", test_state="")],
        opts=DriveOptions(machine="precision", dry_run=True),
    )
    assert driver.run() == EXIT_OK
    out = capsys.readouterr().out
    assert "WORK_AID" in out
    body = out[out.index("{") :]
    parsed = json.loads(body[: body.rindex("}") + 1])
    assert parsed["WORK_AID"] == "w1"
    assert driver.recorded == []  # type: ignore[attr-defined]


def test_driver_reports_an_unconfigured_repo_as_a_usage_error(driver_factory):
    driver = driver_factory([board()])
    driver.repo = "not-a-repo"
    with pytest.raises(DriveError) as exc:
        driver.run()
    assert exc.value.exit_code == EXIT_USAGE


def test_driver_writes_the_per_issue_run_log(driver_factory, tmp_path):
    payload = board(status="done", test_state="")
    driver = driver_factory(
        [payload], opts=DriveOptions(machine="precision", skip_test=True, poll=1.0,
                                     deadline_mins=0.05)
    )
    driver.run()
    log = tmp_path / f"{REPO}-{ISSUE}.log"
    assert log.exists() and "ok" in log.read_text()


def test_a_die_on_error_action_raises_a_drive_error(driver_factory, monkeypatch):
    driver = driver_factory([board(status="failed", failure_reason="boom")])

    def failing_run(argv, **kw):
        return subprocess.CompletedProcess(argv, 1, "", "nope")

    monkeypatch.setattr("coord.drive.subprocess.run", failing_run)
    with pytest.raises(DriveError) as exc:
        driver.run()
    assert exc.value.exit_code == EXIT_TERMINAL_FAILURE


def test_a_warn_on_error_action_keeps_looping(driver_factory, monkeypatch, capsys):
    """A failing `coord merge` is "try again next poll", not an abort."""
    payload = board(status="done", test_state="passed")
    payload["assignments"].append(
        {
            "repo_name": REPO,
            "issue_number": ISSUE,
            "type": "review",
            "assignment_id": "r1",
            "dispatched_at": 2.0,
            "status": "done",
            "review_of_assignment_id": "w1",
            "review_verdict": "approve",
        }
    )
    driver = driver_factory(
        [payload],
        opts=DriveOptions(
            machine="precision", poll=1.0, max_merge_attempts=2, deadline_mins=1.0
        ),
    )
    monkeypatch.setattr(
        "coord.drive.subprocess.run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "queue empty"),
    )
    assert driver.run() == EXIT_TERMINAL_FAILURE
    assert "returned non-zero" in capsys.readouterr().err


def test_notify_is_off_by_default(driver_factory, monkeypatch):
    """Two drivers racing to dispatch is #476/#477 — --notify is opt-in."""
    driver = driver_factory([board(status="merged")])
    called: list[str] = []
    monkeypatch.setattr(driver, "run_coord", lambda *a, **k: called.append("x") or 0)
    driver.run_notify()
    assert called == []


def test_notify_nudges_coord_notify_under_the_shared_lock(driver_factory, tmp_path):
    driver = driver_factory(
        [board(status="merged")], opts=DriveOptions(machine="precision", notify=True)
    )
    seen: list[tuple] = []
    driver.run_coord = lambda args, **kw: seen.append(args) or 0  # type: ignore[assignment]
    driver.run_notify()
    assert seen == [("notify",)]


def test_the_config_path_is_threaded_onto_every_coord_subprocess(driver_factory):
    """A `coord drive --config X` run must not dispatch against a different
    config than it reads. The bash driver ran a bare `coord` and had this gap."""
    driver = driver_factory(
        [board(status="failed", failure_reason="boom")],
        opts=DriveOptions(
            machine="precision",
            poll=1.0,
            deadline_mins=0.05,
            config_path="/tmp/custom.yml",
        ),
    )
    driver.run()
    argvs = driver.recorded  # type: ignore[attr-defined]
    assert argvs, "expected at least one coord subprocess"
    for argv in argvs:
        assert argv[-2:] == ["--config", "/tmp/custom.yml"], argv


def test_no_config_flag_is_added_when_none_was_given(driver_factory):
    driver = driver_factory(
        [board(status="failed", failure_reason="boom")],
        opts=DriveOptions(machine="precision", poll=1.0, deadline_mins=0.05),
    )
    driver.run()
    for argv in driver.recorded:  # type: ignore[attr-defined]
        assert "--config" not in argv


def test_coord_argv_is_overridable_for_tests(monkeypatch):
    monkeypatch.setenv("COORD_DRIVE_COORD_BIN", "/x/coord --config /y")
    assert coord_argv() == ["/x/coord", "--config", "/y"]


def test_coord_argv_falls_back_to_the_module_when_not_on_path(monkeypatch):
    monkeypatch.delenv("COORD_DRIVE_COORD_BIN", raising=False)
    monkeypatch.setattr("coord.drive.shutil.which", lambda name: None)
    assert coord_argv()[-2:] == ["-m", "coord.cli"]
