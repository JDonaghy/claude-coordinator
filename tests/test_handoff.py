"""Tests for failure detection, stale assignment handling, retry, and auto-reassign."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from click.testing import CliRunner

from coord.cli import main
from coord.config import Config, ConcurrencyConfig, ProviderDef, ProvidersConfig
from coord.models import Assignment, Board, Machine, Repo
from coord.reconcile import _reassign, describe_no_candidate_machines, reconcile
from coord.state import save_board


# ── Stale detection ─────────────────────────────────────────────────────────


class TestStaleDetection:
    @patch("coord.reconcile._query_agent")
    def test_unreachable_increments_count(self, mock_query: MagicMock) -> None:
        config = Config(
            repos=[Repo(name="api", github="a/a")],
            machines=[Machine(name="laptop", host="l", repos=["api"], repo_paths={"api": "/tmp/a"})],
            concurrency=ConcurrencyConfig(stale_threshold=3),
        )
        board = Board(active=[
            Assignment(machine_name="laptop", repo_name="api", issue_number=1,
                       issue_title="x", assignment_id="a1", status="running"),
        ])
        mock_query.return_value = None  # unreachable

        reconcile(board, config)
        assert board.active[0].unreachable_count == 1
        assert board.active[0].status == "running"

    @patch("coord.reconcile._query_agent")
    def test_stale_after_threshold(self, mock_query: MagicMock) -> None:
        config = Config(
            repos=[Repo(name="api", github="a/a")],
            machines=[Machine(name="laptop", host="l", repos=["api"], repo_paths={"api": "/tmp/a"})],
            concurrency=ConcurrencyConfig(stale_threshold=2),
        )
        a = Assignment(machine_name="laptop", repo_name="api", issue_number=1,
                       issue_title="x", assignment_id="a1", status="running",
                       unreachable_count=1)
        board = Board(active=[a])
        mock_query.return_value = None

        changed = reconcile(board, config)
        assert "a1" in changed
        assert board.active == []
        assert len(board.completed) == 1
        assert board.completed[0].status == "failed"

    @patch("coord.reconcile._query_agent")
    def test_reachable_resets_count(self, mock_query: MagicMock) -> None:
        config = Config(
            repos=[Repo(name="api", github="a/a")],
            machines=[Machine(name="laptop", host="l", repos=["api"], repo_paths={"api": "/tmp/a"})],
        )
        a = Assignment(machine_name="laptop", repo_name="api", issue_number=1,
                       issue_title="x", assignment_id="a1", status="running",
                       unreachable_count=2)
        board = Board(active=[a])
        mock_query.return_value = {"active": [{"id": "a1"}], "completed": []}

        reconcile(board, config)
        assert board.active[0].unreachable_count == 0


# ── Reassign ────────────────────────────────────────────────────────────────


class TestReassign:
    def test_picks_different_machine(self) -> None:
        config = Config(
            repos=[Repo(name="api", github="a/a")],
            machines=[
                Machine(name="laptop", host="l", repos=["api"], repo_paths={"api": "/tmp/a"}),
                Machine(name="server", host="s", repos=["api"], repo_paths={"api": "/tmp/a"}),
            ],
        )
        failed = Assignment(
            machine_name="laptop", repo_name="api", issue_number=42,
            issue_title="Fix auth", assignment_id="a1", status="failed",
            briefing="do the thing",
        )
        board = Board()

        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"id": "retry1"}
        mock_resp.raise_for_status = lambda: None

        with patch("coord.reconcile.httpx.post", return_value=mock_resp):
            result = _reassign(failed, board, config)

        assert result is not None
        assert result.machine_name == "server"
        assert result.assignment_id == "retry1"
        assert "[retry]" in result.issue_title
        assert result in board.active

    def test_returns_none_when_no_machine(self) -> None:
        config = Config(
            repos=[Repo(name="api", github="a/a")],
            machines=[Machine(name="laptop", host="l", repos=["other"])],
        )
        failed = Assignment(
            machine_name="laptop", repo_name="api", issue_number=1,
            issue_title="x", assignment_id="a1", status="failed",
        )
        assert _reassign(failed, Board(), config) is None

    def test_retry_targets_feature_branch_for_opted_in_milestone(self) -> None:
        """#934 review should-fix: _reassign's milestone-aware retry base
        (coord/reconcile.py:392-409) shipped with no test. A repo that
        opted into the git model, retrying an issue that belongs to a
        milestone, must post `branch: feature/ms-NN` to the agent — not
        default_branch — so the retry's own base matches where the
        original work branched from."""
        config = Config(
            repos=[Repo(name="api", github="a/a", default_branch="main",
                        develop_branch="develop")],
            machines=[
                Machine(name="laptop", host="l", repos=["api"], repo_paths={"api": "/tmp/a"}),
                Machine(name="server", host="s", repos=["api"], repo_paths={"api": "/tmp/a"}),
            ],
        )
        failed = Assignment(
            machine_name="laptop", repo_name="api", issue_number=42,
            issue_title="Fix auth", assignment_id="a1", status="failed",
            briefing="do the thing",
        )
        board = Board()

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"id": "retry1"}
        mock_resp.raise_for_status = lambda: None

        with patch("coord.reconcile.httpx.post", return_value=mock_resp) as mock_post, \
             patch("coord.github_ops.get_issue",
                   return_value={"milestone": {"number": 9, "title": "M9"}}):
            result = _reassign(failed, board, config)

        assert result is not None
        posted_payload = mock_post.call_args.kwargs["json"]
        assert posted_payload["branch"] == "feature/ms-9"

    def test_retry_targets_default_branch_when_not_opted_in(self) -> None:
        """No develop_branch configured → default_branch, unchanged behavior."""
        config = Config(
            repos=[Repo(name="api", github="a/a", default_branch="main")],
            machines=[
                Machine(name="laptop", host="l", repos=["api"], repo_paths={"api": "/tmp/a"}),
                Machine(name="server", host="s", repos=["api"], repo_paths={"api": "/tmp/a"}),
            ],
        )
        failed = Assignment(
            machine_name="laptop", repo_name="api", issue_number=42,
            issue_title="Fix auth", assignment_id="a1", status="failed",
            briefing="do the thing",
        )
        board = Board()

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"id": "retry1"}
        mock_resp.raise_for_status = lambda: None

        with patch("coord.reconcile.httpx.post", return_value=mock_resp) as mock_post, \
             patch("coord.github_ops.get_issue") as get_issue:
            result = _reassign(failed, board, config)

        get_issue.assert_not_called()
        assert result is not None
        posted_payload = mock_post.call_args.kwargs["json"]
        assert posted_payload["branch"] == "main"


# ── #1417: capacity-based retry (replaces "any running = busy") ────────────


class TestReassignCapacity:
    def test_two_machines_one_running_each_under_fleet_cap_succeeds(self) -> None:
        """The exact #1417 reproduction: two machines, one running
        assignment apiece, fleet cap well above 2 (the operator's real
        config: concurrency.max_workers: 8). Retry must succeed — a single
        running assignment is not "full"."""
        config = Config(
            repos=[Repo(name="claude-coordinator", github="a/a")],
            machines=[
                Machine(name="precision", host="p", repos=["claude-coordinator"],
                        repo_paths={"claude-coordinator": "/tmp/a"}),
                Machine(name="elitebook", host="e", repos=["claude-coordinator"],
                        repo_paths={"claude-coordinator": "/tmp/a"}),
            ],
            concurrency=ConcurrencyConfig(max_workers=8),
        )
        failed = Assignment(
            machine_name="elitebook", repo_name="claude-coordinator", issue_number=1402,
            issue_title="x", assignment_id="a-failed", status="failed",
            briefing="do the thing",
        )
        board = Board(active=[
            Assignment(machine_name="precision", repo_name="claude-coordinator",
                       issue_number=1348, issue_title="other work",
                       assignment_id="running-1", status="running", type="work"),
            Assignment(machine_name="elitebook", repo_name="claude-coordinator",
                       issue_number=1395, issue_title="other work 2",
                       assignment_id="running-2", status="running", type="work"),
        ])

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"id": "retry1"}
        mock_resp.raise_for_status = lambda: None

        with patch("coord.machine_pause.paused_set", return_value=set()), \
             patch("coord.reconcile.httpx.post", return_value=mock_resp):
            result = _reassign(failed, board, config)

        assert result is not None
        # Prefers the machine that isn't the one that just failed.
        assert result.machine_name == "precision"

    def test_machine_at_its_own_cap_is_excluded(self) -> None:
        """A machine with an explicit `max_workers` override that's already
        saturated must stay excluded even though the fleet-wide cap has
        plenty of headroom (#1417 per-machine override)."""
        config = Config(
            repos=[Repo(name="api", github="a/a")],
            machines=[
                Machine(name="tiny", host="t", repos=["api"],
                        repo_paths={"api": "/tmp/a"}, max_workers=1),
            ],
            concurrency=ConcurrencyConfig(max_workers=8),
        )
        failed = Assignment(
            machine_name="other", repo_name="api", issue_number=1,
            issue_title="x", assignment_id="a1", status="failed",
        )
        board = Board(active=[
            Assignment(machine_name="tiny", repo_name="api", issue_number=2,
                       issue_title="busy work", assignment_id="running-1",
                       status="running", type="work"),
        ])

        with patch("coord.machine_pause.paused_set", return_value=set()):
            result = _reassign(failed, board, config)
            msg = describe_no_candidate_machines(failed, board, config)

        assert result is None
        assert "tiny" in msg
        assert "at capacity" in msg
        assert "1/1" in msg

    def test_fleet_wide_cap_blocks_retry_even_with_idle_machine_headroom(self) -> None:
        """concurrency.max_workers is a fleet-wide budget: once the total
        running count across all machines hits it, retry must refuse even
        though no single machine looks "full" on its own."""
        config = Config(
            repos=[Repo(name="api", github="a/a")],
            machines=[
                Machine(name="m1", host="h1", repos=["api"], repo_paths={"api": "/tmp/a"}),
                Machine(name="m2", host="h2", repos=["api"], repo_paths={"api": "/tmp/a"}),
            ],
            concurrency=ConcurrencyConfig(max_workers=2),
        )
        failed = Assignment(
            machine_name="m1", repo_name="api", issue_number=1,
            issue_title="x", assignment_id="a1", status="failed",
        )
        board = Board(active=[
            Assignment(machine_name="m1", repo_name="api", issue_number=2,
                       issue_title="w1", assignment_id="running-1",
                       status="running", type="work"),
            Assignment(machine_name="m2", repo_name="api", issue_number=3,
                       issue_title="w2", assignment_id="running-2",
                       status="running", type="work"),
        ])

        with patch("coord.machine_pause.paused_set", return_value=set()):
            result = _reassign(failed, board, config)
            msg = describe_no_candidate_machines(failed, board, config)

        assert result is None
        assert "fleet at capacity" in msg
        assert "2/2" in msg


# ── Auto-reassign from reconcile ────────────────────────────────────────────


class TestAutoReassign:
    @patch("coord.reconcile._query_agent")
    @patch("coord.reconcile.httpx.post")
    def test_auto_reassign_on_failure(self, mock_post: MagicMock, mock_query: MagicMock) -> None:
        config = Config(
            repos=[Repo(name="api", github="a/a")],
            machines=[
                Machine(name="laptop", host="l", repos=["api"], repo_paths={"api": "/tmp/a"}),
                Machine(name="server", host="s", repos=["api"], repo_paths={"api": "/tmp/a"}),
            ],
            concurrency=ConcurrencyConfig(auto_reassign=True),
        )
        board = Board(active=[
            Assignment(machine_name="laptop", repo_name="api", issue_number=42,
                       issue_title="Fix", assignment_id="a1", status="running",
                       type="work", briefing="do it"),
        ])
        mock_query.return_value = {
            "active": [],
            "completed": [{"id": "a1", "status": "failed", "finished_at": 100.0}],
        }
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"id": "retry1"}
        mock_resp.raise_for_status = lambda: None
        mock_post.return_value = mock_resp

        changed = reconcile(board, config)
        assert "a1" in changed
        assert "retry1" in changed
        retry_assignments = [a for a in board.active if "[retry]" in a.issue_title]
        assert len(retry_assignments) == 1

    @patch("coord.reconcile._query_agent")
    def test_no_reassign_when_disabled(self, mock_query: MagicMock) -> None:
        config = Config(
            repos=[Repo(name="api", github="a/a")],
            machines=[
                Machine(name="laptop", host="l", repos=["api"], repo_paths={"api": "/tmp/a"}),
                Machine(name="server", host="s", repos=["api"], repo_paths={"api": "/tmp/a"}),
            ],
            concurrency=ConcurrencyConfig(auto_reassign=False),
        )
        board = Board(active=[
            Assignment(machine_name="laptop", repo_name="api", issue_number=1,
                       issue_title="x", assignment_id="a1", status="running",
                       type="work"),
        ])
        mock_query.return_value = {
            "active": [],
            "completed": [{"id": "a1", "status": "failed", "finished_at": 100.0}],
        }
        changed = reconcile(board, config)
        assert "a1" in changed
        assert len(board.active) == 0

    @patch("coord.reconcile._query_agent")
    @patch("coord.reconcile.httpx.post")
    def test_no_reassign_on_usage_limit_kill(
        self, mock_post: MagicMock, mock_query: MagicMock,
    ) -> None:
        """#1461 review finding 1: a usage-limit kill is an account-wide
        exhausted budget, not a per-machine defect — auto_reassign must NOT
        re-dispatch it onto a different machine (that just burns the same
        budget and is guaranteed to die the same way until the reset)."""
        config = Config(
            repos=[Repo(name="api", github="a/a")],
            machines=[
                Machine(name="laptop", host="l", repos=["api"], repo_paths={"api": "/tmp/a"}),
                Machine(name="server", host="s", repos=["api"], repo_paths={"api": "/tmp/a"}),
            ],
            concurrency=ConcurrencyConfig(auto_reassign=True),
        )
        board = Board(active=[
            Assignment(machine_name="laptop", repo_name="api", issue_number=42,
                       issue_title="Fix", assignment_id="a1", status="running",
                       type="work", briefing="do it"),
        ])
        mock_query.return_value = {
            "active": [],
            "completed": [{
                "id": "a1",
                "status": "failed",
                "finished_at": 100.0,
                "usage_limit_reason": "usage limit — resets 8:30pm (America/Chicago)",
            }],
        }

        changed = reconcile(board, config)
        assert "a1" in changed
        assert len(board.active) == 0  # no retry dispatched
        retry_assignments = [a for a in board.active if "[retry]" in a.issue_title]
        assert len(retry_assignments) == 0
        mock_post.assert_not_called()


