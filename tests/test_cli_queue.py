"""Tests for `coord queue` / `coord unqueue` — #1500 "Mark ready" / "Unmark
ready".

`queue` stages a Pipeline issue as "next up" by adding `status:queued`;
`unqueue` reverses it. Deliberately a separate label from `status:ready`:
that one is already applied automatically by `coord track` / the refinement
finalize step for every issue sent to the Pipeline, so it carries no "an
operator specifically staged this" signal (see the label_change_for_subcommand
doc comment in tui/src/app/settings_ui.rs, which this test's expectations must
stay in sync with).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from coord.cli import main

_CONFIG_YAML = """\
repos:
  - name: api
    github: acme/api
    default_branch: main
machines:
  - name: laptop
    host: laptop.tailnet
    repos: [api]
"""


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(_CONFIG_YAML)
    return p


def _run(
    subcommand: str, config_file: Path, current_labels: list[str]
) -> tuple[Any, list[list[str]]]:
    """Invoke `coord <subcommand> api 1` with `gh` mocked to report
    *current_labels*.

    Returns the CliRunner result and the captured `gh issue edit` argv
    (empty when no edit was performed, i.e. a no-op).
    """
    edits: list[list[str]] = []

    def _fake_run(cmd: list[str], **_kw: Any) -> MagicMock:
        m = MagicMock()
        m.returncode = 0
        m.stderr = ""
        if "view" in cmd:
            m.stdout = json.dumps({"labels": [{"name": n} for n in current_labels]})
        elif "edit" in cmd:
            edits.append(list(cmd))
            m.stdout = ""
        else:
            m.stdout = "{}"
        return m

    with patch("subprocess.run", side_effect=_fake_run), patch(
        "coord.state.update_issue_labels"
    ):
        result = CliRunner().invoke(
            main, [subcommand, "api", "1", "--config", str(config_file)]
        )
    return result, edits


# ── coord queue ──────────────────────────────────────────────────────────────


def test_queue_adds_status_queued(config_file: Path) -> None:
    result, edits = _run("queue", config_file, current_labels=["coord"])
    assert result.exit_code == 0, result.output
    assert len(edits) == 1, "expected one gh issue edit"
    flat = " ".join(edits[0])
    assert "--add-label status:queued" in flat


def test_queue_does_not_touch_status_ready(config_file: Path) -> None:
    """`queue` must be additive only — it must NOT clear or otherwise touch
    `status:ready` (that label means something else entirely, per #1500's
    design note: reusing it would make every Pipeline-tracked issue read as
    operator-staged)."""
    result, edits = _run(
        "queue", config_file, current_labels=["coord", "status:ready"]
    )
    assert result.exit_code == 0, result.output
    assert len(edits) == 1, "status:queued is still absent, so this is not a no-op"
    flat = " ".join(edits[0])
    assert "--remove-label status:ready" not in flat


def test_queue_noop_when_already_queued(config_file: Path) -> None:
    result, edits = _run(
        "queue", config_file, current_labels=["coord", "status:queued"]
    )
    assert result.exit_code == 0, result.output
    assert edits == [], "status:queued already present ⇒ no gh edit"
    assert "already marked ready" in result.output


# ── coord unqueue ────────────────────────────────────────────────────────────


def test_unqueue_removes_status_queued(config_file: Path) -> None:
    result, edits = _run(
        "unqueue", config_file, current_labels=["coord", "status:queued"]
    )
    assert result.exit_code == 0, result.output
    assert len(edits) == 1, "expected one gh issue edit"
    flat = " ".join(edits[0])
    assert "--remove-label status:queued" in flat


def test_unqueue_noop_when_not_queued(config_file: Path) -> None:
    result, edits = _run("unqueue", config_file, current_labels=["coord"])
    assert result.exit_code == 0, result.output
    assert edits == [], "no status:queued label present ⇒ no gh edit"
    assert "not marked ready" in result.output


# ── backlog / untrack must also clear status:queued (#1500) ────────────────


def test_backlog_also_clears_status_queued(config_file: Path) -> None:
    """A staged (queued) issue dropped back to Backlog must not carry the
    marker back in with it — a stale status:queued would silently re-surface
    it as In-progress:ready the moment it's re-tracked."""
    result, edits = _run(
        "backlog", config_file, current_labels=["status:queued", "status:ready"]
    )
    assert result.exit_code == 0, result.output
    assert len(edits) == 1
    flat = " ".join(edits[0])
    assert "--remove-label status:queued" in flat
    assert "--remove-label status:ready" in flat


def test_untrack_also_clears_status_queued(config_file: Path) -> None:
    result, edits = _run(
        "untrack", config_file, current_labels=["coord", "status:queued"]
    )
    assert result.exit_code == 0, result.output
    assert len(edits) == 1
    flat = " ".join(edits[0])
    assert "--remove-label coord" in flat
    assert "--remove-label status:queued" in flat
