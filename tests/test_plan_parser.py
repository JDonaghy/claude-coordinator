"""Tests for coord.plan_parser — structured plan extraction from worker logs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coord.plan_parser import (
    WorkerPlan,
    _parse_file_list,
    _parse_sections,
    parse_plan_from_log,
    parse_plan_text,
)


# ---------------------------------------------------------------------------
# WorkerPlan dataclass
# ---------------------------------------------------------------------------


class TestWorkerPlan:
    def test_is_empty_when_all_fields_blank(self) -> None:
        assert WorkerPlan().is_empty()

    def test_not_empty_when_plan_set(self) -> None:
        assert not WorkerPlan(plan="summary").is_empty()

    def test_not_empty_when_files_read_set(self) -> None:
        assert not WorkerPlan(files_read=["a.py"]).is_empty()

    def test_not_empty_when_approach_set(self) -> None:
        assert not WorkerPlan(approach="do the thing").is_empty()

    def test_to_dict_roundtrip(self) -> None:
        wp = WorkerPlan(
            plan="summary",
            files_read=["a.py", "b.py"],
            files_modify=["c.py"],
            approach="multi-step",
            risks="might break",
            estimate="small",
            raw_text="original",
        )
        d = wp.to_dict()
        assert d["plan"] == "summary"
        assert d["files_read"] == ["a.py", "b.py"]
        assert d["estimate"] == "small"

        restored = WorkerPlan.from_dict(d)
        assert restored.plan == wp.plan
        assert restored.files_read == wp.files_read
        assert restored.estimate == wp.estimate
        assert restored.raw_text == wp.raw_text

    def test_from_dict_missing_fields_defaults(self) -> None:
        wp = WorkerPlan.from_dict({})
        assert wp.plan == ""
        assert wp.files_read == []
        assert wp.is_empty()


# ---------------------------------------------------------------------------
# _parse_file_list
# ---------------------------------------------------------------------------


class TestParseFileList:
    def test_empty_string(self) -> None:
        assert _parse_file_list("") == []

    def test_single_file(self) -> None:
        assert _parse_file_list("coord/models.py") == ["coord/models.py"]

    def test_comma_separated(self) -> None:
        result = _parse_file_list("a.py, b.py, c.py")
        assert result == ["a.py", "b.py", "c.py"]

    def test_strips_whitespace(self) -> None:
        result = _parse_file_list("  a.py ,  b.py  ")
        assert result == ["a.py", "b.py"]

    def test_ignores_blank_segments(self) -> None:
        result = _parse_file_list("a.py,,b.py,")
        assert result == ["a.py", "b.py"]


# ---------------------------------------------------------------------------
# _parse_sections
# ---------------------------------------------------------------------------


class TestParseSections:
    def test_empty_text(self) -> None:
        assert _parse_sections("") == {}

    def test_no_sections(self) -> None:
        assert _parse_sections("some random text") == {}

    def test_single_section(self) -> None:
        text = "APPROACH: do the thing\n"
        sections = _parse_sections(text)
        assert sections["APPROACH"] == "do the thing"

    def test_multiple_sections(self) -> None:
        text = (
            "FILES_READ: a.py, b.py\n"
            "FILES_MODIFY: c.py\n"
            "APPROACH: step 1. step 2.\n"
            "RISKS: might break\n"
            "ESTIMATE: small\n"
        )
        sections = _parse_sections(text)
        assert sections["FILES_READ"] == "a.py, b.py"
        assert sections["FILES_MODIFY"] == "c.py"
        assert sections["APPROACH"] == "step 1. step 2."
        assert sections["RISKS"] == "might break"
        assert sections["ESTIMATE"] == "small"

    def test_multi_line_section_value(self) -> None:
        text = (
            "APPROACH: First, do this.\n"
            "Then do that.\n"
            "Finally, wrap up.\n"
            "ESTIMATE: medium\n"
        )
        sections = _parse_sections(text)
        assert "First, do this." in sections["APPROACH"]
        assert "Then do that." in sections["APPROACH"]
        assert sections["ESTIMATE"] == "medium"

    def test_plan_section(self) -> None:
        text = "PLAN: implement auth\nFILES_READ: auth.py\n"
        sections = _parse_sections(text)
        assert sections["PLAN"] == "implement auth"
        assert sections["FILES_READ"] == "auth.py"

    # -- markdown-formatted heading tolerance ---------------------------------

    def test_hash_heading_parsed(self) -> None:
        """### FILES_READ: should be recognised the same as bare FILES_READ:"""
        text = "### FILES_READ: coord/cli.py\n### ESTIMATE: small\n"
        sections = _parse_sections(text)
        assert sections["FILES_READ"] == "coord/cli.py"
        assert sections["ESTIMATE"] == "small"

    def test_bold_heading_parsed(self) -> None:
        """**FILES_READ:** should be recognised as a section header."""
        text = "**FILES_READ:** coord/cli.py\n**ESTIMATE:** small\n"
        sections = _parse_sections(text)
        assert sections["FILES_READ"] == "coord/cli.py"
        assert sections["ESTIMATE"] == "small"

    def test_single_hash_heading_parsed(self) -> None:
        """# PLAN: ... should match."""
        text = "# PLAN: implement auth\n# FILES_READ: auth.py\n"
        sections = _parse_sections(text)
        assert sections["PLAN"] == "implement auth"
        assert sections["FILES_READ"] == "auth.py"

    def test_mixed_plain_and_markdown_headings(self) -> None:
        """A log mixing bare and markdown-prefixed headings is fully parsed."""
        text = (
            "### PLAN: add caching\n"
            "FILES_READ: cache.py, main.py\n"
            "**FILES_MODIFY:** cache.py\n"
            "APPROACH: wrap function\n"
        )
        sections = _parse_sections(text)
        assert sections["PLAN"] == "add caching"
        assert "cache.py" in sections["FILES_READ"]
        assert sections["FILES_MODIFY"] == "cache.py"
        assert sections["APPROACH"] == "wrap function"


