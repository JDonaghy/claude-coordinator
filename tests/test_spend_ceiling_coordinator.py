"""Per-leg spend ceiling (#2131) — the coordinator half.

``tests/test_spend_ceiling.py`` covers the agent: config parsing, the live
cost meter, the reap watchdog, and the black-box kill through a real
``AgentServer``. This file covers what the coordinator does with the result,
which is the half that actually protects the money:

* the ceiling reaches the agent on the ``POST /assign`` wire, and is omitted
  (byte-identical to pre-#2131) when nobody configured one;
* ``coord retry`` refuses a ceiling-killed leg without an explicit
  acknowledgement — "does not silently re-spend" is an acceptance criterion,
  not a nicety;
* ``auto_reassign`` declines to re-dispatch one at all, since there is no
  human in that loop to acknowledge anything.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from coord.cli import main
from coord.config import BudgetConfig, ConcurrencyConfig, Config, ModelsConfig
from coord.models import Assignment, Board, Machine, Proposal, Repo
from coord.spend_ceiling import format_spend_ceiling_reason

from .conftest import output_and_stderr

_CEILING_REASON = format_spend_ceiling_reason(12.41, 8.0, "work")


def _cfg(budget: BudgetConfig | None = None, **kw) -> Config:
    return Config(
        repos=[Repo(name="api", github="acme/api")],
        machines=[
            Machine(
                name="laptop", host="laptop.tailnet", repos=["api"],
                repo_paths={"api": "/tmp/api"},
            ),
            Machine(
                name="server", host="server.tailnet", repos=["api"],
                repo_paths={"api": "/tmp/api"},
            ),
        ],
        models=ModelsConfig(default="sonnet"),
        budget=budget or BudgetConfig(),
        **kw,
    )


def _failed(**overrides) -> Assignment:
    base = dict(
        machine_name="laptop",
        repo_name="api",
        issue_number=42,
        issue_title="t",
        briefing="b",
        assignment_id="burnedid",
        status="failed",
        type="work",
        model="sonnet",
        branch="issue-42-t",
    )
    base.update(overrides)
    return Assignment(**base)


# ── the ceiling reaches the agent, and only when configured ────────────────


def _proposal(assignment_type: str = "work") -> Proposal:
    return Proposal(
        id=1,
        rationale="r",
        machine_name="laptop",
        repo_name="api",
        issue_number=42,
        issue_title="t",
        briefing="b",
        type=assignment_type,
    )


@patch("coord.dispatch.httpx.post")
def test_no_budget_block_means_the_payload_is_unchanged(post: MagicMock) -> None:
    """No-config parity: an agent that predates the field must not even see
    the key, because ``AssignmentSpec(**body)`` 400s on any unknown kwarg."""
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"id": "newid"}
    post.return_value = resp

    from coord.dispatch import dispatch

    with patch("coord.dispatch.github_ops.post_issue_comment"):
        dispatch(_proposal(), _cfg())

    assert "cost_ceiling_usd" not in post.call_args.kwargs["json"]


@patch("coord.dispatch.httpx.post")
def test_configured_ceiling_rides_the_assign_wire_per_type(post: MagicMock) -> None:
    """It is resolved coordinator-side and carried, not read from the agent's
    own config — a config-free agent has none to read."""
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"id": "newid"}
    post.return_value = resp

    from coord.dispatch import dispatch

    cfg = _cfg(BudgetConfig(
        per_leg_ceiling_usd=8.0, type_ceilings={"smoke": 2.0},
    ))
    with patch("coord.dispatch.github_ops.post_issue_comment"):
        dispatch(_proposal("work"), cfg)
    assert post.call_args.kwargs["json"]["cost_ceiling_usd"] == 8.0

    with patch("coord.dispatch.github_ops.post_issue_comment"):
        dispatch(_proposal("smoke"), cfg)
    assert post.call_args.kwargs["json"]["cost_ceiling_usd"] == 2.0


@patch("coord.dispatch.httpx.post")
def test_an_agent_that_rejects_the_field_gets_an_uncapped_dispatch(
    post: MagicMock,
) -> None:
    """The agent lane lags the CLI/daemon lane. Without this fallback the
    first operator to enable ``budget:`` would take the whole fleet's
    dispatch down until every agent had been updated — an uncapped leg is
    today's behaviour and strictly better than no leg at all."""
    rejected = MagicMock()
    rejected.status_code = 400
    accepted = MagicMock()
    accepted.status_code = 200
    accepted.json.return_value = {"id": "newid"}
    post.side_effect = [rejected, accepted]

    from coord.dispatch import dispatch

    with patch("coord.dispatch.github_ops.post_issue_comment"):
        result = dispatch(_proposal(), _cfg(BudgetConfig(per_leg_ceiling_usd=8.0)))

    assert result["id"] == "newid"
    assert post.call_count == 2
    assert "cost_ceiling_usd" in post.call_args_list[0].kwargs["json"]
    assert "cost_ceiling_usd" not in post.call_args_list[1].kwargs["json"]


