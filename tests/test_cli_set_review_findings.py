"""Tests for `coord set-review-findings` — #587.

Verifies that the command writes review findings to the DB so that
`_load_review_findings` can serve them from the DB cache, preventing
the "(No structured findings were captured)" fallback for human-attended
(claude-pty) reviews.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from coord.cli import main
from coord.state import load_assignment_review_findings


CONFIG_YAML = """\
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


@pytest.fixture
def config_path(tmp_path):
    p = tmp_path / "coordinator.yml"
    p.write_text(CONFIG_YAML)
    return p


@pytest.fixture
def runner():
    return CliRunner()


def _seed_assignment(conn, assignment_id: str, review_verdict: str | None = None) -> None:
    """Insert a minimal assignment row for testing."""
    conn.execute(
        "INSERT INTO assignments "
        "(assignment_id, type, repo_name, machine_name, issue_number, issue_title, "
        " status, review_verdict) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (
            assignment_id, "review", "api", "laptop", 42,
            "Test issue", "done", review_verdict,
        ),
    )
    conn.commit()


def test_set_review_findings_writes_to_db(runner, config_path, coord_db):
    """Happy path: findings are persisted and readable via load_assignment_review_findings."""
    _seed_assignment(coord_db, "rev-abc123", review_verdict="request-changes")

    result = runner.invoke(
        main,
        [
            "set-review-findings", "--config", str(config_path),
            "rev-abc123",
            "--findings", "The error path is not tested.",
        ],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    assert "findings recorded" in result.output

    row = load_assignment_review_findings("rev-abc123")
    assert row is not None
    verdict, body = row
    assert verdict == "request-changes"
    assert body == "The error path is not tested."


def test_set_review_findings_empty_findings_is_error(runner, config_path):
    """--findings with only whitespace must exit non-zero."""
    result = runner.invoke(
        main,
        [
            "set-review-findings", "--config", str(config_path),
            "rev-xyz",
            "--findings", "   ",
        ],
    )
    assert result.exit_code != 0
    combined = result.output + (result.stderr or "")
    assert "empty" in combined.lower()


def test_set_review_findings_missing_findings_flag_is_error(runner, config_path):
    """Omitting --findings must exit non-zero (it is required)."""
    result = runner.invoke(
        main,
        [
            "set-review-findings", "--config", str(config_path),
            "rev-xyz",
        ],
    )
    assert result.exit_code != 0


def test_set_review_findings_refuses_overwrite_without_force(runner, config_path, coord_db):
    """#650: a second call with a DIFFERENT body is refused (clobber guard) —
    a single assignment backs exactly one review, so a second, differing
    write is a re-run of the rework dialog, never a legitimate new review.
    """
    _seed_assignment(coord_db, "rev-dup", review_verdict="request-changes")

    runner.invoke(
        main,
        [
            "set-review-findings", "--config", str(config_path),
            "rev-dup",
            "--findings", "First draft findings.",
        ],
        catch_exceptions=False,
    )
    result = runner.invoke(
        main,
        [
            "set-review-findings", "--config", str(config_path),
            "rev-dup",
            "--findings", "Revised findings.",
        ],
        catch_exceptions=False,
    )

    assert result.exit_code != 0
    combined = result.output + (result.stderr or "")
    assert "refused" in combined.lower()

    row = load_assignment_review_findings("rev-dup")
    assert row is not None
    _, body = row
    assert body == "First draft findings."


def test_set_review_findings_force_overwrites_existing(runner, config_path, coord_db):
    """#650: --force confirms the overwrite explicitly."""
    _seed_assignment(coord_db, "rev-dup-force", review_verdict="request-changes")

    runner.invoke(
        main,
        [
            "set-review-findings", "--config", str(config_path),
            "rev-dup-force",
            "--findings", "First draft findings.",
        ],
        catch_exceptions=False,
    )
    result = runner.invoke(
        main,
        [
            "set-review-findings", "--config", str(config_path),
            "rev-dup-force",
            "--findings", "Revised findings.",
            "--force",
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0, result.output
    row = load_assignment_review_findings("rev-dup-force")
    assert row is not None
    _, body = row
    assert body == "Revised findings."
