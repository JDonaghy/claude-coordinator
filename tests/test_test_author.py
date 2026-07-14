"""Tests for coord.test_author — #931 (docs/ORACLE_LOOP.md independent
`type="test-author"` dispatch).

Mirrors tests/test_milestone_dispatch.py's shape: pure-function tests for
machine picking + briefing content seed Config/Machine objects directly;
`dispatch_test_author` tests mock `coord.github_ops`, `coord.milestone_
dispatch.fetch_milestone_context`, and the HTTP POST so no live network call
ever happens.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from coord.config import AcceptanceConfig, AcceptanceDriverConfig, Config
from coord.milestone_dispatch import MilestoneContext, MilestoneDispatchError
from coord.milestone_order import WorkOrder, WorkOrderNode
from coord.models import Machine, Repo, WorkerPermissionsConfig
from coord.test_author import (
    TEST_AUTHOR_DENY_COMMANDS,
    build_test_author_briefing,
    dispatch_test_author,
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
