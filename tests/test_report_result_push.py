"""#949: ``coord report-result --status done`` must push the interactive
worktree's commits so completed work is never stranded.

Root cause of #407 / #782: an interactive fix session committed its work and
declared done via ``coord report-result --status done``.  That path recorded
the outcome but never pushed, ``finalize_interactive_exit`` (which *does* push)
was bypassed by the detached tmux session, and the stale-session reaper skips
already-``done`` rows — so the commit sat unpushed in the worktree and the next
test/review agent tested stale code.  These tests pin the push into the
report-result path.
"""

from __future__ import annotations

import os
import subprocess
from unittest.mock import MagicMock

from click.testing import CliRunner

from coord.commands.review import report_result

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "T",
    "GIT_AUTHOR_EMAIL": "t@e",
    "GIT_COMMITTER_NAME": "T",
    "GIT_COMMITTER_EMAIL": "t@e",
}


def _git(cwd, *args):
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        env={**os.environ, **_GIT_ENV},
        capture_output=True,
        text=True,
        check=True,
    )


def _origin_count(origin):
    r = subprocess.run(
        ["git", "rev-list", "--count", "main"],
        cwd=str(origin),
        capture_output=True,
        text=True,
        check=True,
    )
    return int(r.stdout.strip())


def _setup_worktree_ahead(tmp_path, aid):
    """Bare origin + a worktree at COORD_DIR/worktrees/<aid> one commit ahead."""
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", str(origin)], check=True, capture_output=True
    )
    wt = tmp_path / ".coord" / "worktrees" / aid
    wt.mkdir(parents=True)
    _git(wt, "init", "-b", "main")
    _git(wt, "remote", "add", "origin", str(origin))
    (wt / "a.txt").write_text("one\n")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-m", "init")
    _git(wt, "push", "-u", "origin", "main")
    # second commit — ahead of origin, unpushed (the "fix")
    (wt / "b.txt").write_text("two\n")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-m", "the fix")
    return origin, wt


def _patch_resolution(monkeypatch, tmp_path, aid):
    """Stub out board/config/github so report_result reaches the push locally."""
    import coord.client as client
    import coord.commands.review as review
    import coord.issue_store as issue_store
    import coord.state as state

    monkeypatch.setattr(state, "COORD_DIR", tmp_path / ".coord")
    monkeypatch.setattr(client, "resolve_board_service", lambda: None)
    monkeypatch.setattr(
        state,
        "load_dispatched",
        lambda: [
            {
                "assignment_id": aid,
                "repo_github": "o/r",
                "repo_name": "r",
                "machine_name": "m",
                "issue_number": 407,
            }
        ],
    )
    board = MagicMock()
    board.find_by_id.return_value = None
    monkeypatch.setattr(state, "build_board", lambda: board)
    cfg = MagicMock()
    cfg.repo.return_value = None
    monkeypatch.setattr(review, "_load_config", lambda *a, **k: cfg)
    monkeypatch.setattr(
        issue_store,
        "post_result",
        lambda *a, **k: MagicMock(
            status="done", event="completed", posted=False, error=None
        ),
    )


def test_report_result_done_pushes_stranded_commit(tmp_path, monkeypatch):
    aid = "abc123def456"
    origin, _ = _setup_worktree_ahead(tmp_path, aid)
    assert _origin_count(origin) == 1  # only the pushed init; the fix is local
    _patch_resolution(monkeypatch, tmp_path, aid)

    res = CliRunner().invoke(
        report_result,
        ["--assignment", aid, "--status", "done", "--summary", "done"],
    )

    assert res.exit_code == 0, res.output
    assert _origin_count(origin) == 2, "the fix commit must reach origin"
    assert "pushed" in res.output


def test_report_result_blocked_does_not_push(tmp_path, monkeypatch):
    aid = "blocked999999"
    origin, _ = _setup_worktree_ahead(tmp_path, aid)
    _patch_resolution(monkeypatch, tmp_path, aid)

    res = CliRunner().invoke(
        report_result,
        ["--assignment", aid, "--status", "blocked", "--summary", "wip"],
    )

    assert res.exit_code == 0, res.output
    # 'blocked' work stays local for human inspection — must NOT push.
    assert _origin_count(origin) == 1


def test_report_result_done_no_worktree_is_noop(tmp_path, monkeypatch):
    # A review session has no worktree — the push must be skipped, not crash.
    aid = "noworktree999"
    (tmp_path / ".coord" / "worktrees").mkdir(parents=True)
    _patch_resolution(monkeypatch, tmp_path, aid)

    res = CliRunner().invoke(
        report_result,
        [
            "--assignment",
            aid,
            "--status",
            "done",
            "--summary",
            "approved",
            "--verdict",
            "approve",
        ],
    )

    assert res.exit_code == 0, res.output
