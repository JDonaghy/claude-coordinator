"""Tests for `coord review-reaffirm` (#1488) — the audited escape hatch that
re-points a stale-but-content-changed approved review's `review_head_sha` /
`review_patch_id` to the branch's current head instead of requiring a full
re-review, or leaving the merge queue entirely (the `gh pr merge` workaround
that left no board-visible record at all).

The CLI wrapper's own logic (missing/no-branch/already-fresh/no-candidate/
diff-fetch-failure/over-the-sanity-bound/declined-confirmation guards, and
the success path) is covered here — `find_scoped_review_candidate` /
`has_approved_review`'s own exhaustive coverage already lives in
test_merge_queue.py, so they're exercised through real calls (not mocked)
against small hand-built boards/queues, mirroring how the CLI actually uses
them.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from coord.cli import main
from coord.models import Assignment, Board
from coord import state as state_mod

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
  enabled: true
  reaffirm_max_diff_lines: 20
"""


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(CONFIG_YAML)
    return p


def _work(aid: str = "work-001", **overrides) -> Assignment:
    defaults = dict(
        machine_name="laptop", repo_name="api", issue_number=42,
        issue_title="Some issue", assignment_id=aid, type="work",
        status="done", branch="issue-42-fix",
    )
    defaults.update(overrides)
    return Assignment(**defaults)


def _review(of_aid: str, **overrides) -> Assignment:
    defaults = dict(
        machine_name="laptop", repo_name="api", issue_number=42,
        issue_title="Some issue", assignment_id="review-001", type="review",
        status="done", review_of_assignment_id=of_aid, review_verdict="approve",
        review_head_sha="old-sha", review_patch_id="old-patch",
        dispatched_at=100.0,
    )
    defaults.update(overrides)
    return Assignment(**defaults)


def _conflict_fix(entry_aid: str = "work-001", **overrides) -> Assignment:
    defaults = dict(
        machine_name="laptop", repo_name="api", issue_number=42,
        issue_title="[conflict-fix] Some issue", assignment_id="cf-001",
        type="conflict-fix", status="done", review_of_assignment_id=entry_aid,
        dispatched_at=200.0,
    )
    defaults.update(overrides)
    return Assignment(**defaults)


_SMALL_DIFF = (
    "--- a/foo.py\n+++ b/foo.py\n@@ -1,2 +1,2 @@\n-old line\n+new line\n"
)
_BIG_DIFF = "".join(f"-old{i}\n+new{i}\n" for i in range(50))  # 100 changed lines


def _invoke(*args, input: str | None = None):
    return CliRunner().invoke(main, list(args), input=input)


