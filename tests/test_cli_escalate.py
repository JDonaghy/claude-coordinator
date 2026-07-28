"""CLI-surface tests for `coord escalate` (#1505).

Board-visible "driver stuck" records: `record` is what `coord drive`'s merge
stage writes (see coord/drive.py's `_escalate_merge` + `Driver.run`'s exit
handling); `run`/`dismiss`/`list` are the human's one-key responses, also
reachable from coord-tui's Pipeline right-click menu (which shells out to
exactly these subcommands).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from click.testing import CliRunner

from coord import state
from coord.cli import main


def test_escalate_is_registered_on_the_main_cli():
    assert "escalate" in main.commands


def test_record_writes_the_local_db_and_echoes_the_reason(
    coord_db, valid_config_path: Path
):
    result = CliRunner().invoke(
        main,
        [
            "escalate", "record", "api", "7",
            "--config", str(valid_config_path),
            "--reason", "merge_status=NEEDS_ATTENTION",
            "--gate", "merge_status=NEEDS_ATTENTION",
            "--gate", "pr_url=https://github.com/acme/api/pull/9",
            "--command", "gh pr merge 9 --rebase && coord reconcile-merges",
            "--assignment", "w1",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "merge_status=NEEDS_ATTENTION" in result.output
    entry = state._get_drive_escalation_local("api", 7)
    assert entry is not None
    assert entry["stage"] == "merge"
    assert entry["assignment_id"] == "w1"
    assert entry["proposed_command"] == "gh pr merge 9 --rebase && coord reconcile-merges"
    assert "merge_status=NEEDS_ATTENTION" in entry["gate_readings"]
    assert "pr_url=https://github.com/acme/api/pull/9" in entry["gate_readings"]


def test_list_reports_no_open_escalations_when_empty(coord_db, valid_config_path: Path):
    result = CliRunner().invoke(
        main, ["escalate", "list", "--config", str(valid_config_path)]
    )
    assert result.exit_code == 0
    assert "no open escalations" in result.output


def test_list_shows_a_recorded_escalation(coord_db, valid_config_path: Path):
    state._record_drive_escalation_local(
        "api", 7, stage="merge", reason="stuck", gate_readings="x=y",
        proposed_command="coord merge --plan --repo api",
    )
    result = CliRunner().invoke(
        main, ["escalate", "list", "--config", str(valid_config_path)]
    )
    assert result.exit_code == 0
    assert "api #7" in result.output
    assert "stuck" in result.output
    assert "coord merge --plan --repo api" in result.output


def test_dismiss_clears_the_record(coord_db, valid_config_path: Path):
    state._record_drive_escalation_local(
        "api", 7, stage="merge", reason="stuck", gate_readings="",
        proposed_command="c",
    )
    result = CliRunner().invoke(
        main, ["escalate", "dismiss", "api", "7", "--config", str(valid_config_path)]
    )
    assert result.exit_code == 0
    assert "dismissed" in result.output
    assert state._get_drive_escalation_local("api", 7) is None


def test_dismiss_reports_when_nothing_is_on_file(coord_db, valid_config_path: Path):
    result = CliRunner().invoke(
        main, ["escalate", "dismiss", "api", "7", "--config", str(valid_config_path)]
    )
    assert result.exit_code == 0
    assert "no escalation on file" in result.output


def test_run_with_no_record_is_a_clean_error(coord_db, valid_config_path: Path):
    result = CliRunner().invoke(
        main, ["escalate", "run", "api", "7", "--config", str(valid_config_path)]
    )
    assert result.exit_code != 0
    assert "no escalation on file" in result.output


def test_run_executes_the_proposed_command_and_dismisses_on_success(
    coord_db, valid_config_path: Path, monkeypatch
):
    state._record_drive_escalation_local(
        "api", 7, stage="merge", reason="stuck", gate_readings="",
        proposed_command="echo hi",
    )
    seen: dict = {}

    def fake_run(command, shell=False, **kw):  # noqa: ANN001
        seen["command"] = command
        seen["shell"] = shell
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr("coord.commands.drive.subprocess.run", fake_run)
    result = CliRunner().invoke(
        main, ["escalate", "run", "api", "7", "--config", str(valid_config_path)]
    )
    assert result.exit_code == 0, result.output
    assert seen["command"] == "echo hi"
    assert seen["shell"] is True
    # dismissed by default on success
    assert state._get_drive_escalation_local("api", 7) is None


def test_run_leaves_the_record_on_failure(coord_db, valid_config_path: Path, monkeypatch):
    state._record_drive_escalation_local(
        "api", 7, stage="merge", reason="stuck", gate_readings="",
        proposed_command="false",
    )
    monkeypatch.setattr(
        "coord.commands.drive.subprocess.run",
        lambda command, shell=False, **kw: subprocess.CompletedProcess(command, 1),
    )
    result = CliRunner().invoke(
        main, ["escalate", "run", "api", "7", "--config", str(valid_config_path)]
    )
    assert result.exit_code != 0
    assert state._get_drive_escalation_local("api", 7) is not None


def test_run_no_dismiss_keeps_the_record_after_success(
    coord_db, valid_config_path: Path, monkeypatch
):
    state._record_drive_escalation_local(
        "api", 7, stage="merge", reason="stuck", gate_readings="",
        proposed_command="echo hi",
    )
    monkeypatch.setattr(
        "coord.commands.drive.subprocess.run",
        lambda command, shell=False, **kw: subprocess.CompletedProcess(command, 0),
    )
    result = CliRunner().invoke(
        main,
        [
            "escalate", "run", "api", "7",
            "--no-dismiss", "--config", str(valid_config_path),
        ],
    )
    assert result.exit_code == 0, result.output
    assert state._get_drive_escalation_local("api", 7) is not None
