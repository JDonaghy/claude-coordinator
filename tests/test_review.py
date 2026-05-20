"""Tests for adversarial code review dispatch (coord/review.py)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from coord.config import Config, ReviewsConfig, load
from coord.models import Assignment, Board, Machine, Repo
from coord.review import (
    REVIEWER_SYSTEM_PROMPT,
    build_review_briefing,
    dispatch_review,
    pick_reviewer_machine,
)


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def repo() -> Repo:
    return Repo(name="api", github="acme/api", depends_on=[], default_branch="main")


@pytest.fixture
def two_machine_config(repo: Repo) -> Config:
    return Config(
        repos=[repo],
        machines=[
            Machine(
                name="laptop", host="laptop.tail",
                capabilities=["python"], repos=["api"],
                repo_paths={"api": "/work/api"},
            ),
            Machine(
                name="server", host="server.tail",
                capabilities=["python"], repos=["api"],
                repo_paths={"api": "/srv/api"},
            ),
        ],
        reviews=ReviewsConfig(enabled=True, auto_dispatch=True),
    )


@pytest.fixture
def one_machine_config(repo: Repo) -> Config:
    return Config(
        repos=[repo],
        machines=[
            Machine(
                name="laptop", host="laptop.tail",
                capabilities=["python"], repos=["api"],
                repo_paths={"api": "/work/api"},
            ),
        ],
        reviews=ReviewsConfig(enabled=True, auto_dispatch=True),
    )


def _completed_assignment(machine: str = "laptop", branch: str = "issue-1-fix") -> Assignment:
    return Assignment(
        machine_name=machine,
        repo_name="api",
        issue_number=1,
        issue_title="Fix the thing",
        briefing="Worker briefing",
        assignment_id="abc123",
        status="done",
        branch=branch,
        dispatched_at=0.0,
        finished_at=1.0,
        type="work",
    )


# ── Config parsing ──────────────────────────────────────────────────────────


def test_reviews_config_defaults_to_enabled(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n  - name: api\n    github: acme/api\n"
        "machines:\n  - name: laptop\n    host: laptop.tail\n    repos: [api]\n"
    )
    cfg = load(p)
    assert cfg.reviews.enabled is True   # enabled by default; set enabled: false to opt out
    assert cfg.reviews.auto_dispatch is True
    assert cfg.reviews.checklist == ["Check for platform-specific code in shared/cross-platform paths"]
    assert cfg.reviews.repo_overrides == {}


def test_reviews_config_can_be_disabled(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n  - name: api\n    github: acme/api\n"
        "machines:\n  - name: laptop\n    host: laptop.tail\n    repos: [api]\n"
        "reviews:\n  enabled: false\n"
    )
    cfg = load(p)
    assert cfg.reviews.enabled is False


def test_reviews_config_parses_all_fields(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        """\
repos:
  - name: api
    github: acme/api
machines:
  - name: laptop
    host: laptop.tail
    repos: [api]
reviews:
  enabled: true
  auto_dispatch: false
  require_approval: true
  reviewer_prompt: |
    Focus on correctness.
  checklist:
    - "Did tests get added?"
    - "Stay in scope?"
  repo_overrides:
    api:
      - "Check no SQL injection."
"""
    )
    cfg = load(p)
    assert cfg.reviews.enabled is True
    assert cfg.reviews.auto_dispatch is False
    assert cfg.reviews.require_approval is True
    assert "Focus on correctness." in cfg.reviews.reviewer_prompt
    assert cfg.reviews.checklist == ["Did tests get added?", "Stay in scope?"]
    assert cfg.reviews.repo_overrides == {"api": ["Check no SQL injection."]}


def test_reviews_config_rejects_unknown_repo_override(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        """\
repos:
  - name: api
    github: acme/api
machines:
  - name: laptop
    host: laptop.tail
    repos: [api]
reviews:
  enabled: true
  repo_overrides:
    ghost:
      - "this repo does not exist"
