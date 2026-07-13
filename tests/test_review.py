"""Tests for adversarial code review dispatch (coord/review.py)."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from coord.config import Config, PipelineConfig, ReviewsConfig, load
from coord.models import Assignment, Board, Machine, Repo
from coord.review import (
    REVIEWER_SYSTEM_PROMPT,
    ReviewFindings,
    build_review_briefing,
    dispatch_pending_reviews,
    dispatch_review,
    parse_review_from_log,
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
    # Reviewer must output structured verdict; coordinator posts the PR review.
    assert "REVIEW_VERDICT:" in briefing
    assert "gh pr review" not in briefing
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
    # Must fetch first and diff against origin/<base> — diffing bare local
    # `main` against a recently-cut branch on a stale agent checkout sweeps in
    # every PR merged since the local ref last moved (the #563 "bundled 5
    # issues" false positive). Always fetch + origin/<base>...origin/<branch>.
    assert "git fetch origin && git diff origin/main...origin/my-branch" in briefing
    assert "git diff main...my-branch" not in briefing
    assert "gh pr review" not in briefing


def test_briefing_no_pr_diff_uses_default_branch_not_hardcoded_main() -> None:
    """The no-PR diff base must follow ``default_branch`` (e.g. ``develop``),
    not a hardcoded ``main`` — otherwise develop-default repos diff against the
    wrong base."""
    briefing = build_review_briefing(
        pr_number=None, pr_url=None, repo_github="acme/api", repo_name="api",
        issue_number=7, issue_title="X", issue_body="",
        branch="my-branch", worker_machine="laptop", same_as_worker=False,
        reviews_cfg=ReviewsConfig(enabled=True), repo_claude_md=None,
        default_branch="develop",
    )
    assert "git diff origin/develop...origin/my-branch" in briefing
    assert "origin/main" not in briefing


def test_briefing_first_review_is_full_scope() -> None:
    """review_iteration=0 (default) reviews the whole PR — no incremental
    language."""
    briefing = build_review_briefing(
        pr_number=42, pr_url=None, repo_github="acme/api", repo_name="api",
        issue_number=7, issue_title="X", issue_body="",
        branch="my-branch", worker_machine="laptop", same_as_worker=False,
        reviews_cfg=ReviewsConfig(enabled=True), repo_claude_md=None,
    )
    assert "re-review iteration" not in briefing
    assert "Run the project's test suite." in briefing


def test_briefing_re_review_is_incremental_and_nit_suppressing() -> None:
    """review_iteration>0 scopes the review to the fix delta and tells the
    reviewer not to raise new non-blocking nits on already-reviewed code
    (#476)."""
    briefing = build_review_briefing(
        pr_number=42, pr_url=None, repo_github="acme/api", repo_name="api",
        issue_number=7, issue_title="X", issue_body="",
        branch="my-branch", worker_machine="laptop", same_as_worker=False,
        reviews_cfg=ReviewsConfig(enabled=True), repo_claude_md=None,
        default_branch="main", review_iteration=3,
    )
    assert "re-review iteration 3" in briefing
    assert "do NOT re-review" in briefing.lower() or "not re-review" in briefing.lower()
    assert "Do NOT raise new non-blocking nits" in briefing
    # Points the reviewer at the fix delta, not the full PR diff.
    assert "git log --oneline origin/main...origin/my-branch" in briefing


def test_briefing_embeds_diff_text_when_supplied() -> None:
    """#612: a supplied merge-base diff is embedded verbatim and the reviewer
    is told NOT to compute its own diff (a stale-base diff false-flags
    already-merged commits as deletions)."""
    diff = (
        "diff --git a/foo.py b/foo.py\n"
        "@@ -1,2 +1,3 @@\n"
        "+added_line = 1\n"
    )
    briefing = build_review_briefing(
        pr_number=42, pr_url=None, repo_github="acme/api", repo_name="api",
        issue_number=7, issue_title="X", issue_body="",
        branch="my-branch", worker_machine="laptop", same_as_worker=False,
        reviews_cfg=ReviewsConfig(enabled=True), repo_claude_md=None,
        diff_text=diff,
    )
    assert "## Diff to review (authoritative)" in briefing
    assert "added_line = 1" in briefing
    assert "Do NOT compute your own diff" in briefing
    # The "What to do" step 1 points at the embedded section, not a git command.
    assert "already fetched for you" in briefing


def test_briefing_no_diff_text_keeps_three_dot_fallback() -> None:
    """#612: with diff_text=None the existing three-dot ``git diff origin/``
    fallback instructions stand (no embedded diff section)."""
    briefing = build_review_briefing(
        pr_number=None, pr_url=None, repo_github="acme/api", repo_name="api",
        issue_number=7, issue_title="X", issue_body="",
        branch="my-branch", worker_machine="laptop", same_as_worker=False,
        reviews_cfg=ReviewsConfig(enabled=True), repo_claude_md=None,
        diff_text=None,
    )
    assert "## Diff to review (authoritative)" not in briefing
    assert "git diff origin/main...origin/my-branch" in briefing


def test_briefing_no_sealed_paths_by_default() -> None:
    briefing = build_review_briefing(
        pr_number=42, pr_url=None, repo_github="acme/api", repo_name="api",
        issue_number=7, issue_title="X", issue_body="",
        branch="my-branch", worker_machine="laptop", same_as_worker=False,
        reviews_cfg=ReviewsConfig(enabled=True), repo_claude_md=None,
    )
    assert "SEALED" not in briefing
    assert "Sealed paths" not in briefing


def test_briefing_sealed_paths_reminder_when_diff_untouched() -> None:
    """#944 sealing v1: when a repo has an acceptance driver, the reviewer
    always gets told the oracle is sealed — even if this diff doesn't touch
    it — so REQUEST-changes is the default reflex, not something it has to
    infer from the checklist alone."""
    diff = (
        "diff --git a/src/foo.py b/src/foo.py\n"
        "@@ -1,2 +1,3 @@\n"
        "+added_line = 1\n"
    )
    briefing = build_review_briefing(
        pr_number=42, pr_url=None, repo_github="acme/coord-tui", repo_name="coord-tui",
        issue_number=944, issue_title="X", issue_body="",
        branch="my-branch", worker_machine="laptop", same_as_worker=False,
        reviews_cfg=ReviewsConfig(enabled=True), repo_claude_md=None,
        diff_text=diff,
        sealed_paths=["tests/acceptance/"],
    )
    assert "## Sealed paths (do not touch)" in briefing
    assert "tests/acceptance/" in briefing
    assert "TAMPER DETECTED" not in briefing


def test_briefing_flags_tamper_when_diff_touches_sealed_path() -> None:
    diff = (
        "diff --git a/tests/acceptance/ms01/foo.rs b/tests/acceptance/ms01/foo.rs\n"
        "--- a/tests/acceptance/ms01/foo.rs\n"
        "+++ b/tests/acceptance/ms01/foo.rs\n"
        "@@ -1,2 +1,3 @@\n"
        "+cheated = True\n"
    )
    briefing = build_review_briefing(
        pr_number=42, pr_url=None, repo_github="acme/coord-tui", repo_name="coord-tui",
        issue_number=944, issue_title="X", issue_body="",
        branch="my-branch", worker_machine="laptop", same_as_worker=False,
        reviews_cfg=ReviewsConfig(enabled=True), repo_claude_md=None,
        diff_text=diff,
        sealed_paths=["tests/acceptance/"],
    )
    assert "SEALED ORACLE TAMPER DETECTED" in briefing
    assert "tests/acceptance/" in briefing
    assert "request-changes is mandatory" in briefing


def test_diff_touched_sealed_paths_matches_diff_git_header() -> None:
    from coord.review import _diff_touched_sealed_paths

    diff = "diff --git a/tests/acceptance/ms01/foo.rs b/tests/acceptance/ms01/foo.rs\n"
    assert _diff_touched_sealed_paths(diff, ["tests/acceptance/"]) == ["tests/acceptance/"]


def test_diff_touched_sealed_paths_no_match() -> None:
    from coord.review import _diff_touched_sealed_paths

    diff = "diff --git a/src/foo.py b/src/foo.py\n"
    assert _diff_touched_sealed_paths(diff, ["tests/acceptance/"]) == []


def test_pr_diff_truncates_at_max_chars(monkeypatch) -> None:
    """#612: github_ops.pr_diff caps a huge diff and appends a truncation note."""
    from coord import github_ops

    big = "x" * 10_000
    monkeypatch.setattr(github_ops, "_gh", lambda *args: big)
    out = github_ops.pr_diff("acme/api", 42, max_chars=100)
    assert out is not None
    assert out.startswith("x" * 100)
    assert "[diff truncated at 100 chars]" in out
    assert len(out) < len(big)


def test_pr_diff_returns_none_on_gh_error(monkeypatch) -> None:
    """#612: pr_diff is best-effort — a gh failure yields None, not a raise."""
    from coord import github_ops

    def _boom(*args):
        raise RuntimeError("gh exploded")

    monkeypatch.setattr(github_ops, "_gh", _boom)
    assert github_ops.pr_diff("acme/api", 42) is None


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


class _BadRequestResponse:
    """Simulates an agent 400 'does not handle repo' response (#904)."""

    def raise_for_status(self) -> None:
        import httpx
        raise httpx.HTTPStatusError(
            "400 Bad Request",
            request=httpx.Request("POST", "http://test/assign"),
            response=httpx.Response(
                400,
                text='{"error": "this agent does not handle repo"}',
            ),
        )

    def json(self) -> dict:
        return {"error": "this agent does not handle repo"}


class _FallThroughClient:
    """HTTP client that rejects one URL with 400, succeeds for all others (#904)."""

    def __init__(self, reject_fragment: str, success_payload: dict) -> None:
        self._reject_fragment = reject_fragment
        self._success_payload = success_payload
        self.calls: list[str] = []

    def post(self, url: str, *, json: dict, timeout: float):
        self.calls.append(url)
        if self._reject_fragment in url:
            return _BadRequestResponse()
        return _FakeHTTPResponse(self._success_payload)


class _AllRejectingClient:
    """HTTP client that always returns 400 (#904 exhaustion test)."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def post(self, url: str, *, json: dict, timeout: float):
        self.calls.append(url)
        return _BadRequestResponse()


class _ServerErrorResponse:
    """Simulates a transient agent 500 (mid-restart, unhandled exception, #904
    fix #2) — NOT a definitive "this agent doesn't handle this repo" rejection."""

    def raise_for_status(self) -> None:
        import httpx
        raise httpx.HTTPStatusError(
            "500 Internal Server Error",
            request=httpx.Request("POST", "http://test/assign"),
            response=httpx.Response(500, text='{"error": "internal error"}'),
        )

    def json(self) -> dict:
        return {"error": "internal error"}


class _AllServerErrorClient:
    """HTTP client that always returns 500 (#904 fix #2 transient-5xx test)."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def post(self, url: str, *, json: dict, timeout: float):
        self.calls.append(url)
        return _ServerErrorResponse()


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


def test_dispatch_review_dispatches_for_mock_author_type(
    two_machine_config: Config,
) -> None:
    """#930 fix: a completed ``type="mock-author"`` (Gate A) assignment must
    be eligible for review dispatch, not just ``type="work"`` — otherwise a
    Gate A branch can never reach a review through any `coord` command
    (`coord pr`, `coord notify`, the daemon tick), contradicting the type's
    own docstring/system-prompt promise that it flows through the same
    Work -> Test -> Review -> Merge pipeline as ordinary work."""
    board = Board()
    completed = replace(
        _completed_assignment(),
        type="mock-author",
        assignment_id="ma-1",
        branch="ms-5-gate-a",
    )
    client = _FakeHTTPClient({"id": "review-id-ma"})

    result = dispatch_review(
        completed, board, two_machine_config,
        http_client=client,
        pr_lookup=lambda repo_github, **kw: {
            "number": 43,
            "url": "https://github.com/acme/api/pull/43",
            "existed": True,
        },
        claude_md_reader=lambda p: "# Project rules\n",
        issue_body_fetcher=lambda repo, num: "issue body text",
        now=123.0,
        remote_branch_checker=lambda repo, branch: True,
    )

    assert result is not None
    assert result.type == "review"
    assert result.review_of_assignment_id == "ma-1"
    assert board.active == [result]


def test_dispatch_review_skipped_when_work_terminal(
    two_machine_config: Config, monkeypatch
) -> None:
    """#522 chokepoint: a completed work whose issue is closed / PR merged must
    not be reviewed — the second flood vector (reviews of already-merged
    #349/#194). Short-circuits before opening a PR and marks the row done."""
    monkeypatch.setattr("coord.github_ops.work_is_terminal", lambda *a, **k: True)
    completed = _completed_assignment()
    board = Board()
    pr_calls = {"n": 0}

    def _pr_lookup(repo_github, **kw):
        pr_calls["n"] += 1
        return {"number": 1, "url": "u", "existed": True}

    result = dispatch_review(
        completed, board, two_machine_config,
        http_client=_FakeHTTPClient({"id": "x"}),
        pr_lookup=_pr_lookup,
        claude_md_reader=lambda p: None,
        issue_body_fetcher=lambda repo, num: "",
    )
    assert result is None
    assert board.active == []
    assert pr_calls["n"] == 0, "must short-circuit before opening a PR"
    assert completed.review_state == "done"


def test_dispatch_pending_reviews_skips_terminal_rows(
    two_machine_config: Config, monkeypatch
) -> None:
    """#522: the bulk pending-review loop never dispatches a review for an
    already-merged row — it marks it done so it drops out of `eligible`."""
    from coord.review import dispatch_pending_reviews

    monkeypatch.setattr("coord.github_ops.work_is_terminal", lambda *a, **k: True)
    # test_state="passed" so the row clears the Test-before-Review gate and the
    # bulk loop reaches the #522 terminal check (a merged row was smoke-tested
    # before it merged).
    completed = replace(
        _completed_assignment(), review_state="pending", test_state="passed"
    )
    board = Board(completed=[completed])

    dispatched = dispatch_pending_reviews(board, two_machine_config)

    assert dispatched == []
    assert completed.review_state == "done"


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
        # Branch check mocked: this test covers routing, not remote-branch detection.
        remote_branch_checker=lambda repo, branch: True,
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


def test_dispatch_review_flags_sealed_acceptance_dir_when_driver_configured(
    two_machine_config: Config,
) -> None:
    """#944 sealing v1: dispatch_review must thread sealed_paths through to
    the briefing for any repo with an oracle-loop acceptance driver."""
    from coord.config import AcceptanceConfig, AcceptanceDriverConfig
    from dataclasses import replace as _replace

    cfg = _replace(
        two_machine_config,
        acceptance=AcceptanceConfig(drivers={
            "api": AcceptanceDriverConfig(kind="tui-tuidriver", run="cargo test"),
        }),
    )
    board = Board()
    completed = _completed_assignment(machine="laptop")
    client = _FakeHTTPClient({"id": "review-id-1"})

    dispatch_review(
        completed, board, cfg,
        http_client=client,
        pr_lookup=lambda repo_github, **kw: {
            "number": 42, "url": "https://github.com/acme/api/pull/42", "existed": True,
        },
        claude_md_reader=lambda p: None,
        issue_body_fetcher=lambda repo, num: "",
        now=123.0,
        remote_branch_checker=lambda repo, branch: True,
    )

    assert len(client.calls) == 1
    _, payload = client.calls[0]
    assert "Sealed paths (do not touch)" in payload["briefing"]
    assert "tests/acceptance/" in payload["briefing"]


def test_dispatch_review_flags_sealed_acceptance_dir_when_driver_is_routed(
    two_machine_config: Config,
) -> None:
    """#1125 review finding 1: same as
    test_dispatch_review_flags_sealed_acceptance_dir_when_driver_configured,
    but the repo's driver is routed rather than flat — sealing must still
    trigger since `driver_for(repo_name)` (no path) can't select a route and
    would otherwise silently return None here."""
    from coord.config import AcceptanceConfig, AcceptanceDriverConfig
    from dataclasses import replace as _replace

    cfg = _replace(
        two_machine_config,
        acceptance=AcceptanceConfig(drivers={
            "api": AcceptanceDriverConfig(routes=[
                AcceptanceDriverConfig(match="**", kind="cli-pytest", run="pytest"),
            ]),
        }),
    )
    board = Board()
    completed = _completed_assignment(machine="laptop")
    client = _FakeHTTPClient({"id": "review-id-1"})

    dispatch_review(
        completed, board, cfg,
        http_client=client,
        pr_lookup=lambda repo_github, **kw: {
            "number": 42, "url": "https://github.com/acme/api/pull/42", "existed": True,
        },
        claude_md_reader=lambda p: None,
        issue_body_fetcher=lambda repo, num: "",
        now=123.0,
        remote_branch_checker=lambda repo, branch: True,
    )

    assert len(client.calls) == 1
    _, payload = client.calls[0]
    assert "Sealed paths (do not touch)" in payload["briefing"]
    assert "tests/acceptance/" in payload["briefing"]


def test_dispatch_review_captures_branch_sha(
    two_machine_config: Config,
) -> None:
    """#821: dispatch_review must set review_head_sha on the returned Assignment.

    When the branch SHA can be fetched, the review Assignment carries it so
    has_approved_review can later reject the approval if new commits are pushed
    onto the branch after the review ran (stale-SHA check).
    """
    board = Board()
    completed = _completed_assignment(machine="laptop")
    client = _FakeHTTPClient({"id": "sha-review-1"})

    result = dispatch_review(
        completed, board, two_machine_config,
        http_client=client,
        pr_lookup=lambda repo_github, **kw: {
            "number": 7, "url": "https://github.com/acme/api/pull/7", "existed": True,
        },
        claude_md_reader=lambda p: "",
        issue_body_fetcher=lambda repo, num: "",
        remote_branch_checker=lambda repo, branch: True,
        branch_sha_fetcher=lambda repo, branch: "deadbeef1234",  # injected for test
    )

    assert result is not None
    assert result.review_head_sha == "deadbeef1234", (
        "review_head_sha must be captured from branch tip at dispatch time"
    )


def test_dispatch_review_tolerates_sha_fetch_failure(
    two_machine_config: Config,
) -> None:
    """#821: dispatch_review must not fail when the SHA fetcher raises."""
    board = Board()
    completed = _completed_assignment(machine="laptop")
    client = _FakeHTTPClient({"id": "sha-fail-1"})

    def _failing_sha(repo, branch):
        raise RuntimeError("GitHub unavailable")

    result = dispatch_review(
        completed, board, two_machine_config,
        http_client=client,
        pr_lookup=lambda repo_github, **kw: {
            "number": 8, "url": "https://github.com/acme/api/pull/8", "existed": True,
        },
        claude_md_reader=lambda p: "",
        issue_body_fetcher=lambda repo, num: "",
        remote_branch_checker=lambda repo, branch: True,
        branch_sha_fetcher=_failing_sha,
    )

    # Dispatch must succeed; review_head_sha is None (unavailable is not blocking).
    assert result is not None
    assert result.review_head_sha is None


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
    two_machine_config: Config, coord_db,
) -> None:
    from coord import state as state_mod

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
        # Branch check mocked: this test covers DB recording, not remote-branch detection.
        remote_branch_checker=lambda repo, branch: True,
    )

    assert result is not None
    records = state_mod.load_dispatched()
    assert len(records) == 1
    assert records[0]["assignment_id"] == "review-ledger-1"
    assert records[0]["repo_github"] == "acme/api"
    assert records[0]["machine_name"] == "server"


# ── _find_or_open_pr — PR body carries closing keyword (#287) ───────────────


def test_find_or_open_pr_body_includes_closes_keyword() -> None:
    """_find_or_open_pr must prepend 'Closes #{issue_number}' so GitHub
    auto-closes the linked issue when the PR is merged (#287).  Without
    it the issue stays stranded open and the coordinator brain keeps
    re-syncing it as state=open.
    """
    from coord.review import _find_or_open_pr
    import coord.github_ops as github_ops_mod

    captured: dict = {}

    def _fake_find_pr(repo_github, branch):
        return None  # no existing PR → trigger create_pr path

    def _fake_create_pr(repo_github, *, base, head, title, body):
        captured["body"] = body
        return {"number": 55, "url": "https://github.com/acme/api/pull/55", "existed": False}

    import unittest.mock as mock
    with (
        mock.patch.object(github_ops_mod, "find_pr_for_branch", _fake_find_pr),
        mock.patch.object(github_ops_mod, "create_pr", _fake_create_pr),
    ):
        result = _find_or_open_pr(
            "acme/api",
            branch="issue-42-fix",
            default_branch="main",
            issue_number=42,
            issue_title="Fix the login bug",
        )

    assert result is not None
    assert result["number"] == 55
    assert "Closes #42" in captured["body"]
    # The closing keyword must come at the very start so GitHub parses it.
    assert captured["body"].startswith("Closes #42\n\n")


def test_find_or_open_pr_uses_refs_for_mock_author() -> None:
    """#1077: a mock-author (Gate A) PR's issue_number is the milestone's
    tracking issue, not something this PR resolves — the body must use the
    non-closing 'Refs #N' so merging doesn't auto-close the tracking issue.
    """
    from coord.review import _find_or_open_pr
    import coord.github_ops as github_ops_mod

    captured: dict = {}

    def _fake_find_pr(repo_github, branch):
        return None

    def _fake_create_pr(repo_github, *, base, head, title, body):
        captured["body"] = body
        return {"number": 56, "url": "https://github.com/acme/api/pull/56", "existed": False}

    import unittest.mock as mock
    with (
        mock.patch.object(github_ops_mod, "find_pr_for_branch", _fake_find_pr),
        mock.patch.object(github_ops_mod, "create_pr", _fake_create_pr),
    ):
        result = _find_or_open_pr(
            "acme/api",
            branch="ms-33-gate-a",
            default_branch="main",
            issue_number=1041,
            issue_title="Milestone #33 tracking issue",
            assignment_type="mock-author",
        )

    assert result is not None
    assert captured["body"].startswith("Refs #1041\n\n")
    assert "Closes #1041" not in captured["body"]


def test_dispatch_review_passes_assignment_type_to_pr_lookup(
    two_machine_config: Config,
) -> None:
    """#1077: dispatch_review must forward the completed assignment's type
    so pr_lookup (``_find_or_open_pr``) can decide the Closes-vs-Refs
    keyword — otherwise a mock-author PR's body would always default to the
    closing form and merging it would wrongly close the tracking issue."""
    board = Board()
    captured: dict = {}
    completed = replace(
        _completed_assignment(),
        type="mock-author",
        assignment_id="ga-1",
        branch="ms-33-gate-a",
        issue_number=1041,
    )
    client = _FakeHTTPClient({"id": "review-id-ga"})

    def _pr_lookup(repo_github, **kw):
        captured.update(kw)
        return {"number": 1, "url": "u", "existed": True}

    dispatch_review(
        completed, board, two_machine_config,
        http_client=client,
        pr_lookup=_pr_lookup,
        claude_md_reader=lambda p: "# Project rules\n",
        issue_body_fetcher=lambda repo, num: "issue body text",
        now=123.0,
        remote_branch_checker=lambda repo, branch: True,
    )

    assert captured.get("assignment_type") == "mock-author"


# ── Reviewer system prompt ──────────────────────────────────────────────────


def test_reviewer_system_prompt_does_not_allow_gh_commands() -> None:
    """Workers must not call gh — coordinator posts the review for them."""
    assert "gh pr review" not in REVIEWER_SYSTEM_PROMPT
    assert "NOT allowed to run any `gh` commands" in REVIEWER_SYSTEM_PROMPT


def test_reviewer_system_prompt_instructs_structured_output() -> None:
    assert "REVIEW_VERDICT:" in REVIEWER_SYSTEM_PROMPT
    assert "REVIEW_BODY:" in REVIEWER_SYSTEM_PROMPT
    assert "END_REVIEW" in REVIEWER_SYSTEM_PROMPT


def test_reviewer_system_prompt_forbids_running_the_test_suite() -> None:
    """A reviewer reads the diff; it must NOT run the test suite. Running it
    on a headless GUI project (e.g. vimcode) hangs the session, and build/test
    is the separate pre-merge smoke gate's job. Regression for that hang."""
    assert "DO NOT run the project's test suite" in REVIEWER_SYSTEM_PROMPT
    # the old mandate must be gone
    assert "Run the test suite" not in REVIEWER_SYSTEM_PROMPT
    assert "allowed to run the project's test suite" not in REVIEWER_SYSTEM_PROMPT


# ── Briefing: structured output instructions ────────────────────────────────


def test_briefing_does_not_contain_gh_pr_review_command() -> None:
    """The briefing must not tell the reviewer to call gh pr review."""
    briefing = build_review_briefing(
        pr_number=42,
        pr_url="https://github.com/acme/api/pull/42",
        repo_github="acme/api",
        repo_name="api",
        issue_number=7,
        issue_title="Fix login",
        issue_body="",
        branch="issue-7-fix-login",
        worker_machine="laptop",
        same_as_worker=False,
        reviews_cfg=ReviewsConfig(enabled=True),
        repo_claude_md=None,
    )
    assert "gh pr review" not in briefing


def test_briefing_contains_structured_output_instructions() -> None:
    """The briefing must contain the REVIEW_VERDICT / REVIEW_BODY / END_REVIEW instructions."""
    briefing = build_review_briefing(
        pr_number=42,
        pr_url=None,
        repo_github="acme/api",
        repo_name="api",
        issue_number=7,
        issue_title="Fix login",
        issue_body="",
        branch="issue-7",
        worker_machine="laptop",
        same_as_worker=False,
        reviews_cfg=ReviewsConfig(enabled=True),
        repo_claude_md=None,
    )
    assert "REVIEW_VERDICT: approve" in briefing
    assert "REVIEW_BODY:" in briefing
    assert "END_REVIEW" in briefing
    assert "do NOT run any `gh` commands" in briefing


# ── parse_review_from_log ───────────────────────────────────────────────────


def _write_plain_log(path: Path, content: str) -> Path:
    """Write a plain-text log file."""
    path.write_text(content, encoding="utf-8")
    return path


def _write_stream_json_log(path: Path, assistant_texts: list[str]) -> Path:
    """Write a stream-json format log with assistant messages."""
    lines = []
    for text in assistant_texts:
        event = {
            "type": "assistant",
            "message": {
                "content": [{"type": "text", "text": text}]
            }
        }
        lines.append(json.dumps(event))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class TestParseReviewFromLog:
    def test_plain_text_approve(self, tmp_path: Path) -> None:
        log = tmp_path / "review.log"
        _write_plain_log(log, """\
I reviewed the diff carefully.

REVIEW_VERDICT: approve
REVIEW_BODY:
The implementation looks correct. Tests pass.
No CLAUDE.md violations found.
END_REVIEW
""")
        result = parse_review_from_log(log)
        assert result is not None
        assert result.verdict == "approve"
        assert "Tests pass." in result.body

    def test_plain_text_request_changes(self, tmp_path: Path) -> None:
        log = tmp_path / "review.log"
        _write_plain_log(log, """\
REVIEW_VERDICT: request-changes
REVIEW_BODY:
## Issues found

- `src/auth.py:42` — missing input validation
- Tests do not cover the error path
END_REVIEW
""")
        result = parse_review_from_log(log)
        assert result is not None
        assert result.verdict == "request-changes"
        assert "missing input validation" in result.body

    def test_plain_text_last_block_wins(self, tmp_path: Path) -> None:
        """When multiple blocks exist, the last one is used."""
        log = tmp_path / "review.log"
        _write_plain_log(log, """\
REVIEW_VERDICT: approve
REVIEW_BODY:
First pass — looks OK.
END_REVIEW

Actually I missed something...

REVIEW_VERDICT: request-changes
REVIEW_BODY:
Found a critical bug at line 42.
END_REVIEW
""")
        result = parse_review_from_log(log)
        assert result is not None
        assert result.verdict == "request-changes"
        assert "critical bug" in result.body

    def test_plain_text_no_review_body_marker(self, tmp_path: Path) -> None:
        """#608: reviewer omits the `REVIEW_BODY:` line and writes Markdown
        findings directly after the verdict. The body must still be captured
        (this is the exact shape that stranded the #607 review)."""
        log = tmp_path / "review.log"
        _write_plain_log(log, """\
REVIEW_VERDICT: request-changes

#### 1. Out-of-scope removal

`tui/src/app.rs` deletes `session_pane_live`.

**Must be restored.**

### Fix instructions

Revert the out-of-scope removals.
END_REVIEW
""")
        result = parse_review_from_log(log)
        assert result is not None
        assert result.verdict == "request-changes"
        assert "session_pane_live" in result.body
        assert "Fix instructions" in result.body
        # The optional marker must not leak into the captured body.
        assert "REVIEW_BODY:" not in result.body

    def test_stream_json_no_review_body_marker(self, tmp_path: Path) -> None:
        """#608: same markers-only shape, but in stream-json transcript form."""
        log = tmp_path / "review.log"
        _write_stream_json_log(log, [
            "Reviewing the diff...",
            "REVIEW_VERDICT: request-changes\n\n## Findings\n\nBug at auth.py:10.\nEND_REVIEW",
        ])
        result = parse_review_from_log(log)
        assert result is not None
        assert result.verdict == "request-changes"
        assert "Bug at auth.py:10." in result.body

    def test_last_block_wins_without_marker(self, tmp_path: Path) -> None:
        """The optional-marker change must not break 'last block wins' when
        neither block uses the `REVIEW_BODY:` header."""
        log = tmp_path / "review.log"
        _write_plain_log(log, """\
REVIEW_VERDICT: approve
First pass looks fine.
END_REVIEW

On reflection:

REVIEW_VERDICT: request-changes
Found a blocker at line 42.
END_REVIEW
""")
        result = parse_review_from_log(log)
        assert result is not None
        assert result.verdict == "request-changes"
        assert "blocker at line 42" in result.body
        assert "First pass" not in result.body

    def test_stream_json_approve(self, tmp_path: Path) -> None:
        log = tmp_path / "review.log"
        _write_stream_json_log(log, [
            "I'm reading the diff now...",
            "The tests look good.\n\nREVIEW_VERDICT: approve\nREVIEW_BODY:\nLGTM — clean diff, tests pass.\nEND_REVIEW",
        ])
        result = parse_review_from_log(log)
        assert result is not None
        assert result.verdict == "approve"
        assert "LGTM" in result.body

    def test_stream_json_request_changes(self, tmp_path: Path) -> None:
        log = tmp_path / "review.log"
        _write_stream_json_log(log, [
            "Let me check the diff...",
            "REVIEW_VERDICT: request-changes\nREVIEW_BODY:\nSecurity issue at auth.py:10.\nEND_REVIEW",
        ])
        result = parse_review_from_log(log)
        assert result is not None
        assert result.verdict == "request-changes"
        assert "Security issue" in result.body

    def test_stream_json_last_assistant_message_wins(self, tmp_path: Path) -> None:
        """The last assistant message containing the block is used."""
        log = tmp_path / "review.log"
        _write_stream_json_log(log, [
            "REVIEW_VERDICT: approve\nREVIEW_BODY:\nInitially approved.\nEND_REVIEW",
            "Wait, I found a bug.\nREVIEW_VERDICT: request-changes\nREVIEW_BODY:\nBug at line 7.\nEND_REVIEW",
        ])
        result = parse_review_from_log(log)
        assert result is not None
        assert result.verdict == "request-changes"
        assert "Bug at line 7" in result.body

    def test_not_found_returns_none(self, tmp_path: Path) -> None:
        log = tmp_path / "review.log"
        _write_plain_log(log, "I reviewed the diff. It looks fine.\n")
        result = parse_review_from_log(log)
        assert result is None

    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        result = parse_review_from_log(tmp_path / "nonexistent.log")
        assert result is None

    def test_stream_json_no_review_block_returns_none(self, tmp_path: Path) -> None:
        log = tmp_path / "review.log"
        _write_stream_json_log(log, [
            "I read the diff.",
            "The code looks okay but I forgot to output my verdict.",
        ])
        result = parse_review_from_log(log)
        assert result is None

    def test_case_insensitive_verdict(self, tmp_path: Path) -> None:
        log = tmp_path / "review.log"
        _write_plain_log(log, """\
REVIEW_VERDICT: Approve
REVIEW_BODY:
Looks good to me.
END_REVIEW
""")
        result = parse_review_from_log(log)
        assert result is not None
        assert result.verdict == "approve"  # normalised to lowercase

    def test_multiline_body_preserved(self, tmp_path: Path) -> None:
        log = tmp_path / "review.log"
        body_text = "## Summary\n\nLine 1.\nLine 2.\n\n### Details\n\n- Point A\n- Point B"
        _write_plain_log(log, f"REVIEW_VERDICT: request-changes\nREVIEW_BODY:\n{body_text}\nEND_REVIEW\n")
        result = parse_review_from_log(log)
        assert result is not None
        assert "Line 1." in result.body
        assert "Point B" in result.body

    def test_pass_alias_maps_to_approve(self, tmp_path: Path) -> None:
        """PASS is accepted as an alias for approve."""
        log = tmp_path / "review.log"
        _write_plain_log(log, """\
REVIEW_VERDICT: PASS
REVIEW_BODY:
All checks pass. Clean diff.
END_REVIEW
""")
        result = parse_review_from_log(log)
        assert result is not None
        assert result.verdict == "approve"
        assert "All checks pass." in result.body

    def test_fail_alias_maps_to_request_changes(self, tmp_path: Path) -> None:
        """FAIL is accepted as an alias for request-changes."""
        log = tmp_path / "review.log"
        _write_plain_log(log, """\
REVIEW_VERDICT: FAIL
REVIEW_BODY:
Security issue at auth.py:10.
END_REVIEW
""")
        result = parse_review_from_log(log)
        assert result is not None
        assert result.verdict == "request-changes"
        assert "Security issue" in result.body

    def test_pass_alias_case_insensitive(self, tmp_path: Path) -> None:
        """'pass' in any case is normalized to 'approve'."""
        log = tmp_path / "review.log"
        _write_plain_log(log, "REVIEW_VERDICT: Pass\nREVIEW_BODY:\nOK.\nEND_REVIEW\n")
        result = parse_review_from_log(log)
        assert result is not None
        assert result.verdict == "approve"

    def test_fail_alias_case_insensitive(self, tmp_path: Path) -> None:
        """'fail' in any case is normalized to 'request-changes'."""
        log = tmp_path / "review.log"
        _write_plain_log(log, "REVIEW_VERDICT: Fail\nREVIEW_BODY:\nProblems found.\nEND_REVIEW\n")
        result = parse_review_from_log(log)
        assert result is not None
        assert result.verdict == "request-changes"


class TestParseReviewFromAgent:
    """Cover the HTTP-fetch path used when the worker's log file lives on a
    remote agent and notify can't open it directly.
    """

    def test_fetches_log_via_agent_and_parses_verdict(self, monkeypatch) -> None:
        """Plain-text log served by the agent → verdict extracted."""
        from coord.review import parse_review_from_agent

        body = (
            "REVIEW_VERDICT: approve\n"
            "REVIEW_BODY:\n"
            "Diff looks clean.\n"
            "END_REVIEW\n"
        )

        class FakeResponse:
            text = body
            def raise_for_status(self): pass

        def fake_get(url, timeout):
            assert url == "http://elitebook:7433/logs/abc123"
            return FakeResponse()

        monkeypatch.setattr("coord.review.httpx.get", fake_get)
        result = parse_review_from_agent("elitebook", "abc123")
        assert result is not None
        assert result.verdict == "approve"
        assert "Diff looks clean" in result.body

    def test_stream_json_log_from_agent(self, monkeypatch) -> None:
        """Stream-json log fetched over HTTP → verdict still extracted."""
        from coord.review import parse_review_from_agent
        import json

        assistant_text = (
            "Reviewing...\n\n"
            "REVIEW_VERDICT: request-changes\n"
            "REVIEW_BODY:\n"
            "Missing test coverage on the auth path.\n"
            "END_REVIEW"
        )
        body = (
            json.dumps({
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": assistant_text}]},
            }) + "\n"
        )

        class FakeResponse:
            text = body
            def raise_for_status(self): pass

        def fake_get(url, timeout):
            return FakeResponse()

        monkeypatch.setattr("coord.review.httpx.get", fake_get)
        result = parse_review_from_agent("dellserver", "xyz789")
        assert result is not None
        assert result.verdict == "request-changes"
        assert "Missing test coverage" in result.body

    def test_returns_none_on_http_error(self, monkeypatch) -> None:
        """Agent unreachable → None (caller falls back gracefully)."""
        from coord.review import parse_review_from_agent
        import httpx

        def fake_get(url, timeout):
            raise httpx.ConnectError("connection refused")

        monkeypatch.setattr("coord.review.httpx.get", fake_get)
        assert parse_review_from_agent("offline-host", "any") is None

    def test_returns_none_on_empty_log(self, monkeypatch) -> None:
        """Agent returns an empty body → None."""
        from coord.review import parse_review_from_agent

        class FakeResponse:
            text = ""
            def raise_for_status(self): pass

        monkeypatch.setattr("coord.review.httpx.get", lambda *a, **kw: FakeResponse())
        assert parse_review_from_agent("any-host", "any") is None


# ── #248: machine-readable review header ────────────────────────────────────


class TestReviewHeader:
    """Coverage for format_review_header / parse_review_header /
    estimate_review_counts — the helpers that let the coordinator embed
    a verdict + counts in posted review bodies so the TUI / coordinator
    session can surface them without re-ingesting prose."""

    def test_format_header_carries_verdict_only_by_default(self) -> None:
        from coord.review import format_review_header
        out = format_review_header(verdict="approve")
        assert out == "<!-- coord:review verdict=approve -->"

    def test_format_header_emits_all_provided_tokens(self) -> None:
        from coord.review import format_review_header
        out = format_review_header(
            verdict="request-changes",
            reviewer_machine="elitebook",
            assignment_id="144ffa027a31",
            blocking=2,
            nonblocking=5,
            nits=2,
        )
        # Order is stable and counts come before identity fields, matching
        # the example in #248's issue body.
        assert (
            out
            == "<!-- coord:review verdict=request-changes blocking=2 "
            "nonblocking=5 nits=2 reviewer=elitebook "
            "assignment=144ffa027a31 -->"
        )

    def test_parse_header_round_trips(self) -> None:
        from coord.review import format_review_header, parse_review_header
        header = format_review_header(
            verdict="approve",
            reviewer_machine="precision",
            assignment_id="abc123",
            blocking=0,
            nonblocking=3,
            nits=1,
        )
        parsed = parse_review_header(header)
        assert parsed == {
            "verdict": "approve",
            "blocking": 0,
            "nonblocking": 3,
            "nits": 1,
            "reviewer": "precision",
            "assignment": "abc123",
        }

    def test_parse_header_from_full_body(self) -> None:
        """The parser must find the header even when it's followed by
        a full prose body — that's the normal case after the coordinator
        prepends it to findings.body."""
        from coord.review import parse_review_header
        body = (
            "<!-- coord:review verdict=approve blocking=0 reviewer=dellserver -->\n"
            "\n"
            "## Review Complete — ✅ Approved\n"
            "\n"
            "Looks good — all tests pass and the diff stays in scope.\n"
        )
        parsed = parse_review_header(body)
        assert parsed is not None
        assert parsed["verdict"] == "approve"
        assert parsed["blocking"] == 0
        assert parsed["reviewer"] == "dellserver"

    def test_parse_returns_none_when_header_missing(self) -> None:
        from coord.review import parse_review_header
        assert parse_review_header("## Review\n\nLooks fine.") is None

    def test_parse_returns_none_when_verdict_missing(self) -> None:
        """A coord:review HTML comment without a verdict is invalid —
        the parser refuses to return a partial result."""
        from coord.review import parse_review_header
        assert parse_review_header("<!-- coord:review reviewer=x -->") is None

    def test_parse_ignores_unknown_tokens(self) -> None:
        from coord.review import parse_review_header
        parsed = parse_review_header(
            "<!-- coord:review verdict=approve future-token=hello extra=42 -->"
        )
        assert parsed is not None
        assert parsed["verdict"] == "approve"
        # Unknown tokens land as strings; never raise.
        assert parsed["future-token"] == "hello"
        assert parsed["extra"] == "42"

    def test_estimate_counts_picks_up_section_bullets(self) -> None:
        from coord.review import estimate_review_counts
        body = (
            "## Required changes\n"
            "- HUMAN_REQUIRED never persists (coord/cli.py:2616-2663)\n"
            "- retry cap not enforced (coord/conflict_fix.py:161-167)\n"
            "\n"
            "## Non-blocking concerns\n"
            "- Consider extracting the helper into a shared module\n"
            "* And another point that's not blocking\n"
            "\n"
            "## Polish / nits\n"
            "- Trailing whitespace at coord/agent.py:42\n"
        )
        b, nb, nits = estimate_review_counts(body)
        assert (b, nb, nits) == (2, 2, 1)

    def test_estimate_counts_returns_none_when_no_recognised_sections(
        self,
    ) -> None:
        """When the prose doesn't use the conventional headings, the
        heuristic refuses to guess — better an absent count than a
        misleading one."""
        from coord.review import estimate_review_counts
        body = "Looks fine to me — approving.\n"
        assert estimate_review_counts(body) == (None, None, None)

    def test_estimate_counts_empty_section_records_zero(self) -> None:
        """Reaching a recognised heading sets the bucket to 0 even when
        no bullets follow — distinguishes 'no items found' from 'didn't
        check that section'."""
        from coord.review import estimate_review_counts
        body = (
            "## Blocking\n"
            "\n"
            "(none — diff is clean)\n"
            "\n"
            "## Nits\n"
            "- One trailing space at line 42\n"
        )
        b, nb, nits = estimate_review_counts(body)
        # blocking section was visited (set to 0); no Non-blocking
        # heading appears (stays None); nits has one bullet.
        assert b == 0
        assert nb is None
        assert nits == 1


# ── Flood guard: dispatch_pending_reviews (incident 2026-06-08) ──────────────


def _pending_work(n: int) -> list[Assignment]:
    """n completed work rows, all eligible for review (review_state=None)."""
    return [
        Assignment(
            machine_name="laptop",
            repo_name="api",
            issue_number=i + 1,
            issue_title=f"work {i + 1}",
            assignment_id=f"w{i + 1}",
            status="done",
            branch=f"issue-{i + 1}-x",
            type="work",
            review_state=None,
            dispatched_at=0.0,
            finished_at=1.0,
        )
        for i in range(n)
    ]


def _flood_config(**review_kw) -> Config:
    # Pin a review-first gate order so the Test-before-Review gate is OFF for
    # these flood-guard tests — they exercise the cap / surge / #459 dedupe,
    # orthogonal to the test gate (which has its own test below, exercised via
    # the explicit test_gate_active=True parameter).
    return Config(
        repos=[],
        machines=[],
        reviews=ReviewsConfig(**review_kw),
        pipeline=PipelineConfig(default_gates=["review", "test", "merge"]),
    )


@pytest.fixture
def fake_dispatch(monkeypatch):
    """Replace the real (network) dispatch_review with a recording stub.

    Returns the list of assignment_ids that got a review dispatched.
    """
    calls: list[str] = []

    def _fake(completed, board, config, *, now=None, **kw):
        calls.append(completed.assignment_id)
        review = Assignment(
            machine_name="server",
            repo_name=completed.repo_name,
            issue_number=completed.issue_number,
            issue_title=f"[review] {completed.issue_title}",
            assignment_id=f"rev-{completed.assignment_id}",
            status="running",
            type="review",
            review_of_assignment_id=completed.assignment_id,
            dispatched_at=0.0,
        )
        board.active.append(review)
        return review

    monkeypatch.setattr("coord.review.dispatch_review", _fake)
    return calls


def test_flood_guard_dispatches_all_below_cap(fake_dispatch) -> None:
    board = Board(completed=_pending_work(3))
    cfg = _flood_config(max_auto_dispatch_per_pass=5, flood_threshold=12)
    out = dispatch_pending_reviews(board, cfg)
    assert len(out) == 3
    assert len(fake_dispatch) == 3
    assert all(c.review_state == "dispatched" for c in board.completed)


def test_dispatch_pending_reviews_includes_mock_author(fake_dispatch) -> None:
    """#930 fix: the bulk/auto dispatch path (`coord notify`, `reconcile()`)
    must pick up a completed `type="mock-author"` (Gate A) row the same as
    ordinary work — previously the ``eligible`` filter hard-required
    ``type == "work"`` so a Gate A branch could never get an automatic
    review."""
    mock_author = Assignment(
        machine_name="laptop",
        repo_name="api",
        issue_number=930,
        issue_title="Gate A mock",
        assignment_id="ma-2",
        status="done",
        branch="ms-5-gate-a",
        type="mock-author",
        review_state=None,
        dispatched_at=0.0,
        finished_at=1.0,
    )
    board = Board(completed=[mock_author])
    cfg = _flood_config(max_auto_dispatch_per_pass=5, flood_threshold=12)

    out = dispatch_pending_reviews(board, cfg)

    assert len(out) == 1
    assert fake_dispatch == ["ma-2"]
    assert mock_author.review_state == "dispatched"


def test_dispatch_pending_reviews_skips_interactive_work(fake_dispatch) -> None:
    """#555: an *interactive* (provider_name='claude-pty') work completion must
    NOT get a headless auto-review — its review is human-attended. An
    otherwise-identical non-interactive row still dispatches one."""
    interactive = Assignment(
        machine_name="laptop",
        repo_name="api",
        issue_number=541,
        issue_title="interactive work",
        assignment_id="w-interactive",
        status="done",
        branch="issue-541-x",
        type="work",
        review_state=None,
        provider_name="claude-pty",
        dispatched_at=0.0,
        finished_at=1.0,
    )
    headless = Assignment(
        machine_name="laptop",
        repo_name="api",
        issue_number=542,
        issue_title="headless work",
        assignment_id="w-headless",
        status="done",
        branch="issue-542-x",
        type="work",
        review_state=None,
        provider_name=None,
        dispatched_at=0.0,
        finished_at=1.0,
    )
    board = Board(completed=[interactive, headless])
    cfg = _flood_config(max_auto_dispatch_per_pass=5, flood_threshold=12)

    out = dispatch_pending_reviews(board, cfg)

    assert len(out) == 1
    assert fake_dispatch == ["w-headless"]  # only the non-interactive row
    assert interactive.review_state is None  # never eligible → untouched
    assert headless.review_state == "dispatched"


def test_flood_guard_caps_per_pass(fake_dispatch) -> None:
    board = Board(completed=_pending_work(10))
    cfg = _flood_config(max_auto_dispatch_per_pass=5, flood_threshold=12)
    out = dispatch_pending_reviews(board, cfg)
    assert len(out) == 5  # capped this pass
    pending = [c for c in board.completed if c.review_state in (None, "pending")]
    assert len(pending) == 5  # remainder held for the next pass
    # A second pass drains the rest (still under threshold).
    out2 = dispatch_pending_reviews(board, cfg)
    assert len(out2) == 5
    assert all(c.review_state == "dispatched" for c in board.completed)


def test_flood_guard_surge_gate_refuses_all(fake_dispatch) -> None:
    board = Board(completed=_pending_work(20))  # > flood_threshold
    cfg = _flood_config(max_auto_dispatch_per_pass=5, flood_threshold=12)
    out = dispatch_pending_reviews(board, cfg)
    assert out == []
    assert fake_dispatch == []  # nothing dispatched
    assert all(c.review_state is None for c in board.completed)  # board untouched


def test_flood_guard_surge_gate_config_override(fake_dispatch) -> None:
    board = Board(completed=_pending_work(20))
    cfg = _flood_config(
        max_auto_dispatch_per_pass=5, flood_threshold=12, allow_review_flood=True
    )
    out = dispatch_pending_reviews(board, cfg)
    assert len(out) == 5  # surge gate overridden, per-pass cap still applies


def test_flood_guard_surge_gate_env_override(fake_dispatch, monkeypatch) -> None:
    monkeypatch.setenv("COORD_ALLOW_REVIEW_FLOOD", "1")
    board = Board(completed=_pending_work(20))
    cfg = _flood_config(max_auto_dispatch_per_pass=5, flood_threshold=12)
    out = dispatch_pending_reviews(board, cfg)
    assert len(out) == 5


def test_flood_guard_threshold_zero_disables_surge_gate(fake_dispatch) -> None:
    board = Board(completed=_pending_work(50))
    cfg = _flood_config(max_auto_dispatch_per_pass=5, flood_threshold=0)
    out = dispatch_pending_reviews(board, cfg)
    assert len(out) == 5  # no surge gate, but cap still bounds the pass


def test_flood_guard_skips_active_fix_followup(fake_dispatch) -> None:
    # #459: a row whose issue has a live work/conflict-fix is not eligible.
    rows = _pending_work(2)
    board = Board(
        completed=rows,
        active=[
            Assignment(
                machine_name="laptop",
                repo_name="api",
                issue_number=1,  # matches rows[0]
                issue_title="[fix-1] work 1",
                assignment_id="fix1",
                status="running",
                type="work",
            )
        ],
    )
    cfg = _flood_config(max_auto_dispatch_per_pass=5, flood_threshold=12)
    out = dispatch_pending_reviews(board, cfg)
    assert len(out) == 1  # only issue #2 (issue #1 has an active fix)
    assert fake_dispatch == ["w2"]


def test_flood_guard_respects_test_gate(fake_dispatch) -> None:
    rows = _pending_work(4)
    rows[0].test_state = "passed"
    rows[1].test_state = "skipped"
    # rows[2], rows[3] have test_state=None → not eligible under an active gate
    board = Board(completed=rows)
    cfg = _flood_config(max_auto_dispatch_per_pass=5, flood_threshold=12)
    out = dispatch_pending_reviews(board, cfg, test_gate_active=True)
    assert len(out) == 2
    assert sorted(fake_dispatch) == ["w1", "w2"]


def test_bulk_review_gate_activates_from_test_first_default(fake_dispatch) -> None:
    """Test-before-Review reorder: when default_gates orders Test before Review,
    the bulk path holds review until the work has a passed/skipped test verdict
    — no explicit test_gate_active flag needed."""
    rows = _pending_work(3)
    rows[0].test_state = "passed"
    # rows[1], rows[2] untested → held by the config-driven gate.
    board = Board(completed=rows)
    cfg = Config(
        repos=[],
        machines=[],
        reviews=ReviewsConfig(max_auto_dispatch_per_pass=5, flood_threshold=12),
        pipeline=PipelineConfig(default_gates=["test", "review", "merge"]),
    )
    out = dispatch_pending_reviews(board, cfg)
    assert len(out) == 1
    assert fake_dispatch == ["w1"]
    # The untested rows stay pending for a later pass (after they're tested).
    assert rows[1].review_state in (None, "pending")
    assert rows[2].review_state in (None, "pending")


def test_mock_author_auto_skips_test_gate(fake_dispatch) -> None:
    """#1076: a completed `type="mock-author"` (Gate A) row never gets a real
    Test-gate verdict — a contract/fixture-only diff matches no smoke
    capability rule by construction — so under an active test-precedes-review
    gate it must be auto-backfilled to test_state="skipped" and dispatched,
    not silently excluded from `eligible` forever."""
    mock_author = Assignment(
        machine_name="laptop",
        repo_name="api",
        issue_number=1076,
        issue_title="Gate A mock",
        assignment_id="ma-gate-a",
        status="done",
        branch="ms-9-gate-a",
        type="mock-author",
        review_state=None,
        test_state=None,
        dispatched_at=0.0,
        finished_at=1.0,
    )
    board = Board(completed=[mock_author])
    cfg = Config(
        repos=[],
        machines=[],
        reviews=ReviewsConfig(max_auto_dispatch_per_pass=5, flood_threshold=12),
        pipeline=PipelineConfig(default_gates=["test", "review", "merge"]),
    )

    out = dispatch_pending_reviews(board, cfg)

    assert len(out) == 1
    assert fake_dispatch == ["ma-gate-a"]
    assert mock_author.test_state == "skipped"
    assert mock_author.review_state == "dispatched"


def test_mock_author_auto_skip_does_not_weaken_work_gate(fake_dispatch) -> None:
    """#1076: the mock-author auto-skip must not leak onto `type="work"` rows
    — the test gate keeps holding untested real-code completions exactly as
    before."""
    mock_author = Assignment(
        machine_name="laptop",
        repo_name="api",
        issue_number=1076,
        issue_title="Gate A mock",
        assignment_id="ma-gate-a",
        status="done",
        branch="ms-9-gate-a",
        type="mock-author",
        review_state=None,
        test_state=None,
        dispatched_at=0.0,
        finished_at=1.0,
    )
    work = _pending_work(1)[0]  # type="work", test_state=None
    board = Board(completed=[mock_author, work])
    cfg = Config(
        repos=[],
        machines=[],
        reviews=ReviewsConfig(max_auto_dispatch_per_pass=5, flood_threshold=12),
        pipeline=PipelineConfig(default_gates=["test", "review", "merge"]),
    )

    out = dispatch_pending_reviews(board, cfg)

    assert len(out) == 1
    assert fake_dispatch == ["ma-gate-a"]
    assert mock_author.test_state == "skipped"
    assert work.test_state is None
    assert work.review_state in (None, "pending")


# ── Flood guard: config parsing ──────────────────────────────────────────────


def test_reviews_config_flood_guard_defaults(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n  - name: api\n    github: acme/api\n"
        "machines:\n  - name: laptop\n    host: laptop.tail\n    repos: [api]\n"
    )
    cfg = load(p)
    assert cfg.reviews.max_auto_dispatch_per_pass == 5
    assert cfg.reviews.flood_threshold == 12
    assert cfg.reviews.allow_review_flood is False


def test_reviews_config_flood_guard_custom(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n  - name: api\n    github: acme/api\n"
        "machines:\n  - name: laptop\n    host: laptop.tail\n    repos: [api]\n"
        "reviews:\n"
        "  max_auto_dispatch_per_pass: 3\n"
        "  flood_threshold: 25\n"
        "  allow_review_flood: true\n"
    )
    cfg = load(p)
    assert cfg.reviews.max_auto_dispatch_per_pass == 3
    assert cfg.reviews.flood_threshold == 25
    assert cfg.reviews.allow_review_flood is True


def test_reviews_config_rejects_negative_flood_threshold(tmp_path: Path) -> None:
    from coord.config import ConfigError

    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n  - name: api\n    github: acme/api\n"
        "machines:\n  - name: laptop\n    host: laptop.tail\n    repos: [api]\n"
        "reviews:\n  flood_threshold: -1\n"
    )
    with pytest.raises(ConfigError, match="flood_threshold must be a non-negative integer"):
        load(p)


