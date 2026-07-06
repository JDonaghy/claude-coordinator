"""Tests for smoke-test orchestration (coord/smoke.py)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from coord.config import Config, SmokeRule, SmokeTestsConfig, load
from coord.models import Assignment, Board, Machine, Repo
from coord.smoke import (
    SMOKE_SYSTEM_PROMPT,
    build_smoke_briefing,
    dispatch_smoke,
    match_rules,
    pick_smoke_machine,
)


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def repo() -> Repo:
    return Repo(
        name="api", github="acme/api", depends_on=[], default_branch="main",
        test_command="make test",
    )


def _machine(name: str, host: str, *, caps: list[str], path: str = "/work/api") -> Machine:
    return Machine(
        name=name, host=host, capabilities=caps, repos=["api"],
        repo_paths={"api": path},
    )


@pytest.fixture
def gtk_and_server_config(repo: Repo) -> Config:
    return Config(
        repos=[repo],
        machines=[
            _machine("server", "server.tail", caps=["python"], path="/srv/api"),
            _machine("desktop-a", "desktop-a.tail", caps=["python", "gtk"], path="/d/api"),
        ],
        smoke_tests=SmokeTestsConfig(
            auto_queue=True,
            capability_rules=[
                SmokeRule(files=["src/gtk/"], requires=["gtk"]),
                SmokeRule(files=["src/tui_main/"], requires=["terminal"]),
            ],
        ),
    )


def _completed(
    *, machine: str = "server", branch: str = "issue-1-fix", repo: str = "api",
) -> Assignment:
    return Assignment(
        machine_name=machine,
        repo_name=repo,
        issue_number=287,
        issue_title="GTK key routing fix",
        briefing="Worker briefing",
        assignment_id="abc123",
        status="done",
        branch=branch,
        dispatched_at=0.0,
        finished_at=1.0,
        type="work",
    )


# ── Rule matching ───────────────────────────────────────────────────────────


def test_match_rules_returns_required_capabilities() -> None:
    rules = [
        SmokeRule(files=["src/gtk/"], requires=["gtk"]),
        SmokeRule(files=["src/tui_main/"], requires=["terminal"]),
    ]
    caps = match_rules(["src/gtk/window.c", "src/lib/util.c"], rules)
    assert caps == ["gtk"]


def test_match_rules_unions_caps_across_rules() -> None:
    rules = [
        SmokeRule(files=["src/gtk/"], requires=["gtk"]),
        SmokeRule(files=["src/tui_main/"], requires=["terminal"]),
    ]
    caps = match_rules(["src/gtk/a.c", "src/tui_main/b.c"], rules)
    assert set(caps) == {"gtk", "terminal"}


def test_match_rules_returns_empty_for_no_match() -> None:
    rules = [SmokeRule(files=["src/gtk/"], requires=["gtk"])]
    assert match_rules(["docs/README.md", "src/core/util.c"], rules) == []


def test_match_rules_deduplicates_capabilities() -> None:
    rules = [
        SmokeRule(files=["src/gtk/"], requires=["gtk"]),
        SmokeRule(files=["app/gtk_main.c"], requires=["gtk"]),
    ]
    caps = match_rules(["src/gtk/x.c", "app/gtk_main.c"], rules)
    assert caps == ["gtk"]


def test_match_rules_no_trailing_slash_matches_files_too() -> None:
    """A rule `src/gtk` (no slash) is the loose form — catches gtk_helpers.c."""
    rules = [SmokeRule(files=["src/gtk"], requires=["gtk"])]
    assert match_rules(["src/gtk_helpers.c"], rules) == ["gtk"]


# ── Machine selection ───────────────────────────────────────────────────────


def test_pick_smoke_prefers_capable_machine_different_from_worker(
    gtk_and_server_config: Config,
) -> None:
    board = Board()
    choice = pick_smoke_machine(
        ["gtk"], "api", "server", board, gtk_and_server_config
    )
    assert choice is not None
    assert choice.machine.name == "desktop-a"
    assert choice.is_worker is False


def test_pick_smoke_returns_none_when_no_machine_has_capability(
    gtk_and_server_config: Config,
) -> None:
    cfg = replace(
        gtk_and_server_config,
        machines=[_machine("server", "server.tail", caps=["python"])],
    )
    choice = pick_smoke_machine(["gtk"], "api", "server", Board(), cfg)
    assert choice is None


def test_pick_smoke_falls_back_to_worker_machine_when_only_capable(
    repo: Repo,
) -> None:
    cfg = Config(
        repos=[repo],
        machines=[_machine("desktop-a", "d.tail", caps=["python", "gtk"])],
        smoke_tests=SmokeTestsConfig(auto_queue=True),
    )
    choice = pick_smoke_machine(["gtk"], "api", "desktop-a", Board(), cfg)
    assert choice is not None
    assert choice.machine.name == "desktop-a"
    assert choice.is_worker is True
    assert "same machine" in choice.rationale


def test_pick_smoke_picks_busy_different_over_idle_worker(repo: Repo) -> None:
    """Capability + different-machine wins over idle but worker."""
    cfg = Config(
        repos=[repo],
        machines=[
            _machine("server", "server.tail", caps=["python", "gtk"]),
            _machine("desktop-a", "d.tail", caps=["python", "gtk"]),
        ],
        smoke_tests=SmokeTestsConfig(auto_queue=True),
    )
    board = Board(active=[
        Assignment(
            machine_name="desktop-a", repo_name="api", issue_number=99,
            issue_title="other", status="running", assignment_id="x",
        )
    ])
    choice = pick_smoke_machine(["gtk"], "api", "server", board, cfg)
    assert choice is not None
    assert choice.machine.name == "desktop-a"
    assert "busy" in choice.rationale


# ── Config parsing ──────────────────────────────────────────────────────────


def test_smoke_config_defaults(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n  - name: api\n    github: acme/api\n"
        "machines:\n  - name: laptop\n    host: laptop.tail\n    repos: [api]\n"
    )
    cfg = load(p)
    assert cfg.smoke_tests.auto_queue is False
    assert cfg.smoke_tests.capability_rules == []


def test_smoke_config_parses_capability_rules(tmp_path: Path) -> None:
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
smoke_tests:
  auto_queue: true
  default_command: "make smoke"
  timeout_seconds: 300
  capability_rules:
    - files: ["src/gtk/"]
      requires: [gtk]
    - files: ["src/tui_main/"]
      requires: [terminal]
"""
    )
    cfg = load(p)
    assert cfg.smoke_tests.auto_queue is True
    assert cfg.smoke_tests.default_command == "make smoke"
    assert cfg.smoke_tests.timeout_seconds == 300
    assert len(cfg.smoke_tests.capability_rules) == 2
    assert cfg.smoke_tests.capability_rules[0].requires == ["gtk"]