# ---------------------------------------------------------------------------
# parse_plan_text
# ---------------------------------------------------------------------------


class TestParsePlanText:
    def test_all_sections(self) -> None:
        text = (
            "PLAN: add logging\n"
            "FILES_READ: coord/agent.py, coord/cli.py\n"
            "FILES_MODIFY: coord/agent.py\n"
            "APPROACH: Add a logger at module level and call it in assign().\n"
            "RISKS: Thread-safety of the logger.\n"
            "ESTIMATE: trivial\n"
        )
        plan = parse_plan_text(text)
        assert plan.plan == "add logging"
        assert plan.files_read == ["coord/agent.py", "coord/cli.py"]
        assert plan.files_modify == ["coord/agent.py"]
        assert "logger" in plan.approach
        assert "Thread" in plan.risks
        assert plan.estimate == "trivial"
        assert not plan.is_empty()

    def test_no_sections_preserves_raw_text(self) -> None:
        text = "Just some plain text without any headers."
        plan = parse_plan_text(text)
        assert plan.is_empty()
        assert plan.raw_text == text

    def test_raw_text_always_set(self) -> None:
        text = "FILES_READ: foo.py\n"
        plan = parse_plan_text(text)
        assert plan.raw_text == text

    def test_missing_plan_section_ok(self) -> None:
        text = (
            "FILES_READ: a.py\n"
            "APPROACH: do the thing\n"
            "ESTIMATE: small\n"
        )
        plan = parse_plan_text(text)
        assert plan.plan == ""
        assert not plan.is_empty()


# ---------------------------------------------------------------------------
# parse_plan_from_log — plain-text log
# ---------------------------------------------------------------------------


class TestParsePlanFromLogPlainText:
    def test_nonexistent_file_returns_none(self, tmp_path: Path) -> None:
        result = parse_plan_from_log(tmp_path / "missing.log")
        assert result is None

    def test_plain_text_log_with_plan(self, tmp_path: Path) -> None:
        log = tmp_path / "a1.log"
        log.write_text(
            "FILES_READ: coord/models.py\n"
            "FILES_MODIFY: coord/models.py\n"
            "APPROACH: Add a field.\n"
            "RISKS: Existing board.json files.\n"
            "ESTIMATE: small\n"
        )
        plan = parse_plan_from_log(log)
        assert plan is not None
        assert plan.files_read == ["coord/models.py"]
        assert plan.estimate == "small"

    def test_plain_text_log_without_plan_sections_returns_none(
        self, tmp_path: Path
    ) -> None:
        log = tmp_path / "a2.log"
        log.write_text("random worker output with no plan headings\n")
        assert parse_plan_from_log(log) is None

    def test_accepts_path_string(self, tmp_path: Path) -> None:
        log = tmp_path / "a3.log"
        log.write_text("ESTIMATE: trivial\n")
        plan = parse_plan_from_log(str(log))
        assert plan is not None
        assert plan.estimate == "trivial"