class TestReviewReaffirmGuards:
    def test_missing_work_assignment_errors(self, config_file: Path, coord_db) -> None:
        result = _invoke(
            "review-reaffirm", "no-such-id", "--reason", "conflict resolution",
            "--config", str(config_file),
        )
        assert result.exit_code != 0
        assert "not found" in result.output

    def test_no_branch_errors(self, config_file: Path, coord_db) -> None:
        work = _work(branch=None)
        state_mod.save_board(Board(active=[], completed=[work]))

        result = _invoke(
            "review-reaffirm", "work-001", "--reason", "conflict resolution",
            "--config", str(config_file),
        )
        assert result.exit_code != 0
        assert "no branch recorded" in result.output

    def test_empty_reason_errors(self, config_file: Path, coord_db) -> None:
        work = _work()
        state_mod.save_board(Board(active=[], completed=[work]))

        result = _invoke(
            "review-reaffirm", "work-001", "--reason", "  ",
            "--config", str(config_file),
        )
        assert result.exit_code != 0
        assert "--reason must not be empty" in result.output

    def test_already_fresh_reports_nothing_to_reaffirm(
        self, config_file: Path, coord_db
    ) -> None:
        """review_head_sha already matches the live branch head — nothing to do."""
        work = _work()
        review = _review("work-001", review_head_sha="cur-sha")
        state_mod.save_board(Board(active=[], completed=[work, review]))

        with patch(
            "coord.github_ops.get_branch_sha", return_value="cur-sha"
        ), patch("coord.state.record_review_reaffirm") as rec:
            result = _invoke(
                "review-reaffirm", "work-001", "--reason", "conflict resolution",
                "--config", str(config_file),
            )
        assert result.exit_code == 0
        assert "nothing to reaffirm" in result.output
        rec.assert_not_called()

    def test_no_reaffirmable_candidate_errors(
        self, config_file: Path, coord_db
    ) -> None:
        """No approved review exists at all — a full review is required."""
        work = _work()
        state_mod.save_board(Board(active=[], completed=[work]))

        with patch("coord.github_ops.get_branch_sha", return_value="cur-sha"):
            result = _invoke(
                "review-reaffirm", "work-001", "--reason", "conflict resolution",
                "--config", str(config_file),
            )
        assert result.exit_code != 0
        assert "no reaffirmable approval found" in result.output

    def test_diff_fetch_failure_errors(self, config_file: Path, coord_db) -> None:
        work = _work()
        review = _review("work-001", review_head_sha="old-sha", review_patch_id="old-patch")
        state_mod.save_board(Board(active=[], completed=[work, review]))

        with patch("coord.github_ops.get_branch_sha", return_value="cur-sha"), \
             patch("coord.github_ops.get_branch_patch_id", return_value="cur-patch"), \
             patch("coord.github_ops.get_compare_diff", return_value=None):
            result = _invoke(
                "review-reaffirm", "work-001", "--reason", "conflict resolution",
                "--config", str(config_file),
            )
        assert result.exit_code != 0
        assert "could not fetch the diff" in result.output

    def test_diff_exceeding_sanity_bound_refuses(
        self, config_file: Path, coord_db
    ) -> None:
        """reaffirm_max_diff_lines: 20 in the fixture config; _BIG_DIFF is 100."""
        work = _work()
        review = _review("work-001", review_head_sha="old-sha", review_patch_id="old-patch")
        state_mod.save_board(Board(active=[], completed=[work, review]))

        with patch("coord.github_ops.get_branch_sha", return_value="cur-sha"), \
             patch("coord.github_ops.get_branch_patch_id", return_value="cur-patch"), \
             patch("coord.github_ops.get_compare_diff", return_value=_BIG_DIFF), \
             patch("coord.state.record_review_reaffirm") as rec:
            result = _invoke(
                "review-reaffirm", "--yes", "work-001", "--reason", "conflict resolution",
                "--config", str(config_file),
            )
        assert result.exit_code != 0
        assert "reaffirm_max_diff_lines" in result.output
        rec.assert_not_called()

    def test_declined_confirmation_aborts(self, config_file: Path, coord_db) -> None:
        work = _work()
        review = _review("work-001", review_head_sha="old-sha", review_patch_id="old-patch")
        state_mod.save_board(Board(active=[], completed=[work, review]))

        with patch("coord.github_ops.get_branch_sha", return_value="cur-sha"), \
             patch("coord.github_ops.get_branch_patch_id", return_value="cur-patch"), \
             patch("coord.github_ops.get_compare_diff", return_value=_SMALL_DIFF), \
             patch("coord.state.record_review_reaffirm") as rec:
            result = _invoke(
                "review-reaffirm", "work-001", "--reason", "conflict resolution",
                "--config", str(config_file), input="n\n",
            )
        assert result.exit_code != 0
        assert "aborted" in result.output
        rec.assert_not_called()