# ── #1396: diagnosable "no available machine" message ──────────────────────


class TestDescribeNoCandidateMachines:
    """describe_no_candidate_machines names the blocking machine + its load."""

    def test_names_busy_machine_and_age(self) -> None:
        config = Config(
            repos=[Repo(name="claude-coordinator", github="a/a")],
            machines=[
                Machine(name="elitebook", host="e", repos=["claude-coordinator"],
                        repo_paths={"claude-coordinator": "/tmp/a"}),
            ],
            # #1417: pin the fleet cap to 1 so this sole machine's single
            # running row genuinely saturates it — with the default cap of
            # 2, one running assignment now leaves room and this scenario
            # would (correctly) find elitebook as a fallback candidate
            # instead of reproducing the "genuinely full" case under test.
            concurrency=ConcurrencyConfig(max_workers=1),
        )
        failed = Assignment(
            machine_name="elitebook", repo_name="claude-coordinator",
            issue_number=99, issue_title="x", assignment_id="a-failed",
            status="failed",
        )
        # A stale phantom `running` row — exactly the #1396 reproduction:
        # a dead interactive session nothing reaped, dispatched_at far in
        # the past so its age is large.
        phantom = Assignment(
            machine_name="elitebook", repo_name="claude-coordinator",
            issue_number=767, issue_title="chat", assignment_id="phantom-1",
            status="running", type="chat", dispatched_at=1.0,
        )
        board = Board(active=[phantom])

        # Explicitly mock the pause set — this repo is dogfooded on the
        # operator's own real fleet (machines named elitebook/precision/
        # laptop per coordinator.yml), so an unmocked `paused_set()` reads
        # the real `~/.coord/paused_machines.json` on whatever box runs the
        # suite (#1396 review finding 3).
        with patch("coord.machine_pause.paused_set", return_value=set()):
            msg = describe_no_candidate_machines(failed, board, config)

        assert "elitebook" in msg
        assert "busy" in msg
        assert "chat" in msg  # names the type occupying the machine
        assert "#767" in msg
        assert "age=" in msg  # the age is what makes a stale phantom obvious

    def test_names_paused_machine(self) -> None:
        config = Config(
            repos=[Repo(name="api", github="a/a")],
            machines=[
                Machine(name="laptop", host="l", repos=["api"], repo_paths={"api": "/tmp/a"}),
            ],
        )
        failed = Assignment(
            machine_name="other", repo_name="api", issue_number=1,
            issue_title="x", assignment_id="a1", status="failed",
        )
        with patch("coord.machine_pause.paused_set", return_value={"laptop"}):
            msg = describe_no_candidate_machines(failed, Board(), config)

        assert "laptop" in msg
        assert "paused" in msg

    def test_no_relevant_machine_says_so(self) -> None:
        config = Config(
            repos=[Repo(name="api", github="a/a")],
            machines=[Machine(name="laptop", host="l", repos=["other"])],
        )
        failed = Assignment(
            machine_name="laptop", repo_name="api", issue_number=1,
            issue_title="x", assignment_id="a1", status="failed",
        )
        msg = describe_no_candidate_machines(failed, Board(), config)
        assert "api" in msg
        assert "no machine" in msg

    def test_fallback_same_machine_counts_as_free_candidate(self) -> None:
        """#1396 review finding 1: the machine that just failed is a REAL
        fallback candidate when it's idle (not busy/paused) — `_reassign`'s
        fallback pass drops only the "different machine" constraint. The
        message must not say "no available machine" in this case; it must
        say a candidate existed and explain the actual failure.

        Here the sole machine's resolved provider is `claude-pty`
        (human_attended_only=True), so the diagnostic re-check hits the
        TOS gate and the message must name it specifically.
        """
        config = Config(
            repos=[Repo(name="api", github="a/a")],
            machines=[
                Machine(name="solo", host="s", repos=["api"], repo_paths={"api": "/tmp/a"}),
            ],
            providers=ProvidersConfig(
                default="my-pty",
                definitions={"my-pty": ProviderDef(type="claude-pty")},
            ),
        )
        failed = Assignment(
            machine_name="solo", repo_name="api", issue_number=1,
            issue_title="x", assignment_id="a1", status="failed",
        )
        with patch("coord.machine_pause.paused_set", return_value=set()):
            msg = describe_no_candidate_machines(failed, Board(), config)

        assert "no available machine" not in msg
        assert "TOS gate" in msg
        assert "human_attended_only" in msg

    def test_fallback_same_machine_dispatch_failure_message(self) -> None:
        """Same fallback-candidate scenario, but with a provider that
        passes the TOS gate — the message must describe a dispatch
        failure, not repeat "no available machine" (#1396 review
        finding 2: the old "check daemon logs" advice was a dead end
        because neither failure path logged anything)."""
        config = Config(
            repos=[Repo(name="api", github="a/a")],
            machines=[
                Machine(name="solo", host="s", repos=["api"], repo_paths={"api": "/tmp/a"}),
            ],
        )
        failed = Assignment(
            machine_name="solo", repo_name="api", issue_number=1,
            issue_title="x", assignment_id="a1", status="failed",
        )
        with patch("coord.machine_pause.paused_set", return_value=set()):
            msg = describe_no_candidate_machines(failed, Board(), config)

        assert "no available machine" not in msg
        assert "candidate machine was available" in msg
        assert "dispatch request failed" in msg


