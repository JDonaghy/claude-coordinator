"""Tests for the mock-author seed builder and dispatcher (#930, Gate A —
docs/ORACLE_LOOP.md)."""
from __future__ import annotations

from unittest.mock import patch

from coord.agent import (
    MOCK_AUTHOR_DENY_COMMANDS,
    MOCK_AUTHOR_SYSTEM_PROMPT,
    WRITE_CAPABLE_SPEC_TYPES,
    AssignmentSpec,
    default_worker_command,
)
from coord.config import AcceptanceConfig, AcceptanceDriverConfig, Config, ModelsConfig
from coord.models import Machine, Repo
from coord import mock_author


# ── build_mock_author_briefing ───────────────────────────────────────────────


def test_briefing_includes_repo_and_milestone():
    out = mock_author.build_mock_author_briefing(
        repo_slug="acme/api",
        milestone_title="Q3 push",
        milestone_number=9,
        tracking_issue_number=100,
        tracking_issue_body="",
        issues=[],
        driver_kind="tui-tuidriver",
        driver_mock_glob="*.screen",
    )
    assert "acme/api" in out
    assert "Q3 push" in out
    assert "#100" in out


def test_briefing_names_the_exact_output_paths():
    out = mock_author.build_mock_author_briefing(
        repo_slug="acme/api",
        milestone_title="M",
        milestone_number=9,
        tracking_issue_number=100,
        tracking_issue_body="",
        issues=[],
        driver_kind="tui-tuidriver",
        driver_mock_glob="*.screen",
    )
    assert "tests/acceptance/ms-9/mocks/" in out
    assert "tests/acceptance/ms-9/contract.md" in out
    assert "tui-tuidriver" in out
    assert "*.screen" in out


def test_briefing_handles_empty_tracking_body():
    out = mock_author.build_mock_author_briefing(
        repo_slug="acme/api",
        milestone_title="M",
        milestone_number=9,
        tracking_issue_number=100,
        tracking_issue_body="",
        issues=[],
        driver_kind="tui-tuidriver",
        driver_mock_glob="*.screen",
    )
    assert "(empty)" in out


def test_briefing_includes_issue_bodies():
    issues = [
        {"number": 1, "title": "Foo", "body": "some detail"},
    ]
    out = mock_author.build_mock_author_briefing(
        repo_slug="acme/api",
        milestone_title="M",
        milestone_number=9,
        tracking_issue_number=100,
        tracking_issue_body="",
        issues=issues,
        driver_kind="tui-tuidriver",
        driver_mock_glob="*.screen",
    )
    assert "#1: Foo" in out
    assert "some detail" in out


def test_briefing_handles_no_issues():
    out = mock_author.build_mock_author_briefing(
        repo_slug="acme/api",
        milestone_title="M",
        milestone_number=9,
        tracking_issue_number=100,
        tracking_issue_body="",
        issues=[],
        driver_kind="tui-tuidriver",
        driver_mock_glob="*.screen",
    )
    assert "(none fetched)" in out


# ── dispatch_acceptance_mock ─────────────────────────────────────────────────


def _make_machine(name: str, repos: list[str], path: str) -> Machine:
    return Machine(
        name=name, host=f"{name}.tailnet", repos=repos,
        repo_paths={r: f"{path}/{r}" for r in repos},
    )


def _cfg_with_driver(tmp_path, *, with_driver: bool = True) -> Config:
    drivers = {}
    if with_driver:
        drivers["api"] = AcceptanceDriverConfig(kind="tui-tuidriver", run="cargo test", mock="*.screen")
    repo = Repo(name="api", github="acme/api", default_branch="main")
    machine = _make_machine("laptop", ["api"], str(tmp_path))
    return Config(
        repos=[repo],
        machines=[machine],
        models=ModelsConfig(default=None),
        acceptance=AcceptanceConfig(drivers=drivers),
    )


def test_dispatch_raises_for_unknown_repo(tmp_path):
    cfg = _cfg_with_driver(tmp_path)
    try:
        mock_author.dispatch_acceptance_mock("nope", 100, cfg)
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert "not in coordinator.yml" in str(e)


def test_dispatch_raises_when_no_acceptance_driver_configured(tmp_path):
    cfg = _cfg_with_driver(tmp_path, with_driver=False)
    try:
        mock_author.dispatch_acceptance_mock("api", 100, cfg)
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert "no acceptance driver configured" in str(e)


def test_dispatch_raises_when_tracking_issue_has_no_milestone(tmp_path):
    cfg = _cfg_with_driver(tmp_path)
    with patch(
        "coord.github_ops.get_issue",
        return_value={"number": 100, "title": "t", "body": "", "milestone": None},
    ):
        try:
            mock_author.dispatch_acceptance_mock("api", 100, cfg)
            assert False, "expected RuntimeError"
        except RuntimeError as e:
            assert "no milestone" in str(e)