class TestReviewReaffirmConflictFixGuardrail:
    """#1488 review round 1: `find_scoped_review_candidate` alone doesn't
    establish "voided ONLY by a content-changing rebase" — that guarantee comes
    from pairing it with `only_conflict_fix_since_review`, exactly as the
    automated scoped dispatcher does. The CLI splits the guardrail's two False
    reasons: intervening work ⇒ hard refuse; unattributed delta ⇒ loud warn."""

    @staticmethod
    def _fix_round(**overrides) -> Assignment:
        defaults = dict(
            machine_name="laptop", repo_name="api", issue_number=42,
            issue_title="[fix-1] Some issue", assignment_id="fix-001",
            type="work", status="done", branch="issue-42-fix",
            review_of_assignment_id="work-001", dispatched_at=250.0,
        )
        defaults.update(overrides)
        return Assignment(**defaults)

    def test_intervening_fix_round_is_refused(
        self, config_file: Path, coord_db
    ) -> None:
        """A bounce round dispatched AFTER the approval is new logic the
        approval never saw — refused outright, no override flag, even though
        the delta is well under reaffirm_max_diff_lines."""
        work = _work(dispatched_at=10.0)
        review = _review("work-001", dispatched_at=100.0)
        fix = self._fix_round()
        state_mod.save_board(Board(active=[], completed=[work, review, fix]))

        with patch("coord.github_ops.get_branch_sha", return_value="cur-sha"), \
             patch("coord.github_ops.get_branch_patch_id", return_value="cur-patch"), \
             patch("coord.github_ops.get_compare_diff", return_value=_SMALL_DIFF), \
             patch("coord.state.record_review_reaffirm") as rec:
            result = _invoke(
                "review-reaffirm", "--yes", "work-001", "--reason",
                "conflict resolution", "--config", str(config_file),
            )
        assert result.exit_code != 0
        assert "dispatched" in result.output
        assert "fix-001" in result.output
        assert "not a mechanical conflict resolution" in result.output
        rec.assert_not_called()

    def test_conflict_fix_only_reports_attribution_and_no_warning(
        self, config_file: Path, coord_db
    ) -> None:
        work = _work(dispatched_at=10.0)
        review = _review("work-001", dispatched_at=100.0)
        cf = _conflict_fix("work-001")
        state_mod.save_board(Board(active=[], completed=[work, review, cf]))

        with patch("coord.github_ops.get_branch_sha", return_value="cur-sha"), \
             patch("coord.github_ops.get_branch_patch_id", return_value="cur-patch"), \
             patch("coord.github_ops.get_compare_diff", return_value=_SMALL_DIFF):
            result = _invoke(
                "review-reaffirm", "--yes", "work-001", "--reason",
                "conflict resolution", "--config", str(config_file),
            )
        assert result.exit_code == 0, result.output
        assert "a completed conflict-fix accounts for this delta" in result.output
        assert "WARNING" not in result.output

        audit = coord_db.execute(
            "SELECT details_json FROM audit_log WHERE assignment_id='review-001'"
        ).fetchone()
        assert '"conflict_fix_only": true' in audit["details_json"]

    def test_unattributed_delta_warns_loudly_but_proceeds(
        self, config_file: Path, coord_db
    ) -> None:
        """No coord-tracked conflict-fix (the operator rebased by hand — the
        gap this escape hatch exists to fill): warn, don't refuse."""
        work = _work(dispatched_at=10.0)
        review = _review("work-001", dispatched_at=100.0)
        state_mod.save_board(Board(active=[], completed=[work, review]))

        with patch("coord.github_ops.get_branch_sha", return_value="cur-sha"), \
             patch("coord.github_ops.get_branch_patch_id", return_value="cur-patch"), \
             patch("coord.github_ops.get_compare_diff", return_value=_SMALL_DIFF):
            result = _invoke(
                "review-reaffirm", "--yes", "work-001", "--reason",
                "hand-resolved rebase", "--config", str(config_file),
            )
        assert result.exit_code == 0, result.output
        assert "UNATTRIBUTED" in result.output
        assert "WARNING" in result.output
        assert "vouching for this diff yourself" in result.output

        audit = coord_db.execute(
            "SELECT details_json FROM audit_log WHERE assignment_id='review-001'"
        ).fetchone()
        assert '"conflict_fix_only": false' in audit["details_json"]


class TestCountDiffChangedLines:
    """The delta bound is the one guarantee that holds unconditionally (no
    override flag), so its line counter must never UNDER-count."""

    @staticmethod
    def _count(text: str) -> int:
        from coord.commands.review import _count_diff_changed_lines
        return _count_diff_changed_lines(text)

    def test_skips_file_headers(self) -> None:
        assert self._count(_SMALL_DIFF) == 2

    def test_counts_content_lines_that_look_like_headers(self) -> None:
        """`++counter` renders as `+++counter`; a removed `---` YAML separator
        renders as `----`. Both are content — a bare startswith() test dropped
        them and undercounted the delta against reaffirm_max_diff_lines."""
        diff = (
            "diff --git a/a.yml b/a.yml\n"
            "--- a/a.yml\n"
            "+++ b/a.yml\n"
            "@@ -1,3 +1,3 @@\n"
            "----\n"          # removed a line whose text is '---'
            "-  x: 1\n"
            "+++counter\n"    # added a line whose text is '++counter'
            "+  x: 2\n"
        )
        assert self._count(diff) == 4

    def test_multi_file_headers_still_skipped(self) -> None:
        diff = (
            "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n"
            "@@ -1 +1 @@\n-a\n+b\n"
            "diff --git a/b.py b/b.py\n--- a/b.py\n+++ b/b.py\n"
            "@@ -1 +1 @@\n-c\n+d\n"
        )
        assert self._count(diff) == 4

    def test_new_file_header_skipped(self) -> None:
        diff = (
            "diff --git a/n.py b/n.py\nnew file mode 100644\n"
            "--- /dev/null\n+++ b/n.py\n@@ -0,0 +1,2 @@\n+one\n+two\n"
        )
        assert self._count(diff) == 2

    def test_unrecognizable_input_overcounts_rather_than_under(self) -> None:
        """Fail closed: with no `@@` at all the counter still counts every
        +/- line rather than silently reporting a smaller delta."""
        assert self._count(_BIG_DIFF) == 100


