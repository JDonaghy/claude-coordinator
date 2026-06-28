"""Black-box tests for the remote interactive launch auto-prune-and-retry path (#759).

When a dead orphan worktree holds the issue's branch, the launcher now
attempts a safety-gated auto-prune before giving up:

  1. dead + clean + pushed  → auto-prune succeeds → retry → session launched
  2. dirty orphan           → NOT auto-pruned → manual prune command printed
  3. unpushed orphan        → NOT auto-pruned → manual prune command printed
  4. live holder            → unchanged "reattach first" message (never auto-killed)

Scenarios (a) and (b/c) are tested for the ``--fix-of`` path (the case that
surfaced the bug in #737) and for the plain work path.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from coord.cli import main


# ── Config + seed helpers ─────────────────────────────────────────────────────

CONFIG_YAML_REMOTE = """\
repos:
  - name: api
    github: acme/api
    default_branch: main
machines:
  - name: precision
    host: precision.tailnet
    repos: [api]
    repo_paths:
      api: ~/src/api
"""


@pytest.fixture
def remote_cfg(tmp_path: Path) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(CONFIG_YAML_REMOTE)
    return p


def _seed_fix_board(work_id: str, review_id: str, branch: str) -> None:
    """Seed a completed work + request-changes review for --fix-of tests."""
    from coord.models import Assignment, Board, Repo
    from coord.state import save_board

    work = Assignment(
        machine_name="precision",
        repo_name="api",
        issue_number=1,
        issue_title="Fix bug",
        assignment_id=work_id,
        status="done",
        branch=branch,
        type="work",
        dispatched_at=0.0,
        finished_at=1.0,
    )
    review = Assignment(
        machine_name="precision",
        repo_name="api",
        issue_number=1,
        issue_title="[review] Fix bug",
        assignment_id=review_id,
        status="done",
        branch=branch,
        type="review",
        review_of_assignment_id=work_id,
        review_verdict="request-changes",
        dispatched_at=2.0,
        finished_at=3.0,
    )
    board = Board(
        repos=[Repo(name="api", github="acme/api")],
        machines=[],
        active=[],
        completed=[work, review],
    )
    save_board(board)


def _make_finalize_result(*, push_ok: bool = True) -> Any:
    from coord.interactive import InteractiveFinalizeResult

    return InteractiveFinalizeResult(
        terminal_status="done" if push_ok else "failed",
        commits_ahead=1 if push_ok else None,
        push_ok=push_ok,
        push_error=None if push_ok else "no such directory",
        already_recorded=False,
    )


_HOLDER_PATH = "/home/john/.coord/worktrees/deadbeefcafe"


# ── Scenario (a): dead + clean + pushed → auto-prune + retry ─────────────────


class TestAutoprune:
    """Dead, clean, fully-pushed orphan is auto-pruned and the launch retried."""

    def test_fix_of_dead_clean_pushed_orphan_auto_pruned(
        self, remote_cfg: Path,
    ) -> None:
        """--fix-of: a dead clean pushed orphan is auto-pruned and the launch
        retried; the session succeeds on retry without any manual intervention."""
        _seed_fix_board("work-ap1", "rev-ap1", "issue-1-fix-bug")

        # First launch fails (exit 1, worktree never created).
        # Retry succeeds (exit 0).
        tmux_returns = iter([1, 0])

        def _fake_tmux(argv: Any, briefing: Any, sname: Any, **kw: Any) -> int:
            return next(tmux_returns)

        remove_calls: list[tuple[str, str, str]] = []

        def _fake_remove(ssh_target: str, remote_repo_sh: str, wt: str, **kw: Any) -> bool:
            remove_calls.append((ssh_target, remote_repo_sh, wt))
            return True

        # All _tmux_alive calls return False (new session dead, no live attachment).
        with patch("coord.github_ops.get_issue",
                   return_value={"title": "Fix bug", "body": "b"}), \
             patch("coord.interactive._launch_via_tmux", side_effect=_fake_tmux), \
             patch("coord.interactive.tmux_session_alive", return_value=False), \
             patch("coord.interactive.remote_worktree_exists", return_value=False), \
             patch("coord.interactive.find_remote_branch_holder",
                   return_value=_HOLDER_PATH), \
             patch("coord.interactive._remote_orphan_is_safe_to_prune",
                   return_value=True), \
             patch("coord.interactive._remote_worktree_remove",
                   side_effect=_fake_remove), \
             patch("coord.interactive.finalize_remote_interactive_exit",
                   return_value=_make_finalize_result(push_ok=True)):
            result = CliRunner().invoke(
                main,
                ["assign", "precision", "api", "1",
                 "--config", str(remote_cfg),
                 "--interactive", "--fix-of", "rev-ap1"],
            )

        out = result.output

        # _remote_worktree_remove must have been called exactly once (auto-prune).
        assert len(remove_calls) == 1, (
            f"expected _remote_worktree_remove called once; got {remove_calls!r}"
        )
        assert remove_calls[0][2] == _HOLDER_PATH, (
            f"wrong worktree path pruned: {remove_calls[0][2]!r}"
        )

        # The output must confirm the auto-prune happened.
        assert "auto-prun" in out, (
            f"expected 'auto-prun' in output to confirm cleanup; got:\n{out}"
        )

        # Must NOT print the manual "prune it first" command.
        assert "prune it first" not in out, (
            f"manual prune command must not appear on auto-prune success; got:\n{out}"
        )
        # Must NOT print "setup failed" / "already checked out".
        assert "setup failed" not in out, out

    def test_fix_of_auto_prune_retry_session_still_alive(
        self, remote_cfg: Path,
    ) -> None:
        """After auto-prune the retry may start a session that is still running
        (user detached). The launcher must exit 0 with the reattach hint."""
        _seed_fix_board("work-ap2", "rev-ap2", "issue-1-fix-bug")

        # First call fails (orphan blocks), retry succeeds (exit 0).
        tmux_launch_returns = iter([1, 0])

        def _fake_tmux(argv: Any, briefing: Any, sname: Any, **kw: Any) -> int:
            return next(tmux_launch_returns)

        # tmux_session_alive:
        # call 1 → main session after first launch (dead)
        # call 2 → holder session (dead → orphan)
        # call 3 → main session after retry (alive → detached)
        alive_seq = iter([False, False, True])

        def _fake_alive(sname: str, **_kw: Any) -> bool:
            return next(alive_seq)

        with patch("coord.github_ops.get_issue",
                   return_value={"title": "Fix bug", "body": "b"}), \
             patch("coord.interactive._launch_via_tmux", side_effect=_fake_tmux), \
             patch("coord.interactive.tmux_session_alive", side_effect=_fake_alive), \
             patch("coord.interactive.remote_worktree_exists", return_value=False), \
             patch("coord.interactive.find_remote_branch_holder",
                   return_value=_HOLDER_PATH), \
             patch("coord.interactive._remote_orphan_is_safe_to_prune",
                   return_value=True), \
             patch("coord.interactive._remote_worktree_remove", return_value=True):
            result = CliRunner().invoke(
                main,
                ["assign", "precision", "api", "1",
                 "--config", str(remote_cfg),
                 "--interactive", "--fix-of", "rev-ap2"],
            )

        assert result.exit_code == 0, result.output
        out = result.output
        assert "auto-prun" in out, out
        assert "session still running" in out or "reattach" in out, out


# ── Scenario (b/c): dirty or unpushed → manual prune message ─────────────────


class TestAutopruneSkippedWhenUnsafe:
    """When the orphan is dirty or has unpushed commits, auto-prune is skipped
    and the existing manual-command message is printed."""

    def test_fix_of_dirty_orphan_prints_manual_command(
        self, remote_cfg: Path,
    ) -> None:
        """Dirty orphan (uncommitted changes) → safety check fails → manual command."""
        _seed_fix_board("work-ap3", "rev-ap3", "issue-1-fix-bug")

        remove_calls: list[Any] = []

        with patch("coord.github_ops.get_issue",
                   return_value={"title": "Fix bug", "body": "b"}), \
             patch("coord.interactive._launch_via_tmux", return_value=1), \
             patch("coord.interactive.tmux_session_alive", return_value=False), \
             patch("coord.interactive.remote_worktree_exists", return_value=False), \
             patch("coord.interactive.find_remote_branch_holder",
                   return_value=_HOLDER_PATH), \
             patch("coord.interactive._remote_orphan_is_safe_to_prune",
                   return_value=False), \
             patch("coord.interactive._remote_worktree_remove",
                   side_effect=lambda *a, **kw: remove_calls.append(a) or True), \
             patch("coord.interactive.finalize_remote_interactive_exit",
                   return_value=_make_finalize_result(push_ok=False)):
            result = CliRunner().invoke(
                main,
                ["assign", "precision", "api", "1",
                 "--config", str(remote_cfg),
                 "--interactive", "--fix-of", "rev-ap3"],
            )

        out = result.output

        # _remote_worktree_remove must NOT have been called (not safe to prune).
        assert remove_calls == [], (
            f"_remote_worktree_remove must not be called on unsafe orphan; calls={remove_calls!r}"
        )

        # Manual prune command must be present.
        assert "already checked out" in out, out
        assert _HOLDER_PATH in out, out
        assert "prune" in out or "worktree remove" in out, out

    def test_fix_of_unpushed_orphan_prints_manual_command(
        self, remote_cfg: Path,
    ) -> None:
        """Unpushed orphan (local commits not on origin) → safety check fails
        → manual command, no auto-remove."""
        _seed_fix_board("work-ap4", "rev-ap4", "issue-1-fix-bug")

        remove_calls: list[Any] = []

        # _remote_orphan_is_safe_to_prune returns False (simulates unpushed case).
        with patch("coord.github_ops.get_issue",
                   return_value={"title": "Fix bug", "body": "b"}), \
             patch("coord.interactive._launch_via_tmux", return_value=1), \
             patch("coord.interactive.tmux_session_alive", return_value=False), \
             patch("coord.interactive.remote_worktree_exists", return_value=False), \
             patch("coord.interactive.find_remote_branch_holder",
                   return_value=_HOLDER_PATH), \
             patch("coord.interactive._remote_orphan_is_safe_to_prune",
                   return_value=False), \
             patch("coord.interactive._remote_worktree_remove",
                   side_effect=lambda *a, **kw: remove_calls.append(a) or True), \
             patch("coord.interactive.finalize_remote_interactive_exit",
                   return_value=_make_finalize_result(push_ok=False)):
            result = CliRunner().invoke(
                main,
                ["assign", "precision", "api", "1",
                 "--config", str(remote_cfg),
                 "--interactive", "--fix-of", "rev-ap4"],
            )

        out = result.output

        assert remove_calls == [], (
            f"_remote_worktree_remove must not be called; calls={remove_calls!r}"
        )
        assert "already checked out" in out, out
        assert _HOLDER_PATH in out, out
        assert "prune" in out or "worktree remove" in out, out


# ── Scenario (d): live holder → reattach (unchanged) ─────────────────────────


class TestLiveHolderUnchanged:
    """A live holder session must never be auto-killed; the existing reattach
    message must be shown regardless of the #759 changes."""

    def test_fix_of_live_holder_shows_reattach(
        self, remote_cfg: Path,
    ) -> None:
        """Live session holds the branch → 'exit the session first' + reattach."""
        _seed_fix_board("work-ap5", "rev-ap5", "issue-1-fix-bug")

        holder_path = "/home/john/.coord/worktrees/971a1947ad91"

        # _tmux_alive: first call (main session) → False; second call (holder) → True.
        alive_seq = iter([False, True])

        remove_calls: list[Any] = []

        with patch("coord.github_ops.get_issue",
                   return_value={"title": "Fix bug", "body": "b"}), \
             patch("coord.interactive._launch_via_tmux", return_value=1), \
             patch("coord.interactive.tmux_session_alive",
                   side_effect=lambda sname, **_kw: next(alive_seq)), \
             patch("coord.interactive.remote_worktree_exists", return_value=False), \
             patch("coord.interactive.find_remote_branch_holder",
                   return_value=holder_path), \
             patch("coord.interactive._remote_worktree_remove",
                   side_effect=lambda *a, **kw: remove_calls.append(a) or True), \
             patch("coord.interactive.finalize_remote_interactive_exit",
                   return_value=_make_finalize_result(push_ok=False)):
            result = CliRunner().invoke(
                main,
                ["assign", "precision", "api", "1",
                 "--config", str(remote_cfg),
                 "--interactive", "--fix-of", "rev-ap5"],
            )

        out = result.output

        # Auto-prune must NOT be attempted for a live session.
        assert remove_calls == [], (
            f"_remote_worktree_remove must not be called for a live holder; got {remove_calls!r}"
        )

        # Must show the live-session / reattach message.
        assert "already checked out" in out, out
        assert "reattach" in out or "active tmux" in out, out
        # Must NOT show the manual-prune command.
        assert "prune it first" not in out, out
        # Must NOT auto-prune.
        assert "auto-prun" not in out, out


