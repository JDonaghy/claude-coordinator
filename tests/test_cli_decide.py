"""CLI-surface tests for `coord decide` (#2370).

Generalizes `coord escalate run`'s one-key "run the proposed fix, dismiss on
success" pattern to BOTH `decisions` report sources (#2369): a
`drive_escalations` row (source-1) and a drive-queue `blocked`/`failed` row
with no escalation record at all (source-2) — and to any option on the card,
not just the recommended one.

Same acceptance bar as `tests/test_cli_escalate.py`: drives the real Click
CLI against a seeded local DB and asserts on its rendered output / DB state,
never calls the fold functions directly.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from click.testing import CliRunner

from coord import state
from coord.cli import main
from coord.reports import run_decisions


def _fake_run(seen: dict):
    def run(command, shell=False, **kw):  # noqa: ANN001
        seen["command"] = command
        seen["shell"] = shell
        return subprocess.CompletedProcess(command, 0)

    return run


def test_decide_is_registered_on_the_main_cli():
    assert "decide" in main.commands


def test_no_decision_on_file_is_a_clean_error(coord_db, valid_config_path: Path):
    result = CliRunner().invoke(
        main, ["decide", "api", "7", "--config", str(valid_config_path)]
    )
    assert result.exit_code != 0
    assert "no decision on file" in result.output
    # a click.ClickException, never a raw traceback
    assert "Traceback" not in result.output


def test_no_index_on_escalation_card_behaves_exactly_like_escalate_run(
    coord_db, valid_config_path: Path, monkeypatch
):
    state._record_drive_escalation_local(
        "api", 7, stage="merge", reason="stuck", gate_readings="",
        proposed_command="echo hi",
    )
    seen: dict = {}
    monkeypatch.setattr("coord.commands.drive.subprocess.run", _fake_run(seen))

    result = CliRunner().invoke(
        main, ["decide", "api", "7", "--config", str(valid_config_path)]
    )
    assert result.exit_code == 0, result.output
    assert seen["command"] == "echo hi"
    assert seen["shell"] is True
    # dismissed by default on success, exactly like `escalate run`
    assert state._get_drive_escalation_local("api", 7) is None


def test_index_0_is_the_same_as_omitting_the_index(
    coord_db, valid_config_path: Path, monkeypatch
):
    state._record_drive_escalation_local(
        "api", 7, stage="merge", reason="stuck", gate_readings="",
        proposed_command="echo hi",
    )
    seen: dict = {}
    monkeypatch.setattr("coord.commands.drive.subprocess.run", _fake_run(seen))

    result = CliRunner().invoke(
        main, ["decide", "api", "7", "0", "--config", str(valid_config_path)]
    )
    assert result.exit_code == 0, result.output
    assert seen["command"] == "echo hi"
    assert state._get_drive_escalation_local("api", 7) is None


def test_non_default_option_on_an_escalation_card_never_dismisses(
    coord_db, valid_config_path: Path, monkeypatch
):
    """Option 1 on a source-1 card ("Inspect") is NOT the record's
    `proposed_command` — running it must leave the escalation record alone
    and must not claim it dismissed anything."""
    state._record_drive_escalation_local(
        "api", 7, stage="merge", reason="stuck", gate_readings="",
        proposed_command="echo hi",
    )
    seen: dict = {}
    monkeypatch.setattr("coord.commands.drive.subprocess.run", _fake_run(seen))

    result = CliRunner().invoke(
        main, ["decide", "api", "7", "1", "--config", str(valid_config_path)]
    )
    assert result.exit_code == 0, result.output
    assert seen["command"] == "coord escalate list --repo api"
    assert "no escalation record to dismiss" in result.output
    assert state._get_drive_escalation_local("api", 7) is not None


def test_out_of_range_index_fails_cleanly_naming_the_valid_range(
    coord_db, valid_config_path: Path
):
    state._record_drive_escalation_local(
        "api", 7, stage="merge", reason="stuck", gate_readings="",
        proposed_command="echo hi",
    )
    result = CliRunner().invoke(
        main, ["decide", "api", "7", "5", "--config", str(valid_config_path)]
    )
    assert result.exit_code != 0
    assert "out of range" in result.output
    assert "0..1" in result.output
    # a clean click.ClickException, never a raw traceback
    assert "Traceback" not in result.output


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
        main, ["decide", "api", "7", "--config", str(valid_config_path)]
    )
    assert result.exit_code != 0
    assert state._get_drive_escalation_local("api", 7) is not None


def test_no_dismiss_keeps_the_record_after_success(
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
        ["decide", "api", "7", "--no-dismiss", "--config", str(valid_config_path)],
    )
    assert result.exit_code == 0, result.output
    assert state._get_drive_escalation_local("api", 7) is not None


# ── source-2: drive-queue-only card, no escalation record at all (#2283) ────

_TWO_OPTION_REASON = (
    "acceptance author aid-1 failed.\n"
    "   inspect: coord log aid-1 --machine dellserver\n"
    "   remedy: echo remedy-command"
)


def _seed_blocked_queue_row(repo: str, issue: int, last_reason: str) -> None:
    state.enqueue_drive_queue(repo, issue)
    state._update_drive_queue_entry_local(
        repo, issue, state="blocked", last_reason=last_reason
    )


def test_source2_card_default_index_runs_the_recommended_option(
    coord_db, valid_config_path: Path, monkeypatch
):
    _seed_blocked_queue_row("api", 55, _TWO_OPTION_REASON)
    seen: dict = {}
    monkeypatch.setattr("coord.commands.drive.subprocess.run", _fake_run(seen))

    result = CliRunner().invoke(
        main, ["decide", "api", "55", "--config", str(valid_config_path)]
    )
    assert result.exit_code == 0, result.output
    assert seen["command"] == "coord log aid-1 --machine dellserver"
    assert "no escalation record to dismiss" in result.output
    # never wrote a drive_escalations row for a source-2 card
    assert state._get_drive_escalation_local("api", 55) is None


def test_source2_card_explicit_index_runs_that_option_and_never_touches_escalations(
    coord_db, valid_config_path: Path, monkeypatch
):
    _seed_blocked_queue_row("api", 55, _TWO_OPTION_REASON)
    seen: dict = {}
    monkeypatch.setattr("coord.commands.drive.subprocess.run", _fake_run(seen))

    result = CliRunner().invoke(
        main, ["decide", "api", "55", "1", "--config", str(valid_config_path)]
    )
    assert result.exit_code == 0, result.output
    assert seen["command"] == "echo remedy-command"
    assert state._get_drive_escalation_local("api", 55) is None


# ── --list: discovery path, executes nothing (#2375) ────────────────────────


def test_list_prints_numbered_options_and_runs_nothing_for_escalation_card(
    coord_db, valid_config_path: Path, monkeypatch
):
    state._record_drive_escalation_local(
        "api", 7, stage="merge", reason="stuck", gate_readings="",
        proposed_command="echo hi",
    )
    seen: dict = {}
    monkeypatch.setattr("coord.commands.drive.subprocess.run", _fake_run(seen))

    result = CliRunner().invoke(
        main, ["decide", "api", "7", "--list", "--config", str(valid_config_path)]
    )
    assert result.exit_code == 0, result.output
    assert "command" not in seen
    assert "0:" in result.output and "Recommended: echo hi" in result.output
    assert "1:" in result.output and "Inspect: coord escalate list --repo api" in result.output
    # nothing dismissed either — --list never touches state
    assert state._get_drive_escalation_local("api", 7) is not None


def test_list_prints_numbered_options_for_source2_card(
    coord_db, valid_config_path: Path, monkeypatch
):
    _seed_blocked_queue_row("api", 55, _TWO_OPTION_REASON)
    seen: dict = {}
    monkeypatch.setattr("coord.commands.drive.subprocess.run", _fake_run(seen))

    result = CliRunner().invoke(
        main, ["decide", "api", "55", "--list", "--config", str(valid_config_path)]
    )
    assert result.exit_code == 0, result.output
    assert "command" not in seen
    assert "coord log aid-1 --machine dellserver" in result.output
    assert "echo remedy-command" in result.output


def test_list_matches_the_decisions_report_options_exactly(
    coord_db, valid_config_path: Path
):
    """#2375 acceptance: `--list`'s numbered options are the same ones
    `coord report run decisions` (and thus `format_option_cell`) would show
    for this same card, byte-for-byte."""
    _seed_blocked_queue_row("api", 55, _TWO_OPTION_REASON)

    result = CliRunner().invoke(
        main, ["decide", "api", "55", "--list", "--config", str(valid_config_path)]
    )
    assert result.exit_code == 0, result.output

    from coord.reports import format_option_cell

    report = run_decisions(repo="api")
    card = next(r for r in report.rows if r["issue"] == 55)
    for i, opt in enumerate(card["options"]):
        assert f"  {i}: {format_option_cell(opt)}" in result.output


def test_list_with_no_decision_on_file_is_a_clean_error(
    coord_db, valid_config_path: Path
):
    result = CliRunner().invoke(
        main, ["decide", "api", "7", "--list", "--config", str(valid_config_path)]
    )
    assert result.exit_code != 0
    assert "no decision on file" in result.output
    assert "Traceback" not in result.output


def test_executed_command_is_byte_identical_to_the_decisions_report_option(
    coord_db, valid_config_path: Path, monkeypatch
):
    """#2370 acceptance: `coord decide` must never paraphrase or re-derive
    the command — it runs exactly what the `decisions` report's
    `options[i].command_or_action` says for the same repo/issue/index."""
    _seed_blocked_queue_row("api", 55, _TWO_OPTION_REASON)
    seen: dict = {}
    monkeypatch.setattr("coord.commands.drive.subprocess.run", _fake_run(seen))

    result = CliRunner().invoke(
        main, ["decide", "api", "55", "1", "--config", str(valid_config_path)]
    )
    assert result.exit_code == 0, result.output

    report = run_decisions(repo="api")
    card = next(r for r in report.rows if r["issue"] == 55)
    assert seen["command"] == card["options"][1]["command_or_action"]
