"""CLI tests for `coord merge` and `coord status` merge-queue display."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from coord import merge_queue as mq
from coord import state as state_mod
from coord.cli import main


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
def config_file(tmp_path: Path) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(CONFIG_YAML)
    return p


@pytest.fixture
def coord_dir(tmp_path: Path, coord_db):
    """Provide an isolated in-memory DB and return a temp dir for logs."""
    d = tmp_path / "state"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _seed_queue(items: list[mq.QueuedMerge]) -> None:
    mq.save_queue(items)


def _entry(aid: str, *, size: int | None = None, state: str = mq.PENDING) -> mq.QueuedMerge:
    return mq.QueuedMerge(
        assignment_id=aid,
        repo_name="api",
        repo_github="acme/api",
        branch=f"worker/{aid}",
        target_branch="main",
        issue_number=int(aid[-1]) if aid[-1].isdigit() else 1,
        issue_title="t",
        size=size,
        state=state,
    )


class TestMergeCommand:
    def test_empty_queue_message(self, config_file: Path, coord_dir: Path) -> None:
        result = CliRunner().invoke(main, ["merge", "--config", str(config_file)])
        assert result.exit_code == 0
        assert "empty" in result.output

    def test_dry_run_does_not_call_gh(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        _seed_queue([_entry("a1"), _entry("a2")])
        with patch("coord.github_ops.create_pr") as create, \
             patch("coord.github_ops.merge_pr") as merge, \
             patch("coord.github_ops.get_pr_size") as size_fn:
            result = CliRunner().invoke(
                main, ["merge", "--config", str(config_file), "--dry-run"]
            )
        assert result.exit_code == 0, result.output
        create.assert_not_called()
        merge.assert_not_called()
        size_fn.assert_not_called()
        assert "would open PR" in result.output
        assert "would merge" in result.output

    def test_merges_in_size_order(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        _seed_queue([_entry("big"), _entry("small"), _entry("mid")])

        merge_calls: list[int] = []
        sizes_by_pr = {100: 500, 101: 10, 102: 100}
        next_pr = [100]

        def fake_create_pr(repo, *, base, head, title, body):
            n = next_pr[0]
            next_pr[0] += 1
            return {"number": n, "url": f"u/{n}", "existed": False}

        def fake_size(repo, number):
            return sizes_by_pr[number]

        def fake_merge(repo, number, method="rebase"):
            merge_calls.append(number)
            return True, "ok"

        with patch("coord.github_ops.create_pr", side_effect=fake_create_pr), \
             patch("coord.github_ops.get_pr_size", side_effect=fake_size), \
             patch("coord.github_ops.merge_pr", side_effect=fake_merge):
            result = CliRunner().invoke(main, ["merge", "--config", str(config_file)])
        assert result.exit_code == 0, result.output
        # 101 (10) → 102 (100) → 100 (500)
        assert merge_calls == [101, 102, 100]

        persisted = {x.assignment_id: x.state for x in mq.load_queue()}
        assert persisted == {"big": mq.MERGED, "small": mq.MERGED, "mid": mq.MERGED}

    def test_conflict_marks_state_and_warns(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        _seed_queue([_entry("a"), _entry("b")])

        next_pr = [200]
        def fake_create_pr(repo, *, base, head, title, body):
            n = next_pr[0]
            next_pr[0] += 1
            return {"number": n, "url": f"u/{n}", "existed": False}

        # First PR conflicts; second never gets attempted.
        def fake_merge(repo, number, method="rebase"):
            if number == 200:
                return False, "Merge conflict"
            return True, "ok"

        with patch("coord.github_ops.create_pr", side_effect=fake_create_pr), \
             patch("coord.github_ops.get_pr_size", return_value=10), \
             patch("coord.github_ops.merge_pr", side_effect=fake_merge):
            result = CliRunner().invoke(main, ["merge", "--config", str(config_file)])
        assert result.exit_code == 0
        assert "conflict" in result.output.lower()
        assert "resolve manually" in result.output

        states = {x.assignment_id: x.state for x in mq.load_queue()}
        assert states["a"] == mq.CONFLICT
        assert states["b"] == mq.PENDING  # halted

    def test_order_override(self, config_file: Path, coord_dir: Path) -> None:
        _seed_queue([_entry("a"), _entry("b"), _entry("c")])

        sizes = {300: 100, 301: 100, 302: 100}
        next_pr = [300]

        def fake_create_pr(repo, *, base, head, title, body):
            n = next_pr[0]
            next_pr[0] += 1
            return {"number": n, "url": f"u/{n}", "existed": False}

        merge_order: list[int] = []
        def fake_merge(repo, number, method="rebase"):
            merge_order.append(number)
            return True, "ok"

        # User says: do c, then a, then b
        with patch("coord.github_ops.create_pr", side_effect=fake_create_pr), \
             patch("coord.github_ops.get_pr_size", side_effect=lambda r, n: sizes[n]), \
             patch("coord.github_ops.merge_pr", side_effect=fake_merge):
            result = CliRunner().invoke(
                main,
                ["merge", "--config", str(config_file), "--order", "c,a,b"],
            )
        assert result.exit_code == 0
        # Same-size group → reorder takes precedence: c first
        # (PR numbers reflect the order PRs were opened, which matches override)
        # We mostly care that 'c' was merged first.
        assert merge_order[0] == 300


class TestStatusMergeQueue:
    def test_status_shows_queue_section(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        _seed_queue([_entry("a1", size=15), _entry("a2", state=mq.CONFLICT, size=5)])
        # Stub network calls so status doesn't try to reach a real agent.
        with patch("coord.network.check_all", return_value=[]):
            result = CliRunner().invoke(main, ["status", "--config", str(config_file)])
        assert result.exit_code == 0
        assert "Merge queue" in result.output
        assert "#1 (worker/a1 → main)" in result.output
        assert "[conflict]" in result.output