# ── Scenario (a) for the plain work path ─────────────────────────────────────


class TestAutopruneWorkPath:
    """Auto-prune also covers the plain work assignment path (no --fix-of)."""

    def test_work_dead_clean_pushed_orphan_auto_pruned(
        self, remote_cfg: Path,
    ) -> None:
        """Plain work session: dead+clean+pushed orphan auto-pruned, retry succeeds."""
        tmux_returns = iter([1, 0])

        def _fake_tmux(argv: Any, briefing: Any, sname: Any, **kw: Any) -> int:
            return next(tmux_returns)

        remove_calls: list[tuple[str, str, str]] = []

        def _fake_remove(ssh_target: str, remote_repo_sh: str, wt: str, **kw: Any) -> bool:
            remove_calls.append((ssh_target, remote_repo_sh, wt))
            return True

        with patch("coord.github_ops.get_issue",
                   return_value={"title": "New feature", "body": "b"}), \
             patch("coord.claim.find_work_claim", return_value=None), \
             patch("coord.state.record_dispatched"), \
             patch("coord.state.save_board"), \
             patch("coord.state.build_board",
                   return_value=MagicMock(active=[], completed=[])), \
             patch("coord.interactive._launch_via_tmux", side_effect=_fake_tmux), \
             patch("coord.interactive.tmux_session_alive", return_value=False), \
             patch("coord.interactive.remote_worktree_exists", return_value=False), \
             patch("coord.interactive.find_remote_branch_holder",
                   return_value=_HOLDER_PATH), \
             patch("coord.interactive._remote_orphan_is_safe_to_prune",
                   return_value=True), \
             patch("coord.interactive._remote_worktree_remove",
                   side_effect=_fake_remove), \
             patch("coord.interactive.finalize_remote_interactive_exit",
                   return_value=_make_finalize_result(push_ok=True)):
            result = CliRunner().invoke(
                main,
                ["assign", "precision", "api", "42",
                 "--config", str(remote_cfg),
                 "--interactive"],
            )

        out = result.output
        assert len(remove_calls) == 1, (
            f"_remote_worktree_remove must be called once; got {remove_calls!r}"
        )
        assert "auto-prun" in out, out
        assert "prune it first" not in out, out