def test_reviews_config_rejects_bool_for_int_field(tmp_path: Path) -> None:
    from coord.config import ConfigError

    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n  - name: api\n    github: acme/api\n"
        "machines:\n  - name: laptop\n    host: laptop.tail\n    repos: [api]\n"
        "reviews:\n  max_auto_dispatch_per_pass: true\n"
    )
    with pytest.raises(ConfigError, match="max_auto_dispatch_per_pass must be a non-negative integer"):
        load(p)


# ── #586: branch-not-on-remote guard in dispatch_review ─────────────────────


def test_dispatch_review_routes_back_to_worker_when_branch_not_on_remote(
    two_machine_config: Config,
) -> None:
    """When the branch isn't on the remote, review is routed back to the
    original worker machine (which has it locally) rather than dispatching
    to a different machine that would crash in 2 seconds."""
    board = Board()
    completed = _completed_assignment(machine="laptop", branch="issue-1-fix")
    client = _FakeHTTPClient({"id": "review-local-1"})

    result = dispatch_review(
        completed, board, two_machine_config,
        http_client=client,
        pr_lookup=lambda repo_github, **kw: {"number": 10, "url": "u", "existed": True},
        claude_md_reader=lambda p: None,
        issue_body_fetcher=lambda repo, num: "",
        # Simulate branch absent on remote.
        remote_branch_checker=lambda repo, branch: False,
    )

    assert result is not None
    # Must have routed to the worker's own machine, not the other one.
    assert result.machine_name == "laptop"
    # Exactly one HTTP call, to laptop.tail (the worker machine).
    assert len(client.calls) == 1
    url, _ = client.calls[0]
    assert "laptop.tail" in url


