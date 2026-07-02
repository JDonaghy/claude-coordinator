"""Tests for the milestone-chat seed builder and dispatcher (#770, Phase 2
of #767)."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from coord.agent import (
    MILESTONE_CHAT_DENY_COMMANDS,
    MILESTONE_CHAT_SYSTEM_PROMPT,
    WRITE_CAPABLE_SPEC_TYPES,
    AssignmentSpec,
    default_worker_command,
)
from coord.models import Machine
from coord import milestone_chat


# ── build_milestone_chat_briefing ────────────────────────────────────────────


def test_briefing_includes_repo_and_milestone():
    out = milestone_chat.build_milestone_chat_briefing(
        repo_name="api",
        repo_slug="acme/api",
        milestone_title="Q3 push",
        tracking_issue_number=100,
        tracking_issue_body="",
        issues=[],
    )
    assert "acme/api" in out
    assert "Q3 push" in out
    assert "#100" in out


def test_briefing_includes_tracking_issue_body():
    out = milestone_chat.build_milestone_chat_briefing(
        repo_name="api",
        repo_slug="acme/api",
        milestone_title="M",
        tracking_issue_number=100,
        tracking_issue_body="## Work order\n- [ ] #1\n",
        issues=[],
    )
    assert "## Work order\n- [ ] #1" in out


def test_briefing_handles_empty_tracking_body():
    out = milestone_chat.build_milestone_chat_briefing(
        repo_name="api",
        repo_slug="acme/api",
        milestone_title="M",
        tracking_issue_number=100,
        tracking_issue_body="",
        issues=[],
    )
    assert "(empty)" in out


def test_briefing_includes_issue_bodies_for_cohort_inference():
    issues = [
        {"number": 1, "title": "Foo", "body": "depends on #2"},
        {"number": 2, "title": "Bar", "body": "no deps"},
    ]
    out = milestone_chat.build_milestone_chat_briefing(
        repo_name="api",
        repo_slug="acme/api",
        milestone_title="M",
        tracking_issue_number=100,
        tracking_issue_body="",
        issues=issues,
    )
    assert "#1: Foo" in out
    assert "depends on #2" in out
    assert "#2: Bar" in out


def test_briefing_handles_no_issues():
    out = milestone_chat.build_milestone_chat_briefing(
        repo_name="api",
        repo_slug="acme/api",
        milestone_title="M",
        tracking_issue_number=100,
        tracking_issue_body="",
        issues=[],
    )
    assert "(none fetched)" in out


def test_briefing_names_the_write_order_command():
    """The seed must tell the model the exact write-path command, scoped to
    the caller's repo/tracking-issue — never raw `gh`."""
    out = milestone_chat.build_milestone_chat_briefing(
        repo_name="api",
        repo_slug="acme/api",
        milestone_title="M",
        tracking_issue_number=100,
        tracking_issue_body="",
        issues=[],
    )
    assert "coord milestone write-order api 100" in out


# ── _fetch_milestone_issues ──────────────────────────────────────────────────


def test_fetch_milestone_issues_filters_by_milestone_number():
    all_issues = [
        {"number": 1, "title": "In", "body": "b1", "milestone": {"number": 9}},
        {"number": 2, "title": "Out", "body": "b2", "milestone": {"number": 5}},
        {"number": 3, "title": "NoMilestone", "body": "", "milestone": None},
    ]
    with patch("coord.github_ops.get_open_issues", return_value=all_issues):
        out = milestone_chat._fetch_milestone_issues("acme/api", 9)
    assert [i["number"] for i in out] == [1]


def test_fetch_milestone_issues_truncates_long_bodies():
    long_body = "x" * (milestone_chat.MAX_ISSUE_BODY_CHARS + 500)
    issues = [{"number": 1, "title": "T", "body": long_body, "milestone": {"number": 9}}]
    with patch("coord.github_ops.get_open_issues", return_value=issues):
        out = milestone_chat._fetch_milestone_issues("acme/api", 9)
    assert len(out[0]["body"]) <= milestone_chat.MAX_ISSUE_BODY_CHARS + len("\n...(truncated)")
    assert out[0]["body"].endswith("(truncated)")


def test_fetch_milestone_issues_returns_empty_on_failure():
    with patch("coord.github_ops.get_open_issues", side_effect=RuntimeError("gh boom")):
        out = milestone_chat._fetch_milestone_issues("acme/api", 9)
    assert out == []


# ── pick_milestone_chat_machine ──────────────────────────────────────────────


def _make_machine(name: str, repos: list[str], host: str = "host", path: str = "/tmp") -> Machine:
    return Machine(
        name=name,
        host=host,
        capabilities=[],
        repos=repos,
        repo_paths={r: f"{path}/{r}" for r in repos},
    )


def test_pick_machine_returns_first_qualified(tmp_path):
    a = _make_machine("a", ["x"], path=str(tmp_path))
    b = _make_machine("b", ["x", "y"], path=str(tmp_path))
    cfg = type("Cfg", (), {"machines": [a, b]})()
    picked = milestone_chat.pick_milestone_chat_machine(cfg, "x")  # type: ignore[arg-type]
    assert picked is a


def test_pick_machine_returns_none_when_no_match(tmp_path):
    a = _make_machine("a", ["x"], path=str(tmp_path))
    cfg = type("Cfg", (), {"machines": [a]})()
    assert milestone_chat.pick_milestone_chat_machine(cfg, "y") is None  # type: ignore[arg-type]


# ── dispatch_milestone_chat ──────────────────────────────────────────────────