"""
    )
    from coord.config import ConfigError
    with pytest.raises(ConfigError, match="unknown repo: 'ghost'"):
        load(p)


# ── Machine selection ───────────────────────────────────────────────────────


def test_pick_reviewer_prefers_different_machine(two_machine_config: Config) -> None:
    board = Board()
    choice = pick_reviewer_machine("laptop", "api", board, two_machine_config)
    assert choice is not None
    assert choice.machine.name == "server"
    assert choice.same_as_worker is False


def test_pick_reviewer_falls_back_to_same_machine_when_only_one(
    one_machine_config: Config,
) -> None:
    board = Board()
    choice = pick_reviewer_machine("laptop", "api", board, one_machine_config)
    assert choice is not None
    assert choice.machine.name == "laptop"
    assert choice.same_as_worker is True
    assert "fresh but not on separate hardware" in choice.rationale


def test_pick_reviewer_returns_none_when_no_machine_handles_repo(
    repo: Repo,
) -> None:
    cfg = Config(
        repos=[repo],
        machines=[
            Machine(name="laptop", host="laptop.tail", repos=["other"], repo_paths={}),
        ],
        reviews=ReviewsConfig(enabled=True),
    )
    board = Board()
    assert pick_reviewer_machine("laptop", "api", board, cfg) is None


def test_pick_reviewer_picks_busy_different_machine_over_same_idle(
    two_machine_config: Config,
) -> None:
    # Both machines handle api; server is busy. We still prefer server (the
    # different machine) — independence outweighs queuing delay.
    board = Board(
        active=[
            Assignment(
                machine_name="server", repo_name="api", issue_number=99,
                issue_title="busy work", status="running",
                assignment_id="other",
            )
        ]
    )
    choice = pick_reviewer_machine("laptop", "api", board, two_machine_config)
    assert choice is not None
    assert choice.machine.name == "server"
    assert "currently busy" in choice.rationale


# ── Briefing construction ───────────────────────────────────────────────────


def test_briefing_includes_claude_md_and_checklist() -> None:
    cfg = ReviewsConfig(
        enabled=True,
        checklist=["Did tests get added?", "Any security issues?"],
        repo_overrides={"api": ["No SQL injection."]},
    )
    briefing = build_review_briefing(
        pr_number=42,
        pr_url="https://github.com/acme/api/pull/42",
        repo_github="acme/api",
        repo_name="api",
        issue_number=7,
        issue_title="Fix login",
        issue_body="Login is broken on Firefox.",
        branch="issue-7-fix-login",
        worker_machine="laptop",
        same_as_worker=False,
        reviews_cfg=cfg,
        repo_claude_md="# CLAUDE.md\nDo not use raw SQL.",
    )
    assert "acme/api PR #42" in briefing
    assert "Fix login" in briefing
    assert "Login is broken on Firefox." in briefing
    assert "Do not use raw SQL." in briefing
    assert "Did tests get added?" in briefing
    assert "Any security issues?" in briefing
    assert "No SQL injection." in briefing
    assert "gh pr review 42 --repo acme/api" in briefing
    # No same-machine warning when the reviewer is on a different machine.
    assert "running on the same machine as the worker" not in briefing


def test_briefing_warns_when_same_machine() -> None:
    briefing = build_review_briefing(
        pr_number=42, pr_url=None, repo_github="acme/api", repo_name="api",
        issue_number=7, issue_title="Fix login", issue_body="",
        branch="issue-7", worker_machine="laptop", same_as_worker=True,
        reviews_cfg=ReviewsConfig(enabled=True), repo_claude_md=None,
    )
    assert "running on the same machine as the worker" in briefing


def test_briefing_uses_generic_checklist_when_none_configured() -> None:
    briefing = build_review_briefing(
        pr_number=1, pr_url=None, repo_github="acme/api", repo_name="api",
        issue_number=7, issue_title="X", issue_body="",
        branch="b", worker_machine="laptop", same_as_worker=False,
        reviews_cfg=ReviewsConfig(enabled=True, checklist=[]), repo_claude_md=None,
    )
    assert "Do tests pass?" in briefing
    assert "Did the worker stay within the assigned file scope?" in briefing


def test_briefing_falls_back_to_branch_diff_when_no_pr() -> None:
    briefing = build_review_briefing(
        pr_number=None, pr_url=None, repo_github="acme/api", repo_name="api",
        issue_number=7, issue_title="X", issue_body="",
        branch="my-branch", worker_machine="laptop", same_as_worker=False,
        reviews_cfg=ReviewsConfig(enabled=True), repo_claude_md=None,
    )
    assert "git diff main...my-branch" in briefing
    assert "gh pr review" not in briefing


# ── dispatch_review (integration with mocked agent HTTP) ────────────────────


class _FakeHTTPResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class _FakeHTTPClient:
    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.calls: list[tuple[str, dict]] = []

    def post(self, url: str, *, json: dict, timeout: float) -> _FakeHTTPResponse:
        self.calls.append((url, json))
        return _FakeHTTPResponse(self._payload)


def test_dispatch_review_skipped_when_disabled(two_machine_config: Config) -> None:
    cfg = replace(two_machine_config, reviews=ReviewsConfig(enabled=False))
    board = Board()
    result = dispatch_review(
        _completed_assignment(), board, cfg,
        http_client=_FakeHTTPClient({"id": "x"}),
        pr_lookup=lambda repo_github, **kw: {"number": 1, "url": "u", "existed": True},
        claude_md_reader=lambda p: None,
        issue_body_fetcher=lambda repo, num: "",
    )
    assert result is None
    assert board.active == []


def test_dispatch_review_skipped_for_failed_assignment(
    two_machine_config: Config,
) -> None:
    failed = replace(_completed_assignment(), status="failed")
    board = Board()
    result = dispatch_review(
        failed, board, two_machine_config,
        http_client=_FakeHTTPClient({"id": "x"}),
        pr_lookup=lambda repo_github, **kw: {"number": 1, "url": "u", "existed": True},
        claude_md_reader=lambda p: None,
        issue_body_fetcher=lambda repo, num: "",
    )
    assert result is None


def test_dispatch_review_skipped_when_no_branch(two_machine_config: Config) -> None:
    no_branch = replace(_completed_assignment(), branch=None)
    board = Board()
    result = dispatch_review(
        no_branch, board, two_machine_config,
        http_client=_FakeHTTPClient({"id": "x"}),
        pr_lookup=lambda repo_github, **kw: {"number": 1, "url": "u", "existed": True},
        claude_md_reader=lambda p: None,
        issue_body_fetcher=lambda repo, num: "",
    )
    assert result is None


def test_dispatch_review_skipped_for_review_type(two_machine_config: Config) -> None:
    """Reviews don't trigger reviews-of-reviews — avoid infinite loops."""
    review = replace(_completed_assignment(), type="review")
    board = Board()
    result = dispatch_review(
        review, board, two_machine_config,
        http_client=_FakeHTTPClient({"id": "x"}),
        pr_lookup=lambda repo_github, **kw: {"number": 1, "url": "u", "existed": True},
        claude_md_reader=lambda p: None,
        issue_body_fetcher=lambda repo, num: "",
    )
    assert result is None