def test_dispatch_review_blocks_and_sets_state_when_branch_not_on_remote_and_worker_unavailable(
    two_machine_config: Config,
) -> None:
    """When branch isn't on remote AND the original worker machine is absent
    from config, dispatch_review must return None and set
    review_state='branch_not_on_remote' so coord status surfaces a visible
    error instead of silently failing."""
    # Build a config where only one machine exists (NOT the original worker).
    from dataclasses import replace as dc_replace
    single_machine_cfg = dc_replace(
        two_machine_config,
        machines=[
            Machine(
                name="server", host="server.tail",
                capabilities=["python"], repos=["api"],
                repo_paths={"api": "/srv/api"},
            ),
        ],
    )
    board = Board()
    # Completed assignment was done on "laptop" which is no longer in config.
    completed = _completed_assignment(machine="laptop", branch="issue-1-fix")

    result = dispatch_review(
        completed, board, single_machine_cfg,
        http_client=_FakeHTTPClient({"id": "should-not-fire"}),
        pr_lookup=lambda repo_github, **kw: {"number": 10, "url": "u", "existed": True},
        claude_md_reader=lambda p: None,
        issue_body_fetcher=lambda repo, num: "",
        remote_branch_checker=lambda repo, branch: False,
    )

    assert result is None
    assert board.active == []
    assert completed.review_state == "branch_not_on_remote"