# ---------------------------------------------------------------------------
# parse_plan_from_log — stream-json log
# ---------------------------------------------------------------------------

# Minimal stream-json event payloads that mimic claude -p output.

def _assistant_event(text: str) -> str:
    """Build a single-line stream-json assistant event."""
    return json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [{"type": "text", "text": text}]
            },
        }
    )


def _system_init_event() -> str:
    return json.dumps({"type": "system", "subtype": "init", "session_id": "s1"})


class TestParsePlanFromLogStreamJson:
    def _write_stream_log(self, path: Path, plan_text: str) -> None:
        lines = [
            "# agent=test argv=claude -p",
            _system_init_event(),
            _assistant_event("Thinking..."),
            _assistant_event(plan_text),
        ]
        path.write_text("\n".join(lines) + "\n")

    def test_stream_json_plan_extracted(self, tmp_path: Path) -> None:
        log = tmp_path / "stream.log"
        plan_text = (
            "FILES_READ: coord/agent.py\n"
            "FILES_MODIFY: coord/cli.py\n"
            "APPROACH: Add the command.\n"
            "RISKS: Merge conflicts.\n"
            "ESTIMATE: small\n"
        )
        self._write_stream_log(log, plan_text)
        plan = parse_plan_from_log(log)
        assert plan is not None
        assert plan.files_read == ["coord/agent.py"]
        assert plan.files_modify == ["coord/cli.py"]
        assert plan.estimate == "small"

    def test_stream_json_no_plan_returns_none(self, tmp_path: Path) -> None:
        log = tmp_path / "stream_nop.log"
        self._write_stream_log(log, "No structured sections here.")
        assert parse_plan_from_log(log) is None


# ---------------------------------------------------------------------------
# WorkerPlan — format round-trip via comments.format_plan
# ---------------------------------------------------------------------------


class TestFormatPlanComment:
    def test_format_plan_includes_sections(self) -> None:
        from coord.comments import format_plan

        plan = WorkerPlan(
            plan="implement feature X",
            files_read=["a.py", "b.py"],
            files_modify=["c.py"],
            approach="Step 1. Step 2.",
            risks="DB migration needed.",
            estimate="medium",
        )
        body = format_plan(
            assignment_id="abc123",
            machine_name="laptop",
            repo_name="api",
            issue_number=42,
            plan=plan,
            duration_seconds=30,
        )
        assert "Implementation Plan" in body
        assert "implement feature X" in body
        assert "`a.py`" in body
        assert "`c.py`" in body
        assert "Step 1." in body
        assert "DB migration" in body
        assert "medium" in body
        assert "<!-- coord:event=plan" in body

    def test_format_plan_empty_sections_omitted(self) -> None:
        from coord.comments import format_plan

        plan = WorkerPlan(estimate="trivial")
        body = format_plan(
            assignment_id="x",
            machine_name="m",
            repo_name="r",
            issue_number=1,
            plan=plan,
        )
        assert "trivial" in body
        # Empty sections should not appear
        assert "### Summary" not in body
        assert "### Files Read" not in body


# ---------------------------------------------------------------------------
# State persistence — save_plan / load_plans
# ---------------------------------------------------------------------------