def test_dispatch_review_sends_to_different_machine_and_appends_to_board(
    two_machine_config: Config,
) -> None:
    board = Board()
    completed = _completed_assignment(machine="laptop")
    client = _FakeHTTPClient({"id": "review-id-1"})

    result = dispatch_review(
        completed, board, two_machine_config,
        http_client=client,
        pr_lookup=lambda repo_github, **kw: {
            "number": 42,
            "url": "https://github.com/acme/api/pull/42",
            "existed": True,
        },
        claude_md_reader=lambda p: "# Project rules\n",
        issue_body_fetcher=lambda repo, num: "issue body text",
        now=123.0,
    )

    assert result is not None
    assert result.type == "review"
    assert result.machine_name == "server"  # different from worker (laptop)
    assert result.review_target == "42"
    assert result.review_of_assignment_id == "abc123"
    assert result.status == "running"
    assert result.assignment_id == "review-id-1"
    assert result.dispatched_at == 123.0
    assert board.active == [result]

    # Verify the HTTP payload went to the reviewer machine with the review
    # type and the reviewer system prompt.
    assert len(client.calls) == 1
    url, payload = client.calls[0]
    assert "server.tail" in url
    assert payload["type"] == "review"
    assert payload["system_prompt"] == REVIEWER_SYSTEM_PROMPT
    assert payload["review_target"] == "42"
    assert payload["repo_path"] == "/srv/api"  # reviewer's local path
    assert "# Project rules" in payload["briefing"]


def test_dispatch_review_handles_http_failure_gracefully(
    two_machine_config: Config,
) -> None:
    import httpx

    class _FailingClient:
        def post(self, url, *, json, timeout):
            raise httpx.ConnectError("agent unreachable")

    board = Board()
    result = dispatch_review(
        _completed_assignment(), board, two_machine_config,
        http_client=_FailingClient(),
        pr_lookup=lambda repo_github, **kw: {"number": 1, "url": "u", "existed": True},
        claude_md_reader=lambda p: None,
        issue_body_fetcher=lambda repo, num: "",
    )
    assert result is None
    assert board.active == []


def test_dispatch_review_falls_back_when_no_pr_can_be_opened(
    two_machine_config: Config,
) -> None:
    board = Board()
    completed = _completed_assignment()
    result = dispatch_review(
        completed, board, two_machine_config,
        http_client=_FakeHTTPClient({"id": "rev1"}),
        pr_lookup=lambda repo_github, **kw: None,  # PR open failed
        claude_md_reader=lambda p: None,
        issue_body_fetcher=lambda repo, num: "",
    )
    assert result is not None
    # With no PR, the review_target is the branch name.
    assert result.review_target == "issue-1-fix"
    assert result.pr_url is None


def test_dispatch_review_records_to_dispatched_ledger(
    two_machine_config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from coord import state as state_mod

    monkeypatch.setattr(state_mod, "DISPATCHED_FILE", tmp_path / "dispatched.json")

    board = Board()
    completed = _completed_assignment(machine="laptop")

    result = dispatch_review(
        completed, board, two_machine_config,
        http_client=_FakeHTTPClient({"id": "review-ledger-1"}),
        pr_lookup=lambda repo_github, **kw: {
            "number": 99, "url": "https://github.com/acme/api/pull/99", "existed": True,
        },
        claude_md_reader=lambda p: "",
        issue_body_fetcher=lambda repo, num: "",
    )

    assert result is not None
    records = state_mod.load_dispatched(tmp_path / "dispatched.json")
    assert len(records) == 1
    assert records[0]["assignment_id"] == "review-ledger-1"
    assert records[0]["repo_github"] == "acme/api"
    assert records[0]["machine_name"] == "server"