def test_smoke_config_rejects_empty_files_or_requires(tmp_path: Path) -> None:
    from coord.config import ConfigError

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
smoke_tests:
  auto_queue: true
  capability_rules:
    - files: []
      requires: [gtk]
"""
    )
    with pytest.raises(ConfigError, match="files must be non-empty"):
        load(p)


# ── Briefing assembly ───────────────────────────────────────────────────────


def test_briefing_includes_branch_command_and_required_caps() -> None:
    briefing = build_smoke_briefing(
        repo_github="acme/api",
        repo_name="api",
        branch="issue-287-fix",
        issue_number=287,
        issue_title="GTK fix",
        smoke_command="make smoke",
        required_caps=["gtk"],
        timeout_seconds=600,
        is_worker=False,
    )
    assert "issue-287-fix" in briefing
    assert "make smoke" in briefing
    assert "gtk" in briefing
    assert "SMOKE: pass" in briefing
    assert "running on the same machine" not in briefing


def test_briefing_warns_when_run_on_worker_machine() -> None:
    briefing = build_smoke_briefing(
        repo_github="acme/api", repo_name="api", branch="b",
        issue_number=1, issue_title="X", smoke_command="cmd",
        required_caps=["gtk"], timeout_seconds=60, is_worker=True,
    )
    assert "running on the same machine" in briefing


# ── dispatch_smoke (HTTP mocked) ────────────────────────────────────────────


class _FakeResp:
    def __init__(self, payload: dict) -> None:
        self._p = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._p


class _FakeClient:
    def __init__(self, payload: dict) -> None:
        self._p = payload
        self.calls: list[tuple[str, dict]] = []

    def post(self, url, *, json, timeout) -> _FakeResp:
        self.calls.append((url, json))
        return _FakeResp(self._p)


def test_dispatch_smoke_skipped_when_auto_queue_off(
    gtk_and_server_config: Config,
) -> None:
    cfg = replace(
        gtk_and_server_config,
        smoke_tests=replace(gtk_and_server_config.smoke_tests, auto_queue=False),
    )
    result = dispatch_smoke(
        _completed(), Board(), cfg,
        http_client=_FakeClient({"id": "x"}),
        diff_lookup=lambda repo, branch: ["src/gtk/window.c"],
    )
    assert result is None


def test_dispatch_smoke_skipped_when_no_rule_matches(
    gtk_and_server_config: Config,
) -> None:
    result = dispatch_smoke(
        _completed(), Board(), gtk_and_server_config,
        http_client=_FakeClient({"id": "x"}),
        diff_lookup=lambda repo, branch: ["docs/README.md"],
    )
    assert result is None


def test_dispatch_smoke_skipped_for_failed_or_review(
    gtk_and_server_config: Config,
) -> None:
    failed = replace(_completed(), status="failed")
    review = replace(_completed(), type="review")
    diff = lambda repo, branch: ["src/gtk/x.c"]
    assert dispatch_smoke(
        failed, Board(), gtk_and_server_config,
        http_client=_FakeClient({"id": "x"}), diff_lookup=diff,
    ) is None
    assert dispatch_smoke(
        review, Board(), gtk_and_server_config,
        http_client=_FakeClient({"id": "x"}), diff_lookup=diff,
    ) is None


def test_dispatch_smoke_dispatches_for_mock_author_type(
    gtk_and_server_config: Config,
) -> None:
    """#930 fix: a completed ``type="mock-author"`` (Gate A) assignment must
    be eligible for automatic smoke dispatch, not just ``type="work"`` —
    mirrors the same fix applied to review/merge dispatch so the Test stage
    of Work -> Test -> Review -> Merge also fires for Gate A branches."""
    mock_author = replace(_completed(), type="mock-author", assignment_id="ma1")
    result = dispatch_smoke(
        mock_author, Board(), gtk_and_server_config,
        http_client=_FakeClient({"id": "smoke-ma"}),
        diff_lookup=lambda repo, branch: ["src/gtk/window.c"],
    )
    assert result is not None
    assert result.type == "smoke"
    assert result.review_of_assignment_id == "ma1"


def test_dispatch_smoke_sends_to_capable_different_machine(
    gtk_and_server_config: Config,
) -> None:
    board = Board()
    client = _FakeClient({"id": "smoke-1"})
    result = dispatch_smoke(
        _completed(machine="server"), board, gtk_and_server_config,
        http_client=client,
        diff_lookup=lambda repo, branch: ["src/gtk/window.c"],
        now=42.0,
    )
    assert result is not None
    assert result.type == "smoke"
    assert result.machine_name == "desktop-a"  # has gtk; server doesn't
    assert result.assignment_id == "smoke-1"
    assert result.review_of_assignment_id == "abc123"
    assert result.dispatched_at == 42.0
    assert board.active == [result]

    assert len(client.calls) == 1
    url, payload = client.calls[0]
    assert "desktop-a.tail" in url
    assert payload["type"] == "smoke"
    assert payload["system_prompt"] == SMOKE_SYSTEM_PROMPT
    assert payload["review_target"] == "issue-1-fix"
    assert payload["repo_path"] == "/d/api"
    # Briefing should mention the test_command fallback (make test).
    assert "make test" in payload["briefing"]


def test_dispatch_smoke_uses_default_command_when_set(
    gtk_and_server_config: Config,
) -> None:
    cfg = replace(
        gtk_and_server_config,
        smoke_tests=replace(
            gtk_and_server_config.smoke_tests,
            default_command="make smoke",
        ),
    )
    client = _FakeClient({"id": "s2"})
    result = dispatch_smoke(
        _completed(), Board(), cfg, http_client=client,
        diff_lookup=lambda repo, branch: ["src/gtk/x.c"],
    )
    assert result is not None
    assert "make smoke" in client.calls[0][1]["briefing"]


def test_dispatch_smoke_returns_none_on_http_failure(
    gtk_and_server_config: Config,
) -> None:
    import httpx

    class _Bad:
        def post(self, url, *, json, timeout):
            raise httpx.ConnectError("unreachable")

    board = Board()
    result = dispatch_smoke(
        _completed(), board, gtk_and_server_config,
        http_client=_Bad(),
        diff_lookup=lambda repo, branch: ["src/gtk/x.c"],
    )
    assert result is None
    assert board.active == []


def test_dispatch_smoke_returns_none_when_no_capable_machine(
    repo: Repo,
) -> None:
    cfg = Config(
        repos=[repo],
        machines=[_machine("server", "server.tail", caps=["python"])],
        smoke_tests=SmokeTestsConfig(
            auto_queue=True,
            capability_rules=[SmokeRule(files=["src/gtk/"], requires=["gtk"])],
        ),
    )
    result = dispatch_smoke(
        _completed(), Board(), cfg,
        http_client=_FakeClient({"id": "x"}),
        diff_lookup=lambda repo, branch: ["src/gtk/x.c"],
    )
    assert result is None


# ── #685: get_issue_test_mode ───────────────────────────────────────────────


def test_get_issue_test_mode_returns_none_when_no_row(coord_db) -> None:
    """Returns None when the issue isn't in the local cache."""
    from coord.state import get_issue_test_mode

    assert get_issue_test_mode("api", 42) is None


