"""Tests for coord.test_author — #931 (docs/ORACLE_LOOP.md independent
`type="test-author"` dispatch).

Mirrors tests/test_milestone_dispatch.py's shape: pure-function tests for
machine picking + briefing content seed Config/Machine objects directly;
`dispatch_test_author` tests mock `coord.github_ops`, `coord.milestone_
dispatch.fetch_milestone_context`, and the HTTP POST so no live network call
ever happens.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from coord.agent import _slugify
from coord.config import AcceptanceConfig, AcceptanceDriverConfig, Config
from coord.milestone_dispatch import MilestoneContext, MilestoneDispatchError
from coord.milestone_order import WorkOrder, WorkOrderNode
from coord.models import Machine, Repo, WorkerPermissionsConfig
from coord.test_author import (
    TEST_AUTHOR_DENY_COMMANDS,
    TEST_AUTHOR_INTERACTIVE_SYSTEM_PROMPT,
    build_test_author_briefing,
    dispatch_test_author,
    dispatch_test_author_interactive,
    pick_test_author_machine,
)


def _machine(name: str, repos: list[str], caps: list[str] | None = None) -> Machine:
    return Machine(
        name=name,
        host=f"{name}.tailnet",
        repos=repos,
        repo_paths={r: f"/tmp/{name}/{r}" for r in repos},
        capabilities=caps or [],
    )


def _config(
    machines: list[Machine],
    *,
    repo_name: str = "coord-tui",
    driver: AcceptanceDriverConfig | None = None,
    worker_permissions: WorkerPermissionsConfig | None = None,
) -> Config:
    repo = Repo(
        name=repo_name,
        github="acme/coord-tui",
        worker_permissions=worker_permissions,
    )
    acceptance = AcceptanceConfig(drivers={repo_name: driver} if driver else {})
    return Config(repos=[repo], machines=machines, acceptance=acceptance)


WORK_ORDER = WorkOrder(nodes=(WorkOrderNode(101), WorkOrderNode(102)))


@pytest.fixture(autouse=True)
def _pr_not_merged(monkeypatch):
    """#1172: default the merged-branch dispatch guard to "not merged" so
    the existing happy-path tests below (which don't care about this check)
    don't shell out to a real ``gh`` subprocess via
    ``coord.github_ops.pr_is_merged``. Tests exercising the guard itself
    re-patch it to opt in — mirrors conftest.py's module-attr-stub
    convention for ``work_is_terminal``, scoped locally to this file since
    the guard is specific to ``dispatch_test_author``."""
    monkeypatch.setattr("coord.test_author.github_ops.pr_is_merged", lambda *a, **k: False)


# ── pick_test_author_machine ────────────────────────────────────────────────


class TestPickTestAuthorMachine:
    def test_picks_machine_with_repo(self) -> None:
        cfg = _config([_machine("laptop", ["coord-tui"])])
        m = pick_test_author_machine(cfg, "coord-tui")
        assert m is not None
        assert m.name == "laptop"

    def test_excludes_machine_without_repo(self) -> None:
        cfg = _config([_machine("dellserver", ["quadraui"])])
        assert pick_test_author_machine(cfg, "coord-tui") is None

    def test_filters_by_required_capability(self) -> None:
        cfg = _config([
            _machine("no-gtk", ["coord-tui"], caps=[]),
            _machine("has-gtk", ["coord-tui"], caps=["gtk"]),
        ])
        m = pick_test_author_machine(cfg, "coord-tui", "gtk")
        assert m is not None
        assert m.name == "has-gtk"

    def test_no_capability_match_returns_none(self) -> None:
        cfg = _config([_machine("laptop", ["coord-tui"], caps=[])])
        assert pick_test_author_machine(cfg, "coord-tui", "gtk") is None

    def test_excludes_paused_machine(self) -> None:
        cfg = _config([_machine("laptop", ["coord-tui"])])
        with patch("coord.test_author.paused_set", return_value={"laptop"}):
            assert pick_test_author_machine(cfg, "coord-tui") is None


# ── build_test_author_briefing ──────────────────────────────────────────────


class TestBuildBriefing:
    def _kwargs(self, **overrides):
        base = dict(
            repo_name="coord-tui",
            repo_github="acme/coord-tui",
            ms_dir="ms-25",
            tracking_issue=947,
            milestone_number=25,
            milestone_issue_numbers=[101, 102],
            driver_kind="tui-tuidriver",
            driver_run="cargo test --test acceptance",
            issue_number=None,
            issue_title=None,
            issue_body=None,
        )
        base.update(overrides)
        return base

    def test_milestone_mode_mentions_full_authoring(self) -> None:
        briefing = build_test_author_briefing(**self._kwargs())
        assert "tests/acceptance/ms-25/contract.md" in briefing
        assert "full milestone authoring" in briefing
        assert "101" in briefing and "102" in briefing
        assert "cargo test --test acceptance" in briefing

    def test_jit_mode_scopes_to_one_issue(self) -> None:
        briefing = build_test_author_briefing(**self._kwargs(
            issue_number=101, issue_title="Add foo", issue_body="Body text",
        ))
        assert "just-in-time slice extension for issue #101" in briefing
        assert "Add foo" in briefing
        assert "Body text" in briefing


# ── dispatch_test_author ────────────────────────────────────────────────────


class TestDispatchTestAuthor:
    def _driver(self, capability: str = "") -> AcceptanceDriverConfig:
        return AcceptanceDriverConfig(
            kind="tui-tuidriver", run="cargo test --test acceptance", capability=capability,
        )

    def _ctx(self, milestone_number: int = 25) -> MilestoneContext:
        return MilestoneContext(
            tracking_issue=947, milestone_number=milestone_number, work_order=WORK_ORDER,
        )

    def test_unknown_repo_raises(self) -> None:
        cfg = _config([_machine("laptop", ["coord-tui"])], driver=self._driver())
        with pytest.raises(RuntimeError, match="not in coordinator.yml"):
            dispatch_test_author("nope", 947, cfg)

    def test_missing_driver_raises(self) -> None:
        cfg = _config([_machine("laptop", ["coord-tui"])], driver=None)
        with pytest.raises(RuntimeError, match="no acceptance driver configured"):
            dispatch_test_author("coord-tui", 947, cfg)

    def _routed_config(self, machines: list[Machine]) -> Config:
        repo = Repo(name="coord-tui", github="acme/coord-tui")
        acceptance = AcceptanceConfig(drivers={
            "coord-tui": AcceptanceDriverConfig(routes=[
                AcceptanceDriverConfig(
                    match="coord/**", kind="cli-pytest",
                    run="pytest tests/acceptance/{ms}",
                ),
            ]),
        })
        return Config(repos=[repo], machines=machines, acceptance=acceptance)

    def test_routed_driver_without_path_raises_actionable_error(self) -> None:
        """#1125 review finding 1: a routed repo with no --for-path must not
        get the generic "no acceptance driver configured" message (it DOES
        have one — just no path to resolve it) — the error should point at
        --for-path instead."""
        cfg = self._routed_config([_machine("laptop", ["coord-tui"])])
        with pytest.raises(RuntimeError, match="no route matched"):
            dispatch_test_author("coord-tui", 947, cfg)

    def test_routed_driver_with_matching_path_resolves(self) -> None:
        """#1125 review finding 1/2: with a path that matches a route, the
        test-author dispatches using that route's kind/run (e.g. reaching
        the briefing, which embeds driver_kind/driver_run)."""
        cfg = self._routed_config([_machine("laptop", ["coord-tui"])])
        fake_client = MagicMock()
        fake_resp = MagicMock()
        fake_resp.json.return_value = {"id": "routed-1"}
        fake_client.post.return_value = fake_resp

        with patch("coord.test_author.fetch_milestone_context", return_value=self._ctx()), \
             patch("coord.state.record_dispatched_assignment"):
            assignment_id, machine_name = dispatch_test_author(
                "coord-tui", 947, cfg, path="coord/acceptance.py",
                http_client=fake_client,
            )

        assert assignment_id == "routed-1"
        payload = fake_client.post.call_args.kwargs["json"]
        assert "cli-pytest" in payload["briefing"]
        assert "pytest tests/acceptance/{ms}" in payload["briefing"]

    def test_milestone_fetch_failure_raises(self) -> None:
        cfg = _config([_machine("laptop", ["coord-tui"])], driver=self._driver())
        with patch(
            "coord.test_author.fetch_milestone_context",
            side_effect=MilestoneDispatchError("no milestone"),
        ):
            with pytest.raises(RuntimeError, match="no milestone"):
                dispatch_test_author("coord-tui", 947, cfg)

    def test_issue_not_in_work_order_raises(self) -> None:
        cfg = _config([_machine("laptop", ["coord-tui"])], driver=self._driver())
        with patch("coord.test_author.fetch_milestone_context", return_value=self._ctx()):
            with pytest.raises(RuntimeError, match="not a member"):
                dispatch_test_author("coord-tui", 947, cfg, issue_number=999)

    def test_no_capable_machine_raises(self) -> None:
        cfg = _config(
            [_machine("laptop", ["coord-tui"], caps=[])],
            driver=self._driver(capability="gtk"),
        )
        with patch("coord.test_author.fetch_milestone_context", return_value=self._ctx()):
            with pytest.raises(RuntimeError, match="no machine claims repo"):
                dispatch_test_author("coord-tui", 947, cfg)

    def test_machine_override_unknown_raises(self) -> None:
        cfg = _config([_machine("laptop", ["coord-tui"])], driver=self._driver())
        with patch("coord.test_author.fetch_milestone_context", return_value=self._ctx()):
            with pytest.raises(RuntimeError, match="not in coordinator.yml"):
                dispatch_test_author(
                    "coord-tui", 947, cfg, machine_override="ghost",
                )

    def test_happy_path_milestone_mode_posts_and_records(self) -> None:
        cfg = _config([_machine("laptop", ["coord-tui"])], driver=self._driver())
        fake_client = MagicMock()
        fake_resp = MagicMock()
        fake_resp.json.return_value = {"id": "abc123"}
        fake_client.post.return_value = fake_resp

        with patch("coord.test_author.fetch_milestone_context", return_value=self._ctx()), \
             patch("coord.state.record_dispatched_assignment") as record_mock:
            assignment_id, machine_name = dispatch_test_author(
                "coord-tui", 947, cfg, http_client=fake_client,
            )

        assert assignment_id == "abc123"
        assert machine_name == "laptop"
        fake_resp.raise_for_status.assert_called_once()

        url, kwargs = fake_client.post.call_args
        assert url[0] == "http://laptop.tailnet:7433/assign"
        payload = kwargs["json"]
        assert payload["type"] == "test-author"
        assert payload["files_forbidden"] == []
        assert payload["issue_number"] == 947
        assert "acceptance suite" in payload["issue_title"]
        for cmd in TEST_AUTHOR_DENY_COMMANDS:
            assert cmd in payload["deny_commands"]
        record_mock.assert_called_once()

        # #1171: milestone mode keeps the single shared-branch behavior —
        # no target_branch override, and the recorded Assignment.branch is
        # the tracking-issue-keyed derivation (mirrors AgentServer's
        # `issue-{N}-{slug(title)}` formula) so repeated Gate-A calls land
        # on the same branch/PR.
        assert "target_branch" not in payload
        recorded = record_mock.call_args.kwargs["assignment"]
        assert recorded.branch == "issue-947-test-author-ms-25-acceptance-suite"

    def test_happy_path_jit_mode_fetches_issue(self) -> None:
        cfg = _config([_machine("laptop", ["coord-tui"])], driver=self._driver())
        fake_client = MagicMock()
        fake_resp = MagicMock()
        fake_resp.json.return_value = {"id": "xyz789"}
        fake_client.post.return_value = fake_resp

        with patch("coord.test_author.fetch_milestone_context", return_value=self._ctx()), \
             patch(
                 "coord.test_author.github_ops.get_issue",
                 return_value={"title": "Add foo", "body": "Body text"},
             ) as get_issue_mock, \
             patch("coord.state.record_dispatched_assignment"):
            assignment_id, machine_name = dispatch_test_author(
                "coord-tui", 947, cfg, issue_number=101, http_client=fake_client,
            )

        assert assignment_id == "xyz789"
        get_issue_mock.assert_called_once_with("acme/coord-tui", 101)
        payload = fake_client.post.call_args.kwargs["json"]
        assert "Add foo" in payload["briefing"]
        assert "Body text" in payload["briefing"]

    def test_jit_slice_gets_its_own_branch_outside_issue_namespace(self) -> None:
        """#1171: a JIT slice must NOT collapse onto the milestone's shared
        `issue-{tracking_issue}-*` branch — the previous behavior meant a
        squash-merged first slice's PR silently stranded every later slice
        pushed to the same branch. The per-slice branch must also avoid the
        `issue-{issue_number}-*` namespace (the member issue's OWN prefix) or
        `coord.claim`'s remote-branch check would false-positive against
        that issue's Work dispatch if the branch survives the merge."""
        cfg = _config([_machine("laptop", ["coord-tui"])], driver=self._driver())
        fake_client = MagicMock()
        fake_resp = MagicMock()
        fake_resp.json.return_value = {"id": "xyz789"}
        fake_client.post.return_value = fake_resp

        with patch("coord.test_author.fetch_milestone_context", return_value=self._ctx()), \
             patch(
                 "coord.test_author.github_ops.get_issue",
                 return_value={"title": "Add foo", "body": "Body text"},
             ), \
             patch("coord.state.record_dispatched_assignment") as record_mock:
            dispatch_test_author(
                "coord-tui", 947, cfg, issue_number=101, http_client=fake_client,
            )

        payload = fake_client.post.call_args.kwargs["json"]
        target_branch = payload["target_branch"]
        assert not target_branch.startswith("issue-101-")
        assert not target_branch.startswith("issue-947-")
        recorded = record_mock.call_args.kwargs["assignment"]
        assert recorded.branch == target_branch

    def test_jit_slices_for_different_issues_get_different_branches(self) -> None:
        """The whole point of #1171: two JIT slices for the SAME milestone
        but DIFFERENT member issues must not collide on one branch, or the
        second slice strands behind the first slice's already-merged PR."""
        cfg = _config([_machine("laptop", ["coord-tui"])], driver=self._driver())

        def _dispatch(issue_number: int) -> str:
            fake_client = MagicMock()
            fake_resp = MagicMock()
            fake_resp.json.return_value = {"id": f"id-{issue_number}"}
            fake_client.post.return_value = fake_resp
            with patch(
                "coord.test_author.fetch_milestone_context", return_value=self._ctx(),
            ), patch(
                "coord.test_author.github_ops.get_issue",
                return_value={"title": "t", "body": "b"},
            ), patch("coord.state.record_dispatched_assignment"):
                dispatch_test_author(
                    "coord-tui", 947, cfg, issue_number=issue_number,
                    http_client=fake_client,
                )
            payload = fake_client.post.call_args.kwargs["json"]
            return payload["target_branch"]

        assert _dispatch(101) != _dispatch(102)

    def test_jit_slice_retry_reuses_same_branch(self) -> None:
        """A retry/continuation for the SAME (tracking_issue, issue_number)
        pair must resolve to the same branch name so it keeps extending its
        own slice's still-open PR instead of forking a new one each time."""
        cfg = _config([_machine("laptop", ["coord-tui"])], driver=self._driver())

        def _dispatch() -> str:
            fake_client = MagicMock()
            fake_resp = MagicMock()
            fake_resp.json.return_value = {"id": "abc"}
            fake_client.post.return_value = fake_resp
            with patch(
                "coord.test_author.fetch_milestone_context", return_value=self._ctx(),
            ), patch(
                "coord.test_author.github_ops.get_issue",
                return_value={"title": "t", "body": "b"},
            ), patch("coord.state.record_dispatched_assignment"):
                dispatch_test_author(
                    "coord-tui", 947, cfg, issue_number=101, http_client=fake_client,
                )
            payload = fake_client.post.call_args.kwargs["json"]
            return payload["target_branch"]

        assert _dispatch() == _dispatch()

    def test_milestone_mode_branch_unaffected_by_jit_change(self) -> None:
        """Milestone mode (no --issue) must keep deriving the single shared
        branch from the fixed title — unchanged by the JIT per-slice fix."""
        cfg = _config([_machine("laptop", ["coord-tui"])], driver=self._driver())
        fake_client = MagicMock()
        fake_resp = MagicMock()
        fake_resp.json.return_value = {"id": "abc123"}
        fake_client.post.return_value = fake_resp

        with patch("coord.test_author.fetch_milestone_context", return_value=self._ctx()), \
             patch("coord.state.record_dispatched_assignment"):
            dispatch_test_author("coord-tui", 947, cfg, http_client=fake_client)

        payload = fake_client.post.call_args.kwargs["json"]
        assert "target_branch" not in payload

    def test_merged_branch_refuses_milestone_mode(self) -> None:
        """#1172: a stale milestone-mode dispatch after Gate A's shared-suite
        branch already has a merged PR must fail loudly instead of pushing a
        new commit onto a dead branch with nothing to review/merge it."""
        cfg = _config([_machine("laptop", ["coord-tui"])], driver=self._driver())
        fake_client = MagicMock()

        with patch("coord.test_author.fetch_milestone_context", return_value=self._ctx()), \
             patch("coord.test_author.github_ops.pr_is_merged", return_value=True) as pr_merged_mock, \
             patch("coord.state.record_dispatched_assignment") as record_mock:
            with pytest.raises(RuntimeError, match="already has a merged PR"):
                dispatch_test_author("coord-tui", 947, cfg, http_client=fake_client)

        # Refused BEFORE dispatching — no HTTP call, no board row recorded.
        fake_client.post.assert_not_called()
        record_mock.assert_not_called()
        pr_merged_mock.assert_called_once_with(
            "acme/coord-tui", "issue-947-test-author-ms-25-acceptance-suite",
        )

    def test_merged_branch_refuses_jit_mode_retry(self) -> None:
        """#1172 defence-in-depth: even with #1171's branch-per-slice fix, a
        RETRY of the same (tracking_issue, issue_number) pair after that
        slice's own PR already merged (e.g. via #1138's oracle gate) must
        refuse rather than silently stranding the retry's commit."""
        cfg = _config([_machine("laptop", ["coord-tui"])], driver=self._driver())
        fake_client = MagicMock()

        with patch("coord.test_author.fetch_milestone_context", return_value=self._ctx()), \
             patch(
                 "coord.test_author.github_ops.get_issue",
                 return_value={"title": "Add foo", "body": "Body text"},
             ), \
             patch("coord.test_author.github_ops.pr_is_merged", return_value=True), \
             patch("coord.state.record_dispatched_assignment") as record_mock:
            with pytest.raises(RuntimeError, match="already has a merged PR"):
                dispatch_test_author(
                    "coord-tui", 947, cfg, issue_number=101, http_client=fake_client,
                )

        fake_client.post.assert_not_called()
        record_mock.assert_not_called()

    def test_open_pr_on_branch_does_not_block_dispatch(self) -> None:
        """The guard must only fire on a MERGED PR — a branch with a still-
        open PR (the normal "extend the same in-flight suite" case) must
        keep dispatching exactly as before."""
        cfg = _config([_machine("laptop", ["coord-tui"])], driver=self._driver())
        fake_client = MagicMock()
        fake_resp = MagicMock()
        fake_resp.json.return_value = {"id": "abc123"}
        fake_client.post.return_value = fake_resp

        with patch("coord.test_author.fetch_milestone_context", return_value=self._ctx()), \
             patch("coord.test_author.github_ops.pr_is_merged", return_value=False), \
             patch("coord.state.record_dispatched_assignment") as record_mock:
            assignment_id, machine_name = dispatch_test_author(
                "coord-tui", 947, cfg, http_client=fake_client,
            )

        assert assignment_id == "abc123"
        fake_client.post.assert_called_once()
        record_mock.assert_called_once()

    def test_repo_deny_commands_merged(self) -> None:
        cfg = _config(
            [_machine("laptop", ["coord-tui"])],
            driver=self._driver(),
            worker_permissions=WorkerPermissionsConfig(deny=["Bash(rm -rf *)"]),
        )
        fake_client = MagicMock()
        fake_resp = MagicMock()
        fake_resp.json.return_value = {"id": "abc123"}
        fake_client.post.return_value = fake_resp

        with patch("coord.test_author.fetch_milestone_context", return_value=self._ctx()), \
             patch("coord.state.record_dispatched_assignment"):
            dispatch_test_author("coord-tui", 947, cfg, http_client=fake_client)

        payload = fake_client.post.call_args.kwargs["json"]
        assert "Bash(rm -rf *)" in payload["deny_commands"]
        assert "Bash(gh *)" in payload["deny_commands"]


class TestDispatchTestAuthorInteractive:
    """#1173: `dispatch_test_author_interactive` — the human-attended
    counterpart to `dispatch_test_author`, reusing `coord.interactive` /
    `coord.commands.dispatch._build_interactive_launch_setup` /
    `coord.agent.setup_interactive_worktree` instead of POSTing to an
    agent's `/assign`. Only the git/tmux/PTY primitives are mocked here —
    `record_dispatched_assignment` runs for real against the autouse
    in-memory DB (see conftest.coord_db) so assertions read the resulting
    board row back, the same black-box shape as tests/test_cli_assign.py's
    interactive flavours."""

    def _driver(self, capability: str = "") -> AcceptanceDriverConfig:
        return AcceptanceDriverConfig(
            kind="tui-tuidriver", run="cargo test --test acceptance", capability=capability,
        )

    def _ctx(self, milestone_number: int = 25) -> MilestoneContext:
        return MilestoneContext(
            tracking_issue=947, milestone_number=milestone_number, work_order=WORK_ORDER,
        )

    def _expected_branch(self, milestone_number: int = 25, tracking_issue: int = 947) -> str:
        title = f"[test-author] ms-{milestone_number} acceptance suite"
        return f"issue-{tracking_issue}-{_slugify(title)}"

    # ── resolution failures — same failure modes as dispatch_test_author ──

    def test_unknown_repo_raises(self) -> None:
        cfg = _config([_machine("laptop", ["coord-tui"])], driver=self._driver())
        with pytest.raises(RuntimeError, match="not in coordinator.yml"):
            dispatch_test_author_interactive("nope", 947, cfg)

    def test_missing_driver_raises(self) -> None:
        cfg = _config([_machine("laptop", ["coord-tui"])], driver=None)
        with pytest.raises(RuntimeError, match="no acceptance driver configured"):
            dispatch_test_author_interactive("coord-tui", 947, cfg)

    def test_issue_not_in_work_order_raises(self) -> None:
        cfg = _config([_machine("laptop", ["coord-tui"])], driver=self._driver())
        with patch("coord.test_author.fetch_milestone_context", return_value=self._ctx()):
            with pytest.raises(RuntimeError, match="not a member"):
                dispatch_test_author_interactive("coord-tui", 947, cfg, issue_number=999)

    def test_no_capable_machine_raises(self) -> None:
        cfg = _config(
            [_machine("laptop", ["coord-tui"], caps=[])],
            driver=self._driver(capability="gtk"),
        )
        with patch("coord.test_author.fetch_milestone_context", return_value=self._ctx()):
            with pytest.raises(RuntimeError, match="no machine claims repo"):
                dispatch_test_author_interactive("coord-tui", 947, cfg)

    # ── dry run ─────────────────────────────────────────────────────────

    def test_dry_run_does_not_persist_or_launch(self) -> None:
        cfg = _config([_machine("laptop", ["coord-tui"])], driver=self._driver())
        setup_spy = MagicMock()
        launch_spy = MagicMock()
        with patch("coord.test_author.fetch_milestone_context", return_value=self._ctx()), \
             patch("socket.gethostname", return_value="laptop"), \
             patch("coord.agent.setup_interactive_worktree", setup_spy), \
             patch("coord.interactive.launch_human_attended_interactive", launch_spy):
            exit_code = dispatch_test_author_interactive(
                "coord-tui", 947, cfg, dry_run=True,
            )

        assert exit_code == 0
        assert setup_spy.call_count == 0
        assert launch_spy.call_count == 0

        from coord.state import build_board
        b = build_board()
        assert b.active == []
        assert b.completed == []

    # ── local: creates the row, launches, and finalizes ────────────────

    def test_local_creates_test_author_row_with_claude_pty(self) -> None:
        """Core #1173 acceptance bar: the row that lands on the board is
        `type="test-author"` + `provider_name="claude-pty"`, and the
        session ran through the human-attended launcher — never the
        headless POST-to-/assign path."""
        cfg = _config([_machine("laptop", ["coord-tui"])], driver=self._driver())
        fake_finalize = MagicMock(
            already_recorded=False, terminal_status="done",
            commits_ahead=1, push_ok=True,
        )
        setup_spy = MagicMock(return_value=(Path("/tmp/wt-1"), self._expected_branch()))
        launch_spy = MagicMock(return_value=0)
        finalize_spy = MagicMock(return_value=fake_finalize)
        headless_spy = MagicMock(side_effect=AssertionError("must not fall through to headless"))

        with patch("coord.test_author.fetch_milestone_context", return_value=self._ctx()), \
             patch("socket.gethostname", return_value="laptop"), \
             patch("coord.agent.setup_interactive_worktree", setup_spy), \
             patch("coord.interactive.launch_human_attended_interactive", launch_spy), \
             patch("coord.interactive.finalize_interactive_exit", finalize_spy), \
             patch("coord.interactive.tmux_available", return_value=False), \
             patch("coord.test_author.dispatch_test_author", headless_spy):
            exit_code = dispatch_test_author_interactive("coord-tui", 947, cfg)

        assert exit_code == 0
        assert setup_spy.call_count == 1
        assert launch_spy.call_count == 1
        assert finalize_spy.call_count == 1
        assert headless_spy.call_count == 0, "must not dispatch the headless worker"

        # setup_interactive_worktree got the SAME branch name the record used
        # (continuation-safe: a later JIT/retry dispatch derives identically).
        assert setup_spy.call_args.kwargs["existing_branch"] == self._expected_branch()

        # The independence contract is preserved verbatim (plus the
        # human-attended note) in the argv the launcher actually ran.
        launched_argv = launch_spy.call_args.args[0]
        prompt_idx = launched_argv.index("--system-prompt") + 1
        launched_prompt = launched_argv[prompt_idx]
        assert "ZERO shared context" in launched_prompt
        assert "HUMAN-ATTENDED" in launched_prompt
        assert launched_prompt.startswith(TEST_AUTHOR_INTERACTIVE_SYSTEM_PROMPT.split("\n\n")[0])

        from coord.state import build_board
        rows = [
            a for a in build_board().active + build_board().completed
            if a.type == "test-author"
        ]
        assert len(rows) == 1
        row = rows[0]
        assert row.provider_name == "claude-pty"
        assert row.branch == self._expected_branch()
        assert row.for_issue_number is None
        assert row.issue_number == 947

    def test_local_jit_mode_sets_for_issue_number(self) -> None:
        cfg = _config([_machine("laptop", ["coord-tui"])], driver=self._driver())
        fake_finalize = MagicMock(
            already_recorded=False, terminal_status="done",
            commits_ahead=1, push_ok=True,
        )
        with patch("coord.test_author.fetch_milestone_context", return_value=self._ctx()), \
             patch(
                 "coord.test_author.github_ops.get_issue",
                 return_value={"title": "Add foo", "body": "Body text"},
             ), \
             patch("socket.gethostname", return_value="laptop"), \
             patch(
                 "coord.agent.setup_interactive_worktree",
                 return_value=(Path("/tmp/wt-2"), self._expected_branch()),
             ), \
             patch("coord.interactive.launch_human_attended_interactive", return_value=0), \
             patch("coord.interactive.finalize_interactive_exit", return_value=fake_finalize), \
             patch("coord.interactive.tmux_available", return_value=False):
            dispatch_test_author_interactive("coord-tui", 947, cfg, issue_number=101)

        from coord.state import build_board
        rows = [
            a for a in build_board().active + build_board().completed
            if a.type == "test-author"
        ]
        assert len(rows) == 1
        assert rows[0].for_issue_number == 101
        assert "Add foo" in rows[0].briefing

    def test_local_worktree_failure_raises_and_marks_failure_reason(self) -> None:
        from coord.agent import _GitError

        cfg = _config([_machine("laptop", ["coord-tui"])], driver=self._driver())
        with patch("coord.test_author.fetch_milestone_context", return_value=self._ctx()), \
             patch("socket.gethostname", return_value="laptop"), \
             patch(
                 "coord.agent.setup_interactive_worktree",
                 side_effect=_GitError("boom"),
             ):
            with pytest.raises(RuntimeError, match="worktree-add failed"):
                dispatch_test_author_interactive("coord-tui", 947, cfg)

        from coord.state import build_board
        rows = [a for a in build_board().active + build_board().completed if a.type == "test-author"]
        assert len(rows) == 1
        assert rows[0].status == "failed"
        assert rows[0].failure_reason and "boom" in rows[0].failure_reason

    # ── remote: named-branch continuation over ssh+tmux ────────────────

    def test_remote_creates_test_author_row_via_tmux(self) -> None:
        cfg = _config([_machine("dellserver", ["coord-tui"])], driver=self._driver())
        fake_finalize = MagicMock(
            already_recorded=False, terminal_status="done",
            commits_ahead=2, push_ok=True,
        )
        tmux_spy = MagicMock(return_value=0)
        finalize_spy = MagicMock(return_value=fake_finalize)
        headless_spy = MagicMock(side_effect=AssertionError("must not fall through to headless"))

        with patch("coord.test_author.fetch_milestone_context", return_value=self._ctx()), \
             patch(
                 "coord.test_author.github_ops.get_issue",
                 return_value={"title": "Add foo", "body": "Body text"},
             ), \
             patch("socket.gethostname", return_value="operator-laptop"), \
             patch("coord.interactive._launch_via_tmux", tmux_spy), \
             patch("coord.interactive.tmux_session_alive", return_value=False), \
             patch("coord.interactive.finalize_remote_interactive_exit", finalize_spy), \
             patch("coord.test_author.dispatch_test_author", headless_spy):
            exit_code = dispatch_test_author_interactive(
                "coord-tui", 947, cfg, issue_number=101,
            )

        assert exit_code == 0
        assert tmux_spy.call_count == 1
        assert finalize_spy.call_count == 1
        assert headless_spy.call_count == 0

        from coord.state import build_board
        rows = [
            a for a in build_board().active + build_board().completed
            if a.type == "test-author"
        ]
        assert len(rows) == 1
        row = rows[0]
        assert row.provider_name == "claude-pty"
        assert row.for_issue_number == 101
        assert row.branch == self._expected_branch()

    def test_remote_session_still_alive_skips_finalize(self) -> None:
        cfg = _config([_machine("dellserver", ["coord-tui"])], driver=self._driver())
        finalize_spy = MagicMock()

        with patch("coord.test_author.fetch_milestone_context", return_value=self._ctx()), \
             patch("socket.gethostname", return_value="operator-laptop"), \
             patch("coord.interactive._launch_via_tmux", return_value=0), \
             patch("coord.interactive.tmux_session_alive", return_value=True), \
             patch("coord.interactive.finalize_remote_interactive_exit", finalize_spy):
            exit_code = dispatch_test_author_interactive("coord-tui", 947, cfg)

        assert exit_code == 0
        assert finalize_spy.call_count == 0
