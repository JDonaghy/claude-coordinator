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
reviews:
  enabled: false
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

    def test_human_classified_conflict_persists_as_human_required(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """Permission / branch-protection errors must persist as HUMAN_REQUIRED.

        Regression test for the review of #243: the original code mutated a
        copy loaded from the DB, but the final save block then re-loaded the
        queue and merged ``items`` (still ``CONFLICT``) over the top,
        clobbering ``HUMAN_REQUIRED``.  The TUI's ``human_required`` paths
        never lit up for this code path as a result.
        """
        from coord.models import Board
        from coord.state import save_board
        save_board(Board())  # the conflict-event block is gated on load_board() != None
        _seed_queue([_entry("p1")])

        def fake_create_pr(repo, *, base, head, title, body):
            return {"number": 999, "url": "u/999", "existed": False}

        def fake_merge(repo, number, method="rebase"):
            # gh emits "permission denied" — classify_conflict()'s _HUMAN_SIGNALS
            # picks this up and the merge command should mark HUMAN_REQUIRED.
            return False, "permission denied — branch protection enabled"

        with patch("coord.github_ops.create_pr", side_effect=fake_create_pr), \
             patch("coord.github_ops.get_pr_size", return_value=10), \
             patch("coord.github_ops.merge_pr", side_effect=fake_merge):
            result = CliRunner().invoke(main, ["merge", "--config", str(config_file)])
        assert result.exit_code == 0, result.output
        assert "manual resolution required" in result.output

        persisted = mq.load_queue()
        assert len(persisted) == 1
        assert persisted[0].state == mq.HUMAN_REQUIRED, (
            f"expected HUMAN_REQUIRED, got {persisted[0].state!r}"
        )

    def test_review_gate_refuses_merge_without_approval(
        self, config_file: Path, coord_dir: Path, coord_db
    ) -> None:
        """#253 regression: reproduces the quadraui#233 scenario.

        With reviews enabled and a done-work assignment that has no review on
        the board, `coord merge` must refuse: PR may open (so the user can
        inspect) but ``gh pr merge`` must NOT be called.
        """
        from coord.models import Assignment, Board
        from coord.state import save_board

        # Config with reviews enabled and "review" in the default gate set.
        config_file.write_text(CONFIG_YAML.replace(
            "reviews:\n  enabled: false\n", ""
        ))

        work = Assignment(
            machine_name="laptop",
            repo_name="api",
            issue_number=233,
            issue_title="#233",
            assignment_id="w233",
            type="work",
            status="done",
            branch="issue-233-fix",
        )
        save_board(Board(active=[], completed=[work]))
        _seed_queue([_entry("w233")])

        with patch("coord.github_ops.create_pr") as create, \
             patch("coord.github_ops.merge_pr") as merge_fn, \
             patch("coord.github_ops.get_pr_size", return_value=10):
            create.return_value = {"number": 999, "url": "u/999", "existed": False}
            result = CliRunner().invoke(main, ["merge", "--config", str(config_file)])

        assert result.exit_code == 0, result.output
        assert "review_required" in result.output
        # The smoking gun: merge_pr must not have been called.
        merge_fn.assert_not_called()

    def test_deleted_branch_work_not_re_enqueued(
        self, config_file: Path, coord_dir: Path, coord_db
    ) -> None:
        """Clog fix: a done-work assignment whose branch no longer exists on
        origin (already merged + deleted) must NOT be auto-enqueued — that
        re-enqueue from board.completed is the dominant merge-queue clog
        source (closed issues miss the open-only issues cache)."""
        from coord.models import Assignment, Board
        from coord.state import save_board

        work = Assignment(
            machine_name="laptop", repo_name="api", issue_number=240,
            issue_title="#240", assignment_id="w240", type="work",
            status="done", branch="issue-240-merged-and-deleted",
        )
        save_board(Board(active=[], completed=[work]))  # queue starts empty

        with patch(
            "coord.github_ops.list_remote_branch_names",
            return_value={"main", "some-other-branch"},  # the work branch is gone
        ):
            result = CliRunner().invoke(
                main, ["merge", "--dry-run", "--config", str(config_file)],
            )

        assert result.exit_code == 0, result.output
        assert "#240" not in result.output
        assert not any(e.issue_number == 240 for e in mq.load_queue())

    def test_existing_branch_work_is_enqueued(
        self, config_file: Path, coord_dir: Path, coord_db
    ) -> None:
        """Counterpart: when the branch still exists on origin, done-work IS
        auto-enqueued — the clog fix must not over-skip live work."""
        from coord.models import Assignment, Board
        from coord.state import save_board

        work = Assignment(
            machine_name="laptop", repo_name="api", issue_number=241,
            issue_title="#241", assignment_id="w241", type="work",
            status="done", branch="issue-241-still-open",
        )
        save_board(Board(active=[], completed=[work]))

        with patch(
            "coord.github_ops.list_remote_branch_names",
            return_value={"main", "issue-241-still-open"},  # branch present
        ):
            result = CliRunner().invoke(
                main, ["merge", "--dry-run", "--config", str(config_file)],
            )

        assert result.exit_code == 0, result.output
        assert any(e.issue_number == 241 for e in mq.load_queue())

    def test_skip_review_flag_bypasses_gate(
        self, config_file: Path, coord_dir: Path, coord_db
    ) -> None:
        """#253: --skip-review must allow merging without an approved review."""
        from coord.models import Assignment, Board
        from coord.state import save_board

        config_file.write_text(CONFIG_YAML.replace(
            "reviews:\n  enabled: false\n", ""
        ))

        work = Assignment(
            machine_name="laptop", repo_name="api", issue_number=234,
            issue_title="#234", assignment_id="w234", type="work",
            status="done", branch="issue-234-fix",
            test_state="passed",  # smoke gate satisfied (#465)
        )
        save_board(Board(active=[], completed=[work]))
        _seed_queue([_entry("w234")])

        with patch("coord.github_ops.create_pr") as create, \
             patch("coord.github_ops.merge_pr") as merge_fn, \
             patch("coord.github_ops.get_pr_size", return_value=10):
            create.return_value = {"number": 998, "url": "u/998", "existed": False}
            merge_fn.return_value = (True, "ok")
            result = CliRunner().invoke(
                main, ["merge", "--config", str(config_file), "--skip-review"],
            )

        assert result.exit_code == 0, result.output
        # Surface the override to the user.
        assert "skip-review" in result.output or "skip_review" in result.output
        merge_fn.assert_called_once()

    def test_review_gate_merges_when_approved(
        self, config_file: Path, coord_dir: Path, coord_db
    ) -> None:
        """#253: an approved review on the board lets the merge proceed."""
        from coord.models import Assignment, Board
        from coord.state import save_board

        config_file.write_text(CONFIG_YAML.replace(
            "reviews:\n  enabled: false\n", ""
        ))

        work = Assignment(
            machine_name="laptop", repo_name="api", issue_number=235,
            issue_title="#235", assignment_id="w235", type="work",
            status="done", branch="issue-235-fix",
            test_state="passed",  # smoke gate satisfied (#465)
        )
        review = Assignment(
            machine_name="other", repo_name="api", issue_number=235,
            issue_title="[review] #235", assignment_id="rev-w235",
            type="review", status="done",
            review_of_assignment_id="w235",
            review_verdict="approve",
        )
        save_board(Board(active=[], completed=[work, review]))
        _seed_queue([_entry("w235")])

        with patch("coord.github_ops.create_pr") as create, \
             patch("coord.github_ops.merge_pr") as merge_fn, \
             patch("coord.github_ops.get_pr_size", return_value=10):
            create.return_value = {"number": 997, "url": "u/997", "existed": False}
            merge_fn.return_value = (True, "ok")
            result = CliRunner().invoke(main, ["merge", "--config", str(config_file)])

        assert result.exit_code == 0, result.output
        merge_fn.assert_called_once()

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


class TestMergeAutoEnqueue:
    """#242: `coord merge` must scan board.completed and enqueue eligible
    work assignments, so done-work that reached terminal state via paths
    other than the `coord status` enqueue hook doesn't silently sit
    un-merged forever."""

    def _seed_board_with_done_work(
        self,
        coord_db,
        *,
        issue_number: int = 218,
        assignment_id: str = "w1",
        branch: str = "issue-218-fix",
    ) -> None:
        from coord.models import Assignment, Board
        from coord.state import save_board

        a = Assignment(
            machine_name="laptop",
            repo_name="api",
            issue_number=issue_number,
            issue_title=f"#{issue_number} title",
            briefing="",
            assignment_id=assignment_id,
            status="done",
            branch=branch,
            type="work",
            test_state="passed",  # smoke gate satisfied (#465)
        )
        save_board(Board(active=[], completed=[a]))

    def _seed_issue_state(self, coord_db, *, number: int, state: str) -> None:
        coord_db.execute(
            "INSERT OR REPLACE INTO issues (repo_name, number, title, state) "
            "VALUES ('api', ?, ?, ?)",
            (number, f"#{number}", state),
        )
        coord_db.commit()

    def test_auto_enqueues_done_work_when_queue_empty(
        self, config_file: Path, coord_dir: Path, coord_db
    ) -> None:
        """The #218 scenario: done-work is in the board but the queue is
        empty.  Without the fix, `coord merge` printed "Merge queue is
        empty" and exited.  Now it should enqueue and process."""
        self._seed_board_with_done_work(coord_db)
        self._seed_issue_state(coord_db, number=218, state="open")

        with patch("coord.github_ops.create_pr") as create, \
             patch("coord.github_ops.merge_pr") as merge_fn, \
             patch("coord.github_ops.get_pr_size", return_value=10):
            create.return_value = {"number": 99, "url": "u/99", "existed": False}
            merge_fn.return_value = (True, "ok")
            result = CliRunner().invoke(main, ["merge", "--config", str(config_file)])

        assert result.exit_code == 0, result.output
        assert "auto-enqueued" in result.output
        assert "#218" in result.output
        create.assert_called_once()
        merge_fn.assert_called_once()

    def test_skips_closed_issues(
        self, config_file: Path, coord_dir: Path, coord_db
    ) -> None:
        """A closed issue (already merged externally) must NOT be auto-
        enqueued — that would spawn a spurious PR against a stale branch."""
        self._seed_board_with_done_work(coord_db, issue_number=42)
        self._seed_issue_state(coord_db, number=42, state="closed")

        with patch("coord.github_ops.create_pr") as create, \
             patch("coord.github_ops.merge_pr") as merge_fn:
            result = CliRunner().invoke(main, ["merge", "--config", str(config_file)])

        assert result.exit_code == 0, result.output
        assert "auto-enqueued" not in result.output
        create.assert_not_called()
        merge_fn.assert_not_called()

    def test_auto_enqueues_when_issue_postdates_cache(
        self, config_file: Path, coord_dir: Path, coord_db
    ) -> None:
        """When the issues cache has rows for the repo but no row for THIS
        issue (e.g. the issue was created after the most recent sync), the
        auto-enqueue must treat it as unknown and allow — not falsely
        infer "closed" from cache miss.

        Repro: cache topped out at #271; #278/#280 silently skipped because
        the filter saw the repo had data but no row for 278/280.
        """
        # Cache has rows for other issues in the repo, but NOT for #280.
        self._seed_issue_state(coord_db, number=271, state="closed")
        self._seed_board_with_done_work(
            coord_db, issue_number=280, assignment_id="w280",
            branch="issue-280-foo",
        )

        with patch("coord.github_ops.create_pr") as create, \
             patch("coord.github_ops.merge_pr") as merge_fn, \
             patch("coord.github_ops.get_pr_size", return_value=10):
            create.return_value = {"number": 999, "url": "u/999", "existed": False}
            merge_fn.return_value = (True, "ok")
            result = CliRunner().invoke(main, ["merge", "--config", str(config_file)])

        assert result.exit_code == 0, result.output
        assert "auto-enqueued" in result.output
        assert "#280" in result.output
        create.assert_called_once()

    def test_skips_issues_already_merged_via_other_assignment(
        self, config_file: Path, coord_dir: Path, coord_db
    ) -> None:
        """An issue with a prior merged queue entry should not get a fresh
        enqueue even if board.completed has a different assignment for it
        (e.g. an old failed attempt).  Avoids duplicate PRs per issue."""
        self._seed_board_with_done_work(
            coord_db, issue_number=55, assignment_id="newer-attempt"
        )
        self._seed_issue_state(coord_db, number=55, state="open")
        # Existing merged entry for #55 from a prior assignment.
        mq.save_queue([_entry("older-attempt", state=mq.MERGED)])
        # Patch the issue number on the seeded merged entry to 55.
        coord_db.execute(
            "UPDATE merge_queue SET issue_number=55 WHERE assignment_id='older-attempt'"
        )
        coord_db.commit()

        with patch("coord.github_ops.create_pr") as create, \
             patch("coord.github_ops.merge_pr"):
            result = CliRunner().invoke(main, ["merge", "--config", str(config_file)])

        assert result.exit_code == 0, result.output
        assert "auto-enqueued" not in result.output
        create.assert_not_called()

    def test_clear_message_when_truly_nothing_to_merge(
        self, config_file: Path, coord_dir: Path, coord_db
    ) -> None:
        """When the board has no done-work and the queue is empty, the
        message should say "no completed work to merge" — not the misleading
        "Merge queue is empty" which sounds like a no-op."""
        result = CliRunner().invoke(main, ["merge", "--config", str(config_file)])
        assert result.exit_code == 0
        assert "no completed work to merge" in result.output

    def test_clear_message_when_done_work_already_merged(
        self, config_file: Path, coord_dir: Path, coord_db
    ) -> None:
        """When all done-work is accounted for (already in queue as merged
        or filtered out), distinguish from the "no completed work" case."""
        self._seed_board_with_done_work(coord_db, issue_number=99)
        # All matching entries already merged.
        mq.save_queue([_entry("w1", state=mq.MERGED)])
        coord_db.execute(
            "UPDATE merge_queue SET issue_number=99 WHERE assignment_id='w1'"
        )
        coord_db.commit()

        result = CliRunner().invoke(main, ["merge", "--config", str(config_file)])
        assert result.exit_code == 0
        # Either "already merged" or "all done-work is already merged" or similar.
        # We just check we got a sensible non-"empty" message after the queue
        # turns out to have only the merged entry.
        assert "merged" in result.output.lower() or "no" in result.output.lower()


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
