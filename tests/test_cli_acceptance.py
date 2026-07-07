"""CLI tests for `coord acceptance run` / `coord acceptance record` (#944)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from coord.cli import main
from coord.models import Proposal


CONFIG_YAML = """\
repos:
  - name: coord-tui
    github: acme/coord-tui
machines:
  - name: laptop
    host: laptop.tail
    repos: [coord-tui]
    repo_paths:
      coord-tui: {repo_path}
acceptance:
  drivers:
    coord-tui:
      kind: tui-tuidriver
      run: {run_cmd}
"""


def _write_config(tmp_path: Path, *, repo_path: str, run_cmd: str) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(CONFIG_YAML.format(repo_path=repo_path, run_cmd=json.dumps(run_cmd)))
    return p


def _write_manifest(acceptance_root: Path, mapping: dict[str, int]) -> None:
    ms = acceptance_root / "ms01"
    ms.mkdir(parents=True, exist_ok=True)
    tests_yaml = "\n".join(f"  {k}: {v}" for k, v in mapping.items())
    (ms / "manifest.yml").write_text(f"tests:\n{tests_yaml}\n")


class TestAcceptanceRun:
    def test_run_scoped_to_issue_all_pass(self, tmp_path: Path) -> None:
        blob = json.dumps({"tests": [
            {"id": "ms01::a", "status": "pass"},
            {"id": "ms01::b", "status": "pass"},
        ]})
        cwd = tmp_path / "repo"
        cwd.mkdir()
        _write_manifest(cwd / "tests" / "acceptance", {"ms01::a": 944, "ms01::b": 944})
        config_path = _write_config(tmp_path, repo_path=str(cwd), run_cmd=f"echo '{blob}'")

        result = CliRunner().invoke(main, [
            "acceptance", "run", "--repo", "coord-tui", "--issue", "944",
            "--path", str(cwd), "--config", str(config_path),
        ])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["issue"] == 944
        assert payload["total"] == 2
        assert payload["green"] is True

    def test_run_reports_failure_and_nonzero_exit(self, tmp_path: Path) -> None:
        blob = json.dumps({"tests": [
            {"id": "ms01::a", "status": "pass"},
            {"id": "ms01::b", "status": "fail", "message": "expected A got B"},
        ]})
        cwd = tmp_path / "repo"
        cwd.mkdir()
        _write_manifest(cwd / "tests" / "acceptance", {"ms01::a": 944, "ms01::b": 944})
        config_path = _write_config(tmp_path, repo_path=str(cwd), run_cmd=f"echo '{blob}'")

        result = CliRunner().invoke(main, [
            "acceptance", "run", "--repo", "coord-tui", "--issue", "944",
            "--path", str(cwd), "--config", str(config_path),
        ])
        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["failed"] == 1
        assert payload["green"] is False

    def test_run_filters_out_other_issues_tests(self, tmp_path: Path) -> None:
        blob = json.dumps({"tests": [
            {"id": "ms01::a", "status": "pass"},
            {"id": "ms01::other", "status": "fail"},
        ]})
        cwd = tmp_path / "repo"
        cwd.mkdir()
        _write_manifest(
            cwd / "tests" / "acceptance", {"ms01::a": 944, "ms01::other": 945},
        )
        config_path = _write_config(tmp_path, repo_path=str(cwd), run_cmd=f"echo '{blob}'")

        result = CliRunner().invoke(main, [
            "acceptance", "run", "--repo", "coord-tui", "--issue", "944",
            "--path", str(cwd), "--config", str(config_path),
        ])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        # Only ms01::a belongs to #944 — #945's failure must not leak in.
        assert payload["total"] == 1
        assert payload["green"] is True

    def test_run_all_ignores_manifest(self, tmp_path: Path) -> None:
        blob = json.dumps({"tests": [{"id": "ms01::a", "status": "pass"}]})
        cwd = tmp_path / "repo"
        cwd.mkdir()
        # No manifest at all — --all must still work.
        config_path = _write_config(tmp_path, repo_path=str(cwd), run_cmd=f"echo '{blob}'")

        result = CliRunner().invoke(main, [
            "acceptance", "run", "--repo", "coord-tui", "--all",
            "--path", str(cwd), "--config", str(config_path),
        ])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["scope"] == "all"
        assert payload["total"] == 1

    def test_run_requires_issue_or_all(self, tmp_path: Path) -> None:
        cwd = tmp_path / "repo"
        cwd.mkdir()
        config_path = _write_config(tmp_path, repo_path=str(cwd), run_cmd="echo '{}'")
        result = CliRunner().invoke(main, [
            "acceptance", "run", "--repo", "coord-tui",
            "--path", str(cwd), "--config", str(config_path),
        ])
        assert result.exit_code == 1
        assert "--issue N or --all" in result.output

    def test_run_missing_driver_errors(self, tmp_path: Path) -> None:
        p = tmp_path / "coordinator.yml"
        p.write_text(
            "repos:\n  - name: coord-tui\n    github: acme/coord-tui\n"
            "machines:\n  - name: laptop\n    host: laptop.tail\n    repos: [coord-tui]\n"
        )
        result = CliRunner().invoke(main, [
            "acceptance", "run", "--repo", "coord-tui", "--all", "--config", str(p),
        ])
        assert result.exit_code == 1
        assert "no acceptance driver configured" in result.output

    def test_run_missing_manifest_errors(self, tmp_path: Path) -> None:
        cwd = tmp_path / "repo"
        cwd.mkdir()
        config_path = _write_config(tmp_path, repo_path=str(cwd), run_cmd="echo '{}'")
        result = CliRunner().invoke(main, [
            "acceptance", "run", "--repo", "coord-tui", "--issue", "944",
            "--path", str(cwd), "--config", str(config_path),
        ])
        assert result.exit_code == 1
        assert "not been authored" in result.output

    def test_run_issue_with_no_slice_errors(self, tmp_path: Path) -> None:
        cwd = tmp_path / "repo"
        cwd.mkdir()
        _write_manifest(cwd / "tests" / "acceptance", {"ms01::a": 1})
        config_path = _write_config(tmp_path, repo_path=str(cwd), run_cmd="echo '{}'")
        result = CliRunner().invoke(main, [
            "acceptance", "run", "--repo", "coord-tui", "--issue", "944",
            "--path", str(cwd), "--config", str(config_path),
        ])
        assert result.exit_code == 1
        assert "no acceptance slice" in result.output


def _init_git_repo(path: Path, *, manifest: dict[str, int] | None = None) -> str:
    """Create a minimal git repo (with a real "origin" remote — a bare repo
    alongside it) and one commit pushed to origin; returns the commit SHA.

    ``coord acceptance record`` always does a real ``git fetch origin``
    before checking out the worktree, so the test repo needs an actual
    origin remote, not just local history.

    When *manifest* is given, ``tests/acceptance/ms01/manifest.yml`` mapping
    each test id to its issue number is committed too — ``record`` checks
    out this exact SHA in a throwaway worktree, so the manifest must be part
    of history at the SHA being recorded, not just sitting in the base
    checkout's working tree.
    """
    bare = path.parent / f"{path.name}-origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)

    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    subprocess.run(["git", "remote", "add", "origin", str(bare)], cwd=path, check=True)
    (path / "README.md").write_text("hello\n")
    if manifest:
        _write_manifest(path / "tests" / "acceptance", manifest)
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)
    branch = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=path, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    subprocess.run(["git", "push", "-q", "origin", branch], cwd=path, check=True)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=path, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    return sha


@pytest.fixture(autouse=True)
def _acceptance_worktrees_in_tmp(monkeypatch, tmp_path: Path):
    """Keep `coord acceptance record`'s throwaway worktree under tmp_path —
    the real implementation lives under ``~/.coord/acceptance-worktrees/``
    (outside the base checkout, mirroring `coord test`'s worktree), which
    must never leak into the real user's home directory during tests."""
    wt_root = tmp_path / "acceptance-worktrees"

    def _fake_path(repo_name: str, issue_number: int) -> Path:
        return wt_root / f"{repo_name}-{issue_number}"

    monkeypatch.setattr(
        "coord.commands.acceptance._acceptance_worktree_path", _fake_path,
    )


def _acceptance_row(coord_db, assignment_id: str) -> dict:
    row = coord_db.execute(
        "SELECT acceptance_state, acceptance_reason, acceptance_sha "
        "FROM assignments WHERE assignment_id=?",
        (assignment_id,),
    ).fetchone()
    assert row is not None, f"no assignment row for {assignment_id!r}"
    return dict(row)


class TestAcceptanceRecord:
    def test_record_passed_writes_board_verdict(self, tmp_path: Path, coord_db) -> None:
        from coord import state

        repo_dir = tmp_path / "repo"
        sha = _init_git_repo(repo_dir, manifest={"ms01::a": 944})

        blob = json.dumps({"tests": [{"id": "ms01::a", "status": "pass"}]})
        config_path = _write_config(tmp_path, repo_path=str(repo_dir), run_cmd=f"echo '{blob}'")

        state.record_dispatched(
            assignment_id="aid-1",
            proposal=Proposal(
                id=1, machine_name="laptop", repo_name="coord-tui",
                issue_number=944, issue_title="oracle loop runner", rationale="",
            ),
            repo_github="acme/coord-tui",
        )

        result = CliRunner().invoke(main, [
            "acceptance", "record", "--repo", "coord-tui", "--issue", "944",
            "--sha", sha, "--config", str(config_path),
        ])
        assert result.exit_code == 0, result.output
        assert "Acceptance PASSED" in result.output

        row = _acceptance_row(coord_db, "aid-1")
        assert row["acceptance_state"] == "passed"
        assert row["acceptance_sha"] == sha

    def test_record_failed_writes_board_verdict_and_context(
        self, tmp_path: Path, coord_db,
    ) -> None:
        from coord import state

        repo_dir = tmp_path / "repo"
        sha = _init_git_repo(repo_dir, manifest={"ms01::a": 944})

        blob = json.dumps({"tests": [
            {"id": "ms01::a", "status": "fail", "message": "expected A got B"},
        ]})
        config_path = _write_config(tmp_path, repo_path=str(repo_dir), run_cmd=f"echo '{blob}'")

        state.record_dispatched(
            assignment_id="aid-2",
            proposal=Proposal(
                id=1, machine_name="laptop", repo_name="coord-tui",
                issue_number=944, issue_title="oracle loop runner", rationale="",
            ),
            repo_github="acme/coord-tui",
        )

        result = CliRunner().invoke(main, [
            "acceptance", "record", "--repo", "coord-tui", "--issue", "944",
            "--sha", sha, "--config", str(config_path),
        ])
        assert result.exit_code == 1
        assert "Acceptance FAILED" in result.output

        row = _acceptance_row(coord_db, "aid-2")
        assert row["acceptance_state"] == "failed"
        assert "expected A got B" in (row["acceptance_reason"] or "")

        # #603: a failure is recorded as durable per-issue context too.
        entries = state.list_issue_context("coord-tui", 944)
        assert any("Acceptance FAILED" in e["body"] for e in entries)

    def test_record_no_work_assignment_errors(self, tmp_path: Path, coord_db) -> None:
        repo_dir = tmp_path / "repo"
        sha = _init_git_repo(repo_dir, manifest={"ms01::a": 944})

        blob = json.dumps({"tests": [{"id": "ms01::a", "status": "pass"}]})
        config_path = _write_config(tmp_path, repo_path=str(repo_dir), run_cmd=f"echo '{blob}'")

        result = CliRunner().invoke(main, [
            "acceptance", "record", "--repo", "coord-tui", "--issue", "944",
            "--sha", sha, "--config", str(config_path),
        ])
        assert result.exit_code == 1
        assert "no work assignment found" in result.output

        # #944 review: this is a lookup error (no `work` assignment for the
        # repo/issue), not a real failing-verdict "kept for inspection" case
        # — the throwaway worktree must not be left behind.
        wt_path = tmp_path / "acceptance-worktrees" / "coord-tui-944"
        assert not wt_path.exists(), "worktree leaked on no-work-assignment error path"

    def test_record_manifest_missing_cleans_up_worktree(
        self, tmp_path: Path, coord_db,
    ) -> None:
        # No manifest committed at all — `_scoped_verdict` exit(1)s inside
        # `dump_manifest_error_hint` before a work-assignment lookup ever
        # happens; that path must clean up the worktree too (#944 review).
        repo_dir = tmp_path / "repo"
        sha = _init_git_repo(repo_dir, manifest=None)

        blob = json.dumps({"tests": [{"id": "ms01::a", "status": "pass"}]})
        config_path = _write_config(tmp_path, repo_path=str(repo_dir), run_cmd=f"echo '{blob}'")

        result = CliRunner().invoke(main, [
            "acceptance", "record", "--repo", "coord-tui", "--issue", "944",
            "--sha", sha, "--config", str(config_path),
        ])
        assert result.exit_code == 1
        assert "not been authored" in result.output

        wt_path = tmp_path / "acceptance-worktrees" / "coord-tui-944"
        assert not wt_path.exists(), "worktree leaked on manifest-missing error path"


class TestAcceptanceStall:
    """`coord acceptance stall` (#846) — the worker self-report path for a
    churning acceptance slice: pinned #603 context note, best-effort WIP
    push, one-shot 'needs attention' GitHub comment."""

    def test_stall_pushes_wip_records_context_and_posts_comment(
        self, tmp_path: Path, coord_db,
    ) -> None:
        from coord import state

        repo_dir = tmp_path / "repo"
        _init_git_repo(repo_dir)
        config_path = _write_config(tmp_path, repo_path=str(repo_dir), run_cmd="true")

        state.record_dispatched(
            assignment_id="aid-1",
            proposal=Proposal(
                id=1, machine_name="laptop", repo_name="coord-tui",
                issue_number=944, issue_title="oracle loop runner", rationale="",
            ),
            repo_github="acme/coord-tui",
        )

        with patch("coord.commands.acceptance.github_ops") as mock_gh:
            result = CliRunner().invoke(main, [
                "acceptance", "stall", "--repo", "coord-tui", "--issue", "944",
                "--tried", "tightened the regex, retried the driver",
                "--stuck", "ms01::b keeps failing on the empty-input case",
                "--path", str(repo_dir), "--config", str(config_path),
            ])

        assert result.exit_code == 0, result.output
        assert "Recorded acceptance stall" in result.output
        assert "WIP snapshot pushed" in result.output

        assert mock_gh.post_issue_comment.called
        (repo_github, issue_number, body), _ = mock_gh.post_issue_comment.call_args
        assert repo_github == "acme/coord-tui"
        assert issue_number == 944
        assert "Not converging" in body
        assert "aid-1" in body
        assert "laptop" in body

        entries = state.list_issue_context("coord-tui", 944)
        assert any(
            "Acceptance stall reported" in e["body"]
            and e["pinned"]
            and e["source"] == "acceptance-stall"
            for e in entries
        )

    def test_stall_without_work_assignment_still_reports(
        self, tmp_path: Path, coord_db,
    ) -> None:
        """No dispatched work row for this issue yet — still push, note, and
        post a comment (assignment id just comes back blank)."""
        from coord import state

        repo_dir = tmp_path / "repo"
        _init_git_repo(repo_dir)
        config_path = _write_config(tmp_path, repo_path=str(repo_dir), run_cmd="true")

        with patch("coord.commands.acceptance.github_ops") as mock_gh:
            result = CliRunner().invoke(main, [
                "acceptance", "stall", "--repo", "coord-tui", "--issue", "944",
                "--tried", "x", "--stuck", "y",
                "--path", str(repo_dir), "--config", str(config_path),
            ])

        assert result.exit_code == 0, result.output
        assert mock_gh.post_issue_comment.called
        entries = state.list_issue_context("coord-tui", 944)
        assert any("Acceptance stall reported" in e["body"] for e in entries)

    def test_stall_unknown_repo_errors(self, tmp_path: Path, coord_db) -> None:
        repo_dir = tmp_path / "repo"
        _init_git_repo(repo_dir)
        config_path = _write_config(tmp_path, repo_path=str(repo_dir), run_cmd="true")

        result = CliRunner().invoke(main, [
            "acceptance", "stall", "--repo", "nope", "--issue", "944",
            "--tried", "x", "--stuck", "y",
            "--path", str(repo_dir), "--config", str(config_path),
        ])
        assert result.exit_code != 0
        assert "unknown repo" in result.output


class TestAcceptanceCapabilityRouting:
    """#966: `coord acceptance run --all` / `record` fail loudly instead of
    silently executing when this host lacks a capability the driver
    declares and another configured machine has it — no remote-exec
    plumbing to actually route there yet, so a clear error is the best
    available behavior."""

    CONFIG_YAML = """\
repos:
  - name: coord-tui
    github: acme/coord-tui
machines:
  - name: here
    host: here.tail
    repos: [coord-tui]
    repo_paths:
      coord-tui: {repo_path}
  - name: capable
    host: capable.tail
    repos: [coord-tui]
    capabilities: [browser]
acceptance:
  drivers:
    coord-tui:
      kind: tui-tuidriver
      run: {run_cmd}
      capability: browser
"""

    def _config(self, tmp_path: Path, *, repo_path: str, run_cmd: str) -> Path:
        p = tmp_path / "coordinator.yml"
        p.write_text(self.CONFIG_YAML.format(repo_path=repo_path, run_cmd=json.dumps(run_cmd)))
        return p

    def test_run_all_fails_when_local_host_lacks_capability(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        monkeypatch.setattr("socket.gethostname", lambda: "here")
        cwd = tmp_path / "repo"
        cwd.mkdir()
        config_path = self._config(tmp_path, repo_path=str(cwd), run_cmd="echo '{}'")

        result = CliRunner().invoke(main, [
            "acceptance", "run", "--repo", "coord-tui", "--all",
            "--path", str(cwd), "--config", str(config_path),
        ])
        assert result.exit_code == 1
        assert "lacks the 'browser' capability" in result.output
        assert "'capable'" in result.output
        assert "#966" in result.output

    def test_run_all_proceeds_when_local_host_has_capability(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        monkeypatch.setattr("socket.gethostname", lambda: "capable")
        cwd = tmp_path / "repo"
        cwd.mkdir()
        # "capable" has no repo_paths entry, but --path is passed explicitly
        # so find_local_repo_path is never consulted for this command.
        config_path = self._config(tmp_path, repo_path=str(cwd), run_cmd="echo '{\"tests\": []}'")

        result = CliRunner().invoke(main, [
            "acceptance", "run", "--repo", "coord-tui", "--all",
            "--path", str(cwd), "--config", str(config_path),
        ])
        # No capability gap → falls through to the ordinary "0 tests" exit.
        assert "lacks the" not in result.output
        assert result.exit_code == 1  # total == 0 → non-green, unrelated to #966

    def test_record_fails_when_local_host_lacks_capability(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        monkeypatch.setattr("socket.gethostname", lambda: "here")
        config_path = self._config(
            tmp_path, repo_path=str(tmp_path / "repo"), run_cmd="echo '{}'",
        )

        result = CliRunner().invoke(main, [
            "acceptance", "record", "--repo", "coord-tui", "--issue", "944",
            "--sha", "deadbeef", "--config", str(config_path),
        ])
        assert result.exit_code == 1
        assert "lacks the 'browser' capability" in result.output
        assert "'capable'" in result.output


class TestAcceptanceAuthor:
    """`coord acceptance author` (#931) — thin CLI glue over
    `coord.test_author.dispatch_test_author`. The dispatch logic itself
    (machine picking, briefing content, error surfaces) is unit-tested in
    tests/test_test_author.py; these just check the CLI wiring: arguments
    reach the function correctly and its outcomes map to the right exit
    code/output."""

    def _config_no_driver(self, tmp_path: Path) -> Path:
        p = tmp_path / "coordinator.yml"
        p.write_text(
            "repos:\n"
            "  - name: coord-tui\n"
            "    github: acme/coord-tui\n"
            "machines:\n"
            "  - name: laptop\n"
            "    host: laptop.tail\n"
            "    repos: [coord-tui]\n"
            "    repo_paths:\n"
            "      coord-tui: /tmp/repo\n"
        )
        return p

    def test_happy_path_reports_dispatch(self, tmp_path: Path, monkeypatch) -> None:
        config_path = self._config_no_driver(tmp_path)
        calls = {}

        def fake_dispatch(repo, tracking_issue, cfg, *, issue_number=None, machine_override=None):
            calls.update(
                repo=repo, tracking_issue=tracking_issue,
                issue_number=issue_number, machine_override=machine_override,
            )
            return ("aid-42", "laptop")

        monkeypatch.setattr("coord.test_author.dispatch_test_author", fake_dispatch)

        result = CliRunner().invoke(main, [
            "acceptance", "author", "coord-tui", "947", "--config", str(config_path),
        ])
        assert result.exit_code == 0, result.output
        assert "aid-42" in result.output
        assert "laptop" in result.output
        assert "full milestone" in result.output
        assert calls == {
            "repo": "coord-tui", "tracking_issue": 947,
            "issue_number": None, "machine_override": None,
        }

    def test_issue_scope_and_machine_override_forwarded(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        config_path = self._config_no_driver(tmp_path)
        calls = {}

        def fake_dispatch(repo, tracking_issue, cfg, *, issue_number=None, machine_override=None):
            calls.update(issue_number=issue_number, machine_override=machine_override)
            return ("aid-7", "dellserver")

        monkeypatch.setattr("coord.test_author.dispatch_test_author", fake_dispatch)

        result = CliRunner().invoke(main, [
            "acceptance", "author", "coord-tui", "947",
            "--issue", "101", "--machine", "dellserver",
            "--config", str(config_path),
        ])
        assert result.exit_code == 0, result.output
        assert "issue #101 slice" in result.output
        assert calls == {"issue_number": 101, "machine_override": "dellserver"}

    def test_dispatch_error_surfaces_nonzero_exit(self, tmp_path: Path, monkeypatch) -> None:
        config_path = self._config_no_driver(tmp_path)

        def fake_dispatch(*a, **kw):
            raise RuntimeError("no acceptance driver configured for repo 'coord-tui'")

        monkeypatch.setattr("coord.test_author.dispatch_test_author", fake_dispatch)

        result = CliRunner().invoke(main, [
            "acceptance", "author", "coord-tui", "947", "--config", str(config_path),
        ])
        assert result.exit_code == 1
        assert "no acceptance driver configured" in result.output


class TestAcceptanceMock:
    """#930 (Gate A): `coord acceptance mock <repo> <tracking_issue>`
    dispatches the mock-author. Mocks `coord.github_ops`, `coord.
    board_service.read_board`, and `coord.dispatch.dispatch_with_retry` so
    the test drives the real Click command end to end without a live `gh`
    call or HTTP POST to an agent."""

    def test_dispatches_mock_author_and_prints_assignment_id(
        self, tmp_path: Path,
    ) -> None:
        from unittest.mock import patch

        from coord.models import Board

        config_path = _write_config(
            tmp_path, repo_path=str(tmp_path / "repo"), run_cmd="echo {}",
        )
        issue_data = {
            "number": 100, "title": "Milestone tracker", "body": "",
            "milestone": {"number": 9, "title": "Q3"},
        }
        with patch("coord.github_ops.get_issue", return_value=issue_data), \
             patch("coord.github_ops.get_open_issues", return_value=[]), \
             patch("coord.board_service.read_board", return_value=Board()), \
             patch(
                 "coord.dispatch.dispatch_with_retry",
                 return_value={"id": "mock-asg-1"},
             ) as disp, \
             patch("coord.dispatch.post_briefing"), \
             patch("coord.state.record_dispatched") as mock_record:
            result = CliRunner().invoke(main, [
                "acceptance", "mock", "coord-tui", "100", "--config", str(config_path),
            ])

        assert result.exit_code == 0, result.output
        assert "laptop" in result.output
        assert "mock-asg-1" in result.output
        disp.assert_called_once()
        proposal = disp.call_args[0][0]
        assert proposal.type == "mock-author"
        assert proposal.target_branch == "ms-9-gate-a"
        mock_record.assert_called_once()

    def test_unknown_repo_errors(self, tmp_path: Path) -> None:
        config_path = _write_config(
            tmp_path, repo_path=str(tmp_path / "repo"), run_cmd="echo {}",
        )
        result = CliRunner().invoke(main, [
            "acceptance", "mock", "nope", "100", "--config", str(config_path),
        ])
        assert result.exit_code == 2
        assert "unknown repo" in result.output

    def test_no_milestone_errors(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        config_path = _write_config(
            tmp_path, repo_path=str(tmp_path / "repo"), run_cmd="echo {}",
        )
        with patch(
            "coord.github_ops.get_issue",
            return_value={"number": 100, "title": "t", "body": "", "milestone": None},
        ):
            result = CliRunner().invoke(main, [
                "acceptance", "mock", "coord-tui", "100", "--config", str(config_path),
            ])
        assert result.exit_code == 1
        assert "no milestone" in result.output