# ── `coord retry` must not silently re-spend ───────────────────────────────


class TestRetryRefusesACeilingKill:
    def test_refused_without_acknowledgement(self, valid_config_path: Path) -> None:
        board = Board(completed=[_failed(failure_reason=_CEILING_REASON)])
        with (
            patch("coord.board_service.read_board", return_value=board),
            patch("coord.board_service.write_board"),
            patch("coord.reconcile._reassign") as reassign,
        ):
            result = CliRunner().invoke(
                main, ["retry", "burnedid", "--config", str(valid_config_path)],
            )
        out = output_and_stderr(result)
        assert result.exit_code == 1, out
        assert "spend ceiling" in out
        assert "--acknowledge-cost" in out
        # Refused BEFORE any dispatch — no money spent on the refusal path.
        reassign.assert_not_called()

    def test_proceeds_once_acknowledged(self, valid_config_path: Path) -> None:
        board = Board(completed=[_failed(failure_reason=_CEILING_REASON)])
        retried = Assignment(
            machine_name="server", repo_name="api", issue_number=42,
            issue_title="[retry] t", assignment_id="new-retry-id",
            type="work", status="running", branch="issue-42-t",
        )
        with (
            patch("coord.board_service.read_board", return_value=board),
            patch("coord.board_service.write_board"),
            patch("coord.reconcile._reassign", return_value=retried) as reassign,
            patch("coord.state.dismiss_drive_escalation") as dismiss,
        ):
            result = CliRunner().invoke(
                main,
                ["retry", "burnedid", "--acknowledge-cost",
                 "--config", str(valid_config_path)],
            )
        out = output_and_stderr(result)
        assert result.exit_code == 0, out
        assert "acknowledged spend-ceiling kill" in out
        reassign.assert_called_once()
        # The escalation asked a question the operator just answered; leaving
        # it lit is how an alert channel gets muted.
        dismiss.assert_called_once_with("api", 42)

    def test_an_ordinary_failure_is_completely_unaffected(
        self, valid_config_path: Path
    ) -> None:
        """Regression guard: the refusal keys on the ceiling prefix alone,
        never on 'the row failed'."""
        board = Board(completed=[_failed(failure_reason="tests failed")])
        retried = Assignment(
            machine_name="server", repo_name="api", issue_number=42,
            issue_title="[retry] t", assignment_id="new-retry-id",
            type="work", status="running", branch="issue-42-t",
        )
        with (
            patch("coord.board_service.read_board", return_value=board),
            patch("coord.board_service.write_board"),
            patch("coord.reconcile._reassign", return_value=retried) as reassign,
        ):
            result = CliRunner().invoke(
                main, ["retry", "burnedid", "--config", str(valid_config_path)],
            )
        assert result.exit_code == 0, output_and_stderr(result)
        assert "spend ceiling" not in output_and_stderr(result)
        reassign.assert_called_once()