def test_get_issue_test_mode_returns_none_when_no_label(coord_db) -> None:
    """Returns None when the issue row has no test-mode label."""
    import json
    from coord.state import get_issue_test_mode

    coord_db.execute(
        "INSERT INTO issues (repo_name, number, title, body, state, labels, synced_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("api", 42, "Fix bug", "", "open", json.dumps(["coord", "status:ready"]), 1.0),
    )
    coord_db.commit()
    assert get_issue_test_mode("api", 42) is None


def test_get_issue_test_mode_returns_smoke(coord_db) -> None:
    """Returns 'smoke' when the test-mode:smoke label is present."""
    import json
    from coord.state import get_issue_test_mode

    coord_db.execute(
        "INSERT INTO issues (repo_name, number, title, body, state, labels, synced_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("api", 42, "Fix bug", "", "open", json.dumps(["coord", "test-mode:smoke"]), 1.0),
    )
    coord_db.commit()
    assert get_issue_test_mode("api", 42) == "smoke"


def test_get_issue_test_mode_returns_auto(coord_db) -> None:
    """Returns 'auto' when the test-mode:auto label is present."""
    import json
    from coord.state import get_issue_test_mode

    coord_db.execute(
        "INSERT INTO issues (repo_name, number, title, body, state, labels, synced_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("api", 42, "Fix bug", "", "open", json.dumps(["coord", "test-mode:auto"]), 1.0),
    )
    coord_db.commit()
    assert get_issue_test_mode("api", 42) == "auto"


