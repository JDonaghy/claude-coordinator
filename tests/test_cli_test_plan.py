"""Tests for the `coord test-plan` CLI command — Phase A of #342 + #349.

Covers:
- Cache hit: if test_plan is already in DB, no Claude call is made.
- --refresh: regenerates even when a cached plan exists.
- Unknown assignment_id: exits with error message.
- Happy path: plan generated, printed as JSON, persisted to DB.
- Fallback plan: persisted and printed when generation fails.
- #349: branch_head persisted on set_test_plan, reset to NULL when None.
- #349: _get_assignment_branch_head resolves git HEAD via subprocess.
"""

from __future__ import annotations

import json
import sqlite3
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from coord.cli import main
from coord.db import _ensure_schema


# ── Helpers ───────────────────────────────────────────────────────────────────

VALID_CONFIG = """\
repos:
  - name: api
    github: acme/api

machines:
  - name: laptop
    host: laptop.tailnet
    capabilities: [python]
    repos: [api]
    repo_paths:
      api: /tmp/nonexistent
"""


def _write_config(tmp_path, content: str = VALID_CONFIG):
    p = tmp_path / "coordinator.yml"
    p.write_text(content)
    return p


def _insert_assignment(
    conn: sqlite3.Connection,
    *,
    assignment_id: str = "abc123",
    test_plan: str | None = None,
    branch: str = "issue-42-fix-bug",
) -> None:
    conn.execute(
        """INSERT INTO assignments
           (assignment_id, machine_name, repo_name, repo_github,
            issue_number, issue_title, status, branch, test_plan)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            assignment_id,
            "laptop",
            "api",
            "acme/api",
            42,
            "Fix bug",
            "done",
            branch,
            test_plan,
        ),
    )
    conn.commit()


SAMPLE_PLAN = {"steps": [{"kind": "run", "cmd": "pytest"}], "blockers": []}


# ── Cache hit ─────────────────────────────────────────────────────────────────

class TestTestPlanCacheHit:
    def test_cached_plan_is_returned_without_calling_claude(
        self, coord_db: sqlite3.Connection, tmp_path
    ) -> None:
        _insert_assignment(
            coord_db,
            assignment_id="abc123",
            test_plan=json.dumps(SAMPLE_PLAN),
        )
        cfg = _write_config(tmp_path)

        runner = CliRunner()
        with patch("coord.test_orchestrator.generate_plan") as mock_gen:
            result = runner.invoke(
                main, ["test-plan", "abc123", "--config", str(cfg)]
            )

        assert result.exit_code == 0, result.output
        mock_gen.assert_not_called()

        output_plan = json.loads(result.stdout)
        assert output_plan == SAMPLE_PLAN

    def test_cached_plan_output_is_valid_json(
        self, coord_db: sqlite3.Connection, tmp_path
    ) -> None:
        _insert_assignment(
            coord_db,
            assignment_id="abc123",
            test_plan=json.dumps(SAMPLE_PLAN),
        )
        cfg = _write_config(tmp_path)

        runner = CliRunner()
        result = runner.invoke(main, ["test-plan", "abc123", "--config", str(cfg)])
        assert result.exit_code == 0
        parsed = json.loads(result.stdout)
        assert "steps" in parsed
        assert "blockers" in parsed


# ── --refresh ─────────────────────────────────────────────────────────────────

class TestTestPlanRefresh:
    def test_refresh_regenerates_even_when_cached(
        self, coord_db: sqlite3.Connection, tmp_path
    ) -> None:
        old_plan = {"steps": [{"kind": "run", "cmd": "old"}], "blockers": []}
        _insert_assignment(
            coord_db,
            assignment_id="abc123",
            test_plan=json.dumps(old_plan),
        )
        cfg = _write_config(tmp_path)
        new_plan = {"steps": [{"kind": "run", "cmd": "new"}], "blockers": []}

        runner = CliRunner()
        with patch(
            "coord.test_orchestrator.generate_plan", return_value=new_plan
        ) as mock_gen:
            result = runner.invoke(
                main,
                ["test-plan", "abc123", "--refresh", "--config", str(cfg)],
            )

        assert result.exit_code == 0, result.output
        mock_gen.assert_called_once()
        output_plan = json.loads(result.stdout)
        assert output_plan == new_plan

    def test_refresh_overwrites_cached_plan_in_db(
        self, coord_db: sqlite3.Connection, tmp_path
    ) -> None:
        _insert_assignment(
            coord_db,
            assignment_id="abc123",
            test_plan=json.dumps(SAMPLE_PLAN),
        )
        cfg = _write_config(tmp_path)
        fresh_plan = {"steps": [{"kind": "verify", "check": "app started"}], "blockers": []}

        runner = CliRunner()
        with patch(
            "coord.test_orchestrator.generate_plan", return_value=fresh_plan
        ):
            runner.invoke(
                main,
                ["test-plan", "abc123", "--refresh", "--config", str(cfg)],
            )

        # Verify the DB was updated.
        from coord.state import get_test_plan
        stored = get_test_plan("abc123")
        assert stored == fresh_plan


# ── Unknown assignment ────────────────────────────────────────────────────────

class TestTestPlanUnknownAssignment:
    def test_unknown_id_generates_fallback_and_exits_zero(
        self, coord_db: sqlite3.Connection, tmp_path
    ) -> None:
        """generate_plan returns a plan even for unknown IDs (fallback dict);
        the CLI should still exit 0 and print valid JSON."""
        cfg = _write_config(tmp_path)
        fallback = {"steps": [], "blockers": ["assignment 'no-such' not found"]}

        runner = CliRunner()
        with patch(
            "coord.test_orchestrator.generate_plan", return_value=fallback
        ):
            result = runner.invoke(
                main,
                ["test-plan", "no-such", "--config", str(cfg)],
            )

        # The command should not crash; it prints whatever generate_plan returns.
        assert result.exit_code == 0
        parsed = json.loads(result.stdout)
        assert parsed["steps"] == []


# ── Happy path (no cache) ─────────────────────────────────────────────────────

class TestTestPlanHappyPath:
    def test_generates_and_prints_plan(
        self, coord_db: sqlite3.Connection, tmp_path
    ) -> None:
        _insert_assignment(coord_db, assignment_id="abc123")
        cfg = _write_config(tmp_path)
        expected = {"steps": [{"kind": "run", "cmd": "make test"}], "blockers": []}

        runner = CliRunner()
        with patch(
            "coord.test_orchestrator.generate_plan", return_value=expected
        ):
            result = runner.invoke(
                main, ["test-plan", "abc123", "--config", str(cfg)]
            )

        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout) == expected

    def test_plan_persisted_to_db_after_generation(
        self, coord_db: sqlite3.Connection, tmp_path
    ) -> None:
        _insert_assignment(coord_db, assignment_id="abc123")
        cfg = _write_config(tmp_path)
        expected = {"steps": [{"kind": "verify", "check": "no crash"}], "blockers": []}

        runner = CliRunner()
        with patch(
            "coord.test_orchestrator.generate_plan", return_value=expected
        ):
            runner.invoke(main, ["test-plan", "abc123", "--config", str(cfg)])

        from coord.state import get_test_plan
        stored = get_test_plan("abc123")
        assert stored == expected

    def test_second_call_is_cache_hit(
        self, coord_db: sqlite3.Connection, tmp_path
    ) -> None:
        _insert_assignment(coord_db, assignment_id="abc123")
        cfg = _write_config(tmp_path)
        plan = {"steps": [{"kind": "run", "cmd": "cargo test"}], "blockers": []}

        runner = CliRunner()
        with patch(
            "coord.test_orchestrator.generate_plan", return_value=plan
        ) as mock_gen:
            # First call — generates.
            runner.invoke(main, ["test-plan", "abc123", "--config", str(cfg)])
            # Second call — should be a cache hit.
            result = runner.invoke(
                main, ["test-plan", "abc123", "--config", str(cfg)]
            )

        assert result.exit_code == 0
        # generate_plan was called exactly once across both invocations.
        assert mock_gen.call_count == 1
        assert json.loads(result.stdout) == plan

    def test_fallback_plan_is_persisted_and_printed(
        self, coord_db: sqlite3.Connection, tmp_path
    ) -> None:
        _insert_assignment(coord_db, assignment_id="abc123")
        cfg = _write_config(tmp_path)
        fallback = {"steps": [], "blockers": ["plan generation failed"]}

        runner = CliRunner()
        with patch(
            "coord.test_orchestrator.generate_plan", return_value=fallback
        ):
            result = runner.invoke(
                main, ["test-plan", "abc123", "--config", str(cfg)]
            )

        assert result.exit_code == 0
        output = json.loads(result.stdout)
        assert output == fallback

        from coord.state import get_test_plan
        assert get_test_plan("abc123") == fallback


# ── Model option ──────────────────────────────────────────────────────────────

class TestTestPlanModelOption:
    def test_default_model_is_haiku(
        self, coord_db: sqlite3.Connection, tmp_path
    ) -> None:
        _insert_assignment(coord_db, assignment_id="abc123")
        cfg = _write_config(tmp_path)

        runner = CliRunner()
        with patch(
            "coord.test_orchestrator.generate_plan", return_value=SAMPLE_PLAN
        ) as mock_gen:
            runner.invoke(main, ["test-plan", "abc123", "--config", str(cfg)])

        _, call_kwargs = mock_gen.call_args
        assert call_kwargs.get("model") == "haiku"

    def test_custom_model_forwarded(
        self, coord_db: sqlite3.Connection, tmp_path
    ) -> None:
        _insert_assignment(coord_db, assignment_id="abc123")
        cfg = _write_config(tmp_path)

        runner = CliRunner()
        with patch(
            "coord.test_orchestrator.generate_plan", return_value=SAMPLE_PLAN
        ) as mock_gen:
            runner.invoke(
                main,
                ["test-plan", "abc123", "--model", "opus", "--config", str(cfg)],
            )

        _, call_kwargs = mock_gen.call_args
        assert call_kwargs.get("model") == "opus"


# ── State helpers: set_test_plan / get_test_plan ──────────────────────────────

class TestStateHelpers:
    def test_set_and_get_round_trip(self, coord_db: sqlite3.Connection) -> None:
        _insert_assignment(coord_db)
        from coord.state import get_test_plan, set_test_plan

        plan = {"steps": [{"kind": "run", "cmd": "pytest"}], "blockers": ["need db"]}
        set_test_plan("abc123", plan)
        result = get_test_plan("abc123")
        assert result == plan

    def test_get_returns_none_when_not_set(self, coord_db: sqlite3.Connection) -> None:
        _insert_assignment(coord_db)
        from coord.state import get_test_plan

        assert get_test_plan("abc123") is None

    def test_get_returns_none_for_unknown_assignment(
        self, coord_db: sqlite3.Connection
    ) -> None:
        from coord.state import get_test_plan

        assert get_test_plan("nonexistent") is None

    def test_set_overwrites_existing(self, coord_db: sqlite3.Connection) -> None:
        _insert_assignment(coord_db)
        from coord.state import get_test_plan, set_test_plan

        plan_a = {"steps": [{"kind": "run", "cmd": "a"}], "blockers": []}
        plan_b = {"steps": [{"kind": "run", "cmd": "b"}], "blockers": ["x"]}
        set_test_plan("abc123", plan_a)
        set_test_plan("abc123", plan_b)
        assert get_test_plan("abc123") == plan_b

    def test_set_with_empty_id_is_noop(self, coord_db: sqlite3.Connection) -> None:
        """set_test_plan with an empty id must not raise."""
        from coord.state import set_test_plan

        set_test_plan("", {"steps": [], "blockers": []})  # should not raise

    def test_get_with_empty_id_returns_none(self, coord_db: sqlite3.Connection) -> None:
        from coord.state import get_test_plan

        assert get_test_plan("") is None

    def test_set_with_branch_head_persists_sha(
        self, coord_db: sqlite3.Connection
    ) -> None:
        """#349: branch_head kwarg is stored in test_plan_branch_head column."""
        _insert_assignment(coord_db)
        from coord.state import set_test_plan

        plan = {"steps": [], "blockers": []}
        set_test_plan("abc123", plan, branch_head="deadbeef1234567890")

        row = coord_db.execute(
            "SELECT test_plan_branch_head FROM assignments WHERE assignment_id='abc123'"
        ).fetchone()
        assert row is not None
        assert row[0] == "deadbeef1234567890"

    def test_set_with_none_branch_head_resets_column(
        self, coord_db: sqlite3.Connection
    ) -> None:
        """#349: calling set_test_plan with branch_head=None resets the column to NULL."""
        _insert_assignment(coord_db)
        from coord.state import set_test_plan

        plan = {"steps": [], "blockers": []}
        # First write with a SHA.
        set_test_plan("abc123", plan, branch_head="aabbccdd")
        # Then overwrite with no SHA — should reset to NULL.
        set_test_plan("abc123", plan, branch_head=None)

        row = coord_db.execute(
            "SELECT test_plan_branch_head FROM assignments WHERE assignment_id='abc123'"
        ).fetchone()
        assert row is not None
        assert row[0] is None


# ── #349: _get_assignment_branch_head helper ──────────────────────────────────

class TestGetAssignmentBranchHead:
    """Unit tests for the branch-HEAD resolver helper in cli.py."""

    def _insert_assignment_with_branch(
        self,
        conn: sqlite3.Connection,
        *,
        assignment_id: str = "abc123",
        repo_name: str = "api",
        branch: str | None = "issue-42-my-fix",
    ) -> None:
        conn.execute(
            """INSERT INTO assignments
               (assignment_id, machine_name, repo_name, repo_github,
                issue_number, issue_title, status, branch)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (assignment_id, "laptop", repo_name, "acme/api", 42,
             "Fix bug", "done", branch),
        )
        conn.commit()

    def test_returns_sha_when_git_succeeds(
        self, coord_db: sqlite3.Connection, tmp_path
    ) -> None:
        self._insert_assignment_with_branch(coord_db)
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()

        from pathlib import Path as _Path
        mock_path_fn = lambda _repo, _cfg: repo_dir  # noqa: E731

        from coord.cli import _get_assignment_branch_head
        from unittest.mock import patch

        with patch(
            "subprocess.run",
            return_value=type("R", (), {"returncode": 0, "stdout": "abcdef123\n"})(),
        ):
            result = _get_assignment_branch_head("abc123", object(), mock_path_fn)

        assert result == "abcdef123"

    def test_returns_none_when_branch_is_null(
        self, coord_db: sqlite3.Connection, tmp_path
    ) -> None:
        self._insert_assignment_with_branch(coord_db, branch=None)
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()

        from coord.cli import _get_assignment_branch_head

        result = _get_assignment_branch_head(
            "abc123", object(), lambda _r, _c: repo_dir
        )
        assert result is None

    def test_returns_none_when_assignment_not_found(
        self, coord_db: sqlite3.Connection
    ) -> None:
        from coord.cli import _get_assignment_branch_head

        result = _get_assignment_branch_head(
            "no-such", object(), lambda _r, _c: None
        )
        assert result is None

    def test_returns_none_when_repo_path_missing(
        self, coord_db: sqlite3.Connection, tmp_path
    ) -> None:
        self._insert_assignment_with_branch(coord_db)
        from coord.cli import _get_assignment_branch_head

        result = _get_assignment_branch_head(
            "abc123", object(), lambda _r, _c: None
        )
        assert result is None

    def test_returns_none_when_git_fails(
        self, coord_db: sqlite3.Connection, tmp_path
    ) -> None:
        self._insert_assignment_with_branch(coord_db)
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()

        from coord.cli import _get_assignment_branch_head
        from unittest.mock import patch

        with patch(
            "subprocess.run",
            return_value=type("R", (), {"returncode": 128, "stdout": ""})(),
        ):
            result = _get_assignment_branch_head(
                "abc123", object(), lambda _r, _c: repo_dir
            )

        assert result is None

    def test_branch_head_stored_in_db_on_generation(
        self, coord_db: sqlite3.Connection, tmp_path
    ) -> None:
        """Integration: after `coord test-plan` runs, test_plan_branch_head is set."""
        _insert_assignment(coord_db, assignment_id="abc123", branch="issue-42-fix-bug")
        cfg = _write_config(tmp_path)

        runner = CliRunner()
        with patch(
            "coord.test_orchestrator.generate_plan", return_value=SAMPLE_PLAN
        ):
            with patch(
                "coord.cli._get_assignment_branch_head",
                return_value="abc123456789",
            ):
                result = runner.invoke(
                    main, ["test-plan", "abc123", "--config", str(cfg)]
                )

        assert result.exit_code == 0, result.output
        row = coord_db.execute(
            "SELECT test_plan_branch_head FROM assignments WHERE assignment_id='abc123'"
        ).fetchone()
        assert row is not None
        assert row[0] == "abc123456789"