class TestPlanPersistence:
    def test_save_and_load(self, coord_db) -> None:
        from coord.state import load_plans, save_plan

        wp = WorkerPlan(
            plan="p",
            files_read=["x.py"],
            estimate="large",
        )
        save_plan("aid1", wp.to_dict())

        loaded = load_plans()
        assert "aid1" in loaded
        restored = WorkerPlan.from_dict(loaded["aid1"])
        assert restored.plan == "p"
        assert restored.files_read == ["x.py"]
        assert restored.estimate == "large"

    def test_multiple_plans(self, coord_db) -> None:
        from coord.state import load_plans, save_plan

        save_plan("a1", WorkerPlan(estimate="small").to_dict())
        save_plan("a2", WorkerPlan(estimate="large").to_dict())

        loaded = load_plans()
        assert set(loaded) == {"a1", "a2"}
        assert loaded["a1"]["estimate"] == "small"
        assert loaded["a2"]["estimate"] == "large"

    def test_overwrite_existing(self, coord_db) -> None:
        from coord.state import load_plans, save_plan

        save_plan("a1", WorkerPlan(estimate="small").to_dict())
        save_plan("a1", WorkerPlan(estimate="large").to_dict())

        loaded = load_plans()
        assert loaded["a1"]["estimate"] == "large"

    def test_load_empty_db_returns_empty(self, coord_db) -> None:
        from coord.state import load_plans

        result = load_plans()
        assert result == {}


# ---------------------------------------------------------------------------
# build_board — picks up type and plan from dispatched record
# ---------------------------------------------------------------------------


class TestBuildBoardPlan:
    def test_build_board_includes_type_and_plan(
        self, coord_db,
    ) -> None:
        import time
        from coord.models import Proposal
        from coord.state import build_board, save_plan, record_dispatched

        aid = "plan-abc"
        proposal = Proposal(
            id=1,
            machine_name="laptop",
            repo_name="api",
            issue_number=5,
            issue_title="Plan feat",
            rationale="",
            type="plan",
        )
        record_dispatched(assignment_id=aid, proposal=proposal, repo_github="acme/api")
        save_plan(aid, WorkerPlan(estimate="small").to_dict())

        board = build_board()
        assert len(board.active) == 1
        a = board.active[0]
        assert a.type == "plan"
        assert a.plan is not None
        assert a.plan["estimate"] == "small"

    def test_build_board_defaults_type_work_for_old_records(
        self, coord_db,
    ) -> None:
        from coord.models import Proposal
        from coord.state import build_board, record_dispatched

        proposal = Proposal(
            id=1,
            machine_name="laptop",
            repo_name="api",
            issue_number=1,
            issue_title="Fix bug",
            rationale="",
            # type defaults to "work"
        )
        record_dispatched(assignment_id="old-1", proposal=proposal, repo_github="acme/api")

        board = build_board()
        assert board.active[0].type == "work"
        assert board.active[0].plan is None


# ---------------------------------------------------------------------------
# CLI show-plan command
# ---------------------------------------------------------------------------


class TestShowPlanCommand:
    def _make_board(self, coord_db, assignment_type: str = "plan") -> str:
        import time
        from coord.models import Assignment, Board
        from coord.state import save_board, save_plan

        aid = "plan-show-1"
        board = Board(
            round_number=0,
            completed=[
                Assignment(
                    machine_name="laptop",
                    repo_name="api",
                    issue_number=9,
                    issue_title="Plan the feature",
                    assignment_id=aid,
                    status="done",
                    finished_at=time.time(),
                    type=assignment_type,
                    plan=WorkerPlan(
                        files_read=["coord/cli.py"],
                        approach="Add command.",
                        estimate="small",
                    ).to_dict(),
                )
            ],
        )
        save_board(board)
        return aid

    def test_show_plan_displays_sections(self, coord_db) -> None:
        from click.testing import CliRunner
        from coord.cli import main

        aid = self._make_board(coord_db)

        runner = CliRunner()
        result = runner.invoke(main, ["show-plan", aid])
        assert result.exit_code == 0, result.output
        assert "coord/cli.py" in result.output
        assert "Add command." in result.output
        assert "small" in result.output

    def test_show_plan_wrong_type_fails(self, coord_db) -> None:
        from click.testing import CliRunner
        from coord.cli import main

        aid = self._make_board(coord_db, assignment_type="work")

        runner = CliRunner()
        result = runner.invoke(main, ["show-plan", aid])
        assert result.exit_code != 0
        assert "not 'plan'" in result.output

    def test_show_plan_missing_assignment_fails(self, coord_db) -> None:
        from click.testing import CliRunner
        from coord.cli import main

        self._make_board(coord_db)

        runner = CliRunner()
        result = runner.invoke(main, ["show-plan", "does-not-exist"])
        assert result.exit_code != 0
        assert "not found" in result.output