# ── #685: reconcile smoke-gate respects test-mode:smoke ───────────────────


def test_reconcile_skips_auto_smoke_for_smoke_mode_issue(
    gtk_and_server_config: Config, coord_db
) -> None:
    """reconcile() must NOT auto-dispatch smoke when the issue has test-mode:smoke."""
    import json
    from unittest.mock import patch as _patch
    from coord.reconcile import reconcile
    from coord.state import save_board

    # Seed the issue with test-mode:smoke.
    coord_db.execute(
        "INSERT INTO issues (repo_name, number, title, body, state, labels, synced_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("api", 287, "GTK fix", "", "open", json.dumps(["coord", "test-mode:smoke"]), 1.0),
    )
    coord_db.commit()

    completed_work = _completed(machine="server")
    board = Board(active=[completed_work])
    save_board(board)

    def _fake_agent(_host: str, _port: int = 7433, **kw):
        return {
            "completed": [
                {"id": "abc123", "status": "done", "branch": "issue-287-fix"}
            ]
        }

    with _patch("coord.reconcile._query_agent", side_effect=_fake_agent):
        reconcile(board, gtk_and_server_config)

    # A smoke assignment must NOT have been appended to the board.
    smoke_assignments = [a for a in board.active if a.type == "smoke"]
    assert smoke_assignments == [], (
        "Expected no auto-smoke dispatch for test-mode:smoke issue; "
        f"got {smoke_assignments}"
    )


def test_reconcile_dispatches_auto_smoke_for_auto_mode_issue(
    gtk_and_server_config: Config, coord_db
) -> None:
    """reconcile() MUST call dispatch_smoke when the issue has test-mode:auto."""
    import json
    from unittest.mock import patch as _patch
    from coord.reconcile import reconcile
    from coord.state import save_board

    coord_db.execute(
        "INSERT INTO issues (repo_name, number, title, body, state, labels, synced_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("api", 287, "GTK fix", "", "open", json.dumps(["coord", "test-mode:auto"]), 1.0),
    )
    coord_db.commit()

    completed_work = _completed(machine="server")
    board = Board(active=[completed_work])
    save_board(board)

    def _fake_agent(_host: str, _port: int = 7433, **kw):
        return {
            "completed": [
                {"id": "abc123", "status": "done", "branch": "issue-287-fix"}
            ]
        }

    with _patch("coord.reconcile._query_agent", side_effect=_fake_agent), \
         _patch("coord.smoke.dispatch_smoke", return_value=None) as mock_dispatch:
        reconcile(board, gtk_and_server_config)
        assert mock_dispatch.called, (
            "Expected dispatch_smoke to be called for test-mode:auto issue"
        )


