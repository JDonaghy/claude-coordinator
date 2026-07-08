"""#886 Phase 2 — Milestone Outcome Audit: structured verdict + versioned
runs + diff + the Plans-panel Outcome aggregate.

Covers the acceptance bar from the issue:
- the structured verdict round-trips through `post_result` (the `/result`
  seam `coord report-result` uses) and the #603 context store;
- a second `--audit-of` run increments `run_number` and the diff correctly
  buckets a goal that moved gap -> met (closed) vs one that stayed open;
- `coord report-result --audit-json` parses/validates the JSON file and
  refuses obviously-bad input before ever reaching the seam;
- the epic-comment scorecard (`format_audit_scorecard` /
  `extract_audit_scorecard`) round-trips its own structured fields;
- `coord.plans._latest_audit_outcome` (the Plans-panel aggregate consumed by
  the TUI's Outcome chip) picks the latest run and pre-renders the delta.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from coord import issue_store
from coord import state as state_mod
from coord.commands.review import report_result


def _seed_audit_assignment(
    assignment_id: str,
    *,
    repo_name: str = "api",
    repo_github: str = "acme/api",
    machine: str = "laptop",
    issue_number: int = 751,
) -> None:
    """Insert a `type="audit"` running assignment (mirrors #885's
    `_dispatch_audit_of` — the epic's own issue number doubles as the audit
    assignment's `issue_number`)."""
    from coord.models import Proposal

    proposal = Proposal(
        id=0,
        machine_name=machine,
        repo_name=repo_name,
        issue_number=issue_number,
        issue_title="[audit] Milestone",
        rationale="test",
        briefing="brief",
        type="audit",
    )
    state_mod.record_dispatched(
        assignment_id=assignment_id,
        proposal=proposal,
        repo_github=repo_github,
        provider_name="claude-pty",
    )


GOALS_V1 = [
    {
        "goal": "tests.rs split",
        "metric_before": "22k lines",
        "metric_after": "22k lines",
        "verdict": "gap",
        "evidence": "wc -l tests.rs -> 22104",
    },
    {
        "goal": "docs updated",
        "metric_before": "missing",
        "metric_after": "present",
        "verdict": "met",
        "evidence": "docs/ARCHITECTURE.md updated",
    },
]

GOALS_V2 = [
    {
        "goal": "tests.rs split",
        "metric_before": "22k lines",
        "metric_after": "3 files, largest 4k lines",
        "verdict": "met",
        "evidence": "wc -l tests.rs -> 4102",
    },
    {
        "goal": "docs updated",
        "metric_before": "present",
        "metric_after": "present",
        "verdict": "met",
        "evidence": "unchanged",
    },
    {
        "goal": "new goal",
        "metric_before": "n/a",
        "metric_after": "n/a",
        "verdict": "gap",
        "evidence": "not started",
    },
]


class TestAuditRoundTrip:
    def test_first_run_persists_and_writes_context_note(self) -> None:
        from coord.state import list_issue_context

        _seed_audit_assignment("aid-audit-1")
        with patch("coord.github_ops.post_issue_comment") as post:
            outcome = issue_store.post_result(
                issue_store.ResultRecord(
                    assignment_id="aid-audit-1",
                    machine_name="laptop",
                    repo_name="api",
                    repo_github="acme/api",
                    issue_number=751,
                    status="done",
                    verdict=None,
                    summary="1/2 goals met",
                    audit_goals=GOALS_V1,
                    audit_bottom_line="1/2 goals met",
                )
            )
        assert outcome.status == "done"
        assert outcome.posted is True

        row = state_mod.get_connection().execute(
            "SELECT audit_run_number, audit_bottom_line, audit_goals_json "
            "FROM assignments WHERE assignment_id=?",
            ("aid-audit-1",),
        ).fetchone()
        assert row["audit_run_number"] == 1
        assert row["audit_bottom_line"] == "1/2 goals met"
        assert json.loads(row["audit_goals_json"]) == GOALS_V1

        # #603: a durable one-line note for every future agent on this epic.
        entries = list_issue_context("api", 751)
        assert any("Audit v1" in e["body"] for e in entries)
        assert any(e.get("source") == "audit" for e in entries)

        # The epic comment carries the scorecard under the parseable marker.
        posted_body = post.call_args.args[2]
        assert "coord:audit-scorecard" in posted_body
        assert "tests.rs split" in posted_body

    def test_second_run_increments_and_diffs_against_first(self) -> None:
        _seed_audit_assignment("aid-audit-1")
        _seed_audit_assignment("aid-audit-2")
        with patch("coord.github_ops.post_issue_comment"):
            issue_store.post_result(
                issue_store.ResultRecord(
                    assignment_id="aid-audit-1",
                    machine_name="laptop",
                    repo_name="api",
                    repo_github="acme/api",
                    issue_number=751,
                    status="done",
                    verdict=None,
                    summary="v1",
                    audit_goals=GOALS_V1,
                    audit_bottom_line="1/2 goals met",
                )
            )
        with patch("coord.github_ops.post_issue_comment") as post2:
            outcome2 = issue_store.post_result(
                issue_store.ResultRecord(
                    assignment_id="aid-audit-2",
                    machine_name="laptop",
                    repo_name="api",
                    repo_github="acme/api",
                    issue_number=751,
                    status="done",
                    verdict=None,
                    summary="v2",
                    audit_goals=GOALS_V2,
                    audit_bottom_line="2/3 goals met",
                )
            )
        assert outcome2.status == "done"
        row2 = state_mod.get_connection().execute(
            "SELECT audit_run_number FROM assignments WHERE assignment_id=?",
            ("aid-audit-2",),
        ).fetchone()
        assert row2["audit_run_number"] == 2

        runs = issue_store.get_audit_runs_for_epic("api", 751)
        assert [r["audit_run_number"] for r in runs] == [1, 2]

        diff = issue_store.diff_audit_goals(GOALS_V1, GOALS_V2)
        assert diff["closed"] == ["tests.rs split"]
        assert diff["still_open"] == []
        assert diff["new"] == ["new goal"]
        assert diff["regressed"] == []

        posted_body2 = post2.call_args.args[2]
        assert "Delta vs run v1" in posted_body2
        assert "closed" in posted_body2.lower()

    def test_diff_flags_a_regression(self) -> None:
        regressed_v2 = [
            {**GOALS_V1[0], "verdict": "met"},
            {**GOALS_V1[1], "verdict": "gap"},
        ]
        diff = issue_store.diff_audit_goals(GOALS_V1, regressed_v2)
        assert diff["closed"] == ["tests.rs split"]
        assert diff["regressed"] == ["docs updated"]

    def test_empty_goals_list_rejected(self) -> None:
        _seed_audit_assignment("aid-audit-empty")
        with pytest.raises(ValueError, match="non-empty"):
            issue_store.post_result(
                issue_store.ResultRecord(
                    assignment_id="aid-audit-empty",
                    machine_name="laptop",
                    repo_name="api",
                    repo_github="acme/api",
                    issue_number=751,
                    status="done",
                    verdict=None,
                    summary="",
                    audit_goals=[],
                )
            )

    def test_invalid_goal_verdict_rejected(self) -> None:
        _seed_audit_assignment("aid-audit-bad-verdict")
        with pytest.raises(ValueError, match="invalid audit goal verdict"):
            issue_store.post_result(
                issue_store.ResultRecord(
                    assignment_id="aid-audit-bad-verdict",
                    machine_name="laptop",
                    repo_name="api",
                    repo_github="acme/api",
                    issue_number=751,
                    status="done",
                    verdict=None,
                    summary="",
                    audit_goals=[{"goal": "x", "verdict": "maybe"}],
                )
            )

    def test_audit_goals_on_non_audit_assignment_refused(self) -> None:
        """Mirrors the #646 review-verdict target-invariant gate: a
        structured audit verdict may only land on a type="audit" row."""
        from coord.models import Proposal

        proposal = Proposal(
            id=0,
            machine_name="laptop",
            repo_name="api",
            issue_number=751,
            issue_title="Some work",
            rationale="test",
            briefing="brief",
            type="work",
        )
        state_mod.record_dispatched(
            assignment_id="aid-work-not-audit",
            proposal=proposal,
            repo_github="acme/api",
            provider_name="claude-pty",
        )
        with pytest.raises(ValueError, match="not 'audit'"):
            issue_store.post_result(
                issue_store.ResultRecord(
                    assignment_id="aid-work-not-audit",
                    machine_name="laptop",
                    repo_name="api",
                    repo_github="acme/api",
                    issue_number=751,
                    status="done",
                    verdict=None,
                    summary="",
                    audit_goals=GOALS_V1,
                )
            )


class TestAuditScorecardCommentRoundTrip:
    def test_format_then_extract_round_trips_structured_fields(self) -> None:
        from coord.comments import extract_audit_scorecard, format_audit_scorecard

        body = format_audit_scorecard(
            assignment_id="aid-1",
            run_number=2,
            bottom_line="2/3 goals met",
            goals=GOALS_V2,
            diff=issue_store.diff_audit_goals(GOALS_V1, GOALS_V2),
        )
        parsed = extract_audit_scorecard(body)
        assert parsed is not None
        assert parsed["assignment_id"] == "aid-1"
        assert parsed["run_number"] == 2
        assert parsed["bottom_line"] == "2/3 goals met"
        assert parsed["goals"] == GOALS_V2

    def test_extract_returns_none_when_no_scorecard_present(self) -> None:
        from coord.comments import extract_audit_scorecard

        assert extract_audit_scorecard("just a plain comment") is None


_CONFIG_YAML = """\
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


class TestReportResultAuditJsonCli:
    """--audit-json is parsed/validated client-side, before the seam write —
    #886 mirrors the existing --body-file/request-changes fast-feedback guard
    (#580) rather than letting a malformed file reach `post_result`."""

    def _write_audit_json(self, tmp_path: Path, payload: dict) -> str:
        p = tmp_path / "verdict.json"
        p.write_text(json.dumps(payload))
        return str(p)

    @pytest.fixture
    def config_file(self, tmp_path: Path) -> Path:
        p = tmp_path / "coordinator.yml"
        p.write_text(_CONFIG_YAML)
        return p

    def test_bad_json_file_errors_before_reaching_seam(
        self, tmp_path: Path, config_file: Path
    ) -> None:
        p = tmp_path / "verdict.json"
        p.write_text("{not json")
        res = CliRunner().invoke(
            report_result,
            [
                "--assignment", "aid-x",
                "--status", "done",
                "--summary", "x",
                "--audit-json", str(p),
                "--config", str(config_file),
            ],
        )
        assert res.exit_code == 2
        assert "could not read/parse" in res.output

    def test_missing_goals_key_errors(
        self, tmp_path: Path, config_file: Path
    ) -> None:
        path = self._write_audit_json(tmp_path, {"bottom_line": "x"})
        res = CliRunner().invoke(
            report_result,
            [
                "--assignment", "aid-x",
                "--status", "done",
                "--summary", "x",
                "--audit-json", path,
                "--config", str(config_file),
            ],
        )
        assert res.exit_code == 2
        assert "'goals' list" in res.output

    def test_invalid_verdict_in_goal_errors(
        self, tmp_path: Path, config_file: Path
    ) -> None:
        path = self._write_audit_json(
            tmp_path,
            {"bottom_line": "x", "goals": [{"goal": "g", "verdict": "sorta"}]},
        )
        res = CliRunner().invoke(
            report_result,
            [
                "--assignment", "aid-x",
                "--status", "done",
                "--summary", "x",
                "--audit-json", path,
                "--config", str(config_file),
            ],
        )
        assert res.exit_code == 2
        assert "invalid verdict" in res.output


class TestPlansLatestAuditOutcome:
    def test_no_audit_run_yields_none(self) -> None:
        from coord.models import Board
        from coord.plans import _latest_audit_outcome

        board = Board(active=[], completed=[])
        assert _latest_audit_outcome(board, "api", 751) is None
        assert _latest_audit_outcome(board, "api", None) is None

    def test_latest_run_and_diff_summary_computed_from_board(self) -> None:
        from coord.models import Assignment, Board
        from coord.plans import _latest_audit_outcome

        run1 = Assignment(
            assignment_id="a1",
            machine_name="laptop",
            repo_name="api",
            issue_number=751,
            issue_title="[audit] m",
            status="done",
            type="audit",
            audit_run_number=1,
            audit_goals_json=json.dumps(GOALS_V1),
            audit_bottom_line="1/2 goals met",
        )
        run2 = Assignment(
            assignment_id="a2",
            machine_name="laptop",
            repo_name="api",
            issue_number=751,
            issue_title="[audit] m",
            status="done",
            type="audit",
            audit_run_number=2,
            audit_goals_json=json.dumps(GOALS_V2),
            audit_bottom_line="2/3 goals met",
        )
        board = Board(active=[], completed=[run1, run2])
        result = _latest_audit_outcome(board, "api", 751)
        assert result is not None
        assert result["run_number"] == 2
        assert result["met"] == 2
        assert result["gap"] == 1
        assert result["total"] == 3
        assert result["bottom_line"] == "2/3 goals met"
        assert "v1→v2" in result["diff_summary"]
        assert "closed" in result["diff_summary"]