def _cfg_with_repo_and_machine(tmp_path):
    from coord.config import Config, ModelsConfig
    from coord.models import Repo

    repo = Repo(name="api", github="acme/api", default_branch="main")
    machine = _make_machine("laptop", ["api"], path=str(tmp_path))
    cfg = Config(
        repos=[repo],
        machines=[machine],
        models=ModelsConfig(default=None),
    )
    return cfg


def test_dispatch_raises_for_unknown_repo(tmp_path):
    cfg = _cfg_with_repo_and_machine(tmp_path)
    try:
        milestone_chat.dispatch_milestone_chat("nope", 100, cfg)
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert "not in coordinator.yml" in str(e)


def test_dispatch_raises_when_tracking_issue_has_no_milestone(tmp_path):
    cfg = _cfg_with_repo_and_machine(tmp_path)
    with patch(
        "coord.github_ops.get_issue",
        return_value={"number": 100, "title": "t", "body": "", "milestone": None},
    ):
        try:
            milestone_chat.dispatch_milestone_chat("api", 100, cfg)
            assert False, "expected RuntimeError"
        except RuntimeError as e:
            assert "no milestone" in str(e)


def test_dispatch_raises_when_no_machine_claims_repo(tmp_path):
    from coord.config import Config, ModelsConfig
    from coord.models import Repo

    repo = Repo(name="api", github="acme/api", default_branch="main")
    cfg = Config(repos=[repo], machines=[], models=ModelsConfig(default=None))
    with patch(
        "coord.github_ops.get_issue",
        return_value={
            "number": 100, "title": "t", "body": "",
            "milestone": {"number": 9, "title": "M"},
        },
    ):
        try:
            milestone_chat.dispatch_milestone_chat("api", 100, cfg)
            assert False, "expected RuntimeError"
        except RuntimeError as e:
            assert "no machine claims repo" in str(e)


def test_dispatch_success_records_assignment(tmp_path):
    cfg = _cfg_with_repo_and_machine(tmp_path)
    issue_data = {
        "number": 100, "title": "Milestone tracker", "body": "## Work order\n",
        "milestone": {"number": 9, "title": "Q3"},
    }
    with patch("coord.github_ops.get_issue", return_value=issue_data), \
         patch("coord.github_ops.get_open_issues", return_value=[]), \
         patch("coord.dispatch.dispatch_with_retry", return_value={"id": "asg-xyz"}) as mock_dispatch, \
         patch("coord.state.record_dispatched_assignment") as mock_record:
        assignment_id, machine_name = milestone_chat.dispatch_milestone_chat(
            "api", 100, cfg
        )

    assert assignment_id == "asg-xyz"
    assert machine_name == "laptop"
    mock_dispatch.assert_called_once()
    proposal = mock_dispatch.call_args[0][0]
    assert proposal.type == "milestone-chat"
    assert proposal.issue_number == 100
    assert proposal.issue_title == "Milestone tracker"
    mock_record.assert_called_once()


def test_dispatch_machine_override_must_claim_repo(tmp_path):
    cfg = _cfg_with_repo_and_machine(tmp_path)
    with patch(
        "coord.github_ops.get_issue",
        return_value={
            "number": 100, "title": "t", "body": "",
            "milestone": {"number": 9, "title": "M"},
        },
    ):
        try:
            milestone_chat.dispatch_milestone_chat(
                "api", 100, cfg, machine_override="nonexistent"
            )
            assert False, "expected RuntimeError"
        except RuntimeError as e:
            assert "not in coordinator.yml" in str(e)


# ── agent.py milestone-chat branch ───────────────────────────────────────────


def _spec(**overrides) -> AssignmentSpec:
    defaults = dict(
        repo_name="r",
        repo_path="/tmp/r",
        issue_number=100,
        issue_title="Milestone tracker",
        briefing="b",
        type="milestone-chat",
    )
    defaults.update(overrides)
    return AssignmentSpec(**defaults)


def test_default_worker_command_milestone_chat_uses_read_bash():
    argv = default_worker_command(_spec())
    idx = argv.index("--allowedTools")
    assert argv[idx + 1] == "Read,Bash"


def test_default_worker_command_milestone_chat_uses_milestone_prompt():
    argv = default_worker_command(_spec())
    idx = argv.index("--system-prompt")
    assert MILESTONE_CHAT_SYSTEM_PROMPT in argv[idx + 1]


def test_default_worker_command_milestone_chat_has_deny_list():
    argv = default_worker_command(_spec())
    idx = argv.index("--system-prompt")
    system_prompt = argv[idx + 1]
    assert "FORBIDDEN COMMANDS" in system_prompt
    assert "gh issue edit" in system_prompt
    assert "coord milestone write-order" in system_prompt


def test_default_worker_command_milestone_chat_honours_explicit_system_prompt():
    argv = default_worker_command(_spec(system_prompt="custom prompt"))
    idx = argv.index("--system-prompt")
    assert argv[idx + 1].startswith("custom prompt")
    assert "FORBIDDEN" in argv[idx + 1]


def test_milestone_chat_is_write_capable():
    """#425 safety gate: milestone-chat CAN mutate GitHub (the tracking
    issue body), unlike the other read-only chat types."""
    assert "milestone-chat" in WRITE_CAPABLE_SPEC_TYPES


def test_milestone_chat_deny_list_blocks_raw_gh_and_unrelated_coord_writes():
    denies = " ".join(MILESTONE_CHAT_DENY_COMMANDS)
    assert "gh issue edit" in denies
    assert "gh api -X PATCH" in denies
    assert "coord milestone create" in denies
    assert "coord approve" in denies
    assert "coord merge" in denies