def test_dispatch_review_passes_through_normally_when_branch_on_remote(
    two_machine_config: Config,
) -> None:
    """When branch IS on remote, the normal cross-machine dispatch path runs."""
    board = Board()
    completed = _completed_assignment(machine="laptop", branch="issue-1-fix")
    client = _FakeHTTPClient({"id": "review-remote-1"})

    result = dispatch_review(
        completed, board, two_machine_config,
        http_client=client,
        pr_lookup=lambda repo_github, **kw: {"number": 10, "url": "u", "existed": True},
        claude_md_reader=lambda p: None,
        issue_body_fetcher=lambda repo, num: "",
        # Branch exists on remote — normal cross-machine routing.
        remote_branch_checker=lambda repo, branch: True,
    )

    assert result is not None
    assert result.machine_name == "server"  # different from worker (laptop)


# ── #904: fall-through loop + health-check pre-filter ───────────────────────


def test_dispatch_review_skips_machine_not_advertising_repo_in_health(
    two_machine_config: Config,
) -> None:
    """Fix #2 (PREVENTATIVE, #904): a candidate whose /health does not list the
    target repo is skipped before any POST attempt.

    When ``server`` (the preferred different-machine candidate) advertises an
    empty repo list, ``dispatch_review`` should skip it and fall through to
    ``laptop`` (the worker's own machine) rather than dispatching a guaranteed-
    400 POST."""
    board = Board()
    completed = _completed_assignment(machine="laptop")
    client = _FakeHTTPClient({"id": "health-filter-1"})

    def _health(host: str) -> list[str] | None:
        # server is drifted: /health omits "api" from its repos list.
        if "server" in host:
            return []           # reachable but "api" is absent
        return ["api"]          # laptop advertises "api" correctly

    result = dispatch_review(
        completed, board, two_machine_config,
        http_client=client,
        pr_lookup=lambda repo_github, **kw: {"number": 7, "url": "u", "existed": True},
        claude_md_reader=lambda p: None,
        issue_body_fetcher=lambda repo, num: "",
        remote_branch_checker=lambda repo, branch: True,
        health_checker=_health,
    )

    assert result is not None
    # server was filtered by health check; dispatch fell through to laptop.
    assert result.machine_name == "laptop"
    assert result.assignment_id == "health-filter-1"
    # Only ONE POST — to laptop; server was excluded before any network call.
    assert len(client.calls) == 1
    url, _ = client.calls[0]
    assert "laptop.tail" in url
    assert "server.tail" not in url


