"""Tests for coord.milestone_dispatch — #769 Phase 1 (milestone dispatch:
machine picking + actual dispatch on top of Phase 0's pure frontier).

Pure-function tests (``pick_machine`` / ``plan_dispatch`` / ``is_milestone_
complete``) seed a :class:`~coord.models.Board` + :class:`~coord.config.
Config` directly — no GitHub, no HTTP. ``dispatch_entry`` / ``fetch_
milestone_context`` tests mock ``coord.github_ops`` and ``coord.dispatch``
so no live network call ever happens. CLI-level black-box coverage
(including the #769 acceptance-criteria scenario) lives in
tests/test_cli_milestone_dispatch.py.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from coord.config import Config
from coord.milestone_dispatch import (
    MilestoneContext,
    MilestoneDispatchError,
    dispatch_entry,
    fetch_milestone_context,
    gate_a_status,
    is_milestone_complete,
    pick_machine,
    plan_dispatch,
)
from coord.milestone_order import WorkOrder, WorkOrderNode
from coord.models import Assignment, Board, Machine, Repo


def _config(machines: list[Machine], repos: list[Repo] | None = None) -> Config:
    repos = repos or [Repo(name="api", github="acme/api")]
    return Config(repos=repos, machines=machines)


def _machine(name: str, repos: list[str], repo_paths: dict[str, str] | None = None) -> Machine:
    if repo_paths is None:
        repo_paths = {r: f"/tmp/{r}" for r in repos}
    return Machine(name=name, host=f"{name}.tailnet", repos=repos, repo_paths=repo_paths)


def _running(machine_name: str, issue: int, repo: str = "api") -> Assignment:
    return Assignment(
        machine_name=machine_name,
        repo_name=repo,
        issue_number=issue,
        issue_title="t",
        status="running",
        assignment_id=f"a{issue}",
        type="work",
    )


WORK_ORDER = WorkOrder(
    nodes=(
        WorkOrderNode(762, group="A"),
        WorkOrderNode(763, group="A"),
        WorkOrderNode(765, after=(762, 763)),
    )
)


# ── pick_machine ─────────────────────────────────────────────────────────────


class TestPickMachine:
    def test_picks_idle_capable_machine(self) -> None:
        cfg = _config([_machine("laptop", ["api"])])
        board = Board()
        m = pick_machine("api", board, cfg)
        assert m is not None
        assert m.name == "laptop"

    def test_excludes_machine_without_repo_in_repos_list(self) -> None:
        """The #688 mechanism: a machine whose `repos:` omits the target repo
        (e.g. dellserver's coordinator.yml entry omitting claude-coordinator)
        is never picked — no special-case "coord-self" code needed."""
        cfg = _config([_machine("dellserver", ["quadraui"])])
        board = Board()
        assert pick_machine("claude-coordinator", board, cfg) is None

    def test_excludes_busy_machine(self) -> None:
        cfg = _config([_machine("laptop", ["api"])])
        board = Board(active=[_running("laptop", 1)])
        assert pick_machine("api", board, cfg) is None

    def test_excludes_paused_machine(self) -> None:
        cfg = _config([_machine("laptop", ["api"])])
        board = Board()
        with patch("coord.machine_pause.paused_set", return_value={"laptop"}):
            assert pick_machine("api", board, cfg) is None

    def test_excludes_machine_without_repo_path(self) -> None:
        cfg = _config([_machine("laptop", ["api"], repo_paths={})])
        board = Board()
        assert pick_machine("api", board, cfg) is None

    def test_respects_exclude_set(self) -> None:
        cfg = _config([_machine("laptop", ["api"])])
        board = Board()
        assert pick_machine("api", board, cfg, exclude=frozenset({"laptop"})) is None

    def test_first_match_wins_in_config_order(self) -> None:
        cfg = _config([_machine("laptop", ["api"]), _machine("server", ["api"])])
        board = Board()
        m = pick_machine("api", board, cfg)
        assert m.name == "laptop"

    def test_returns_none_when_no_idle_machine(self) -> None:
        cfg = _config([])
        board = Board()
        assert pick_machine("api", board, cfg) is None


# ── plan_dispatch ────────────────────────────────────────────────────────────


class TestPlanDispatch:
    def test_cohort_fans_out_to_distinct_machines(self) -> None:
        cfg = _config([_machine("laptop", ["api"]), _machine("server", ["api"])])
        board = Board()
        repo = cfg.repo("api")
        plan = plan_dispatch(WORK_ORDER, board, cfg, repo, terminal_issues=set())

        ready_issues = {p.entry.issue_number for p in plan.to_dispatch}
        assert ready_issues == {762, 763}
        picked_machines = {p.machine.name for p in plan.to_dispatch}
        assert picked_machines == {"laptop", "server"}  # distinct, no double-booking

        waiting_issues = {b.issue_number for b in plan.waiting}
        assert waiting_issues == {765}

    def test_ready_entries_beyond_idle_machines_are_skipped(self) -> None:
        cfg = _config([_machine("laptop", ["api"])])  # only one idle machine
        board = Board()
        repo = cfg.repo("api")
        plan = plan_dispatch(WORK_ORDER, board, cfg, repo, terminal_issues=set())

        assert len(plan.to_dispatch) == 1
        assert len(plan.skipped) == 1
        # The one dispatched + the one skipped account for the full cohort.
        covered = {plan.to_dispatch[0].entry.issue_number, plan.skipped[0].entry.issue_number}
        assert covered == {762, 763}
        assert "no idle machine" in plan.skipped[0].reason

    def test_cohort_merging_unblocks_gated_node(self) -> None:
        """#769 acceptance criteria (second half): once the group:A cohort is
        terminal, the after-gated node enters the ready frontier."""
        cfg = _config([_machine("laptop", ["api"])])
        board = Board()
        repo = cfg.repo("api")
        plan = plan_dispatch(WORK_ORDER, board, cfg, repo, terminal_issues={762, 763})

        assert [p.entry.issue_number for p in plan.to_dispatch] == [765]
        assert plan.waiting == ()

    def test_all_terminal_yields_empty_plan(self) -> None:
        cfg = _config([_machine("laptop", ["api"])])
        board = Board()
        repo = cfg.repo("api")
        plan = plan_dispatch(WORK_ORDER, board, cfg, repo, terminal_issues={762, 763, 765})
        assert plan.to_dispatch == ()
        assert plan.skipped == ()
        assert plan.waiting == ()


# ── is_milestone_complete ────────────────────────────────────────────────────


class TestIsMilestoneComplete:
    def test_false_when_any_node_open(self) -> None:
        ctx = MilestoneContext(
            tracking_issue=100, milestone_number=9, work_order=WORK_ORDER,
            terminal_issues=frozenset({762, 763}),
        )
        assert is_milestone_complete(ctx) is False

    def test_true_when_all_terminal(self) -> None:
        ctx = MilestoneContext(
            tracking_issue=100, milestone_number=9, work_order=WORK_ORDER,
            terminal_issues=frozenset({762, 763, 765}),
        )
        assert is_milestone_complete(ctx) is True


# ── gate_a_status (#930, docs/ORACLE_LOOP.md Gate A) ─────────────────────────


class TestGateAStatus:
    def _cfg(self, *, with_driver: bool) -> Config:
        from coord.config import AcceptanceConfig, AcceptanceDriverConfig

        drivers = {}
        if with_driver:
            drivers["api"] = AcceptanceDriverConfig(kind="tui-tuidriver", run="cargo test")
        return Config(
            repos=[Repo(name="api", github="acme/api", default_branch="main")],
            machines=[_machine("laptop", ["api"])],
            acceptance=AcceptanceConfig(drivers=drivers),
        )

    def test_none_when_no_acceptance_driver_configured(self) -> None:
        cfg = self._cfg(with_driver=False)
        repo = cfg.repo("api")
        assert gate_a_status(repo, cfg, 9, file_exists=lambda *a: False) is None

    def _routed_cfg(self) -> Config:
        from coord.config import AcceptanceConfig, AcceptanceDriverConfig

        drivers = {
            "api": AcceptanceDriverConfig(routes=[
                AcceptanceDriverConfig(match="**", kind="cli-pytest", run="pytest"),
            ]),
        }
        return Config(
            repos=[Repo(name="api", github="acme/api", default_branch="main")],
            machines=[_machine("laptop", ["api"])],
            acceptance=AcceptanceConfig(drivers=drivers),
        )

    def test_blocked_when_driver_is_routed_and_contract_missing(self) -> None:
        """#1125 review finding 1: a routed driver (acceptance.drivers.<repo>
        .routes) must still gate Gate A — `driver_for(repo_cfg.name)` (no
        path) can't select a route and would otherwise silently return
        None, making gate_a_status wrongly report "dispatch may proceed"
        for every milestone the instant this repo's driver becomes routed.
        """
        cfg = self._routed_cfg()
        repo = cfg.repo("api")
        reason = gate_a_status(repo, cfg, 9, file_exists=lambda *a: False)
        assert reason is not None
        assert "tests/acceptance/ms-9/contract.md" in reason

    def test_blocked_when_contract_missing(self) -> None:
        cfg = self._cfg(with_driver=True)
        repo = cfg.repo("api")
        reason = gate_a_status(repo, cfg, 9, file_exists=lambda *a: False)
        assert reason is not None
        assert "tests/acceptance/ms-9/contract.md" in reason
        assert "coord acceptance mock api" in reason

    def test_none_when_contract_exists(self) -> None:
        cfg = self._cfg(with_driver=True)
        repo = cfg.repo("api")
        assert gate_a_status(repo, cfg, 9, file_exists=lambda *a: True) is None

    def test_file_exists_called_with_expected_args(self) -> None:
        cfg = self._cfg(with_driver=True)
        repo = cfg.repo("api")
        calls: list[tuple] = []

        def _check(repo_github: str, path: str, branch: str) -> bool:
            calls.append((repo_github, path, branch))
            return True

        gate_a_status(repo, cfg, 9, file_exists=_check)
        assert calls == [("acme/api", "tests/acceptance/ms-9/contract.md", "main")]

    def test_default_file_exists_treats_runtime_error_as_missing(self) -> None:
        cfg = self._cfg(with_driver=True)
        repo = cfg.repo("api")
        with patch("coord.github_ops.get_repo_file", side_effect=RuntimeError("404")):
            reason = gate_a_status(repo, cfg, 9)
        assert reason is not None

    def test_default_file_exists_true_when_no_error(self) -> None:
        cfg = self._cfg(with_driver=True)
        repo = cfg.repo("api")
        with patch("coord.github_ops.get_repo_file", return_value="contract body"):
            reason = gate_a_status(repo, cfg, 9)
        assert reason is None


# ── fetch_milestone_context ──────────────────────────────────────────────────


TRACKING_BODY = """\
## Work order
- [ ] #762  {group: A}
- [ ] #763  {group: A}
- [ ] #765  {after: #762,#763}
"""


class TestFetchMilestoneContext:
    def test_raises_on_fetch_failure(self) -> None:
        repo = Repo(name="api", github="acme/api")
        with patch("coord.github_ops.get_issue", side_effect=RuntimeError("boom")):
            with pytest.raises(MilestoneDispatchError, match="could not fetch #100"):
                fetch_milestone_context(repo, 100)

    def test_raises_when_no_milestone(self) -> None:
        repo = Repo(name="api", github="acme/api")
        with patch(
            "coord.github_ops.get_issue",
            return_value={"number": 100, "body": TRACKING_BODY, "milestone": None},
        ):
            with pytest.raises(MilestoneDispatchError, match="no milestone"):
                fetch_milestone_context(repo, 100)

    def test_empty_work_order_short_circuits(self) -> None:
        repo = Repo(name="api", github="acme/api")
        with patch(
            "coord.github_ops.get_issue",
            return_value={"number": 100, "body": "no block here", "milestone": {"number": 9}},
        ):
            ctx = fetch_milestone_context(repo, 100)
        assert ctx.work_order.nodes == ()
        assert ctx.terminal_issues == frozenset()

    def test_resolves_terminal_issues_and_membership(self) -> None:
        repo = Repo(name="api", github="acme/api")

        def get_issue(_repo, number):
            if number == 100:
                return {
                    "number": 100, "body": TRACKING_BODY,
                    "milestone": {"number": 9},
                }
            state = "CLOSED" if number in (762, 763) else "OPEN"
            return {"number": number, "state": state, "milestone": {"number": 9}}

        open_issues = [{"number": 765, "milestone": {"number": 9}}]
        with patch("coord.github_ops.get_issue", side_effect=get_issue), \
             patch("coord.github_ops.get_open_issues", return_value=open_issues):
            ctx = fetch_milestone_context(repo, 100)

        assert ctx.milestone_number == 9
        assert ctx.terminal_issues == frozenset({762, 763})


# ── dispatch_entry ────────────────────────────────────────────────────────────


class TestDispatchEntry:
    def _pick(self, cfg: Config, board: Board, issue: int = 762):
        repo = cfg.repo("api")
        plan = plan_dispatch(WORK_ORDER, board, cfg, repo, terminal_issues=set())
        return next(p for p in plan.to_dispatch if p.entry.issue_number == issue)

    def test_successful_dispatch_records_and_marks_board_busy(self, coord_db) -> None:
        cfg = _config([_machine("laptop", ["api"])])
        board = Board()
        pick = self._pick(cfg, board)
        repo = cfg.repo("api")

        with patch("coord.github_ops.get_issue", return_value={"title": "Fix X", "body": "b", "labels": []}), \
             patch("coord.dispatch.dispatch", return_value={"id": "asn-1"}), \
             patch("coord.github_ops.post_issue_comment"), \
             patch("coord.github_ops.check_branch_exists", return_value=False):
            outcome = dispatch_entry(pick, repo, cfg, board, tracking_issue=100)

        assert outcome.ok is True
        assert outcome.assignment_id == "asn-1"
        assert outcome.machine_name == "laptop"

        # Board mutated in place so a subsequent plan_dispatch call in the same
        # batch/tick sees "laptop" as busy.
        assert any(a.assignment_id == "asn-1" and a.status == "running" for a in board.active)
        assert pick_machine("api", board, cfg) is None

    def test_claimed_issue_is_not_dispatched(self, coord_db) -> None:
        cfg = _config([_machine("laptop", ["api"])])
        board = Board(active=[_running("server", 762)])  # already claimed elsewhere
        pick = self._pick(cfg, Board())  # plan against an unclaimed board...
        repo = cfg.repo("api")

        with patch("coord.dispatch.dispatch") as disp:
            # ...but dispatch_entry re-checks the LIVE board defensively.
            outcome = dispatch_entry(pick, repo, cfg, board, tracking_issue=100)

        assert outcome.ok is False
        assert "already claimed" in outcome.error
        disp.assert_not_called()
