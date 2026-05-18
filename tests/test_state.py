"""Tests for coord.state — proposal persistence."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from coord.models import Proposal
from coord.state import save_proposals, load_proposals, clear_proposals


@pytest.fixture
def proposals() -> list[Proposal]:
    return [
        Proposal(
            id=1,
            machine_name="laptop",
            repo_name="api",
            issue_number=10,
            issue_title="Fix auth",
            rationale="best fit",
            files_likely=["auth.py"],
            briefing="Fix the auth module",
        ),
        Proposal(
            id=2,
            machine_name="server",
            repo_name="shared",
            issue_number=5,
            issue_title="Add logging",
            rationale="only option",
        ),
    ]


class TestStatePersistence:
    def test_save_and_load_roundtrip(self, tmp_path: Path, proposals: list[Proposal]) -> None:
        proposals_file = tmp_path / "pending_proposals.json"
        with (
            patch("coord.state.COORD_DIR", tmp_path),
            patch("coord.state.PROPOSALS_FILE", proposals_file),
        ):
            save_proposals(proposals)
            loaded = load_proposals()

        assert len(loaded) == 2
        assert loaded[0].id == 1
        assert loaded[0].machine_name == "laptop"
        assert loaded[0].files_likely == ["auth.py"]
        assert loaded[1].id == 2
        assert loaded[1].briefing == ""

    def test_load_empty_returns_empty(self, tmp_path: Path) -> None:
        proposals_file = tmp_path / "pending_proposals.json"
        with patch("coord.state.PROPOSALS_FILE", proposals_file):
            assert load_proposals() == []

    def test_clear_removes_file(self, tmp_path: Path, proposals: list[Proposal]) -> None:
        proposals_file = tmp_path / "pending_proposals.json"
        with (
            patch("coord.state.COORD_DIR", tmp_path),
            patch("coord.state.PROPOSALS_FILE", proposals_file),
        ):
            save_proposals(proposals)
            assert proposals_file.exists()
            clear_proposals()
            assert not proposals_file.exists()

    def test_clear_no_file_is_noop(self, tmp_path: Path) -> None:
        proposals_file = tmp_path / "pending_proposals.json"
        with patch("coord.state.PROPOSALS_FILE", proposals_file):
            clear_proposals()  # should not raise