def test_dispatch_review_falls_through_to_second_candidate_on_400(
    two_machine_config: Config,
) -> None:
    """Fix #1 (PRIMARY, #904): when the first reviewer candidate returns a 400
    'does not handle repo' response, ``dispatch_review`` retries with the next
    candidate instead of silently returning None and leaving review_state as
    'pending'.

    ``http_client=`` is the existing injection seam; the test stubs it so
    ``server.tail`` 400s and ``laptop.tail`` succeeds."""
    board = Board()
    completed = _completed_assignment(machine="laptop")

    client = _FallThroughClient(
        reject_fragment="server.tail",
        success_payload={"id": "fallthrough-review-1"},
    )

    result = dispatch_review(
        completed, board, two_machine_config,
        http_client=client,
        pr_lookup=lambda repo_github, **kw: {"number": 5, "url": "u", "existed": True},
        claude_md_reader=lambda p: None,
        issue_body_fetcher=lambda repo, num: "",
        remote_branch_checker=lambda repo, branch: True,
        # Bypass health pre-filter so only the POST rejection drives fall-through.
        health_checker=lambda host: None,
    )

    assert result is not None
    assert result.machine_name == "laptop"
    assert result.assignment_id == "fallthrough-review-1"
    # Two POST calls — first to server (rejected), then to laptop (accepted).
    assert len(client.calls) == 2
    assert any("server.tail" in url for url in client.calls), (
        "expected a POST to server.tail (the first, rejected candidate)"
    )
    assert any("laptop.tail" in url for url in client.calls), (
        "expected a POST to laptop.tail (the fall-through candidate)"
    )
    # Review assignment is on the board.
    assert result in board.active
    assert result.review_of_assignment_id == completed.assignment_id