# ── CLI retry command ───────────────────────────────────────────────────────


class TestCoordRetry:
    @patch("coord.reconcile.httpx.post")
    def test_retry_dispatches_to_different_machine(
        self, mock_post: MagicMock, tmp_path: Path, coord_db,
    ) -> None:
        config_file = tmp_path / "coordinator.yml"
        config_file.write_text(
            "repos:\n  - name: api\n    github: a/a\n"
            "machines:\n"
            "  - name: laptop\n    host: l\n    repos: [api]\n    repo_paths:\n      api: /tmp/a\n"
            "  - name: server\n    host: s\n    repos: [api]\n    repo_paths:\n      api: /tmp/a\n"
        )
        board = Board(completed=[
            Assignment(machine_name="laptop", repo_name="api", issue_number=42,
                       issue_title="Fix auth", assignment_id="a1", status="failed",
                       briefing="do it", finished_at=1.0),
        ])
        save_board(board)

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"id": "retry1"}
        mock_resp.raise_for_status = lambda: None
        mock_post.return_value = mock_resp

        runner = CliRunner()
        result = runner.invoke(main, ["retry", "a1", "--config", str(config_file)])

        assert result.exit_code == 0
        assert "Retried" in result.output
        assert "server" in result.output

    def test_retry_rejects_non_failed(self, tmp_path: Path, coord_db) -> None:
        config_file = tmp_path / "coordinator.yml"
        config_file.write_text(
            "repos:\n  - name: api\n    github: a/a\n"
            "machines:\n  - name: m\n    host: h\n    repos: [api]\n"
        )
        board = Board(active=[
            Assignment(machine_name="m", repo_name="api", issue_number=1,
                       issue_title="x", assignment_id="a1", status="running"),
        ])
        save_board(board)

        runner = CliRunner()
        result = runner.invoke(main, ["retry", "a1", "--config", str(config_file)])
        assert result.exit_code != 0
        # #1606: the message now names both accepted statuses ('failed' and
        # 'advisory' — a genuine zero-commit advisory is retryable too).
        assert "not 'failed' or 'advisory'" in result.output

    def test_retry_no_available_machine_names_busy_machine(
        self, tmp_path: Path, coord_db,
    ) -> None:
        """#1396: when retry fails, the message must name the blocking
        machine and its apparent load rather than a bare "no available
        machine to retry on" — the #1396 reproduction had every machine
        idle per `coord status` but a phantom `running` row made retry
        refuse with no hint why.
        """
        config_file = tmp_path / "coordinator.yml"
        config_file.write_text(
            "repos:\n  - name: api\n    github: a/a\n"
            "machines:\n"
            "  - name: elitebook\n    host: e\n    repos: [api]\n    repo_paths:\n      api: /tmp/a\n"
            # #1417: pin the fleet cap to 1 — the sole machine's single
            # running (phantom) row must genuinely saturate it for this to
            # still reproduce "no available machine". At the default cap of
            # 2, one running row leaves room and retry would (correctly)
            # succeed via the same-machine fallback instead.
            "concurrency:\n  max_workers: 1\n"
        )
        board = Board(
            active=[
                Assignment(machine_name="elitebook", repo_name="api", issue_number=767,
                           issue_title="chat", assignment_id="phantom-1",
                           status="running", type="chat", dispatched_at=1.0),
            ],
            completed=[
                Assignment(machine_name="elitebook", repo_name="api", issue_number=42,
                           issue_title="Fix auth", assignment_id="a1", status="failed",
                           briefing="do it", finished_at=1.0),
            ],
        )
        save_board(board)

        runner = CliRunner()
        # Mocked for the same reason as test_names_busy_machine_and_age
        # above — an unmocked real pause file would make "elitebook" read
        # as paused instead of busy on a box where it's genuinely paused
        # (#1396 review finding 3).
        with patch("coord.machine_pause.paused_set", return_value=set()):
            result = runner.invoke(main, ["retry", "a1", "--config", str(config_file)])

        assert result.exit_code != 0
        assert "elitebook" in result.output
        assert "busy" in result.output

    def test_retry_unknown_assignment(self, tmp_path: Path, coord_db) -> None:
        config_file = tmp_path / "coordinator.yml"
        config_file.write_text(
            "repos:\n  - name: api\n    github: a/a\n"
            "machines:\n  - name: m\n    host: h\n    repos: [api]\n"
        )
        save_board(Board())

        runner = CliRunner()
        result = runner.invoke(main, ["retry", "nope", "--config", str(config_file)])
        assert result.exit_code != 0
        assert "not found" in result.output


class TestHelpText:
    def test_retry_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["retry", "--help"])
        assert result.exit_code == 0
        assert "Re-dispatch" in result.output
