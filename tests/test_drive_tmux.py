"""Tests for `coord drive --tmux` and its companions (#1398).

Covers:
1. Pure helpers: `drive_session_name` / `parse_drive_session_name` round-trip.
2. `list_drive_sessions` — `tmux list-sessions` output → parsed dicts.
3. `launch_drive_in_tmux` — happy path, tmux unavailable, already-driving,
   `tmux new-session` failure.
4. Regression: `coord.interactive.list_coord_tmux_sessions` (assignment
   discovery) excludes `coord-drive-*`, mirroring the `coord-term-*` guard
   in `test_terminal_command.py`.
5. `_rebuild_drive_argv` — every flag round-trips into the re-exec'd argv.
6. CLI: `coord drive --tmux` (happy path, already-driving, tmux missing),
   `coord drive-sessions [--json]`, `coord drive-attach`, `coord drive-stop`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from coord.cli import main
from coord.commands.drive import _rebuild_drive_argv
from coord.drive import (
    DriveError,
    drive_session_name,
    launch_drive_in_tmux,
    list_drive_sessions,
    parse_drive_session_name,
)
from coord.interactive import DRIVE_SESSION_PREFIX, list_coord_tmux_sessions

from .conftest import output_and_stderr


def _completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    m = MagicMock(spec=subprocess.CompletedProcess)
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr
    return m


# ── pure helpers ─────────────────────────────────────────────────────────────


class TestSessionNameBuildParse:
    def test_build(self) -> None:
        assert drive_session_name("myrepo", 42) == "coord-drive-myrepo-42"

    def test_parse_roundtrip(self) -> None:
        assert parse_drive_session_name(drive_session_name("myrepo", 42)) == ("myrepo", 42)

    def test_parse_repo_name_with_hyphens(self) -> None:
        """Repo names may themselves contain hyphens — the trailing numeric
        segment (after the LAST hyphen) anchors the split, not the first."""
        name = drive_session_name("claude-coordinator", 1398)
        assert parse_drive_session_name(name) == ("claude-coordinator", 1398)

    def test_parse_rejects_assignment_session(self) -> None:
        assert parse_drive_session_name("coord-abc123") is None

    def test_parse_rejects_terminal_session(self) -> None:
        assert parse_drive_session_name("coord-term-scratch") is None

    def test_parse_rejects_non_numeric_trailing_segment(self) -> None:
        assert parse_drive_session_name("coord-drive-myrepo-notanumber") is None

    def test_parse_rejects_bare_prefix(self) -> None:
        assert parse_drive_session_name(DRIVE_SESSION_PREFIX) is None

    def test_parse_rejects_unrelated_name(self) -> None:
        assert parse_drive_session_name("some-other-session") is None


# ── list_drive_sessions ───────────────────────────────────────────────────────


class TestListDriveSessions:
    def test_filters_to_drive_prefix(self) -> None:
        stdout = (
            "coord-drive-myrepo-42\t0\n"
            "coord-abc123\t1\n"
            "coord-term-scratch\t0\n"
            "coord-drive-other-repo-7\t1\n"
        )
        with patch("coord.drive.subprocess.run", return_value=_completed(0, stdout)):
            result = list_drive_sessions()
        by_repo_issue = {(e["repo"], e["issue"]) for e in result}
        assert by_repo_issue == {("myrepo", 42), ("other-repo", 7)}

    def test_attached_flag_parsed(self) -> None:
        stdout = "coord-drive-a-1\t0\ncoord-drive-b-2\t1\n"
        with patch("coord.drive.subprocess.run", return_value=_completed(0, stdout)):
            result = list_drive_sessions()
        by_issue = {e["issue"]: e for e in result}
        assert by_issue[1]["attached"] is False
        assert by_issue[2]["attached"] is True

    def test_no_tmux_server_running_returns_empty(self) -> None:
        with patch(
            "coord.drive.subprocess.run",
            return_value=_completed(1, "", "no server running"),
        ):
            assert list_drive_sessions() == []

    def test_subprocess_error_returns_empty(self) -> None:
        with patch(
            "coord.drive.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="tmux", timeout=5.0),
        ):
            assert list_drive_sessions() == []

    def test_malformed_line_ignored(self) -> None:
        with patch("coord.drive.subprocess.run", return_value=_completed(0, "coord-drive-a\n")):
            assert list_drive_sessions() == []


# ── launch_drive_in_tmux ───────────────────────────────────────────────────────


class TestLaunchDriveInTmux:
    def test_happy_path_creates_session(self) -> None:
        with (
            patch("coord.drive.tmux_available", return_value=True),
            patch("coord.drive.tmux_session_alive", return_value=False),
            patch("coord.drive.subprocess.run", return_value=_completed(0)) as run,
        ):
            session = launch_drive_in_tmux(
                ["coord", "drive", "myrepo", "42"], repo="myrepo", issue=42
            )
        assert session == "coord-drive-myrepo-42"
        argv = run.call_args[0][0]
        assert argv == [
            "tmux", "new-session", "-d", "-s", "coord-drive-myrepo-42",
            "coord", "drive", "myrepo", "42",
        ]

    def test_tmux_unavailable_raises(self) -> None:
        with patch("coord.drive.tmux_available", return_value=False):
            with pytest.raises(DriveError, match="tmux is not available"):
                launch_drive_in_tmux(["sleep", "1"], repo="myrepo", issue=42)

    def test_already_driving_raises(self) -> None:
        with (
            patch("coord.drive.tmux_available", return_value=True),
            patch("coord.drive.tmux_session_alive", return_value=True),
        ):
            with pytest.raises(DriveError, match="already driving myrepo #42"):
                launch_drive_in_tmux(["sleep", "1"], repo="myrepo", issue=42)

    def test_new_session_failure_raises(self) -> None:
        with (
            patch("coord.drive.tmux_available", return_value=True),
            patch("coord.drive.tmux_session_alive", return_value=False),
            patch(
                "coord.drive.subprocess.run",
                return_value=_completed(1, "", "some tmux error"),
            ),
        ):
            with pytest.raises(DriveError, match="some tmux error"):
                launch_drive_in_tmux(["sleep", "1"], repo="myrepo", issue=42)


# ── regression: assignment-session discovery excludes coord-drive-* ────────────


class TestAssignmentDiscoveryExcludesDriveSessions:
    def test_list_coord_tmux_sessions_excludes_coord_drive(self) -> None:
        stdout = (
            "coord-abc123\t0\t0\n"
            "coord-drive-myrepo-42\t0\t0\n"
        )
        with patch("coord.interactive.subprocess.run", return_value=_completed(0, stdout)):
            result = list_coord_tmux_sessions()
        names = {e["session_name"] for e in result}
        assert names == {"coord-abc123"}


# ── _rebuild_drive_argv ────────────────────────────────────────────────────────


class TestRebuildDriveArgv:
    def _defaults(self, **overrides) -> dict:
        base = dict(
            machine="",
            model="",
            briefing_file="",
            do_plan=False,
            max_fix_rounds=3,
            skip_test=False,
            repo_path="",
            poll=60.0,
            max_work_retries=1,
            deadline_mins=240.0,
            stall_mins=20.0,
            notify=False,
            accept_advisory=False,
            force_review=False,
            no_merge=False,
            merge_method="rebase",
            max_merge_attempts=3,
            dry_run=False,
            config_path=None,
        )
        base.update(overrides)
        return base

    def test_bare_defaults(self) -> None:
        argv = _rebuild_drive_argv("myrepo", 42, **self._defaults())
        assert argv[:3] == ["drive", "myrepo", "42"]
        # No flags fired for defaults/empty strings, except the always-emitted
        # numeric options.
        for flag in (
            "--machine", "--model", "--briefing-file", "--plan", "--skip-test",
            "--repo-path", "--notify", "--accept-advisory", "--force-review",
            "--no-merge", "--dry-run", "--config",
        ):
            assert flag not in argv
        for flag, value in (
            ("--max-fix-rounds", "3"),
            ("--poll", "60.0"),
            ("--max-work-retries", "1"),
            ("--deadline", "240.0"),
            ("--stall", "20.0"),
            ("--merge-method", "rebase"),
            ("--max-merge-attempts", "3"),
        ):
            i = argv.index(flag)
            assert argv[i + 1] == value

    def test_every_flag_round_trips(self, tmp_path: Path) -> None:
        cfg = tmp_path / "coordinator.yml"
        cfg.write_text("repos: []\nmachines: []\n")
        argv = _rebuild_drive_argv(
            "myrepo",
            42,
            **self._defaults(
                machine="precision",
                model="opus",
                briefing_file="/tmp/briefing.md",
                do_plan=True,
                max_fix_rounds=7,
                skip_test=True,
                repo_path="/tmp/repo",
                poll=5.0,
                max_work_retries=2,
                deadline_mins=30.0,
                stall_mins=4.0,
                notify=True,
                accept_advisory=True,
                force_review=True,
                no_merge=True,
                merge_method="squash",
                max_merge_attempts=5,
                dry_run=True,
                config_path=cfg,
            ),
        )
        for token in (
            "--machine", "precision",
            "--model", "opus",
            "--briefing-file", "/tmp/briefing.md",
            "--plan",
            "--max-fix-rounds", "7",
            "--skip-test",
            "--repo-path", "/tmp/repo",
            "--poll", "5.0",
            "--max-work-retries", "2",
            "--deadline", "30.0",
            "--stall", "4.0",
            "--notify",
            "--accept-advisory",
            "--force-review",
            "--no-merge",
            "--merge-method", "squash",
            "--max-merge-attempts", "5",
            "--dry-run",
            "--config", str(cfg),
        ):
            assert token in argv
        # --tmux must NEVER be re-emitted — the re-exec runs inline inside
        # the tmux session it's already in.
        assert "--tmux" not in argv


# ── CLI: coord drive --tmux ──────────────────────────────────────────────────


class TestDriveTmuxCli:
    def test_happy_path(self, valid_config_path: Path) -> None:
        with patch(
            "coord.drive.launch_drive_in_tmux", return_value="coord-drive-somerepo-1"
        ) as launch:
            result = CliRunner().invoke(
                main,
                ["drive", "--config", str(valid_config_path), "--tmux", "somerepo", "1"],
            )
        out = output_and_stderr(result)
        assert result.exit_code == 0, out
        assert "coord-drive-somerepo-1" in out
        assert "coord drive-attach somerepo 1" in out
        assert "coord drive-stop somerepo 1" in out
        argv = launch.call_args[0][0]
        i = argv.index("drive")
        assert argv[i + 1 : i + 3] == ["somerepo", "1"]
        assert "--tmux" not in argv
        assert launch.call_args.kwargs == {"repo": "somerepo", "issue": 1}

    def test_already_driving_surfaces_drive_error(self, valid_config_path: Path) -> None:
        with patch(
            "coord.drive.launch_drive_in_tmux",
            side_effect=DriveError("already driving somerepo #1 (...)", 2),
        ):
            result = CliRunner().invoke(
                main,
                ["drive", "--config", str(valid_config_path), "--tmux", "somerepo", "1"],
            )
        out = output_and_stderr(result)
        assert result.exit_code == 2
        assert "already driving" in out

    def test_no_daemon_call_happens_before_launch(self, valid_config_path: Path) -> None:
        """`--tmux` must short-circuit BEFORE `_load_config`/`Driver` construction
        touches the board — the whole point is a near-instant return."""
        with (
            patch("coord.drive.launch_drive_in_tmux", return_value="coord-drive-somerepo-1"),
            patch("coord.drive.Driver.run", side_effect=AssertionError("must not run inline")),
        ):
            result = CliRunner().invoke(
                main,
                ["drive", "--config", str(valid_config_path), "--tmux", "somerepo", "1"],
            )
        assert result.exit_code == 0, output_and_stderr(result)


# ── CLI: coord drive-sessions ────────────────────────────────────────────────


class TestDriveSessionsCli:
    def test_json_output(self) -> None:
        stdout = "coord-drive-myrepo-42\t0\n"
        with patch("coord.drive.subprocess.run", return_value=_completed(0, stdout)):
            result = CliRunner().invoke(main, ["drive-sessions", "--json"])
        assert result.exit_code == 0
        assert '"repo": "myrepo"' in result.output
        assert '"issue": 42' in result.output

    def test_human_output_empty(self) -> None:
        with patch("coord.drive.subprocess.run", return_value=_completed(0, "")):
            result = CliRunner().invoke(main, ["drive-sessions"])
        assert result.exit_code == 0
        assert "No live drive sessions." in result.output

    def test_human_output_lists_attach_and_stop_hints(self) -> None:
        stdout = "coord-drive-myrepo-42\t0\n"
        with patch("coord.drive.subprocess.run", return_value=_completed(0, stdout)):
            result = CliRunner().invoke(main, ["drive-sessions"])
        assert result.exit_code == 0
        assert "coord drive-attach myrepo 42" in result.output
        assert "coord drive-stop myrepo 42" in result.output


# ── CLI: coord drive-attach ──────────────────────────────────────────────────


class TestDriveAttachCli:
    def test_no_live_session_errors(self) -> None:
        with patch("coord.interactive.tmux_session_alive", return_value=False):
            result = CliRunner().invoke(main, ["drive-attach", "myrepo", "42"])
        out = output_and_stderr(result)
        assert result.exit_code == 1
        assert "no live drive session" in out

    def test_attaches_when_live(self) -> None:
        with (
            patch("coord.interactive.tmux_session_alive", return_value=True),
            patch("coord.commands.drive.subprocess.run", return_value=MagicMock(returncode=0)) as run,
        ):
            result = CliRunner().invoke(main, ["drive-attach", "myrepo", "42"])
        assert result.exit_code == 0
        argv = run.call_args[0][0]
        assert argv == ["tmux", "attach-session", "-t", "coord-drive-myrepo-42"]

    def test_nested_tmux_uses_switch_client(self, monkeypatch) -> None:
        monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,1234,0")
        with (
            patch("coord.interactive.tmux_session_alive", return_value=True),
            patch("coord.commands.drive.subprocess.run", return_value=MagicMock(returncode=0)) as run,
        ):
            result = CliRunner().invoke(main, ["drive-attach", "myrepo", "42"])
        assert result.exit_code == 0
        argv = run.call_args[0][0]
        assert argv == ["tmux", "switch-client", "-t", "coord-drive-myrepo-42"]


# ── CLI: coord drive-stop ─────────────────────────────────────────────────────


class TestDriveStopCli:
    def test_no_live_session_errors(self) -> None:
        with patch("coord.interactive.tmux_session_alive", return_value=False):
            result = CliRunner().invoke(main, ["drive-stop", "myrepo", "42"])
        out = output_and_stderr(result)
        assert result.exit_code == 1
        assert "no live drive session" in out

    def test_kills_when_live(self) -> None:
        with (
            patch("coord.interactive.tmux_session_alive", return_value=True),
            patch(
                "coord.commands.drive.subprocess.run",
                return_value=_completed(0),
            ) as run,
        ):
            result = CliRunner().invoke(main, ["drive-stop", "myrepo", "42"])
        out = output_and_stderr(result)
        assert result.exit_code == 0, out
        assert "Stopped driving myrepo #42" in out
        argv = run.call_args[0][0]
        assert argv == ["tmux", "kill-session", "-t", "coord-drive-myrepo-42"]

    def test_kill_failure_errors(self) -> None:
        with (
            patch("coord.interactive.tmux_session_alive", return_value=True),
            patch(
                "coord.commands.drive.subprocess.run",
                return_value=_completed(1, "", "boom"),
            ),
        ):
            result = CliRunner().invoke(main, ["drive-stop", "myrepo", "42"])
        out = output_and_stderr(result)
        assert result.exit_code == 1
        assert "boom" in out