# ── Unit tests for _remote_orphan_is_safe_to_prune ───────────────────────────


class TestRemoteOrphanIsSafeToPrune:
    """Unit tests for the new safety-gate helper in coord.interactive."""

    def _mk_run(self, stdout: str, returncode: int = 0) -> MagicMock:
        m = MagicMock()
        m.returncode = returncode
        m.stdout = stdout
        m.stderr = ""
        return m

    def test_returns_true_when_clean_and_pushed(self) -> None:
        """Returns True when stdout contains __SAFE."""
        from coord.interactive import _remote_orphan_is_safe_to_prune

        with patch("subprocess.run",
                   return_value=self._mk_run("__SAFE\n")):
            assert _remote_orphan_is_safe_to_prune(
                "host", "$HOME/src/api",
                "/home/john/.coord/worktrees/abc123",
                "issue-42-fix-bug",
            ) is True

    def test_returns_false_when_dirty(self) -> None:
        """Returns False when stdout contains __NOT_SAFE (dirty worktree)."""
        from coord.interactive import _remote_orphan_is_safe_to_prune

        with patch("subprocess.run",
                   return_value=self._mk_run("__NOT_SAFE\n")):
            assert _remote_orphan_is_safe_to_prune(
                "host", "$HOME/src/api",
                "/home/john/.coord/worktrees/abc123",
                "issue-42-fix-bug",
            ) is False

    def test_returns_false_when_unpushed(self) -> None:
        """Returns False when stdout contains __NOT_SAFE (unpushed commits)."""
        from coord.interactive import _remote_orphan_is_safe_to_prune

        with patch("subprocess.run",
                   return_value=self._mk_run("__NOT_SAFE\n")):
            assert _remote_orphan_is_safe_to_prune(
                "host", "$HOME/src/api",
                "/home/john/.coord/worktrees/abc123",
                "issue-42-fix-bug",
            ) is False

    def test_returns_false_on_ssh_error(self) -> None:
        """SSH failure → conservative False (do not auto-prune)."""
        from coord.interactive import _remote_orphan_is_safe_to_prune

        with patch("subprocess.run", side_effect=OSError("connection refused")):
            assert _remote_orphan_is_safe_to_prune(
                "host", "$HOME/src/api",
                "/home/john/.coord/worktrees/abc123",
                "issue-42-fix-bug",
            ) is False

    def test_returns_false_on_timeout(self) -> None:
        """Subprocess timeout → conservative False."""
        import subprocess
        from coord.interactive import _remote_orphan_is_safe_to_prune

        with patch("subprocess.run",
                   side_effect=subprocess.TimeoutExpired(cmd=["ssh"], timeout=15)):
            assert _remote_orphan_is_safe_to_prune(
                "host", "$HOME/src/api",
                "/home/john/.coord/worktrees/abc123",
                "issue-42-fix-bug",
            ) is False

    def test_returns_false_when_output_is_empty(self) -> None:
        """Empty stdout (e.g. SSH dies without output) → False."""
        from coord.interactive import _remote_orphan_is_safe_to_prune

        with patch("subprocess.run", return_value=self._mk_run("")):
            assert _remote_orphan_is_safe_to_prune(
                "host", "$HOME/src/api",
                "/home/john/.coord/worktrees/abc123",
                "issue-42-fix-bug",
            ) is False


