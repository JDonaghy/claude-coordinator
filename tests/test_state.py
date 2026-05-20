"""Tests for coord.state — proposal persistence."""

from __future__ import annotations

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
    def test_save_and_load_roundtrip(self, coord_db, proposals: list[Proposal]) -> None:
        save_proposals(proposals)
        loaded = load_proposals()

        assert len(loaded) == 2
        assert loaded[0].id == 1
        assert loaded[0].machine_name == "laptop"
        assert loaded[0].files_likely == ["auth.py"]
        assert loaded[1].id == 2
        assert loaded[1].briefing == ""

    def test_load_empty_returns_empty(self, coord_db) -> None:
        assert load_proposals() == []

    def test_clear_removes_proposals(self, coord_db, proposals: list[Proposal]) -> None:
        save_proposals(proposals)
        assert len(load_proposals()) == 2
        clear_proposals()
        assert load_proposals() == []

    def test_clear_when_empty_is_noop(self, coord_db) -> None:
        clear_proposals()  # should not raise
        assert load_proposals() == []

    def test_save_replaces_previous(self, coord_db, proposals: list[Proposal]) -> None:
        save_proposals(proposals)
        save_proposals([proposals[0]])  # save only first
        loaded = load_proposals()
        assert len(loaded) == 1
        assert loaded[0].id == 1
