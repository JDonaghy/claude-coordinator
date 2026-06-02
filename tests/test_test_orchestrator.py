"""Unit tests for coord/test_orchestrator.py — Phase A of #342.

Covers:
- JSON shape validation (_validate_plan): valid passes, missing keys rejected,
  extra keys allowed, steps capped at 8.
- Manifest-merging logic: when a non-empty manifest is available, it appears
  in the generated prompt (which prompts Claude to prefer pull steps).
- Retry-once behaviour: first call returns malformed JSON → second call is
  made with a "your previous output was not valid JSON; try again" hint.
- generate_plan falls back to the error dict when both attempts fail.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from coord.test_orchestrator import (
    PLAN_SYSTEM_PROMPT,
    _build_user_prompt,
    _strip_fences,
    _validate_plan,
    generate_plan,
)


# ── _strip_fences ─────────────────────────────────────────────────────────────

class TestStripFences:
    def test_no_fence(self) -> None:
        raw = '{"steps": [], "blockers": []}'
        assert _strip_fences(raw) == raw.strip()

    def test_json_fence(self) -> None:
        raw = '```json\n{"steps": [], "blockers": []}\n```'
        assert _strip_fences(raw) == '{"steps": [], "blockers": []}'

    def test_bare_fence(self) -> None:
        raw = '```\n{"steps": [], "blockers": []}\n```'
        assert _strip_fences(raw) == '{"steps": [], "blockers": []}'

    def test_extra_whitespace(self) -> None:
        raw = '  \n```json\n{"steps": [], "blockers": []}\n```\n  '
        assert _strip_fences(raw.strip()) == '{"steps": [], "blockers": []}'


# ── _validate_plan ────────────────────────────────────────────────────────────

class TestValidatePlan:
    def _valid(self) -> dict:
        return {
            "steps": [
                {"kind": "pull", "cmd": "coord pull-artifact abc", "label": "binary"},
                {"kind": "run", "cmd": "pytest tests/"},
                {"kind": "verify", "check": "exit code 0, no FAILED lines"},
            ],
            "blockers": ["python 3.12 required"],
        }

    def test_valid_plan_passes(self) -> None:
        result = _validate_plan(self._valid())
        assert result["blockers"] == ["python 3.12 required"]
        assert len(result["steps"]) == 3
        assert result["steps"][0]["kind"] == "pull"

    def test_extra_keys_are_allowed(self) -> None:
        plan = self._valid()
        plan["steps"][0]["extra_key"] = "ignored"
        plan["future_field"] = "ignored"
        result = _validate_plan(plan)
        # Extra key on step is preserved.
        assert result["steps"][0].get("extra_key") == "ignored"

    def test_missing_steps_key_raises(self) -> None:
        with pytest.raises(ValueError, match="missing required key 'steps'"):
            _validate_plan({"blockers": []})

    def test_missing_blockers_key_raises(self) -> None:
        with pytest.raises(ValueError, match="missing required key 'blockers'"):
            _validate_plan({"steps": []})

    def test_not_a_dict_raises(self) -> None:
        with pytest.raises(ValueError, match="must be a JSON object"):
            _validate_plan([{"kind": "run"}])

    def test_steps_not_a_list_raises(self) -> None:
        with pytest.raises(ValueError, match="'steps' must be an array"):
            _validate_plan({"steps": "run tests", "blockers": []})

    def test_invalid_kind_raises(self) -> None:
        with pytest.raises(ValueError, match="invalid kind"):
            _validate_plan({"steps": [{"kind": "unknown"}], "blockers": []})

    def test_steps_capped_at_8(self) -> None:
        many_steps = [{"kind": "run", "cmd": f"echo {i}"} for i in range(12)]
        result = _validate_plan({"steps": many_steps, "blockers": []})
        assert len(result["steps"]) == 8

    def test_blockers_stringified(self) -> None:
        result = _validate_plan({"steps": [], "blockers": [42, "need gtk"]})
        assert result["blockers"] == ["42", "need gtk"]

    def test_step_element_not_dict_raises(self) -> None:
        with pytest.raises(ValueError, match="step 0 must be an object"):
            _validate_plan({"steps": ["not a dict"], "blockers": []})

    def test_all_valid_kinds(self) -> None:
        for kind in ("pull", "run", "verify"):
            result = _validate_plan({"steps": [{"kind": kind}], "blockers": []})
            assert result["steps"][0]["kind"] == kind


# ── _build_user_prompt / manifest-merging ────────────────────────────────────

class TestBuildUserPrompt:
    """Verify that manifest presence controls what appears in the prompt."""

    def _prompt(self, *, manifest: dict | None = None, diff: str = "diff here") -> str:
        return _build_user_prompt(
            issue_number=42,
            issue_body="Fix the bug",
            claude_md="## Rules",
            diff_text=diff,
            manifest=manifest,
        )

    def test_no_manifest_includes_rebuild_instruction(self) -> None:
        prompt = self._prompt(manifest=None)
        assert "not available" in prompt
        assert "local rebuild" in prompt
        # The phrase that tells Claude there are no artifacts.
        assert "no pre-built artifacts" in prompt.lower() or "not available" in prompt

    def test_non_empty_manifest_included_in_prompt(self) -> None:
        manifest = {
            "files": [{"name": "coord-tui", "size": 2048, "mtime": 1700000000}],
            "total_bytes": 2048,
            "built_by_assignment_id": "abc123",
        }
        prompt = self._prompt(manifest=manifest)
        # Manifest JSON should appear verbatim.
        assert "coord-tui" in prompt
        assert "abc123" in prompt
        # Instruction to prefer pull over rebuild.
        assert "pull-artifact" in prompt or "Pre-built" in prompt

    def test_large_diff_is_truncated(self) -> None:
        big_diff = "+" + "x" * 25_000
        prompt = self._prompt(diff=big_diff)
        assert "truncated" in prompt
        # The raw diff should NOT appear in full.
        assert len(prompt) < 30_000

    def test_diff_present_in_prompt(self) -> None:
        prompt = self._prompt(diff="- old line\n+ new line")
        assert "- old line" in prompt
        assert "+ new line" in prompt

    def test_claude_md_included(self) -> None:
        prompt = self._prompt()
        assert "## Rules" in prompt

    def test_issue_body_included(self) -> None:
        prompt = self._prompt()
        assert "Fix the bug" in prompt


# ── generate_plan — happy path ────────────────────────────────────────────────

def _insert_assignment(
    conn: sqlite3.Connection,
    *,
    assignment_id: str = "abc123",
    branch: str = "issue-42-fix-bug",
) -> None:
    conn.execute(
        """INSERT INTO assignments
           (assignment_id, machine_name, repo_name, repo_github,
            issue_number, issue_title, status, branch)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (assignment_id, "laptop", "api", "acme/api", 42, "Fix bug", "done", branch),
    )
    conn.commit()


