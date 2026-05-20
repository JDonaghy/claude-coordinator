"""Tests for auto-split — SplitProposal model, brain parsing, GitHub ops, CLI."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from click.testing import CliRunner

from coord.brain import parse_proposals, parse_split_proposals
from coord.cli import main
from coord.models import SplitChunk, SplitProposal
from coord.state import save_split_proposals, load_split_proposals, clear_split_proposals


# ── Brain parsing ───────────────────────────────────────────────────────────


class TestParseSplitProposals:
    def test_parses_split_from_mixed_response(self) -> None:
        text = json.dumps([
            {
                "type": "assignment",
                "machine_name": "laptop",
                "repo_name": "api",
                "issue_number": 10,
                "issue_title": "Small fix",
                "rationale": "easy",
                "files_likely": ["a.py"],
                "briefing": "do it",
            },
            {
                "type": "split",
                "repo_name": "api",
                "issue_number": 42,
                "issue_title": "Migrate all surfaces",
                "rationale": "30 surfaces, too large for one session",
                "chunks": [
                    {"title": "Chrome surfaces", "scope": "status bars, toolbar", "files_likely": ["chrome.py"]},
                    {"title": "Editor viewport", "scope": "main editor area", "files_likely": ["editor.py"]},
                ],
            },
        ])

        proposals = parse_proposals(text)
        splits = parse_split_proposals(text)

        assert len(proposals) == 1
        assert proposals[0].issue_number == 10

        assert len(splits) == 1
        assert splits[0].issue_number == 42
        assert len(splits[0].chunks) == 2
        assert splits[0].chunks[0].title == "Chrome surfaces"
        assert splits[0].chunks[1].files_likely == ["editor.py"]

    def test_no_splits_returns_empty(self) -> None:
        text = json.dumps([
            {
                "type": "assignment",
                "machine_name": "laptop",
                "repo_name": "api",
                "issue_number": 10,
                "issue_title": "x",
                "rationale": "",
                "files_likely": [],
                "briefing": "",
            },
        ])
        assert parse_split_proposals(text) == []

    def test_only_splits_returns_empty_proposals(self) -> None:
        text = json.dumps([
            {
                "type": "split",
                "repo_name": "api",
                "issue_number": 42,
                "issue_title": "Big task",
                "rationale": "too large",
                "chunks": [{"title": "A", "scope": "a"}],
            },
        ])
        assert parse_proposals(text) == []
        assert len(parse_split_proposals(text)) == 1

    def test_empty_array(self) -> None:
        assert parse_split_proposals("[]") == []

    def test_backwards_compat_no_type_field(self) -> None:
        text = json.dumps([
            {
                "machine_name": "laptop",
                "repo_name": "api",
                "issue_number": 10,
                "issue_title": "x",
                "rationale": "",
            },
        ])
        proposals = parse_proposals(text)
        splits = parse_split_proposals(text)
        assert len(proposals) == 1
        assert splits == []


# ── State persistence ───────────────────────────────────────────────────────


class TestSplitState:
    def test_save_and_load_roundtrip(self, coord_db) -> None:
        splits = [
            SplitProposal(
                id=1, repo_name="api", issue_number=42,
                issue_title="Big task", rationale="too large",
                chunks=[
                    SplitChunk(title="A", scope="first chunk", files_likely=["a.py"]),
                    SplitChunk(title="B", scope="second chunk"),
                ],
            ),
        ]
        save_split_proposals(splits)
        loaded = load_split_proposals()

        assert len(loaded) == 1
        assert loaded[0].issue_number == 42
        assert len(loaded[0].chunks) == 2
        assert loaded[0].chunks[0].title == "A"
        assert loaded[0].chunks[0].files_likely == ["a.py"]

    def test_load_empty_returns_empty(self, coord_db) -> None:
        assert load_split_proposals() == []

    def test_clear_removes_splits(self, coord_db) -> None:
        save_split_proposals([SplitProposal(
            id=1, repo_name="api", issue_number=1, issue_title="t", rationale="",
        )])
        assert len(load_split_proposals()) == 1
        clear_split_proposals()
        assert load_split_proposals() == []


# ── GitHub ops ──────────────────────────────────────────────────────────────


class TestGitHubOps:
    @patch("coord.github_ops._gh")
    def test_create_issue(self, mock_gh: MagicMock) -> None:
        from coord.github_ops import create_issue

        mock_gh.return_value = "https://github.com/acme/api/issues/99"
        result = create_issue("acme/api", "Test issue", "body text", labels=["sub-task"])
        assert result["number"] == 99
        assert result["url"] == "https://github.com/acme/api/issues/99"
        args = mock_gh.call_args.args
        assert "issue" in args
        assert "create" in args
        assert "--label" in args

    @patch("coord.github_ops._gh")
    def test_update_issue_body(self, mock_gh: MagicMock) -> None:
        from coord.github_ops import update_issue_body

        update_issue_body("acme/api", 42, "new body")
        mock_gh.assert_called_once()
        args = mock_gh.call_args.args
        assert "PATCH" in args
        assert "repos/acme/api/issues/42" in args[3]


# ── CLI coord split ─────────────────────────────────────────────────────────


class TestCoordSplit:
    @pytest.fixture
    def config_file(self, tmp_path: Path) -> Path:
        p = tmp_path / "coordinator.yml"
        p.write_text(
            "repos:\n  - name: api\n    github: acme/api\n"
            "machines:\n  - name: m\n    host: h\n    repos: [api]\n"
        )
        return p

    def test_split_dry_run(self, config_file: Path, coord_db) -> None:
        splits = [SplitProposal(
            id=1, repo_name="api", issue_number=42,
            issue_title="Big task", rationale="too large",
            chunks=[
                SplitChunk(title="A", scope="first", files_likely=["a.py"]),
                SplitChunk(title="B", scope="second"),
            ],
        )]
        save_split_proposals(splits)

        runner = CliRunner()
        result = runner.invoke(main, [
            "split", "S1", "--dry-run", "--config", str(config_file),
        ])

        assert result.exit_code == 0
        assert "would create" in result.output
        assert "A" in result.output
        assert "B" in result.output

    @patch("coord.github_ops.update_issue_body")
    @patch("coord.github_ops.create_issue")
    def test_split_creates_child_issues(
        self, mock_create: MagicMock, mock_update: MagicMock,
        config_file: Path, coord_db,
    ) -> None:
        mock_create.side_effect = [
            {"number": 100, "url": "https://github.com/acme/api/issues/100"},
            {"number": 101, "url": "https://github.com/acme/api/issues/101"},
        ]

        splits = [SplitProposal(
            id=1, repo_name="api", issue_number=42,
            issue_title="Big task", rationale="too large",
            chunks=[
                SplitChunk(title="A", scope="first", files_likely=["a.py"]),
                SplitChunk(title="B", scope="second"),
            ],
        )]
        save_split_proposals(splits)

        runner = CliRunner()
        result = runner.invoke(main, [
            "split", "1", "--config", str(config_file),
        ])

        assert result.exit_code == 0
        assert "#100" in result.output
        assert "#101" in result.output
        assert mock_create.call_count == 2
        mock_update.assert_called_once()
        update_body = mock_update.call_args.args[2]
        assert "#100" in update_body
        assert "#101" in update_body

    def test_split_no_pending(self, config_file: Path, coord_db) -> None:
        runner = CliRunner()
        result = runner.invoke(main, [
            "split", "S1", "--config", str(config_file),
        ])
        assert result.exit_code != 0
        assert "No pending split" in result.output

    def test_split_unknown_id(self, config_file: Path, coord_db) -> None:
        save_split_proposals([SplitProposal(
            id=1, repo_name="api", issue_number=42,
            issue_title="x", rationale="",
        )])
        runner = CliRunner()
        result = runner.invoke(main, [
            "split", "S99", "--config", str(config_file),
        ])
        assert result.exit_code != 0
        assert "unknown" in result.output


class TestHelpText:
    def test_split_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["split", "--help"])
        assert result.exit_code == 0
        assert "sub-issues" in result.output