class TestReviewReaffirmSuccess:
    def test_success_with_yes_writes_and_audits(
        self, config_file: Path, coord_db
    ) -> None:
        work = _work()
        review = _review("work-001", review_head_sha="old-sha", review_patch_id="old-patch")
        state_mod.save_board(Board(active=[], completed=[work, review]))

        with patch("coord.github_ops.get_branch_sha", return_value="cur-sha"), \
             patch("coord.github_ops.get_branch_patch_id", return_value="cur-patch"), \
             patch("coord.github_ops.get_compare_diff", return_value=_SMALL_DIFF):
            result = _invoke(
                "review-reaffirm", "--yes", "work-001", "--reason",
                "conflict resolution: merged filters, suite green",
                "--config", str(config_file),
            )
        assert result.exit_code == 0, result.output
        assert "Reaffirmed" in result.output

        row = coord_db.execute(
            "SELECT review_head_sha, review_patch_id, review_verdict FROM "
            "assignments WHERE assignment_id='review-001'"
        ).fetchone()
        assert row["review_head_sha"] == "cur-sha"
        assert row["review_patch_id"] == "cur-patch"
        assert row["review_verdict"] == "approve"

        audit = coord_db.execute(
            "SELECT event_type, category, summary, details_json FROM audit_log "
            "WHERE assignment_id='review-001'"
        ).fetchone()
        assert audit["event_type"] == "review_reaffirmed"
        assert audit["category"] == "review"
        assert "conflict resolution" in audit["summary"]
        assert "conflict resolution" in audit["details_json"]

    def test_success_confirmed_interactively(
        self, config_file: Path, coord_db
    ) -> None:
        work = _work()
        review = _review("work-001", review_head_sha="old-sha", review_patch_id="old-patch")
        state_mod.save_board(Board(active=[], completed=[work, review]))

        with patch("coord.github_ops.get_branch_sha", return_value="cur-sha"), \
             patch("coord.github_ops.get_branch_patch_id", return_value="cur-patch"), \
             patch("coord.github_ops.get_compare_diff", return_value=_SMALL_DIFF):
            result = _invoke(
                "review-reaffirm", "work-001", "--reason", "conflict resolution",
                "--config", str(config_file), input="y\n",
            )
        assert result.exit_code == 0, result.output
        row = coord_db.execute(
            "SELECT review_head_sha FROM assignments WHERE assignment_id='review-001'"
        ).fetchone()
        assert row["review_head_sha"] == "cur-sha"


def test_record_review_reaffirm_local_raises_for_unknown_assignment(coord_db) -> None:
    with pytest.raises(ValueError, match="no-such-review"):
        state_mod._record_review_reaffirm_local(
            review_assignment_id="no-such-review",
            new_head_sha="new-sha",
            new_patch_id=None,
            reason="conflict resolution",
        )


def test_record_review_reaffirm_local_rejects_non_review_assignment(coord_db) -> None:
    """Defense in depth: the daemon's POST /review-reaffirm takes an arbitrary
    id, so a `work` row must not get review anchors stamped onto it (plus a
    misleading "Review reaffirmed" audit entry)."""
    state_mod.save_board(Board(active=[], completed=[_work("work-001")]))

    with pytest.raises(ValueError, match="not 'review'"):
        state_mod._record_review_reaffirm_local(
            review_assignment_id="work-001",
            new_head_sha="new-sha",
            new_patch_id=None,
            reason="conflict resolution",
        )

    row = coord_db.execute(
        "SELECT review_head_sha FROM assignments WHERE assignment_id='work-001'"
    ).fetchone()
    assert row["review_head_sha"] is None
    assert coord_db.execute(
        "SELECT COUNT(*) c FROM audit_log WHERE assignment_id='work-001'"
    ).fetchone()["c"] == 0