# ── Unit tests for _holder_is_base_checkout ───────────────────────────────────


class TestHolderIsBaseCheckout:
    """Unit tests for the #814 base-checkout-detection helper."""

    def test_worktree_path_returns_false(self) -> None:
        """Coord-managed worktree path → False (it IS a worktree, not the base)."""
        from coord.interactive import _holder_is_base_checkout

        assert _holder_is_base_checkout(
            "/home/john/.coord/worktrees/deadbeefcafe"
        ) is False

    def test_worktree_path_with_subdir_returns_false(self) -> None:
        """Deep path under .coord/worktrees → still False."""
        from coord.interactive import _holder_is_base_checkout

        assert _holder_is_base_checkout(
            "/home/john/.coord/worktrees/abc123/subdirectory"
        ) is False

    def test_base_checkout_src_path_returns_true(self) -> None:
        """~/src/<repo> path → True (this IS the base checkout)."""
        from coord.interactive import _holder_is_base_checkout

        assert _holder_is_base_checkout(
            "/home/john/src/claude-coordinator"
        ) is True

    def test_absolute_non_worktree_path_returns_true(self) -> None:
        """Any absolute path not under .coord/worktrees → True."""
        from coord.interactive import _holder_is_base_checkout

        assert _holder_is_base_checkout("/opt/repos/myrepo") is True