def _make_config() -> MagicMock:
    """Return a minimal Config-like mock."""
    from coord.models import Machine, Repo

    repo = Repo(name="api", github="acme/api", default_branch="main")
    machine = Machine(
        name="laptop",
        host="laptop.tailnet",
        capabilities=["python"],
        repos=["api"],
        repo_paths={"api": "/tmp/nonexistent-repo"},
    )
    cfg = MagicMock()
    cfg.repos = [repo]
    cfg.machines = [machine]
    cfg.repo.return_value = repo
    return cfg


class TestGeneratePlan:
    """Tests for generate_plan() using mocked subprocess and httpx."""

    VALID_PLAN = {"steps": [{"kind": "run", "cmd": "pytest"}], "blockers": []}

    def test_happy_path(self, coord_db: sqlite3.Connection) -> None:
        _insert_assignment(coord_db)
        cfg = _make_config()

        with (
            patch("coord.test_orchestrator._call_claude") as mock_claude,
            patch("coord.test_orchestrator._fetch_artifact_manifest", return_value=None),
            patch("coord.test_orchestrator._get_pr_diff", return_value="diff"),
            patch("coord.test_orchestrator._get_issue_body", return_value="Issue body"),
        ):
            mock_claude.return_value = json.dumps(self.VALID_PLAN)
            result = generate_plan("abc123", cfg)

        assert result["steps"] == [{"kind": "run", "cmd": "pytest"}]
        assert result["blockers"] == []
        mock_claude.assert_called_once()

    def test_unknown_assignment_returns_error(self, coord_db: sqlite3.Connection) -> None:
        cfg = _make_config()
        result = generate_plan("no-such-id", cfg)
        assert result["steps"] == []
        assert "not found" in result["blockers"][0]

    def test_manifest_injected_into_prompt_when_non_empty(
        self, coord_db: sqlite3.Connection
    ) -> None:
        """When manifest is non-empty the prompt sent to Claude contains it."""
        _insert_assignment(coord_db)
        cfg = _make_config()
        manifest = {
            "files": [{"name": "coord-tui", "size": 1024, "mtime": 1700000000}],
            "total_bytes": 1024,
            "built_by_assignment_id": "abc123",
        }

        captured_prompts: list[str] = []

        def fake_claude(system: str, user: str, *, model: str = "haiku") -> str:
            captured_prompts.append(user)
            return json.dumps(self.VALID_PLAN)

        with (
            patch("coord.test_orchestrator._call_claude", side_effect=fake_claude),
            patch(
                "coord.test_orchestrator._fetch_artifact_manifest",
                return_value=manifest,
            ),
            patch("coord.test_orchestrator._get_pr_diff", return_value=""),
            patch("coord.test_orchestrator._get_issue_body", return_value=""),
        ):
            generate_plan("abc123", cfg)

        assert len(captured_prompts) == 1
        prompt = captured_prompts[0]
        # Manifest content must appear in the prompt.
        assert "coord-tui" in prompt

    def test_empty_manifest_triggers_rebuild_note_in_prompt(
        self, coord_db: sqlite3.Connection
    ) -> None:
        """When manifest is None the prompt tells Claude there are no artifacts."""
        _insert_assignment(coord_db)
        cfg = _make_config()

        captured_prompts: list[str] = []

        def fake_claude(system: str, user: str, *, model: str = "haiku") -> str:
            captured_prompts.append(user)
            return json.dumps(self.VALID_PLAN)

        with (
            patch("coord.test_orchestrator._call_claude", side_effect=fake_claude),
            patch("coord.test_orchestrator._fetch_artifact_manifest", return_value=None),
            patch("coord.test_orchestrator._get_pr_diff", return_value=""),
            patch("coord.test_orchestrator._get_issue_body", return_value=""),
        ):
            generate_plan("abc123", cfg)

        assert "not available" in captured_prompts[0]

    def test_retry_once_on_bad_json(self, coord_db: sqlite3.Connection) -> None:
        """First attempt returns invalid JSON → second attempt is called with hint."""
        _insert_assignment(coord_db)
        cfg = _make_config()

        call_count = 0

        def fake_claude(system: str, user: str, *, model: str = "haiku") -> str:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return "this is not json"
            return json.dumps(self.VALID_PLAN)

        with (
            patch("coord.test_orchestrator._call_claude", side_effect=fake_claude),
            patch("coord.test_orchestrator._fetch_artifact_manifest", return_value=None),
            patch("coord.test_orchestrator._get_pr_diff", return_value=""),
            patch("coord.test_orchestrator._get_issue_body", return_value=""),
        ):
            result = generate_plan("abc123", cfg)

        assert call_count == 2, "should have retried exactly once"
        assert result["steps"] == [{"kind": "run", "cmd": "pytest"}]

    def test_retry_hint_contains_error_message(self, coord_db: sqlite3.Connection) -> None:
        """The retry prompt must include 'not valid JSON' language."""
        _insert_assignment(coord_db)
        cfg = _make_config()

        prompts_seen: list[str] = []
        call_count = 0

        def fake_claude(system: str, user: str, *, model: str = "haiku") -> str:
            nonlocal call_count
            call_count += 1
            prompts_seen.append(user)
            if call_count == 1:
                return "not json"
            return json.dumps(self.VALID_PLAN)

        with (
            patch("coord.test_orchestrator._call_claude", side_effect=fake_claude),
            patch("coord.test_orchestrator._fetch_artifact_manifest", return_value=None),
            patch("coord.test_orchestrator._get_pr_diff", return_value=""),
            patch("coord.test_orchestrator._get_issue_body", return_value=""),
        ):
            generate_plan("abc123", cfg)

        assert len(prompts_seen) == 2
        assert "not valid JSON" in prompts_seen[1]

    def test_fallback_on_two_failures(self, coord_db: sqlite3.Connection) -> None:
        """When both attempts fail, generate_plan returns the fallback dict."""
        _insert_assignment(coord_db)
        cfg = _make_config()

        with (
            patch(
                "coord.test_orchestrator._call_claude",
                return_value="still not json",
            ),
            patch("coord.test_orchestrator._fetch_artifact_manifest", return_value=None),
            patch("coord.test_orchestrator._get_pr_diff", return_value=""),
            patch("coord.test_orchestrator._get_issue_body", return_value=""),
        ):
            result = generate_plan("abc123", cfg)

        assert result == {"steps": [], "blockers": ["plan generation failed"]}

    def test_claude_error_triggers_retry(self, coord_db: sqlite3.Connection) -> None:
        """A RuntimeError from claude -p on attempt 1 → attempt 2 is still made."""
        _insert_assignment(coord_db)
        cfg = _make_config()

        call_count = 0

        def fake_claude(system: str, user: str, *, model: str = "haiku") -> str:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("claude -p failed (exit 1): some error")
            return json.dumps(self.VALID_PLAN)

        with (
            patch("coord.test_orchestrator._call_claude", side_effect=fake_claude),
            patch("coord.test_orchestrator._fetch_artifact_manifest", return_value=None),
            patch("coord.test_orchestrator._get_pr_diff", return_value=""),
            patch("coord.test_orchestrator._get_issue_body", return_value=""),
        ):
            result = generate_plan("abc123", cfg)

        assert call_count == 2
        assert result["steps"] == [{"kind": "run", "cmd": "pytest"}]

    def test_model_passed_through(self, coord_db: sqlite3.Connection) -> None:
        """The model parameter is forwarded to _call_claude."""
        _insert_assignment(coord_db)
        cfg = _make_config()

        with (
            patch("coord.test_orchestrator._call_claude") as mock_claude,
            patch("coord.test_orchestrator._fetch_artifact_manifest", return_value=None),
            patch("coord.test_orchestrator._get_pr_diff", return_value=""),
            patch("coord.test_orchestrator._get_issue_body", return_value=""),
        ):
            mock_claude.return_value = json.dumps(self.VALID_PLAN)
            generate_plan("abc123", cfg, model="opus")

        _, call_kwargs = mock_claude.call_args
        assert call_kwargs.get("model") == "opus"


