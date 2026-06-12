"""Tests for `coord track` — Send to Pipeline (#261/#486 Leg 4).

`track` must make an issue a DISPATCHABLE Pipeline:New card, which needs BOTH
the `coord` and `status:ready` labels.  The #486 regression was that it added
only `coord`, so the ~38 coordinator issues *born* with `coord` (but no
`status:ready`) stayed stuck — "Send to Pipeline" was a silent no-op.
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


def _run_track(config_file: Path, current_labels: list[str]) -> tuple[Any, list[list[str]]]:
    """Invoke `coord track api 1` with `gh` mocked to report *current_labels*.

    Returns the CliRunner result and the captured `gh issue edit` argv (empty
    when no edit was performed, i.e. a no-op).
    """
    edits: list[list[str]] = []

    def _fake_run(cmd: list[str], **_kw: Any) -> MagicMock:
        m = MagicMock()
        m.returncode = 0
        m.stderr = ""
        if "view" in cmd:
            m.stdout = json.dumps(
                {"labels": [{"name": n} for n in current_labels]}
            )
        elif "edit" in cmd:
            edits.append(list(cmd))
            m.stdout = ""
        else:
            m.stdout = "{}"
        return m

    with patch("subprocess.run", side_effect=_fake_run), \
         patch("coord.state.update_issue_labels"):
        result = CliRunner().invoke(
            main, ["track", "api", "1", "--config", str(config_file)]
        )
    return result, edits


def test_track_adds_both_labels_when_neither_present(config_file: Path) -> None:
    result, edits = _run_track(config_file, current_labels=[])
    assert result.exit_code == 0, result.output
    assert len(edits) == 1, "expected one gh issue edit"
    flat = " ".join(edits[0])
    assert "--add-label coord" in flat
    assert "--add-label status:ready" in flat


def test_track_adds_status_ready_when_only_coord_present(config_file: Path) -> None:
    """The #486 trap: an issue born with `coord` but no `status:ready` must be
    promoted (status:ready added), not silently no-op'd."""
    result, edits = _run_track(config_file, current_labels=["coord"])
    assert result.exit_code == 0, result.output
    assert len(edits) == 1, "must still edit to add status:ready"
    flat = " ".join(edits[0])
    assert "--add-label status:ready" in flat
    # `coord` already present ⇒ not re-added.
    assert "--add-label coord" not in flat


def test_track_clears_refining(config_file: Path) -> None:
    """Promoting clears a pre-Pipeline status, mirroring `coord ready`."""
    result, edits = _run_track(config_file, current_labels=["status:refining"])
    assert result.exit_code == 0, result.output
    flat = " ".join(edits[0])
    assert "--add-label coord" in flat
    assert "--add-label status:ready" in flat
    assert "--remove-label status:refining" in flat


def test_track_noop_when_already_dispatchable(config_file: Path) -> None:
    result, edits = _run_track(
        config_file, current_labels=["coord", "status:ready"]
    )
    assert result.exit_code == 0, result.output
    assert edits == [], "both labels present ⇒ no gh edit"
    assert "already dispatchable" in result.output
