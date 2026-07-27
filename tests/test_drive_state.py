"""Tests for coord/drive_state.py — the per-issue board projection (#1392).

The Python port of ``scripts/coord_issue_state.py``, which had zero tests. The
projection rules that earn a test here are the ones that were load-bearing in
the shell: keying the review/smoke rows on the *work* assignment id (so a stale
earlier verdict is never read as current), the merge-entry lookup falling back
from ``merge_plan`` to the raw ``merge_queue``, ``pick_machine``'s load/pause
handling, and the ETag cache's single-file atomicity (a two-file cache let two
concurrent drivers pair a fresh ETag with a stale body — PR #1391).
"""

from __future__ import annotations

import json

import pytest

from coord.config import Config
from coord.drive_state import (
    BoardFetcher,
    DriveStateError,
    IssueState,
    pick_machine,
    project,
)
from coord.models import Machine, Repo


REPO = "claude-coordinator"


def make_config(*, machines: list[Machine] | None = None) -> Config:
    return Config(
        repos=[
            Repo(
                name=REPO,
                github="john/claude-coordinator",
                default_branch="main",
                test_command="pytest -q",
            )
        ],
        machines=machines
        if machines is not None
        else [Machine(name="precision", host="precision", repos=[REPO])],
    )


def row(**kw) -> dict:
    base = {
        "repo_name": REPO,
        "issue_number": 1392,
        "type": "work",
        "status": "done",
        "assignment_id": "a1",
        "dispatched_at": 100.0,
    }
    base.update(kw)
    return base


# ── the happy path ───────────────────────────────────────────────────────────


def test_project_reads_the_work_row_and_repo_config():
    payload = {
        "assignments": [
            row(
                assignment_id="w1",
                branch="issue-1392-port",
                machine_name="precision",
                provider_name="claude-code",
                test_state="passed",
                review_state="done",
                review_iteration=2,
                exit_code=0,
            )
        ]
    }
    state = project(payload, REPO, 1392, make_config())
    assert state.work_aid == "w1"
    assert state.work_branch == "issue-1392-port"
    assert state.work_test_state == "passed"
    assert state.work_review_iter == 2
    assert state.work_exit_code == 0
    assert state.repo_github == "john/claude-coordinator"
    assert state.repo_default_branch == "main"
    assert state.repo_test_command == "pytest -q"


def test_project_refuses_an_unconfigured_repo():
    with pytest.raises(DriveStateError, match="not in coordinator.yml"):
        project({"assignments": []}, "nope", 1, make_config())


def test_project_ignores_other_repos_and_issues():
    payload = {
        "assignments": [
            row(assignment_id="other-repo", repo_name="quadraui"),
            row(assignment_id="other-issue", issue_number=999),
        ]
    }
    state = project(payload, REPO, 1392, make_config())
    assert state.work_aid == ""
    assert state.active_count == 0


def test_project_picks_the_most_recently_dispatched_work_row():
    payload = {
        "assignments": [
            row(assignment_id="old", dispatched_at=100.0),
            row(assignment_id="new", dispatched_at=200.0),
        ]
    }
    assert project(payload, REPO, 1392, make_config()).work_aid == "new"


@pytest.mark.parametrize("work_type", ["work", "mock-author", "test-author"])
def test_project_treats_every_work_like_type_as_the_work_row(work_type):
    """#1141: a hardcoded copy of this set going stale stalled the pipeline."""
    payload = {"assignments": [row(assignment_id="w", type=work_type)]}
    state = project(payload, REPO, 1392, make_config())
    assert state.work_aid == "w"
    assert state.work_type == work_type


# ── review/smoke keyed on the work row ───────────────────────────────────────


def test_review_is_keyed_on_the_current_work_row_not_the_issue():
    """A fix round makes a new work row; the OLD review must not be read."""
    payload = {
        "assignments": [
            row(assignment_id="w1", dispatched_at=100.0),
            row(
                assignment_id="r1",
                type="review",
                dispatched_at=110.0,
                review_of_assignment_id="w1",
                review_verdict="request-changes",
            ),
            row(assignment_id="w2", dispatched_at=200.0),
        ]
    }
    state = project(payload, REPO, 1392, make_config())
    assert state.work_aid == "w2"
    assert state.review_aid == ""
    assert state.review_verdict == ""