def test_reconcile_thin_client_respects_smoke_mode_via_daemon(
    gtk_and_server_config: Config, coord_db, monkeypatch
) -> None:
    """#906 regression: reconcile() runs from the thin-client-reachable `coord
    resume` (not just the daemon tick loop, as an earlier #906 allowlist
    comment incorrectly assumed). On a thin client the local `issues` table
    is an empty stub, so ``get_issue_test_mode`` must route to the daemon's
    ``/issue-test-mode`` endpoint rather than reading the local (empty) table
    and silently falling through to auto-dispatching a headless smoke test
    for an issue explicitly labeled ``test-mode:smoke``.
    """
    import coord.client as cc
    from unittest.mock import patch as _patch
    from coord.reconcile import reconcile
    from coord.state import save_board

    # Local `issues` table is EMPTY — mirrors the thin-client reality that
    # triggered the #906 bug (a local read would return None here even though
    # the issue really has test-mode:smoke on the daemon/GitHub).
    assert coord_db.execute("SELECT COUNT(*) FROM issues").fetchone()[0] == 0

    # Seed the board locally first (as-if state.py's autouse fixture wrote it
    # before board_service was configured) so this setup step itself doesn't
    # trip the thin-client local-board guard.
    completed_work = _completed(machine="server")
    board = Board(active=[completed_work])
    save_board(board)

    class _FakeSvc:
        url = "http://daemon:7435"
        token = "t"

    monkeypatch.setattr(cc, "resolve_board_service", lambda *a, **k: _FakeSvc())

    daemon_calls: list[tuple[str, dict]] = []

    def _fake_post_record(svc, path, payload, **kw):
        daemon_calls.append((path, payload))
        return {"test_mode": "smoke"}

    monkeypatch.setattr(cc, "post_record", _fake_post_record)

    def _fake_agent(_host: str, _port: int = 7433, **kw):
        return {
            "completed": [
                {"id": "abc123", "status": "done", "branch": "issue-287-fix"}
            ]
        }

    with _patch("coord.reconcile._query_agent", side_effect=_fake_agent):
        reconcile(board, gtk_and_server_config)

    # The daemon endpoint was consulted for this exact issue.
    assert ("/issue-test-mode", {"repo_name": "api", "issue_number": 287}) in daemon_calls

    # No smoke assignment must have been auto-dispatched.
    smoke_assignments = [a for a in board.active if a.type == "smoke"]
    assert smoke_assignments == [], (
        "Expected no auto-smoke dispatch for test-mode:smoke issue even though "
        "the local `issues` table is empty (daemon has the real label) — "
        f"got {smoke_assignments}"
    )

    # The local issues table was never populated — proves no local fallback read.
    assert coord_db.execute("SELECT COUNT(*) FROM issues").fetchone()[0] == 0


def test_reconcile_thin_client_falls_back_to_local_on_daemon_error(
    gtk_and_server_config: Config, coord_db, monkeypatch
) -> None:
    """If the daemon read for test-mode fails, get_issue_test_mode fails open to
    the (empty) local DB — matching pre-#906 "no label" behaviour rather than
    raising and breaking the whole reconcile pass."""
    import coord.client as cc
    from unittest.mock import patch as _patch
    from coord.reconcile import reconcile
    from coord.state import save_board

    completed_work = _completed(machine="server")
    board = Board(active=[completed_work])
    save_board(board)

    class _FakeSvc:
        url = "http://daemon:7435"
        token = "t"

    monkeypatch.setattr(cc, "resolve_board_service", lambda *a, **k: _FakeSvc())
    monkeypatch.setattr(
        cc,
        "post_record",
        lambda svc, path, payload, **kw: (_ for _ in ()).throw(RuntimeError("daemon down")),
    )

    def _fake_agent(_host: str, _port: int = 7433, **kw):
        return {
            "completed": [
                {"id": "abc123", "status": "done", "branch": "issue-287-fix"}
            ]
        }

    with _patch("coord.reconcile._query_agent", side_effect=_fake_agent), \
         _patch("coord.smoke.dispatch_smoke", return_value=None) as mock_dispatch:
        reconcile(board, gtk_and_server_config)

    # Falls open to "no label" behaviour → respects auto_queue=True → dispatches.
    assert mock_dispatch.called