# ── auto_reassign must never re-spend a ceiling unattended ─────────────────


class TestAutoReassignSkipsACeilingKill:
    def _run(self, board: Board, entry: dict, cfg: Config):
        """Drive `reconcile()`'s auto-reassign arm with one just-failed row."""
        from coord import reconcile as rec

        agent_status = {
            "active": [],
            "completed": [{
                "id": "burnedid", "status": "failed", "exit_code": 125,
                "branch": "issue-42-t", **entry,
            }],
        }
        with (
            patch.object(rec, "_query_agent", return_value=agent_status),
            patch.object(rec, "_reassign") as reassign,
            patch.object(rec, "_record_usage_limit_reason"),
            patch.object(rec, "_escalate_spend_ceiling_best_effort"),
            patch(
                "coord.interactive.reap_stale_interactive_sessions",
                return_value=[],
            ),
            patch(
                "coord.interactive.reap_stale_remote_interactive_sessions",
                return_value=[],
            ),
            patch("coord.review.dispatch_pending_reviews", return_value=[]),
            patch("coord.review.dispatch_scoped_reviews_for_queue", return_value=[]),
            patch("coord.smoke.dispatch_pending_smoke", return_value=[]),
        ):
            rec.reconcile(board, cfg)
        return reassign

    def test_a_ceiling_kill_is_not_auto_reassigned(self) -> None:
        cfg = _cfg(concurrency=ConcurrencyConfig(auto_reassign=True))
        board = Board(active=[_failed(status="running")])
        reassign = self._run(
            board, {"spend_ceiling_reason": _CEILING_REASON}, cfg,
        )
        reassign.assert_not_called()

    def test_an_ordinary_failure_still_auto_reassigns(self) -> None:
        """Control: the skip must be scoped to the ceiling, not widened into
        'stop auto-reassigning failures'."""
        cfg = _cfg(concurrency=ConcurrencyConfig(auto_reassign=True))
        board = Board(active=[_failed(status="running")])
        reassign = self._run(board, {}, cfg)
        reassign.assert_called_once()


# ── escalation: it has to surface where a human will see it ────────────────


class TestEscalation:
    def test_a_ceiling_kill_is_escalated_with_the_acknowledged_retry(self) -> None:
        from coord.reconcile import _escalate_spend_ceiling_best_effort

        with patch("coord.state.record_drive_escalation") as record:
            _escalate_spend_ceiling_best_effort(
                _failed(), {"spend_ceiling_reason": _CEILING_REASON, "exit_code": 125},
            )

        record.assert_called_once()
        args, kwargs = record.call_args
        assert args == ("api", 42)
        assert "spend ceiling" in kwargs["reason"]
        # The operator's next step is a decision, so name the command that
        # says so rather than a bare `coord retry`.
        assert kwargs["proposed_command"] == "coord retry burnedid --acknowledge-cost"
        assert kwargs["assignment_id"] == "burnedid"

    def test_nothing_is_escalated_for_an_ordinary_failure(self) -> None:
        from coord.reconcile import _escalate_spend_ceiling_best_effort

        with patch("coord.state.record_drive_escalation") as record:
            _escalate_spend_ceiling_best_effort(_failed(), {"exit_code": 1})
            _escalate_spend_ceiling_best_effort(
                _failed(), {"usage_limit_reason": "usage limit — resets 8:30pm"},
            )
        record.assert_not_called()

    def test_an_escalation_write_failure_never_breaks_the_transition(self) -> None:
        """Best-effort: a diagnostic must not take down a real status flip."""
        from coord.reconcile import _escalate_spend_ceiling_best_effort

        with patch(
            "coord.state.record_drive_escalation", side_effect=RuntimeError("db down"),
        ):
            _escalate_spend_ceiling_best_effort(
                _failed(), {"spend_ceiling_reason": _CEILING_REASON},
            )  # must not raise