def test_smoke_row_is_keyed_on_the_work_row_too():
    payload = {
        "assignments": [
            row(assignment_id="w1", dispatched_at=100.0),
            row(
                assignment_id="s1",
                type="smoke",
                status="running",
                dispatched_at=110.0,
                review_of_assignment_id="w1",
            ),
        ]
    }
    state = project(payload, REPO, 1392, make_config())
    assert state.smoke_aid == "s1"
    assert state.smoke_status == "running"


# ── active rows ──────────────────────────────────────────────────────────────


def test_active_count_counts_non_terminal_rows_only():
    payload = {
        "assignments": [
            row(assignment_id="w1", status="done"),
            row(assignment_id="s1", type="smoke", status="running"),
            row(assignment_id="r1", type="review", status="dispatched"),
            row(assignment_id="c1", type="review", status="cancelled"),
        ]
    }
    state = project(payload, REPO, 1392, make_config())
    assert state.active_count == 2
    assert state.active_types == ("review", "smoke")


@pytest.mark.parametrize(
    "status", ["done", "failed", "cancelled", "merged", "advisory"]
)
def test_every_terminal_status_is_inactive(status):
    payload = {"assignments": [row(status=status)]}
    assert project(payload, REPO, 1392, make_config()).active_count == 0


# ── merge entry ──────────────────────────────────────────────────────────────


def test_merge_entry_prefers_the_merge_plan():
    payload = {
        "assignments": [row(assignment_id="w1")],
        "merge_plan": [
            {
                "repo_name": REPO,
                "issue_number": 1392,
                "status": "BLOCKED",
                "reason": "review not approved",
                "assignment_id": "w1",
            }
        ],
    }
    state = project(payload, REPO, 1392, make_config())
    assert state.merge_status == "BLOCKED"
    assert state.merge_reason == "review not approved"


def test_merge_entry_falls_back_to_the_raw_queue_and_upcases_state():
    payload = {
        "assignments": [row(assignment_id="w1")],
        "merge_queue": [
            {
                "repo_name": REPO,
                "issue_number": 1392,
                "state": "conflict",
                "error": "rebase failed",
                "pr_url": "https://example/pr/1",
                "assignment_id": "w0",
            }
        ],
    }
    state = project(payload, REPO, 1392, make_config())
    assert state.merge_status == "CONFLICT"
    assert state.merge_reason == "rebase failed"
    assert state.merge_pr_url == "https://example/pr/1"
    # Matched on (repo, issue): the queue entry may be keyed to an earlier work
    # row in a fix chain, and that id is what `coord merge --only` must get.
    assert state.merge_aid == "w0"


# ── pick_machine ─────────────────────────────────────────────────────────────


def test_pick_machine_prefers_the_least_loaded_host():
    config = make_config(
        machines=[
            Machine(name="busy", host="busy", repos=[REPO]),
            Machine(name="idle", host="idle", repos=[REPO]),
        ]
    )
    payload = {
        "assignments": [
            row(assignment_id="x", machine_name="busy", status="running"),
            row(assignment_id="y", machine_name="busy", status="dispatched"),
        ]
    }
    assert pick_machine(payload, REPO, config) == "idle"


def test_pick_machine_skips_machines_that_do_not_host_the_repo():
    config = make_config(
        machines=[
            Machine(name="nope", host="nope", repos=["quadraui"]),
            Machine(name="yes", host="yes", repos=[REPO]),
        ]
    )
    assert pick_machine({}, REPO, config) == "yes"


def test_pick_machine_skips_paused_machines(monkeypatch):
    monkeypatch.setattr("coord.machine_pause.paused_set", lambda: {"paused"})
    config = make_config(
        machines=[
            Machine(name="paused", host="paused", repos=[REPO]),
            Machine(name="running", host="running", repos=[REPO]),
        ]
    )
    assert pick_machine({}, REPO, config) == "running"


def test_pick_machine_returns_empty_when_nothing_hosts_the_repo():
    assert pick_machine({}, REPO, make_config(machines=[])) == ""


def test_pick_machine_is_deterministic_on_a_tie():
    config = make_config(
        machines=[
            Machine(name="zeta", host="zeta", repos=[REPO]),
            Machine(name="alpha", host="alpha", repos=[REPO]),
        ]
    )
    assert pick_machine({}, REPO, config) == "alpha"


# ── fingerprint / flat dict ──────────────────────────────────────────────────


