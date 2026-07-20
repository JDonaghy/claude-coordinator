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

DEVELOP_BRANCH_CONFIG_YAML = """\
repos:
  - name: api
    github: acme/api
    default_branch: main
    develop_branch: develop
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
def develop_branch_config_file(tmp_path: Path) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(DEVELOP_BRANCH_CONFIG_YAML)
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

        # First PR conflicts; second is attempted and succeeds (#735 park-and-continue).
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
        assert states["b"] == mq.MERGED  # #735: sibling merges despite conflict

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

    def test_human_classified_conflict_writes_operational_audit_row(
        self, config_file: Path, coord_dir: Path, coord_db, monkeypatch,
    ) -> None:
        """#1038: promoting a conflict to HUMAN_REQUIRED — the coordinator's
        own conflict-classification decision, not a per-entry human choice —
        writes an operational-tier row (actor="daemon")."""
        from coord.models import Board
        from coord.state import save_board
        # record_audit's level gate reloads config independently — pin it to
        # this test's config (default audit.level="operational").
        monkeypatch.setenv("COORD_CONFIG", str(config_file))
        save_board(Board())
        _seed_queue([_entry("p1")])

        def fake_create_pr(repo, *, base, head, title, body):
            return {"number": 999, "url": "u/999", "existed": False}

        def fake_merge(repo, number, method="rebase"):
            return False, "permission denied — branch protection enabled"

        with patch("coord.github_ops.create_pr", side_effect=fake_create_pr), \
             patch("coord.github_ops.get_pr_size", return_value=10), \
             patch("coord.github_ops.merge_pr", side_effect=fake_merge):
            result = CliRunner().invoke(main, ["merge", "--config", str(config_file)])
        assert result.exit_code == 0, result.output

        rows = coord_db.execute(
            "SELECT * FROM audit_log WHERE tier='operational'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["category"] == "merge"
        assert rows[0]["event_type"] == "conflict_human_required"
        assert rows[0]["actor"] == "daemon"
        assert rows[0]["repo"] == "api"

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
            test_state="passed",  # smoke gate satisfied (#465) — see #946
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


class TestMergeOnly:
    """#780: coord merge --only <aid> — single-entry isolation."""

    def test_only_merges_selected_entry_and_leaves_others_pending(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """The target entry is merged; the other two entries remain PENDING."""
        _seed_queue([_entry("x1"), _entry("x2"), _entry("x3")])

        next_pr = [400]
        merged_prs: list[int] = []

        def fake_create_pr(repo, *, base, head, title, body):
            n = next_pr[0]
            next_pr[0] += 1
            return {"number": n, "url": f"u/{n}", "existed": False}

        def fake_merge(repo, number, method="rebase"):
            merged_prs.append(number)
            return True, "ok"

        with patch("coord.github_ops.create_pr", side_effect=fake_create_pr), \
             patch("coord.github_ops.get_pr_size", return_value=10), \
             patch("coord.github_ops.merge_pr", side_effect=fake_merge):
            result = CliRunner().invoke(
                main, ["merge", "--config", str(config_file), "--only", "x2"]
            )

        assert result.exit_code == 0, result.output
        # Exactly one merge was performed.
        assert len(merged_prs) == 1, f"expected exactly 1 merge, got {merged_prs}"

        # Only x2 is MERGED; x1 and x3 stay PENDING.
        states = {e.assignment_id: e.state for e in mq.load_queue()}
        assert states["x2"] == mq.MERGED, f"x2 should be MERGED, got {states['x2']!r}"
        assert states["x1"] == mq.PENDING, f"x1 should still be PENDING, got {states['x1']!r}"
        assert states["x3"] == mq.PENDING, f"x3 should still be PENDING, got {states['x3']!r}"

    def test_only_errors_when_entry_not_found(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """Specifying an unknown assignment_id with --only exits non-zero."""
        _seed_queue([_entry("y1")])

        result = CliRunner().invoke(
            main, ["merge", "--config", str(config_file), "--only", "nonexistent"]
        )
        assert result.exit_code != 0
        assert "no entry found" in result.output.lower() or "no entry found" in (result.stderr or "").lower()

    def test_only_and_order_are_mutually_exclusive(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """--only and --order cannot be combined."""
        _seed_queue([_entry("z1")])

        result = CliRunner().invoke(
            main, ["merge", "--config", str(config_file), "--only", "z1", "--order", "z1"]
        )
        assert result.exit_code != 0
        assert "mutually exclusive" in result.output.lower() or \
               "mutually exclusive" in (result.stderr or "").lower()

    def test_only_dry_run_does_not_merge(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """--only --dry-run shows the plan but does NOT call gh pr merge."""
        _seed_queue([_entry("d1"), _entry("d2")])

        with patch("coord.github_ops.create_pr") as create, \
             patch("coord.github_ops.merge_pr") as merge_fn, \
             patch("coord.github_ops.get_pr_size", return_value=5):
            result = CliRunner().invoke(
                main, ["merge", "--config", str(config_file), "--only", "d1", "--dry-run"]
            )

        assert result.exit_code == 0, result.output
        merge_fn.assert_not_called()
        # Summary line must reference --only.
        assert "only" in result.output.lower()

    def test_only_errors_when_entry_not_pending(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """--only on an already-merged entry exits non-zero with a clear message."""
        _seed_queue([_entry("m1", state=mq.MERGED)])

        result = CliRunner().invoke(
            main, ["merge", "--config", str(config_file), "--only", "m1"]
        )
        assert result.exit_code != 0
        assert "pending" in result.output.lower() or "pending" in (result.stderr or "").lower()

    def test_only_not_pending_error_is_not_silent(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """#1251 (ask 3): the "not PENDING" --only failure must actually write
        to stderr, not just exit 1 with nothing visible there.  Regression for
        the repro in #1251 where this exact path printed nothing to stderr."""
        _seed_queue([_entry("m1", state=mq.MERGED)])

        result = CliRunner().invoke(
            main, ["merge", "--config", str(config_file), "--only", "m1"]
        )
        assert result.exit_code != 0
        assert result.stderr.strip() != "", "expected a non-empty stderr message"
        assert "pending" in result.stderr.lower()


class TestMergeOverrideHumanRequired:
    """#1251: `coord merge --only <id> --override-human-required "<reason>"` —
    the explicit, audited escape hatch for a HUMAN_REQUIRED merge-queue entry
    that no combination of --skip-smoke/--skip-review/--force-merge can
    touch, since human_required represents "automation gave up", not "a gate
    wasn't run"."""

    def test_override_clears_flag_and_merges_in_same_run(
        self, config_file: Path, coord_dir: Path, coord_db, monkeypatch,
    ) -> None:
        """A HUMAN_REQUIRED entry is cleared to PENDING and merged in the
        same invocation, and an audited business-tier row is written."""
        monkeypatch.setenv("COORD_CONFIG", str(config_file))
        _seed_queue([_entry("h1", state=mq.HUMAN_REQUIRED)])

        def fake_create_pr(repo, *, base, head, title, body):
            return {"number": 500, "url": "u/500", "existed": False}

        def fake_merge(repo, number, method="rebase"):
            return True, "ok"

        with patch("coord.github_ops.create_pr", side_effect=fake_create_pr), \
             patch("coord.github_ops.get_pr_size", return_value=10), \
             patch("coord.github_ops.merge_pr", side_effect=fake_merge):
            result = CliRunner().invoke(
                main,
                [
                    "merge", "--config", str(config_file),
                    "--only", "h1",
                    "--override-human-required", "verified clean rebase + green gate",
                ],
            )

        assert result.exit_code == 0, result.output
        assert "cleared HUMAN_REQUIRED" in result.output

        states = {e.assignment_id: e.state for e in mq.load_queue()}
        assert states["h1"] == mq.MERGED, f"expected h1 MERGED, got {states['h1']!r}"

        rows = coord_db.execute(
            "SELECT * FROM audit_log WHERE event_type='human_required_override'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["tier"] == "business"
        assert rows[0]["category"] == "merge"
        assert rows[0]["actor"] == "user"
        assert rows[0]["assignment_id"] == "h1"
        assert "verified clean rebase" in rows[0]["summary"]

    def test_override_rejected_on_non_human_required_entry(
        self, config_file: Path, coord_dir: Path, coord_db,
    ) -> None:
        """The override only applies to HUMAN_REQUIRED entries — a PENDING
        entry is left untouched and no audit row is written."""
        _seed_queue([_entry("x1", state=mq.PENDING)])

        result = CliRunner().invoke(
            main,
            [
                "merge", "--config", str(config_file),
                "--only", "x1",
                "--override-human-required", "not applicable here",
            ],
        )
        assert result.exit_code != 0
        assert "human_required" in result.stderr.lower()

        states = {e.assignment_id: e.state for e in mq.load_queue()}
        assert states["x1"] == mq.PENDING

        rows = coord_db.execute(
            "SELECT * FROM audit_log WHERE event_type='human_required_override'"
        ).fetchall()
        assert rows == []

    def test_override_requires_only(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """--override-human-required without --only is rejected up front —
        it must never silently apply repo-wide."""
        _seed_queue([_entry("h1", state=mq.HUMAN_REQUIRED)])

        result = CliRunner().invoke(
            main,
            [
                "merge", "--config", str(config_file),
                "--override-human-required", "no --only given",
            ],
        )
        assert result.exit_code != 0
        assert "--only" in result.stderr

        states = {e.assignment_id: e.state for e in mq.load_queue()}
        assert states["h1"] == mq.HUMAN_REQUIRED

    def test_override_rejects_empty_reason(
        self, config_file: Path, coord_dir: Path, coord_db,
    ) -> None:
        """#1251-review (minor): an empty/whitespace-only reason is falsy, so
        it must not silently pass through both the "requires --only" check
        and the actual override gate (which would leave the entry stuck
        HUMAN_REQUIRED with zero feedback that the reason was rejected)."""
        _seed_queue([_entry("h1", state=mq.HUMAN_REQUIRED)])

        result = CliRunner().invoke(
            main,
            [
                "merge", "--config", str(config_file),
                "--only", "h1",
                "--override-human-required", "   ",
            ],
        )
        assert result.exit_code != 0
        assert "non-empty reason" in result.stderr.lower()

        states = {e.assignment_id: e.state for e in mq.load_queue()}
        assert states["h1"] == mq.HUMAN_REQUIRED

        rows = coord_db.execute(
            "SELECT * FROM audit_log WHERE event_type='human_required_override'"
        ).fetchall()
        assert rows == []

    def test_override_dry_run_does_not_persist_or_audit(
        self, config_file: Path, coord_dir: Path, coord_db,
    ) -> None:
        """--dry-run previews the clear (mirroring the review/smoke gate
        dry-run convention) but writes neither the state change nor the
        audit row."""
        _seed_queue([_entry("h1", state=mq.HUMAN_REQUIRED)])

        with patch("coord.github_ops.create_pr") as create, \
             patch("coord.github_ops.merge_pr") as merge_fn, \
             patch("coord.github_ops.get_pr_size", return_value=5):
            result = CliRunner().invoke(
                main,
                [
                    "merge", "--config", str(config_file),
                    "--only", "h1", "--dry-run",
                    "--override-human-required", "dry run preview",
                ],
            )

        assert result.exit_code == 0, result.output
        merge_fn.assert_not_called()
        assert "would clear HUMAN_REQUIRED" in result.output

        states = {e.assignment_id: e.state for e in mq.load_queue()}
        assert states["h1"] == mq.HUMAN_REQUIRED, "dry-run must not persist the clear"

        rows = coord_db.execute(
            "SELECT * FROM audit_log WHERE event_type='human_required_override'"
        ).fetchall()
        assert rows == []


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
        assignment_type: str = "work",
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
            type=assignment_type,
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

    def test_auto_enqueues_done_mock_author_when_queue_empty(
        self, config_file: Path, coord_dir: Path, coord_db
    ) -> None:
        """#930 fix: a completed ``type="mock-author"`` (Gate A) assignment
        must auto-enqueue through `coord merge` the same as ordinary work —
        previously the auto-enqueue scan hard-filtered on ``type == "work"``
        so a Gate A branch could never reach the merge queue via this
        command."""
        self._seed_board_with_done_work(
            coord_db,
            issue_number=930,
            assignment_id="ma1",
            branch="ms-5-gate-a",
            assignment_type="mock-author",
        )
        self._seed_issue_state(coord_db, number=930, state="open")

        with patch("coord.github_ops.create_pr") as create, \
             patch("coord.github_ops.merge_pr") as merge_fn, \
             patch("coord.github_ops.get_pr_size", return_value=10):
            create.return_value = {"number": 99, "url": "u/99", "existed": False}
            merge_fn.return_value = (True, "ok")
            result = CliRunner().invoke(main, ["merge", "--config", str(config_file)])

        assert result.exit_code == 0, result.output
        assert "auto-enqueued" in result.output
        assert "#930" in result.output
        create.assert_called_once()
        merge_fn.assert_called_once()

    def test_auto_enqueues_targeting_feature_branch_for_opted_in_milestone(
        self, develop_branch_config_file: Path, coord_dir: Path, coord_db
    ) -> None:
        """#934 review should-fix: `coord merge`'s auto-enqueue milestone-
        aware target_branch (coord/commands/merge.py:966-976) had no test —
        the "merge targets the right base" seam the issue explicitly named.
        Repo opted into the git model + issue tagged to a milestone → PR
        opened against feature/ms-NN, not default_branch."""
        self._seed_board_with_done_work(coord_db, issue_number=934)
        self._seed_issue_state(coord_db, number=934, state="open")

        with patch("coord.github_ops.create_pr") as create, \
             patch("coord.github_ops.merge_pr") as merge_fn, \
             patch("coord.github_ops.get_pr_size", return_value=10), \
             patch(
                 "coord.github_ops.get_issue",
                 return_value={"milestone": {"number": 9, "title": "M9"}},
             ):
            create.return_value = {"number": 99, "url": "u/99", "existed": False}
            merge_fn.return_value = (True, "ok")
            result = CliRunner().invoke(
                main, ["merge", "--config", str(develop_branch_config_file)]
            )

        assert result.exit_code == 0, result.output
        assert "auto-enqueued" in result.output
        assert "feature/ms-9" in result.output
        create.assert_called_once()
        assert create.call_args.kwargs["base"] == "feature/ms-9"

    def test_auto_enqueues_targeting_default_branch_when_no_milestone(
        self, develop_branch_config_file: Path, coord_dir: Path, coord_db
    ) -> None:
        """Opted-in repo, but this issue isn't tagged to a milestone — falls
        back to default_branch, same as an un-opted-in repo."""
        self._seed_board_with_done_work(coord_db, issue_number=935)
        self._seed_issue_state(coord_db, number=935, state="open")

        with patch("coord.github_ops.create_pr") as create, \
             patch("coord.github_ops.merge_pr") as merge_fn, \
             patch("coord.github_ops.get_pr_size", return_value=10), \
             patch("coord.github_ops.get_issue", return_value={"milestone": None}):
            create.return_value = {"number": 99, "url": "u/99", "existed": False}
            merge_fn.return_value = (True, "ok")
            result = CliRunner().invoke(
                main, ["merge", "--config", str(develop_branch_config_file)]
            )

        assert result.exit_code == 0, result.output
        create.assert_called_once()
        assert create.call_args.kwargs["base"] == "main"

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

    def test_stale_merged_entry_on_different_branch_does_not_block_enqueue(
        self, config_file: Path, coord_dir: Path, coord_db
    ) -> None:
        """#1150: a MERGED queue entry from a prior, *different* assignment
        on a *different* branch for the same issue must not permanently
        block a fresh assignment from being enqueued — the old issue-level
        ``already_merged`` shortcut conflated "this issue has ever had a
        merge" with "this exact branch/commit is already merged", which
        silently blocked legitimate re-merges on a reused branch
        (``--fix-of``/``--force``). Whether the *new* assignment's own
        branch is actually terminal is decided later by the commit-aware
        ``work_is_terminal`` gate (#525), not by this issue-level history."""
        self._seed_board_with_done_work(
            coord_db, issue_number=55, assignment_id="newer-attempt",
            branch="issue-55-newer-attempt",
        )
        self._seed_issue_state(coord_db, number=55, state="open")
        # Existing MERGED entry for #55 from a prior, unrelated assignment/branch.
        mq.save_queue([_entry("older-attempt", state=mq.MERGED)])
        # Patch the issue number on the seeded merged entry to 55.
        coord_db.execute(
            "UPDATE merge_queue SET issue_number=55 WHERE assignment_id='older-attempt'"
        )
        coord_db.commit()

        with patch("coord.github_ops.create_pr") as create, \
             patch("coord.github_ops.merge_pr") as merge_fn, \
             patch("coord.github_ops.get_pr_size", return_value=10):
            create.return_value = {"number": 100, "url": "u/100", "existed": False}
            merge_fn.return_value = (True, "ok")
            result = CliRunner().invoke(main, ["merge", "--config", str(config_file)])

        assert result.exit_code == 0, result.output
        assert "auto-enqueued" in result.output
        assert "#55" in result.output
        create.assert_called_once()
        # The historical MERGED entry is untouched.
        states = {x.assignment_id: x.state for x in mq.load_queue()}
        assert states["older-attempt"] == mq.MERGED

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

    def test_terminal_work_not_enqueued(
        self, config_file: Path, coord_dir: Path, coord_db
    ) -> None:
        """#525: done-work whose issue is closed OR PR is already merged on
        GitHub must be skipped in the auto-enqueue loop.  work_is_terminal
        returning True → no enqueue, no PR opened."""
        self._seed_board_with_done_work(
            coord_db, issue_number=525, assignment_id="w525",
            branch="issue-525-fix",
        )
        self._seed_issue_state(coord_db, number=525, state="open")

        with patch(
            "coord.github_ops.list_remote_branch_names",
            return_value={"main", "issue-525-fix"},
        ), patch(
            "coord.github_ops.work_is_terminal",
            return_value=True,
        ) as terminal_fn, patch(
            "coord.github_ops.create_pr",
        ) as create:
            result = CliRunner().invoke(main, ["merge", "--config", str(config_file)])

        assert result.exit_code == 0, result.output
        assert "auto-enqueued" not in result.output
        create.assert_not_called()
        terminal_fn.assert_called_once()

    def test_non_terminal_work_is_enqueued(
        self, config_file: Path, coord_dir: Path, coord_db
    ) -> None:
        """#525 counterpart: when work_is_terminal returns False the item
        passes the guard and is auto-enqueued normally."""
        self._seed_board_with_done_work(
            coord_db, issue_number=526, assignment_id="w526",
            branch="issue-526-fix",
        )
        self._seed_issue_state(coord_db, number=526, state="open")

        with patch(
            "coord.github_ops.list_remote_branch_names",
            return_value={"main", "issue-526-fix"},
        ), patch(
            "coord.github_ops.work_is_terminal",
            return_value=False,
        ), patch(
            "coord.github_ops.create_pr",
            return_value={"number": 999, "url": "u/999", "existed": False},
        ), patch(
            "coord.github_ops.merge_pr",
            return_value=(True, "ok"),
        ), patch(
            "coord.github_ops.get_pr_size",
            return_value=10,
        ):
            result = CliRunner().invoke(main, ["merge", "--config", str(config_file)])

        assert result.exit_code == 0, result.output
        assert "auto-enqueued" in result.output
        assert "#526" in result.output

    # ── #946: auto-enqueue must be gated on review + test, same as the
    # daemon's enqueue_approved_work.  Prior to the fix, this loop had no
    # gate at all — untested/unreviewed work (#782/#795) reached the queue.

    def test_auto_enqueue_refused_on_failed_test(
        self, config_file: Path, coord_dir: Path, coord_db
    ) -> None:
        """A failed test verdict (and no review) must block auto-enqueue."""
        from coord.models import Assignment, Board
        from coord.state import save_board

        work = Assignment(
            machine_name="laptop", repo_name="api", issue_number=782,
            issue_title="#782", assignment_id="w782", type="work",
            status="done", branch="issue-782-fix", test_state="failed",
        )
        save_board(Board(active=[], completed=[work]))

        with patch(
            "coord.github_ops.list_remote_branch_names",
            return_value={"main", "issue-782-fix"},
        ):
            result = CliRunner().invoke(
                main, ["merge", "--dry-run", "--config", str(config_file)],
            )

        assert result.exit_code == 0, result.output
        assert "auto-enqueued" not in result.output
        assert not any(e.issue_number == 782 for e in mq.load_queue())

    def test_auto_enqueue_refused_with_no_verdict_and_no_review(
        self, config_file: Path, coord_dir: Path, coord_db
    ) -> None:
        """No test verdict at all + reviews required + no review → refused.

        Reviews are enabled for this test (unlike the module-level
        ``config_file`` fixture, which disables them) so both gates are live.
        """
        from coord.models import Assignment, Board
        from coord.state import save_board

        config_file.write_text(CONFIG_YAML.replace(
            "reviews:\n  enabled: false\n", ""
        ))

        work = Assignment(
            machine_name="laptop", repo_name="api", issue_number=795,
            issue_title="#795", assignment_id="w795", type="work",
            status="done", branch="issue-795-fix",
        )
        save_board(Board(active=[], completed=[work]))

        with patch(
            "coord.github_ops.list_remote_branch_names",
            return_value={"main", "issue-795-fix"},
        ):
            result = CliRunner().invoke(
                main, ["merge", "--dry-run", "--config", str(config_file)],
            )

        assert result.exit_code == 0, result.output
        assert "auto-enqueued" not in result.output
        assert not any(e.issue_number == 795 for e in mq.load_queue())

    def test_auto_enqueue_allowed_with_passed_test_and_approved_review(
        self, config_file: Path, coord_dir: Path, coord_db
    ) -> None:
        """Passed test + an approved review on the board → IS enqueued."""
        from coord.models import Assignment, Board
        from coord.state import save_board

        config_file.write_text(CONFIG_YAML.replace(
            "reviews:\n  enabled: false\n", ""
        ))

        work = Assignment(
            machine_name="laptop", repo_name="api", issue_number=947,
            issue_title="#947", assignment_id="w947", type="work",
            status="done", branch="issue-947-fix", test_state="passed",
        )
        review = Assignment(
            machine_name="laptop", repo_name="api", issue_number=947,
            issue_title="#947 review", assignment_id="r947", type="review",
            status="done", branch="issue-947-fix",
            review_of_assignment_id="w947", review_verdict="approve",
        )
        save_board(Board(active=[], completed=[work, review]))

        with patch(
            "coord.github_ops.list_remote_branch_names",
            return_value={"main", "issue-947-fix"},
        ):
            result = CliRunner().invoke(
                main, ["merge", "--dry-run", "--config", str(config_file)],
            )

        assert result.exit_code == 0, result.output
        assert "auto-enqueued" in result.output
        assert any(e.issue_number == 947 for e in mq.load_queue())


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


# ── #779: coord merge --plan ──────────────────────────────────────────────────

class TestMergePlanFlag:
    """#779: `coord merge --plan` prints ranked order + gate status, no side effects."""

    def test_plan_prints_output_format(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """--plan emits a repo→branch header and one ranked line per entry."""
        _seed_queue([_entry("x1", size=14), _entry("x2", size=63)])
        result = CliRunner().invoke(
            main, ["merge", "--config", str(config_file), "--plan"]
        )
        assert result.exit_code == 0, result.output
        # Header: "repo_name → target_branch"
        assert "api → main" in result.output
        # Issue numbers present
        assert "#1" in result.output
        # Rank numbers present
        assert "1." in result.output
        assert "2." in result.output
        # Sizes present
        assert "+14" in result.output
        assert "+63" in result.output
        # Gate status present
        assert "READY" in result.output

    def test_plan_no_side_effects(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """--plan must never open PRs or call merge_pr."""
        _seed_queue([_entry("y1", size=10), _entry("y2", size=20)])
        with patch("coord.github_ops.create_pr") as create, \
             patch("coord.github_ops.merge_pr") as merge_fn, \
             patch("coord.github_ops.get_pr_size") as size_fn:
            result = CliRunner().invoke(
                main, ["merge", "--config", str(config_file), "--plan"]
            )
        assert result.exit_code == 0, result.output
        create.assert_not_called()
        merge_fn.assert_not_called()
        size_fn.assert_not_called()
        # Queue must be unchanged (still PENDING)
        items = mq.load_queue()
        assert all(i.state == mq.PENDING for i in items)

    def test_plan_repo_filter(
        self, tmp_path: Path, coord_dir: Path
    ) -> None:
        """--plan --repo <name> only shows that repo's entries."""
        # Config with two repos.
        cfg_text = """\
repos:
  - name: api
    github: acme/api
    default_branch: main
  - name: lib
    github: acme/lib
    default_branch: main
machines:
  - name: laptop
    host: laptop.tailnet
    repos: [api, lib]
    repo_paths:
      api: /tmp/api
      lib: /tmp/lib
reviews:
  enabled: false
"""
        config_file2 = tmp_path / "coordinator.yml"
        config_file2.write_text(cfg_text)

        api_entry = mq.QueuedMerge(
            assignment_id="api1", repo_name="api", repo_github="acme/api",
            branch="worker/api1", target_branch="main",
            issue_number=10, issue_title="API fix", size=5,
        )
        lib_entry = mq.QueuedMerge(
            assignment_id="lib1", repo_name="lib", repo_github="acme/lib",
            branch="worker/lib1", target_branch="main",
            issue_number=20, issue_title="Lib fix", size=8,
        )
        mq.save_queue([api_entry, lib_entry])

        result = CliRunner().invoke(
            main,
            ["merge", "--config", str(config_file2), "--plan", "--repo", "api"],
        )
        assert result.exit_code == 0, result.output
        assert "api → main" in result.output
        assert "#10" in result.output
        # The lib entry must not appear.
        assert "#20" not in result.output
        assert "lib" not in result.output

    def test_plan_order_override(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """--plan --order <ids> puts those entries first and renumbers ranks.

        Without --order, natural size-ascending sequence is:
          rank 1 → gamma (size=10), rank 2 → beta (size=50), rank 3 → alpha (size=100).
        With --order alpha,..., alpha's size (+100) must appear on the rank-1 line.
        """
        _seed_queue([
            _entry("alpha", size=100),
            _entry("beta",  size=50),
            _entry("gamma", size=10),
        ])
        result = CliRunner().invoke(
            main,
            ["merge", "--config", str(config_file), "--plan", "--order", "alpha,beta,gamma"],
        )
        assert result.exit_code == 0, result.output
        lines = [l for l in result.output.splitlines() if l.strip()]
        # First ranked entry line (starts with "  1.") should have size +100 (alpha).
        rank1_line = next((l for l in lines if l.lstrip().startswith("1.")), None)
        assert rank1_line is not None, f"No rank-1 line in output:\n{result.output}"
        assert "+100" in rank1_line, (
            f"alpha (size=100) should be rank 1 with --order alpha,..., "
            f"got rank-1 line: {rank1_line!r}"
        )

    def test_plan_shows_blocked_status(
        self, config_file: Path, coord_dir: Path, coord_db
    ) -> None:
        """--plan shows BLOCKED with a reason when a gate is not satisfied."""
        from coord.models import Assignment, Board
        from coord.state import save_board

        # Enable the review gate.
        config_file.write_text(CONFIG_YAML.replace(
            "reviews:\n  enabled: false\n", ""
        ))

        work = Assignment(
            machine_name="laptop", repo_name="api", issue_number=301,
            issue_title="#301 needs review", assignment_id="w301",
            type="work", status="done", branch="issue-301-fix",
        )
        save_board(Board(active=[], completed=[work]))
        _seed_queue([_entry("w301")])

        result = CliRunner().invoke(
            main, ["merge", "--config", str(config_file), "--plan"]
        )
        assert result.exit_code == 0, result.output
        assert "BLOCKED" in result.output
        assert "review" in result.output.lower()

    def test_plan_empty_queue(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """--plan on an empty queue prints a clear message and exits cleanly."""
        result = CliRunner().invoke(
            main, ["merge", "--config", str(config_file), "--plan"]
        )
        assert result.exit_code == 0, result.output
        assert "empty" in result.output.lower()


# ── #779-fix: coord merge --plan daemon routing via /board ────────────────────

class TestMergePlanDaemonRouting:
    """#779-fix: --plan fetches merge_plan from /board, never touches /merge.

    Older daemons receive plan=True via /merge but have no show_plan handler
    and fall through to a live merge cycle.  The fix routes --plan through
    /board (merge_plan field present since #776/v0.4.53) instead.
    """

    def _make_plan_payload(self) -> dict:
        """A minimal /board payload that includes a merge_plan list."""
        return {
            "assignments": [],
            "plans": {},
            "round_number": 0,
            "notifications": [],
            "merge_plan": [
                {
                    "assignment_id": "daemon1",
                    "repo_name": "api",
                    "repo_github": "acme/api",
                    "branch": "worker/daemon1",
                    "target_branch": "main",
                    "issue_number": 42,
                    "issue_title": "Daemon fix",
                    "rank": 1,
                    "size": 77,
                    "status": "READY",
                    "reason": None,
                    "enqueued_at": None,
                    "last_attempt": None,
                    "milestone": None,
                },
            ],
        }

    def test_plan_routes_to_board_not_merge(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """When a daemon is configured, --plan fetches /board, never calls /merge."""
        from coord.client import ServiceConfig

        svc = ServiceConfig(url="http://dellserver:7435")
        payload = self._make_plan_payload()

        with (
            patch("coord.client.resolve_board_service", return_value=svc),
            patch("coord.client.fetch_board_payload", return_value=payload) as fetch_mock,
            patch("coord.client.post_record") as post_mock,
        ):
            result = CliRunner().invoke(
                main, ["merge", "--config", str(config_file), "--plan"]
            )

        assert result.exit_code == 0, result.output
        # /board must have been fetched
        fetch_mock.assert_called_once_with(svc)
        # /merge must NOT have been called (old-daemon side-effect guard)
        post_mock.assert_not_called()
        # Plan output present
        assert "#42" in result.output
        assert "+77" in result.output
        assert "READY" in result.output

    def test_plan_daemon_missing_merge_plan_exits_cleanly(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """When /board lacks merge_plan (daemon predates #776) exit with a clear error."""
        from coord.client import ServiceConfig

        svc = ServiceConfig(url="http://dellserver:7435")
        # Payload without merge_plan — simulates a very old daemon.
        old_payload = {"assignments": [], "plans": {}, "round_number": 0,
                       "notifications": []}

        with (
            patch("coord.client.resolve_board_service", return_value=svc),
            patch("coord.client.fetch_board_payload", return_value=old_payload),
            patch("coord.client.post_record") as post_mock,
        ):
            result = CliRunner().invoke(
                main, ["merge", "--config", str(config_file), "--plan"]
            )

        assert result.exit_code != 0
        assert "merge_plan" in result.output or "merge_plan" in (result.stderr or "")
        # /merge must still never be called
        post_mock.assert_not_called()

    def test_plan_daemon_repo_filter_applied_client_side(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """--plan --repo filter is applied to the /board payload on the client."""
        from coord.client import ServiceConfig

        svc = ServiceConfig(url="http://dellserver:7435")
        payload = {
            "assignments": [], "plans": {}, "round_number": 0, "notifications": [],
            "merge_plan": [
                {
                    "assignment_id": "api1", "repo_name": "api",
                    "repo_github": "acme/api", "branch": "w/api1",
                    "target_branch": "main", "issue_number": 10,
                    "issue_title": "API fix", "rank": 1, "size": 5,
                    "status": "READY", "reason": None, "enqueued_at": None,
                    "last_attempt": None, "milestone": None,
                },
                {
                    "assignment_id": "lib1", "repo_name": "lib",
                    "repo_github": "acme/lib", "branch": "w/lib1",
                    "target_branch": "main", "issue_number": 20,
                    "issue_title": "Lib fix", "rank": 2, "size": 8,
                    "status": "READY", "reason": None, "enqueued_at": None,
                    "last_attempt": None, "milestone": None,
                },
            ],
        }

        with (
            patch("coord.client.resolve_board_service", return_value=svc),
            patch("coord.client.fetch_board_payload", return_value=payload),
            patch("coord.client.post_record"),
        ):
            result = CliRunner().invoke(
                main,
                ["merge", "--config", str(config_file), "--plan", "--repo", "api"],
            )

        assert result.exit_code == 0, result.output
        assert "#10" in result.output
        assert "#20" not in result.output