def test_dispatch_raises_when_no_machine_claims_repo(tmp_path):
    repo = Repo(name="api", github="acme/api", default_branch="main")
    cfg = Config(
        repos=[repo], machines=[], models=ModelsConfig(default=None),
        acceptance=AcceptanceConfig(drivers={
            "api": AcceptanceDriverConfig(kind="tui-tuidriver", run="cargo test"),
        }),
    )
    with patch(
        "coord.github_ops.get_issue",
        return_value={
            "number": 100, "title": "t", "body": "",
            "milestone": {"number": 9, "title": "M"},
        },
    ), patch("coord.board_service.read_board") as mock_board:
        from coord.models import Board

        mock_board.return_value = Board()
        try:
            mock_author.dispatch_acceptance_mock("api", 100, cfg)
            assert False, "expected RuntimeError"
        except RuntimeError as e:
            assert "no idle machine claims repo" in str(e)


def test_dispatch_raises_when_gate_a_already_claimed(tmp_path):
    from coord.models import Assignment, Board

    cfg = _cfg_with_driver(tmp_path)
    board = Board(active=[Assignment(
        machine_name="laptop", repo_name="api", issue_number=100,
        issue_title="t", status="running", assignment_id="a1", type="mock-author",
    )])
    with patch(
        "coord.github_ops.get_issue",
        return_value={
            "number": 100, "title": "t", "body": "",
            "milestone": {"number": 9, "title": "M"},
        },
    ), patch("coord.board_service.read_board", return_value=board):
        try:
            mock_author.dispatch_acceptance_mock("api", 100, cfg)
            assert False, "expected RuntimeError"
        except RuntimeError as e:
            assert "already in flight" in str(e)


def test_dispatch_success_records_assignment(tmp_path):
    from coord.models import Board

    cfg = _cfg_with_driver(tmp_path)
    issue_data = {
        "number": 100, "title": "Milestone tracker", "body": "",
        "milestone": {"number": 9, "title": "Q3"},
    }
    with patch("coord.github_ops.get_issue", return_value=issue_data), \
         patch("coord.github_ops.get_open_issues", return_value=[]), \
         patch("coord.board_service.read_board", return_value=Board()), \
         patch("coord.dispatch.dispatch_with_retry", return_value={"id": "asg-xyz"}) as mock_dispatch, \
         patch("coord.dispatch.post_briefing"), \
         patch("coord.state.record_dispatched") as mock_record:
        assignment_id, machine_name = mock_author.dispatch_acceptance_mock("api", 100, cfg)

    assert assignment_id == "asg-xyz"
    assert machine_name == "laptop"
    mock_dispatch.assert_called_once()
    proposal = mock_dispatch.call_args[0][0]
    assert proposal.type == "mock-author"
    assert proposal.issue_number == 100
    assert proposal.target_branch == "ms-9-gate-a"
    mock_record.assert_called_once()


def test_dispatch_machine_override_must_claim_repo(tmp_path):
    from coord.models import Board

    cfg = _cfg_with_driver(tmp_path)
    with patch(
        "coord.github_ops.get_issue",
        return_value={
            "number": 100, "title": "t", "body": "",
            "milestone": {"number": 9, "title": "M"},
        },
    ), patch("coord.board_service.read_board", return_value=Board()):
        try:
            mock_author.dispatch_acceptance_mock(
                "api", 100, cfg, machine_override="nonexistent"
            )
            assert False, "expected RuntimeError"
        except RuntimeError as e:
            assert "not in coordinator.yml" in str(e)


# ── agent.py mock-author branch ──────────────────────────────────────────────


def _spec(**overrides) -> AssignmentSpec:
    defaults = dict(
        repo_name="r",
        repo_path="/tmp/r",
        issue_number=100,
        issue_title="[gate-a] Milestone tracker",
        briefing="b",
        type="mock-author",
    )
    defaults.update(overrides)
    return AssignmentSpec(**defaults)


def test_default_worker_command_mock_author_uses_full_tools():
    argv = default_worker_command(_spec())
    idx = argv.index("--allowedTools")
    assert argv[idx + 1] == "Read,Edit,Write,Bash"


def test_default_worker_command_mock_author_uses_mock_author_prompt():
    argv = default_worker_command(_spec())
    idx = argv.index("--system-prompt")
    assert MOCK_AUTHOR_SYSTEM_PROMPT in argv[idx + 1]


def test_default_worker_command_mock_author_has_deny_list():
    argv = default_worker_command(_spec())
    idx = argv.index("--system-prompt")
    system_prompt = argv[idx + 1]
    assert "FORBIDDEN COMMANDS" in system_prompt
    assert "gh *" in system_prompt


def test_default_worker_command_mock_author_honours_explicit_system_prompt():
    argv = default_worker_command(_spec(system_prompt="custom prompt"))
    idx = argv.index("--system-prompt")
    assert argv[idx + 1].startswith("custom prompt")
    assert "FORBIDDEN" in argv[idx + 1]


def test_mock_author_is_write_capable():
    """#437 TOS-compliance gate: mock-author gets a real worktree + commits
    files, same mutation risk as `work` — must be denied on unverified
    providers."""
    assert "mock-author" in WRITE_CAPABLE_SPEC_TYPES


def test_mock_author_deny_list_blocks_gh_and_dangerous_git():
    denies = " ".join(MOCK_AUTHOR_DENY_COMMANDS)
    assert "gh *" in denies
    assert "git reset --hard" in denies
    assert "coord merge" in denies
    # Unlike milestone-chat, mock-author DOES commit/push.
    assert "git commit" not in denies
    assert "git push *" not in denies