def test_fingerprint_changes_when_a_branched_on_field_changes():
    a = IssueState(repo=REPO, issue=1, work_status="done", work_test_state="")
    b = IssueState(repo=REPO, issue=1, work_status="done", work_test_state="running")
    assert a.fingerprint != b.fingerprint


def test_fingerprint_ignores_fields_the_state_machine_does_not_branch_on():
    a = IssueState(repo=REPO, issue=1, work_machine="precision")
    b = IssueState(repo=REPO, issue=1, work_machine="dellserver")
    assert a.fingerprint == b.fingerprint


def test_flat_dict_uses_the_legacy_upper_case_key_names():
    state = IssueState(
        repo=REPO, issue=1392, active_types=("review", "smoke"), auto_loop=False
    )
    flat = state.as_flat_dict()
    assert flat["WORK_AID"] == ""
    assert flat["ACTIVE_TYPES"] == "review,smoke"
    assert flat["AUTO_LOOP"] == "0"
    assert flat["WORK_EXIT_CODE"] == ""
    # It must survive a json.dumps — --dry-run prints it.
    json.dumps(flat)


# ── the ETag cache (PR #1391: one file, not two) ─────────────────────────────


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict | None, etag: str | None):
        self.status_code = status_code
        self._payload = payload
        self.headers = {"etag": etag} if etag else {}

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise AssertionError(f"HTTP {self.status_code}")


@pytest.fixture
def board_service(monkeypatch):
    from coord.client import ServiceConfig

    svc = ServiceConfig(url="http://dellserver:7435", token=None)
    monkeypatch.setattr("coord.client.resolve_board_service", lambda *a, **k: svc)
    return svc


def test_fetch_writes_etag_and_payload_as_one_file(tmp_path, board_service, monkeypatch):
    payload = {"assignments": [row()]}
    monkeypatch.setattr(
        "httpx.get", lambda *a, **k: _FakeResponse(200, payload, '"abc"')
    )
    fetcher = BoardFetcher(cache_dir=tmp_path)
    assert fetcher.fetch() == payload

    files = list(tmp_path.glob("board-*.json"))
    assert len(files) == 1, "the etag and body must be inseparable (PR #1391)"
    cached = json.loads(files[0].read_text())
    assert cached == {"etag": '"abc"', "payload": payload}
    assert not list(tmp_path.glob("*.tmp")), "the temp file must be renamed away"


def test_fetch_sends_if_none_match_and_serves_the_cached_body_on_304(
    tmp_path, board_service, monkeypatch
):
    payload = {"assignments": [row()]}
    calls: list[dict] = []

    def fake_get(url, headers=None, timeout=None):
        calls.append(dict(headers or {}))
        if len(calls) == 1:
            return _FakeResponse(200, payload, '"abc"')
        assert headers["if-none-match"] == '"abc"'
        return _FakeResponse(304, None, '"abc"')

    monkeypatch.setattr("httpx.get", fake_get)
    fetcher = BoardFetcher(cache_dir=tmp_path)
    assert fetcher.fetch() == payload
    assert fetcher.fetch() == payload  # served from cache via the 304
    assert "if-none-match" not in calls[0]


def test_fetch_ignores_a_torn_or_legacy_cache_file(tmp_path, board_service, monkeypatch):
    payload = {"assignments": []}
    cache = BoardFetcher(cache_dir=tmp_path)._cache_path(board_service.url)
    cache.write_text('{"etag": "\\"stale\\""}')  # a body-less legacy/torn write

    seen: list[dict] = []

    def fake_get(url, headers=None, timeout=None):
        seen.append(dict(headers or {}))
        return _FakeResponse(200, payload, '"fresh"')

    monkeypatch.setattr("httpx.get", fake_get)
    assert BoardFetcher(cache_dir=tmp_path).fetch() == payload
    assert "if-none-match" not in seen[0], (
        "a cache with no body must not be trusted to produce a confident 304"
    )


def test_fetch_uses_the_local_db_when_standalone(monkeypatch, tmp_path):
    monkeypatch.setattr("coord.client.resolve_board_service", lambda *a, **k: None)
    monkeypatch.setattr("coord.board_service.read_board", lambda: "BOARD")
    monkeypatch.setattr("coord.client.serialize_board", lambda b: {"from": b})
    assert BoardFetcher(cache_dir=tmp_path).fetch() == {"from": "BOARD"}