def test_dispatch_review_sets_stall_state_when_all_candidates_rejected(
    two_machine_config: Config,
) -> None:
    """Fix #1 + exhaustion (#904): when ALL reviewer candidates 400, the work
    row's ``review_state`` is set to ``'no_eligible_reviewer'`` (NOT left as
    ``'pending'``), so the pending-review loop stops silently retrying and
    ``coord status`` can surface an actionable error.

    Returns None (same contract as before) but the stall state is now visible."""
    board = Board()
    completed = _completed_assignment(machine="laptop")
    client = _AllRejectingClient()

    result = dispatch_review(
        completed, board, two_machine_config,
        http_client=client,
        pr_lookup=lambda repo_github, **kw: {"number": 3, "url": "u", "existed": True},
        claude_md_reader=lambda p: None,
        issue_body_fetcher=lambda repo, num: "",
        remote_branch_checker=lambda repo, branch: True,
        # Bypass health pre-filter so the POST 400 is the signal.
        health_checker=lambda host: None,
    )

    assert result is None
    assert board.active == []
    # Stall state set — NOT left as None/pending.
    assert completed.review_state == "no_eligible_reviewer", (
        f"expected 'no_eligible_reviewer', got {completed.review_state!r}"
    )
    # Both candidates were tried — not just the first.
    assert len(client.calls) == 2, (
        f"expected 2 POST attempts (one per candidate), got {len(client.calls)}"
    )


