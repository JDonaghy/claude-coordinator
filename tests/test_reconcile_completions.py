"""#625: dispatch-free passive completion reconcile.

A finished headless worker (e.g. a `claude -p` plan) must flip the board to
its terminal status even with the auto-loop off — WITHOUT dispatching or
posting to GitHub. The board (and the TUI box colour) stops lying when nothing
else is polling the agents.
"""

from __future__ import annotations

import time

from coord.config import Config
from coord.models import Assignment, Board, Machine, Repo
from coord.reconcile import reconcile_completed_assignments


def _config() -> Config:
    return Config(
        repos=[Repo(name="cc", github="acme/cc")],
        machines=[Machine(name="dellserver", host="dellserver", repos=["cc"])],
    )


def _running(aid: str = "w1", *, atype: str = "plan", branch: str = "issue-1-x") -> Assignment:
    return Assignment(
        machine_name="dellserver", repo_name="cc",
        issue_number=411, issue_title="t",
        status="running", assignment_id=aid, type=atype, branch=branch,
    )


def _board(*assignments: Assignment) -> Board:
    return Board(
        repos=[Repo(name="cc", github="acme/cc")], machines=[], active=list(assignments)
    )


class _Recorder:
    """Stand-in for issue_store._update_local_state — records writes so the
    test can assert the board is the ONLY thing mutated (no dispatch / GitHub)."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, *, assignment_id, terminal_status, branch, review_state) -> None:
        self.calls.append(
            {
                "assignment_id": assignment_id,
                "terminal_status": terminal_status,
                "branch": branch,
                "review_state": review_state,
            }
        )


def test_flips_running_to_done_when_agent_reports_completed() -> None:
    rec = _Recorder()
    out = reconcile_completed_assignments(
        _config(),
        board=_board(_running("w1")),
        agent_status_fn=lambda host: {"completed": [{"id": "w1", "status": "done"}]},
        update_state_fn=rec,
        capture_plan=False,
    )
    assert len(out) == 1
    assert out[0]["to_status"] == "done"
    assert out[0]["issue_number"] == 411
    assert rec.calls == [
        {"assignment_id": "w1", "terminal_status": "done",
         "branch": "issue-1-x", "review_state": None}
    ]


def test_no_write_when_agent_still_running() -> None:
    # The assignment isn't in the agent's completed list → leave it alone.
    rec = _Recorder()
    out = reconcile_completed_assignments(
        _config(), board=_board(_running("w1")),
        agent_status_fn=lambda host: {"active": [{"id": "w1"}], "completed": []},
        update_state_fn=rec, capture_plan=False,
    )
    assert out == []
    assert rec.calls == []


def test_noop_when_agent_unreachable() -> None:
    rec = _Recorder()
    out = reconcile_completed_assignments(
        _config(), board=_board(_running("w1")),
        agent_status_fn=lambda host: None,  # unreachable → retry next tick
        update_state_fn=rec, capture_plan=False,
    )
    assert out == []
    assert rec.calls == []


def test_maps_failed_and_advisory() -> None:
    rec = _Recorder()
    reconcile_completed_assignments(
        _config(),
        board=_board(_running("w1", atype="work"), _running("w2", atype="work")),
        agent_status_fn=lambda host: {"completed": [
            {"id": "w1", "status": "failed"},
            {"id": "w2", "status": "advisory"},
        ]},
        update_state_fn=rec, capture_plan=False,
    )
    by = {c["assignment_id"]: c["terminal_status"] for c in rec.calls}
    assert by == {"w1": "failed", "w2": "advisory"}


def test_cancelled_maps_to_failed() -> None:
    rec = _Recorder()
    reconcile_completed_assignments(
        _config(), board=_board(_running("w1", atype="work")),
        agent_status_fn=lambda host: {"completed": [{"id": "w1", "status": "cancelled"}]},
        update_state_fn=rec, capture_plan=False,
    )
    assert rec.calls[0]["terminal_status"] == "failed"


def test_only_acts_on_running_rows_idempotent() -> None:
    # A row already terminal (done) lives in board.completed, not active → never
    # re-reconciled even though the agent still holds its completed entry. This
    # is the idempotency guarantee — a later tick can't re-fire on it.
    rec = _Recorder()
    done = _running("w1")
    done.status = "done"
    board = Board(
        repos=[Repo(name="cc", github="acme/cc")], machines=[],
        active=[], completed=[done],
    )
    out = reconcile_completed_assignments(
        _config(), board=board,
        agent_status_fn=lambda host: {"completed": [{"id": "w1", "status": "done"}]},
        update_state_fn=rec, capture_plan=False,
    )
    assert out == []
    assert rec.calls == []


def test_polls_each_agent_at_most_once() -> None:
    calls: list[str] = []

    def status_fn(host: str) -> dict:
        calls.append(host)
        return {"completed": [
            {"id": "w1", "status": "done"}, {"id": "w2", "status": "done"}
        ]}

    reconcile_completed_assignments(
        _config(),
        board=_board(_running("w1", atype="work"), _running("w2", atype="work")),
        agent_status_fn=status_fn, update_state_fn=_Recorder(), capture_plan=False,
    )
    assert calls == ["dellserver"]  # one poll for the shared host, not two


def test_unknown_machine_skipped() -> None:
    # A running assignment on a machine absent from config → no host → skip, no crash.
    rec = _Recorder()
    a = Assignment(
        machine_name="ghost", repo_name="cc", issue_number=9, issue_title="t",
        status="running", assignment_id="w9", type="work",
    )
    out = reconcile_completed_assignments(
        _config(), board=_board(a),
        agent_status_fn=lambda host: {"completed": [{"id": "w9", "status": "done"}]},
        update_state_fn=rec, capture_plan=False,
    )
    assert out == []
    assert rec.calls == []


def test_plan_capture_invoked_for_plan_type(monkeypatch) -> None:
    captured: dict[str, dict] = {}

    class _Plan:
        def is_empty(self) -> bool:
            return False

        def to_dict(self) -> dict:
            return {"plan": "do the thing"}

    monkeypatch.setattr("coord.plan_parser.parse_plan_from_agent", lambda host, aid: _Plan())
    monkeypatch.setattr("coord.state.save_plan", lambda aid, d: captured.update({aid: d}))
    out = reconcile_completed_assignments(
        _config(), board=_board(_running("w1", atype="plan")),
        agent_status_fn=lambda host: {"completed": [{"id": "w1", "status": "done"}]},
        update_state_fn=_Recorder(), capture_plan=True,
    )
    assert out[0]["plan_captured"] is True
    assert captured == {"w1": {"plan": "do the thing"}}


def test_token_counts_captured_from_entry(monkeypatch) -> None:
    """#667: when the /status completed entry carries token counts the
    reconcile path persists them via update_assignment_tokens."""
    captured_tokens: list[dict] = []

    def fake_update_tokens(assignment_id, *, input_tokens, output_tokens,
                           cache_creation_tokens, cache_read_tokens) -> None:
        captured_tokens.append({
            "assignment_id": assignment_id,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_creation_tokens": cache_creation_tokens,
            "cache_read_tokens": cache_read_tokens,
        })

    monkeypatch.setattr("coord.state.update_assignment_tokens", fake_update_tokens)

    entry = {
        "id": "w1",
        "status": "done",
        "input_tokens": 1500,
        "output_tokens": 300,
        "cache_creation_tokens": 50,
        "cache_read_tokens": 200,
    }
    reconcile_completed_assignments(
        _config(), board=_board(_running("w1", atype="work")),
        agent_status_fn=lambda host: {"completed": [entry]},
        update_state_fn=_Recorder(), capture_plan=False,
    )

    assert len(captured_tokens) == 1
    t = captured_tokens[0]
    assert t["assignment_id"] == "w1"
    assert t["input_tokens"] == 1500
    assert t["output_tokens"] == 300
    assert t["cache_creation_tokens"] == 50
    assert t["cache_read_tokens"] == 200


def test_token_capture_zero_skipped(monkeypatch) -> None:
    """#667: when the entry has no token fields the update is skipped (not
    called with zeros)."""
    update_calls: list[str] = []
    monkeypatch.setattr(
        "coord.state.update_assignment_tokens",
        lambda *a, **kw: update_calls.append(a[0]),
    )
    entry = {"id": "w1", "status": "done"}  # no token keys
    reconcile_completed_assignments(
        _config(), board=_board(_running("w1", atype="work")),
        agent_status_fn=lambda host: {"completed": [entry]},
        update_state_fn=_Recorder(), capture_plan=False,
    )
    assert update_calls == []  # nothing to write → update not called


def test_token_capture_failure_does_not_break_status_write(monkeypatch) -> None:
    """#667: if token persistence raises, the terminal-status write already
    landed so the board still gets updated."""
    def boom(*a, **kw) -> None:  # noqa: ANN002, ANN003
        raise RuntimeError("db gone")

    monkeypatch.setattr("coord.state.update_assignment_tokens", boom)

    rec = _Recorder()
    entry = {
        "id": "w1",
        "status": "done",
        "input_tokens": 100,
        "output_tokens": 20,
    }
    out = reconcile_completed_assignments(
        _config(), board=_board(_running("w1", atype="work")),
        agent_status_fn=lambda host: {"completed": [entry]},
        update_state_fn=rec, capture_plan=False,
    )
    # Terminal status still written
    assert rec.calls[0]["terminal_status"] == "done"
    assert len(out) == 1


def test_plan_capture_failure_does_not_break_status_write(monkeypatch) -> None:
    # If plan parsing blows up, the terminal-status write must still land — the
    # stuck box is fixed regardless of whether the plan body could be recovered.
    rec = _Recorder()

    def boom(host, aid):  # noqa: ANN001, ANN202
        raise RuntimeError("agent log gone")

    monkeypatch.setattr("coord.plan_parser.parse_plan_from_agent", boom)
    out = reconcile_completed_assignments(
        _config(), board=_board(_running("w1", atype="plan")),
        agent_status_fn=lambda host: {"completed": [{"id": "w1", "status": "done"}]},
        update_state_fn=rec, capture_plan=True,
    )
    assert rec.calls[0]["terminal_status"] == "done"
    assert out[0]["plan_captured"] is False


# ---------------------------------------------------------------------------
# #666 Gap A: cost capture from agent completed entry
# ---------------------------------------------------------------------------

def test_captures_cost_from_total_cost_usd(monkeypatch) -> None:
    """A completed entry carrying total_cost_usd persists cost via update_assignment_cost."""
    recorded_costs: list[tuple[str, float]] = []

    monkeypatch.setattr(
        "coord.state.update_assignment_cost",
        lambda aid, cost: recorded_costs.append((aid, cost)),
    )
    # Stub tokens writer so we don't need a live DB.
    monkeypatch.setattr("coord.state.update_assignment_tokens", lambda *a, **kw: None)

    reconcile_completed_assignments(
        _config(), board=_board(_running("w1", atype="work")),
        agent_status_fn=lambda host: {
            "completed": [{"id": "w1", "status": "done", "total_cost_usd": 0.42}]
        },
        update_state_fn=_Recorder(), capture_plan=False,
    )
    assert recorded_costs == [("w1", 0.42)]


def test_captures_cost_fallback_to_cost_so_far(monkeypatch) -> None:
    """When total_cost_usd is absent, cost_so_far is used as a fallback."""
    recorded_costs: list[tuple[str, float]] = []

    monkeypatch.setattr(
        "coord.state.update_assignment_cost",
        lambda aid, cost: recorded_costs.append((aid, cost)),
    )
    monkeypatch.setattr("coord.state.update_assignment_tokens", lambda *a, **kw: None)

    reconcile_completed_assignments(
        _config(), board=_board(_running("w1", atype="work")),
        agent_status_fn=lambda host: {
            "completed": [{"id": "w1", "status": "done", "cost_so_far": 0.17}]
        },
        update_state_fn=_Recorder(), capture_plan=False,
    )
    assert recorded_costs == [("w1", 0.17)]


def test_no_cost_write_when_entry_has_no_cost(monkeypatch) -> None:
    """An entry with no cost fields → no update_assignment_cost call (no zero written)."""
    recorded_costs: list[tuple[str, float]] = []

    monkeypatch.setattr(
        "coord.state.update_assignment_cost",
        lambda aid, cost: recorded_costs.append((aid, cost)),
    )
    monkeypatch.setattr("coord.state.update_assignment_tokens", lambda *a, **kw: None)

    reconcile_completed_assignments(
        _config(), board=_board(_running("w1", atype="work")),
        agent_status_fn=lambda host: {
            "completed": [{"id": "w1", "status": "done"}]
        },
        update_state_fn=_Recorder(), capture_plan=False,
    )
    assert recorded_costs == []  # no cost data → no write


def test_cost_capture_failure_does_not_break_status_write(monkeypatch) -> None:
    """An exception in cost capture must not prevent the terminal-status write."""
    rec = _Recorder()

    monkeypatch.setattr(
        "coord.state.update_assignment_cost",
        lambda aid, cost: (_ for _ in ()).throw(RuntimeError("db gone")),
    )
    monkeypatch.setattr("coord.state.update_assignment_tokens", lambda *a, **kw: None)

    out = reconcile_completed_assignments(
        _config(), board=_board(_running("w1", atype="work")),
        agent_status_fn=lambda host: {
            "completed": [{"id": "w1", "status": "done", "total_cost_usd": 0.5}]
        },
        update_state_fn=rec, capture_plan=False,
    )
    # Status write still landed despite the cost-capture blowup.
    assert rec.calls[0]["terminal_status"] == "done"
    assert out[0]["to_status"] == "done"


def test_daemon_lifespan_runs_the_passive_reconcile_tick(monkeypatch, tmp_path) -> None:
    # Wiring: `coord serve`'s lifespan must actually run the tick on its interval.
    from starlette.testclient import TestClient

    import coord.reconcile as rec_mod
    from coord.dao import SqliteStore
    from coord.serve_app import build_app

    calls: list[int] = []
    monkeypatch.setattr(
        rec_mod, "reconcile_completed_assignments",
        lambda config, **k: calls.append(1) or [],
    )
    monkeypatch.setenv("COORD_RECONCILE_INTERVAL", "0.05")

    store = SqliteStore(str(tmp_path / "x.db"))
    app = build_app(store, _config())
    with TestClient(app):  # entering the context runs the lifespan → starts the tick
        for _ in range(50):
            if calls:
                break
            time.sleep(0.02)
    assert calls, "the daemon lifespan must run the dispatch-free reconcile tick"


def test_daemon_tick_disabled_when_interval_zero(monkeypatch, tmp_path) -> None:
    from starlette.testclient import TestClient

    import coord.reconcile as rec_mod
    from coord.dao import SqliteStore
    from coord.serve_app import build_app

    calls: list[int] = []
    monkeypatch.setattr(
        rec_mod, "reconcile_completed_assignments",
        lambda config, **k: calls.append(1) or [],
    )
    monkeypatch.setenv("COORD_RECONCILE_INTERVAL", "0")

    store = SqliteStore(str(tmp_path / "x.db"))
    app = build_app(store, _config())
    with TestClient(app):
        time.sleep(0.2)
    assert calls == []  # interval 0 → no background tick at all