# ── Schema migration idempotency ──────────────────────────────────────────────

class TestSchemaMigration:
    """test_plan column migrations are idempotent (safe to run twice)."""

    def test_add_column_twice_is_safe(self) -> None:
        """Running _migrate_add_columns twice must not raise."""
        from coord.db import _migrate_add_columns

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        from coord.db import _ensure_schema
        _ensure_schema(conn)

        # A second call to _migrate_add_columns must be a no-op (not raise).
        _migrate_add_columns(conn)
        _migrate_add_columns(conn)

        # Confirm the column exists by inserting a value.
        conn.execute(
            "UPDATE assignments SET test_plan = ? WHERE 1=0",
            ('{"steps":[],"blockers":[]}',),
        )

    def test_test_plan_column_exists_after_schema(self) -> None:
        """The test_plan column must be present in a freshly created schema."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        from coord.db import _ensure_schema
        _ensure_schema(conn)

        cursor = conn.execute("PRAGMA table_info(assignments)")
        columns = {row["name"] for row in cursor}
        assert "test_plan" in columns

    def test_test_plan_branch_head_column_exists_after_schema(self) -> None:
        """#349 Phase B: test_plan_branch_head column must exist after migration."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        from coord.db import _ensure_schema
        _ensure_schema(conn)

        cursor = conn.execute("PRAGMA table_info(assignments)")
        columns = {row["name"] for row in cursor}
        assert "test_plan_branch_head" in columns, (
            "test_plan_branch_head column not found — migration not applied"
        )

    def test_branch_head_migration_idempotent(self) -> None:
        """Running _migrate_add_columns twice on a schema that already has
        test_plan_branch_head must not raise."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        from coord.db import _ensure_schema, _migrate_add_columns
        _ensure_schema(conn)
        _migrate_add_columns(conn)  # second call — must be a no-op
        # Confirm the column is writable.
        conn.execute(
            "UPDATE assignments SET test_plan_branch_head = ? WHERE 1=0",
            ("abc123def456",),
        )


# ── set_test_plan branch_head parameter (#349 Phase B) ───────────────────────

class TestSetTestPlanBranchHead:
    """Tests for set_test_plan's branch_head parameter and get_test_plan_branch_head."""

    def _make_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        from coord.db import _ensure_schema
        _ensure_schema(conn)
        return conn

    def _insert(self, conn: sqlite3.Connection, aid: str = "abc123") -> None:
        conn.execute(
            "INSERT INTO assignments (assignment_id, machine_name, repo_name, "
            "issue_number, issue_title, status) VALUES (?, 'm', 'r', 1, 't', 'done')",
            (aid,),
        )
        conn.commit()

    def test_set_test_plan_without_branch_head(self, coord_db: sqlite3.Connection) -> None:
        """Passing branch_head=None must leave test_plan_branch_head as NULL."""
        from coord.state import set_test_plan, get_test_plan_branch_head
        _insert_assignment(coord_db)
        set_test_plan("abc123", {"steps": [], "blockers": []})
        assert get_test_plan_branch_head("abc123") is None

    def test_set_test_plan_with_branch_head(self, coord_db: sqlite3.Connection) -> None:
        """branch_head written by set_test_plan is readable via get_test_plan_branch_head."""
        from coord.state import set_test_plan, get_test_plan_branch_head
        _insert_assignment(coord_db)
        sha = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
        set_test_plan("abc123", {"steps": [], "blockers": []}, branch_head=sha)
        assert get_test_plan_branch_head("abc123") == sha

    def test_set_test_plan_overwrites_branch_head(self, coord_db: sqlite3.Connection) -> None:
        """A second call to set_test_plan with a new branch_head overwrites the previous one."""
        from coord.state import set_test_plan, get_test_plan_branch_head
        _insert_assignment(coord_db)
        set_test_plan("abc123", {"steps": [], "blockers": []}, branch_head="aaa")
        set_test_plan("abc123", {"steps": [], "blockers": []}, branch_head="bbb")
        assert get_test_plan_branch_head("abc123") == "bbb"

    def test_set_test_plan_none_clears_prior_branch_head(self, coord_db: sqlite3.Connection) -> None:
        """If set_test_plan is called with branch_head=None after a previous SHA was
        stored (e.g. git lookup fails during --refresh), the column must be reset to
        NULL — not left holding the stale SHA."""
        from coord.state import set_test_plan, get_test_plan_branch_head
        _insert_assignment(coord_db)
        set_test_plan("abc123", {"steps": [], "blockers": []}, branch_head="abc")
        assert get_test_plan_branch_head("abc123") == "abc"  # precondition
        set_test_plan("abc123", {"steps": [], "blockers": []}, branch_head=None)
        assert get_test_plan_branch_head("abc123") is None, (
            "branch_head=None should reset the column to NULL, not leave the old SHA"
        )

    def test_get_test_plan_branch_head_missing_row(self, coord_db: sqlite3.Connection) -> None:
        """get_test_plan_branch_head returns None for unknown assignment IDs."""
        from coord.state import get_test_plan_branch_head
        assert get_test_plan_branch_head("no-such-id") is None

    def test_get_test_plan_branch_head_empty_id(self, coord_db: sqlite3.Connection) -> None:
        """get_test_plan_branch_head returns None for an empty assignment ID."""
        from coord.state import get_test_plan_branch_head
        assert get_test_plan_branch_head("") is None