# ── Unit tests for _remote_base_checkout_free_branch ─────────────────────────


class TestRemoteBaseCheckoutFreeBranch:
    """Unit tests for the SSH helper that frees the base checkout (#814)."""

    def _mk_run(self, stdout: str, returncode: int = 0) -> MagicMock:
        m = MagicMock()
        m.returncode = returncode
        m.stdout = stdout
        m.stderr = ""
        return m

    def test_returns_true_when_checkout_succeeds(self) -> None:
        from coord.interactive import _remote_base_checkout_free_branch

        with patch("subprocess.run", return_value=self._mk_run("__FREE_DONE\n")):
            assert _remote_base_checkout_free_branch(
                "host", "$HOME/src/api", "main"
            ) is True

    def test_returns_false_when_checkout_fails(self) -> None:
        from coord.interactive import _remote_base_checkout_free_branch

        with patch("subprocess.run", return_value=self._mk_run("__FREE_FAIL\n")):
            assert _remote_base_checkout_free_branch(
                "host", "$HOME/src/api", "main"
            ) is False

    def test_returns_false_on_ssh_error(self) -> None:
        from coord.interactive import _remote_base_checkout_free_branch

        with patch("subprocess.run", side_effect=OSError("connection refused")):
            assert _remote_base_checkout_free_branch(
                "host", "$HOME/src/api", "main"
            ) is False

    def test_returns_false_on_timeout(self) -> None:
        import subprocess as _subprocess
        from coord.interactive import _remote_base_checkout_free_branch

        with patch(
            "subprocess.run",
            side_effect=_subprocess.TimeoutExpired(cmd=["ssh"], timeout=15),
        ):
            assert _remote_base_checkout_free_branch(
                "host", "$HOME/src/api", "main"
            ) is False

    def test_returns_false_when_output_empty(self) -> None:
        from coord.interactive import _remote_base_checkout_free_branch

        with patch("subprocess.run", return_value=self._mk_run("")):
            assert _remote_base_checkout_free_branch(
                "host", "$HOME/src/api", "main"
            ) is False