def test_dispatch_review_leaves_pending_when_all_candidates_5xx(
    two_machine_config: Config,
) -> None:
    """Fix #2 (#904): a 5xx from every candidate is a TRANSIENT failure (agent
    mid-restart, unhandled exception, etc.) — it says nothing about whether
    this agent/repo pairing is valid, unlike a 4xx.  ``dispatch_review`` must
    NOT set ``review_state='no_eligible_reviewer'`` in this case; the row
    should stay eligible (``review_state`` untouched / still pending) so the
    next reconcile/notify pass retries automatically, exactly like the
    existing network-unreachable branch."""
    board = Board()
    completed = _completed_assignment(machine="laptop")
    client = _AllServerErrorClient()

    result = dispatch_review(
        completed, board, two_machine_config,
        http_client=client,
        pr_lookup=lambda repo_github, **kw: {"number": 3, "url": "u", "existed": True},
        claude_md_reader=lambda p: None,
        issue_body_fetcher=lambda repo, num: "",
        remote_branch_checker=lambda repo, branch: True,
        health_checker=lambda host: None,
    )

    assert result is None
    assert board.active == []
    # NOT 'no_eligible_reviewer' — a 5xx is transient, not a definitive
    # rejection, so the caller (dispatch_pending_reviews) must retry it.
    assert completed.review_state != "no_eligible_reviewer", (
        f"5xx must not be treated as a definitive rejection, got "
        f"review_state={completed.review_state!r}"
    )
    assert len(client.calls) == 2, (
        f"expected 2 POST attempts (one per candidate), got {len(client.calls)}"
    )