# ── Scenario (e): base checkout holds branch → free + retry (#814) ───────────


_BASE_CHECKOUT_PATH = "/home/john/src/api"


class TestBaseCheckoutHolder:
    """When the base checkout ~/src/<repo> holds the branch, the launcher must
    free it with ``git checkout <default>`` (NEVER remove it) and then retry.

    This is the #814 fix.  The holder path does NOT contain '.coord/worktrees',
    so _holder_is_base_checkout returns True.
    """

    def test_fix_of_base_checkout_holder_freed_and_retried(
        self, remote_cfg: Path,
    ) -> None:
        """--fix-of: base checkout on issue branch → freed → session retried."""
        _seed_fix_board("work-bc1", "rev-bc1", "issue-1-fix-bug")

        # First launch fails; retry after free succeeds.
        tmux_returns = iter([1, 0])

        def _fake_tmux(argv: Any, briefing: Any, sname: Any, **kw: Any) -> int:
            return next(tmux_returns)

        free_calls: list[tuple[str, str, str]] = []

        def _fake_free(ssh_target: str, remote_repo_sh: str, default_branch: str,
                       **kw: Any) -> bool:
            free_calls.append((ssh_target, remote_repo_sh, default_branch))
            return True

        remove_calls: list[Any] = []

        with patch("coord.github_ops.get_issue",
                   return_value={"title": "Fix bug", "body": "b"}), \
             patch("coord.interactive._launch_via_tmux", side_effect=_fake_tmux), \
             patch("coord.interactive.tmux_session_alive", return_value=False), \
             patch("coord.interactive.remote_worktree_exists", return_value=False), \
             patch("coord.interactive.find_remote_branch_holder",
                   return_value=_BASE_CHECKOUT_PATH), \
             patch("coord.interactive._remote_base_checkout_free_branch",
                   side_effect=_fake_free), \
             patch("coord.interactive._remote_worktree_remove",
                   side_effect=lambda *a, **kw: remove_calls.append(a) or True), \
             patch("coord.interactive.finalize_remote_interactive_exit",
                   return_value=_make_finalize_result(push_ok=True)):
            result = CliRunner().invoke(
                main,
                ["assign", "precision", "api", "1",
                 "--config", str(remote_cfg),
                 "--interactive", "--fix-of", "rev-bc1"],
            )

        out = result.output

        # _remote_base_checkout_free_branch must be called (free the base checkout).
        assert len(free_calls) == 1, (
            f"expected _remote_base_checkout_free_branch called once; got {free_calls!r}"
        )
        assert free_calls[0][2] == "main", (
            f"expected checkout to 'main'; got {free_calls[0][2]!r}"
        )

        # _remote_worktree_remove must NOT be called (base is never pruned).
        assert remove_calls == [], (
            f"_remote_worktree_remove must not be called for base checkout; "
            f"got {remove_calls!r}"
        )

        # Output must mention freeing the base checkout, not "auto-pruning orphan".
        assert "base checkout freed" in out, (
            f"expected 'base checkout freed' in output; got:\n{out}"
        )
        assert "auto-prun" not in out, (
            f"'auto-prun' must not appear for base-checkout case; got:\n{out}"
        )
        # Must NOT say "prune it first".
        assert "prune it first" not in out, out

    def test_fix_of_base_checkout_free_fails_prints_manual_cmd(
        self, remote_cfg: Path,
    ) -> None:
        """If freeing the base checkout fails, a manual command is printed."""
        _seed_fix_board("work-bc2", "rev-bc2", "issue-1-fix-bug")

        remove_calls: list[Any] = []

        with patch("coord.github_ops.get_issue",
                   return_value={"title": "Fix bug", "body": "b"}), \
             patch("coord.interactive._launch_via_tmux", return_value=1), \
             patch("coord.interactive.tmux_session_alive", return_value=False), \
             patch("coord.interactive.remote_worktree_exists", return_value=False), \
             patch("coord.interactive.find_remote_branch_holder",
                   return_value=_BASE_CHECKOUT_PATH), \
             patch("coord.interactive._remote_base_checkout_free_branch",
                   return_value=False), \
             patch("coord.interactive._remote_worktree_remove",
                   side_effect=lambda *a, **kw: remove_calls.append(a) or True), \
             patch("coord.interactive.finalize_remote_interactive_exit",
                   return_value=_make_finalize_result(push_ok=False)):
            result = CliRunner().invoke(
                main,
                ["assign", "precision", "api", "1",
                 "--config", str(remote_cfg),
                 "--interactive", "--fix-of", "rev-bc2"],
            )

        out = result.output

        # _remote_worktree_remove must NOT be called.
        assert remove_calls == [], (
            f"_remote_worktree_remove must not be called for base checkout; "
            f"got {remove_calls!r}"
        )
        # Must show a manual fix command.
        assert "git" in out and "checkout" in out, (
            f"expected manual 'git checkout' command; got:\n{out}"
        )
        # Must NOT say "auto-prun".
        assert "auto-prun" not in out, out

    def test_work_path_base_checkout_holder_freed_and_retried(
        self, remote_cfg: Path,
    ) -> None:
        """Plain work path: base checkout on branch → freed → session retried."""
        tmux_returns = iter([1, 0])

        def _fake_tmux(argv: Any, briefing: Any, sname: Any, **kw: Any) -> int:
            return next(tmux_returns)

        free_calls: list[tuple[str, str, str]] = []

        def _fake_free(ssh_target: str, remote_repo_sh: str, default_branch: str,
                       **kw: Any) -> bool:
            free_calls.append((ssh_target, remote_repo_sh, default_branch))
            return True

        remove_calls: list[Any] = []

        with patch("coord.github_ops.get_issue",
                   return_value={"title": "New feature", "body": "b"}), \
             patch("coord.claim.find_work_claim", return_value=None), \
             patch("coord.state.record_dispatched"), \
             patch("coord.state.save_board"), \
             patch("coord.state.build_board",
                   return_value=MagicMock(active=[], completed=[])), \
             patch("coord.interactive._launch_via_tmux", side_effect=_fake_tmux), \
             patch("coord.interactive.tmux_session_alive", return_value=False), \
             patch("coord.interactive.remote_worktree_exists", return_value=False), \
             patch("coord.interactive.find_remote_branch_holder",
                   return_value=_BASE_CHECKOUT_PATH), \
             patch("coord.interactive._remote_base_checkout_free_branch",
                   side_effect=_fake_free), \
             patch("coord.interactive._remote_worktree_remove",
                   side_effect=lambda *a, **kw: remove_calls.append(a) or True), \
             patch("coord.interactive.finalize_remote_interactive_exit",
                   return_value=_make_finalize_result(push_ok=True)):
            result = CliRunner().invoke(
                main,
                ["assign", "precision", "api", "42",
                 "--config", str(remote_cfg),
                 "--interactive"],
            )

        out = result.output
        assert len(free_calls) == 1, (
            f"_remote_base_checkout_free_branch must be called once; got {free_calls!r}"
        )
        assert remove_calls == [], (
            f"_remote_worktree_remove must not be called; got {remove_calls!r}"
        )
        assert "base checkout freed" in out, out
        assert "auto-prun" not in out, out
