"""#1929 (epic #1440): the milestone gate state machine.

Two layers, matching the module's own split:

1. :mod:`coord.milestone_gate`'s pure core — every edge of the A -> work ->
   B -> C -> D walk driven directly through :func:`evaluate_gate` with a
   hand-built :class:`GateProbes`, no board and no GitHub.
2. :func:`coord.serve_app._milestone_gate_tick` driven directly (the tick is
   module-level precisely so tests don't need the async ``_tick_loop``),
   covering the four cases the issue names: **cold start**, **resume
   mid-gate after a simulated daemon restart**, **terminal state
   deregisters**, and **``--dry-run`` dispatches nothing**.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from coord import milestone_gate as mg
from coord.config import load as load_config


# ── fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def rw_db(tmp_path: Path):
    """File-backed coord.db override (mirrors tests/test_serve.py's fixture)."""
    from coord import db
    from coord.db import _ensure_schema

    conn = sqlite3.connect(str(tmp_path / "rw.db"), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    db.override_connection(conn)
    yield conn


def _make_config(tmp_path: Path, *, auto_dispatch: bool = True) -> Path:
    content = (
        "repos:\n"
        "  - name: api\n"
        "    github: acme/api\n"
        "\n"
        "machines:\n"
        "  - name: laptop\n"
        "    host: laptop.tailnet\n"
        "    repos: [api]\n"
        "    repo_paths:\n"
        "      api: /tmp/api\n"
        "\n"
        "milestone:\n"
        f"  auto_dispatch: {'true' if auto_dispatch else 'false'}\n"
    )
    p = tmp_path / "coordinator-gate.yml"
    p.write_text(content)
    return p


def _stub_github(monkeypatch, *, node_state: str = "OPEN", tracking_state: str = "OPEN"):
    """One-node work order (#762) under milestone 9 on tracking issue #100."""

    def get_issue(repo, number):
        if number == 100:
            return {
                "number": 100, "title": "tracking",
                "body": "## Work order\n- [ ] #762\n",
                "state": tracking_state, "milestone": {"number": 9},
            }
        return {
            "number": 762, "title": "the work", "body": "", "state": node_state,
            "milestone": {"number": 9}, "labels": [],
        }

    monkeypatch.setattr("coord.github_ops.get_issue", get_issue)
    monkeypatch.setattr(
        "coord.github_ops.get_open_issues",
        lambda repo: ([{"number": 762, "milestone": {"number": 9}}]
                      if node_state == "OPEN" else []),
    )


def _stub_dispatch(monkeypatch, dispatched: list) -> None:
    def _dispatch(proposal, config, **kw):
        dispatched.append(proposal)
        return {"id": f"gate-{len(dispatched)}"}

    monkeypatch.setattr("coord.dispatch.dispatch", _dispatch)
    monkeypatch.setattr("coord.github_ops.post_issue_comment", lambda *a, **kw: None)
    monkeypatch.setattr("coord.github_ops.check_branch_exists", lambda *a, **kw: False)


# ── the pure machine ─────────────────────────────────────────────────────────


def test_gate_sequence_is_the_declared_walk() -> None:
    assert mg.GATE_SEQUENCE == (
        mg.GATE_A, mg.WORK, mg.GATE_B, mg.GATE_C, mg.GATE_D, mg.DONE
    )
    assert mg.next_gate(mg.GATE_A) == mg.WORK
    assert mg.next_gate(mg.DONE) is None


def test_gate_a_holds_when_contract_missing() -> None:
    step = mg.evaluate_gate(
        mg.GATE_A, mg.GateProbes(gate_a_blocked="Gate A not satisfied: no contract")
    )
    assert step.action == mg.HOLD
    assert "no contract" in step.reason
    assert step.to_gate is None


def test_gate_a_advances_to_work_when_contract_present() -> None:
    step = mg.evaluate_gate(mg.GATE_A, mg.GateProbes(gate_a_blocked=None))
    assert step.action == mg.ADVANCE
    assert step.to_gate == mg.WORK


def test_work_dispatches_until_complete_then_advances() -> None:
    draining = mg.evaluate_gate(
        mg.WORK, mg.GateProbes(work_complete=False, work_remaining=3)
    )
    assert draining.action == mg.DISPATCH
    assert "3 work-order node" in draining.reason

    done = mg.evaluate_gate(mg.WORK, mg.GateProbes(work_complete=True))
    assert done.action == mg.ADVANCE
    assert done.to_gate == mg.GATE_B


@pytest.mark.parametrize(
    "verdict,action,to_gate",
    [
        ("approve", mg.ADVANCE, mg.GATE_C),
        ("request-changes", mg.HOLD, None),
        (None, mg.HOLD, None),
    ],
)
def test_gate_b_edges(verdict, action, to_gate) -> None:
    step = mg.evaluate_gate(mg.GATE_B, mg.GateProbes(gate_b_verdict=verdict))
    assert step.action == action
    assert step.to_gate == to_gate
    assert step.reason  # never a silent hold


def test_gate_c_holds_on_missing_and_red_but_advances_on_green() -> None:
    # None = no durable Gate C record exists today; a hold, not a guess.
    missing = mg.evaluate_gate(mg.GATE_C, mg.GateProbes(gate_c_green=None))
    assert missing.action == mg.HOLD
    assert "no durable Gate C result" in missing.reason

    red = mg.evaluate_gate(mg.GATE_C, mg.GateProbes(gate_c_green=False))
    assert red.action == mg.HOLD

    green = mg.evaluate_gate(mg.GATE_C, mg.GateProbes(gate_c_green=True))
    assert green.action == mg.ADVANCE
    assert green.to_gate == mg.GATE_D


def test_gate_d_only_observes_ship() -> None:
    held = mg.evaluate_gate(mg.GATE_D, mg.GateProbes(shipped=False))
    assert held.action == mg.HOLD
    assert "coord milestone ship" in held.reason

    shipped = mg.evaluate_gate(mg.GATE_D, mg.GateProbes(shipped=True))
    assert shipped.action == mg.ADVANCE
    assert shipped.to_gate == mg.DONE


def test_done_is_terminal() -> None:
    step = mg.evaluate_gate(mg.DONE, mg.GateProbes())
    assert step.action == mg.TERMINAL
    assert mg.DONE in mg.TERMINAL_GATES


def test_no_gate_ever_falls_through_silently() -> None:
    """The load-bearing invariant: every gate produces an explicit,
    non-empty reason for every combination of probes we can construct."""
    probe_sets = [
        mg.GateProbes(),
        mg.GateProbes(gate_a_blocked="x", work_complete=True, gate_b_verdict="approve",
                      gate_c_green=True, shipped=True),
        mg.GateProbes(gate_b_verdict="comment", gate_c_green=False),
        mg.GateProbes(work_order_empty=True),
    ]
    for gate in mg.GATE_SEQUENCE:
        for probes in probe_sets:
            step = mg.evaluate_gate(gate, probes)
            assert step.reason, f"{gate} produced an empty reason"
            assert step.action in (mg.ADVANCE, mg.HOLD, mg.DISPATCH, mg.TERMINAL)


def test_apply_step_records_cleared_gates_and_stamps_entry() -> None:
    rec = mg.GateRecord(repo_name="api", tracking_issue=100, gate=mg.GATE_A)
    advanced = mg.apply_step(
        rec, mg.evaluate_gate(mg.GATE_A, mg.GateProbes()), now=1000.0
    )
    assert advanced.gate == mg.WORK
    assert advanced.cleared == (mg.GATE_A,)
    assert advanced.entered_at == 1000.0
    assert advanced.waiting_on == ""

    # A hold refreshes waiting_on but leaves entered_at alone, so "how long
    # has this been stuck" stays answerable.
    held = mg.apply_step(
        advanced,
        mg.evaluate_gate(mg.GATE_B, mg.GateProbes(gate_b_verdict=None)),
        now=2000.0,
    )
    assert held.gate == mg.WORK  # a hold never moves the record
    assert held.entered_at == 1000.0
    assert held.updated_at == 2000.0
    assert "no Gate B verdict yet" in held.waiting_on


def test_record_round_trips_and_rejects_unknown_gate() -> None:
    rec = mg.GateRecord(
        repo_name="api", tracking_issue=100, gate=mg.GATE_C,
        entered_at=5.0, updated_at=6.0, waiting_on="waiting",
        milestone_number=9, cleared=(mg.GATE_A, mg.WORK, mg.GATE_B),
    )
    assert mg.GateRecord.from_dict(rec.to_dict()) == rec

    # An unknown gate must NOT be coerced back to Gate A — that would re-run
    # gates the milestone already cleared.
    assert mg.GateRecord.from_dict({**rec.to_dict(), "gate": "gate_z"}) is None
    assert mg.GateRecord.from_dict({"repo_name": "api"}) is None
    assert mg.GateRecord.from_dict({**rec.to_dict(), "schema": 99}) is None


def test_plan_sequence_lists_every_remaining_gate() -> None:
    rec = mg.GateRecord(repo_name="api", tracking_issue=100, gate=mg.WORK)
    steps = mg.plan_sequence(rec, mg.GateProbes(work_complete=False, work_remaining=2))
    assert [s.gate for s in steps] == [
        mg.WORK, mg.GATE_B, mg.GATE_C, mg.GATE_D, mg.DONE
    ]
    # `work` is the live decision; everything downstream is honestly labelled.
    assert steps[0].projected is False
    assert all(s.projected for s in steps[1:])


def test_plan_sequence_from_a_resumed_gate_omits_cleared_gates() -> None:
    rec = mg.GateRecord(
        repo_name="api", tracking_issue=100, gate=mg.GATE_C,
        cleared=(mg.GATE_A, mg.WORK, mg.GATE_B),
    )
    steps = mg.plan_sequence(rec, mg.GateProbes())
    assert [s.gate for s in steps] == [mg.GATE_C, mg.GATE_D, mg.DONE]


# ── persistence ──────────────────────────────────────────────────────────────


def test_gate_record_persists_and_deletes(rw_db) -> None:
    from coord import state

    assert state.list_milestone_gates() == []
    rec = mg.GateRecord(repo_name="api", tracking_issue=100, gate=mg.WORK)
    state.save_milestone_gate(rec.to_dict())
    assert state.get_milestone_gate(repo_name="api", tracking_issue=100)["gate"] == mg.WORK

    # Upsert by (repo, issue) — not append.
    state.save_milestone_gate({**rec.to_dict(), "gate": mg.GATE_B})
    assert len(state.list_milestone_gates()) == 1
    assert state.get_milestone_gate(
        repo_name="api", tracking_issue=100
    )["gate"] == mg.GATE_B

    state.delete_milestone_gate(repo_name="api", tracking_issue=100)
    assert state.list_milestone_gates() == []


def test_save_milestone_gate_routes_to_daemon_when_service_set(monkeypatch) -> None:
    """A thin client's `coord milestone drive` reaches the daemon rather than
    writing a gate record into a local DB nobody reads — same posture as
    register_milestone_drain."""
    from coord import client as cc
    from coord import state

    monkeypatch.setattr(
        cc, "resolve_board_service", lambda *a, **k: cc.ServiceConfig("http://d:7435")
    )
    captured: dict = {}
    monkeypatch.setattr(
        cc, "post_record",
        lambda svc, path, payload, **kw:
            captured.update(path=path, payload=payload) or {"ok": True},
    )

    rec = mg.GateRecord(repo_name="api", tracking_issue=100, gate=mg.WORK)
    state.save_milestone_gate(rec.to_dict())

    assert captured["path"] == "/milestone-gate"
    assert captured["payload"] == {"record": rec.to_dict()}
    assert state.list_milestone_gates() == []  # routed → no local write


def test_daemon_milestone_gate_endpoint_writes_the_record(tmp_path: Path, rw_db) -> None:
    from starlette.testclient import TestClient

    from coord import state
    from coord.dao import SqliteStore
    from coord.serve_app import build_app

    db_path = tmp_path / "board.db"
    conn = sqlite3.connect(str(db_path))
    from coord.db import _ensure_schema

    _ensure_schema(conn)
    conn.commit()
    conn.close()

    rec = mg.GateRecord(repo_name="api", tracking_issue=100, gate=mg.WORK)
    app = build_app(SqliteStore(db_path), load_config(_make_config(tmp_path)))
    with TestClient(app) as cli:
        ok = cli.post("/milestone-gate", json={"record": rec.to_dict()})
        bad = cli.post("/milestone-gate", json={"record": "not-an-object"})

    assert ok.status_code == 200
    assert bad.status_code == 400
    assert mg.GateRecord.from_dict(
        state.get_milestone_gate(repo_name="api", tracking_issue=100)
    ) == rec


# ── the daemon tick ──────────────────────────────────────────────────────────


def test_gate_tick_noop_when_nothing_is_driven(tmp_path: Path, rw_db) -> None:
    from coord.serve_app import _milestone_gate_tick

    assert _milestone_gate_tick(load_config(_make_config(tmp_path))) == []


def test_gate_tick_cold_start_advances_gate_a_to_work(
    tmp_path: Path, rw_db, monkeypatch
) -> None:
    """Cold start: a fresh record sits at Gate A. With a contract present
    (repo has no acceptance driver -> Gate A is skipped) the first tick
    advances it to `work` and records Gate A as cleared."""
    from coord import state
    from coord.serve_app import _milestone_gate_tick

    _stub_github(monkeypatch)
    state.save_milestone_gate(
        mg.GateRecord(repo_name="api", tracking_issue=100).to_dict()
    )

    results = _milestone_gate_tick(load_config(_make_config(tmp_path)), now=100.0)

    assert len(results) == 1
    assert results[0].from_gate == mg.GATE_A
    assert results[0].to_gate == mg.WORK
    assert results[0].action == mg.ADVANCE
    assert results[0].dispatched == ()  # Gate A never dispatches

    saved = mg.GateRecord.from_dict(
        state.get_milestone_gate(repo_name="api", tracking_issue=100)
    )
    assert saved.gate == mg.WORK
    assert saved.cleared == (mg.GATE_A,)
    assert saved.entered_at == 100.0
    assert saved.milestone_number == 9


def test_gate_tick_work_state_drains_the_frontier(
    tmp_path: Path, rw_db, monkeypatch
) -> None:
    """The `work` gate IS the drain — it dispatches through the same
    primitives the manual CLI uses and stays put until every node is
    terminal."""
    from coord import state
    from coord.serve_app import _milestone_gate_tick

    _stub_github(monkeypatch)
    dispatched: list = []
    _stub_dispatch(monkeypatch, dispatched)

    state.save_milestone_gate(
        mg.GateRecord(
            repo_name="api", tracking_issue=100, gate=mg.WORK, cleared=(mg.GATE_A,)
        ).to_dict()
    )

    results = _milestone_gate_tick(load_config(_make_config(tmp_path)), now=200.0)

    assert results[0].action == mg.DISPATCH
    assert results[0].to_gate == mg.WORK  # a dispatch never moves the record
    assert len(results[0].dispatched) == 1
    assert results[0].dispatched[0].ok is True
    assert [r["issue_number"] for r in state.load_dispatched()] == [762]


def test_gate_tick_resumes_mid_gate_after_a_restart(
    tmp_path: Path, rw_db, monkeypatch
) -> None:
    """The issue's resumability bar: a daemon restarting mid-milestone
    resumes from board state and never re-runs a completed gate.

    Simulated by dropping every in-process handle and re-entering the tick
    against the same DB — the record is the only thing carried across.
    """
    from coord import state
    from coord.serve_app import _milestone_gate_tick

    _stub_github(monkeypatch, node_state="CLOSED")
    dispatched: list = []
    _stub_dispatch(monkeypatch, dispatched)
    cfg = load_config(_make_config(tmp_path))

    # Pre-restart: the milestone had already cleared Gate A and drained its
    # work; it is parked at Gate B waiting on a verdict.
    state.save_milestone_gate(
        mg.GateRecord(
            repo_name="api", tracking_issue=100, gate=mg.GATE_B,
            entered_at=50.0, cleared=(mg.GATE_A, mg.WORK), milestone_number=9,
        ).to_dict()
    )

    # ── "restart" — a brand-new tick call, nothing but the DB carried over.
    results = _milestone_gate_tick(cfg, now=300.0)

    assert len(results) == 1
    assert results[0].from_gate == mg.GATE_B, "resumed at the wrong gate"
    assert results[0].action == mg.HOLD
    assert "no Gate B verdict yet" in results[0].reason
    # Nothing re-dispatched: `work` was already cleared and must not re-run.
    assert dispatched == []
    assert state.load_dispatched() == []

    saved = mg.GateRecord.from_dict(
        state.get_milestone_gate(repo_name="api", tracking_issue=100)
    )
    assert saved.gate == mg.GATE_B
    assert saved.cleared == (mg.GATE_A, mg.WORK)
    assert saved.entered_at == 50.0  # a hold preserves the original entry stamp
    assert "no Gate B verdict yet" in saved.waiting_on

    # A second restart is likewise a no-op re-hold, not a rewind.
    again = _milestone_gate_tick(cfg, now=400.0)
    assert again[0].from_gate == mg.GATE_B
    assert mg.GateRecord.from_dict(
        state.get_milestone_gate(repo_name="api", tracking_issue=100)
    ).cleared == (mg.GATE_A, mg.WORK)


def test_gate_tick_terminal_state_deregisters(
    tmp_path: Path, rw_db, monkeypatch
) -> None:
    """Reaching Gate D with the epic closed walks to `done` and drops the
    milestone from gate control — nothing left to re-check."""
    from coord import state
    from coord.serve_app import _milestone_gate_tick

    _stub_github(monkeypatch, node_state="CLOSED", tracking_state="CLOSED")
    state.save_milestone_gate(
        mg.GateRecord(
            repo_name="api", tracking_issue=100, gate=mg.GATE_D,
            cleared=(mg.GATE_A, mg.WORK, mg.GATE_B, mg.GATE_C), milestone_number=9,
        ).to_dict()
    )

    results = _milestone_gate_tick(load_config(_make_config(tmp_path)), now=500.0)

    assert results[0].to_gate == mg.DONE
    assert results[0].deregistered is True
    assert state.list_milestone_gates() == []


def test_gate_tick_fetch_error_holds_without_deregistering(
    tmp_path: Path, rw_db, monkeypatch
) -> None:
    from coord import state
    from coord.serve_app import _milestone_gate_tick

    monkeypatch.setattr(
        "coord.github_ops.get_issue",
        lambda repo, number: (_ for _ in ()).throw(RuntimeError("rate limited")),
    )
    state.save_milestone_gate(
        mg.GateRecord(repo_name="api", tracking_issue=100, gate=mg.WORK).to_dict()
    )

    assert _milestone_gate_tick(load_config(_make_config(tmp_path))) == []
    assert len(state.list_milestone_gates()) == 1  # retried next tick, not dropped


def test_gate_tick_drops_unknown_repo_and_malformed_records(
    tmp_path: Path, rw_db
) -> None:
    from coord import state
    from coord.serve_app import _milestone_gate_tick

    state._save_milestone_gate_local(
        {"repo_name": "ghost", "tracking_issue": 7, "gate": mg.WORK}
    )
    state._save_milestone_gate_local(
        {"repo_name": "api", "tracking_issue": 8, "gate": "gate_z"}
    )

    assert _milestone_gate_tick(load_config(_make_config(tmp_path))) == []
    assert state.list_milestone_gates() == []


def test_drain_tick_skips_gate_driven_milestones(
    tmp_path: Path, rw_db, monkeypatch
) -> None:
    """#1929's auto_dispatch answer: a milestone under gate control is NOT
    also drained by the independently-gated legacy drain path."""
    from coord import state
    from coord.serve_app import _milestone_drain_tick

    _stub_github(monkeypatch)
    dispatched: list = []
    _stub_dispatch(monkeypatch, dispatched)

    state.register_milestone_drain(repo_name="api", tracking_issue=100)
    state.save_milestone_gate(
        mg.GateRecord(repo_name="api", tracking_issue=100, gate=mg.GATE_A).to_dict()
    )

    outcomes = _milestone_drain_tick(load_config(_make_config(tmp_path)))

    assert outcomes == []
    assert dispatched == []
    # Still registered — the gate tick owns it, the drain just declines to act.
    assert state.list_milestone_drains() == [
        {"repo_name": "api", "tracking_issue": 100}
    ]


# ── the CLI dry-run ──────────────────────────────────────────────────────────


def test_drive_dry_run_dispatches_nothing_and_writes_no_record(
    tmp_path: Path, rw_db, monkeypatch
) -> None:
    """#1440's fourth acceptance bullet: --dry-run prints the full planned
    sequence — every gate + the ready frontier — and does nothing else."""
    from click.testing import CliRunner

    from coord import state
    from coord.commands.milestone import milestone_drive_cmd

    _stub_github(monkeypatch)
    dispatched: list = []
    _stub_dispatch(monkeypatch, dispatched)

    result = CliRunner().invoke(
        milestone_drive_cmd,
        ["api", "100", "--dry-run", "--config", str(_make_config(tmp_path))],
    )

    assert result.exit_code == 0, result.output
    # Every gate in the walk is named.
    for label in (
        "Gate A", "work", "Gate B", "Gate C", "Gate D", "done",
    ):
        assert label in result.output, f"{label} missing from dry-run output"
    assert "would dispatch #762" in result.output
    assert "dry run" in result.output

    # Dispatched nothing, wrote nothing.
    assert dispatched == []
    assert state.load_dispatched() == []
    assert state.list_milestone_gates() == []


def test_drive_registers_and_is_resumable(tmp_path: Path, rw_db, monkeypatch) -> None:
    """A non-dry-run `drive` writes the cold-start record; running it again
    resumes from the persisted gate rather than rewinding to Gate A."""
    import dataclasses

    from click.testing import CliRunner

    from coord import state
    from coord.commands.milestone import milestone_drive_cmd

    _stub_github(monkeypatch)
    _stub_dispatch(monkeypatch, [])
    cfg_path = str(_make_config(tmp_path))

    r1 = CliRunner().invoke(milestone_drive_cmd, ["api", "100", "--config", cfg_path])
    assert r1.exit_code == 0, r1.output
    saved = mg.GateRecord.from_dict(
        state.get_milestone_gate(repo_name="api", tracking_issue=100)
    )
    assert saved.gate == mg.GATE_A

    # Pretend the daemon advanced it, then re-drive.
    state.save_milestone_gate(
        dataclasses.replace(
            saved, gate=mg.GATE_C, cleared=(mg.GATE_A, mg.WORK, mg.GATE_B)
        ).to_dict()
    )
    r2 = CliRunner().invoke(milestone_drive_cmd, ["api", "100", "--config", cfg_path])
    assert r2.exit_code == 0, r2.output
    assert mg.GateRecord.from_dict(
        state.get_milestone_gate(repo_name="api", tracking_issue=100)
    ).gate == mg.GATE_C, "drive rewound a resumed milestone back to Gate A"


def test_drive_warns_loudly_when_existing_record_is_malformed(
    tmp_path: Path, rw_db, monkeypatch
) -> None:
    """#1929 review: a record that fails `GateRecord.from_dict` (e.g. an
    unknown `gate` written by a different-schema client via the permissive
    `/milestone-gate` endpoint) must not be silently coerced back to Gate A
    with no indication anything was wrong — that discards `cleared` history
    with no trace. `drive` still has to make forward progress (there's no
    CLI verb to repair/delete the record), but it must say so loudly, the
    same way `_milestone_gate_tick` logs a warning for the identical case."""
    from click.testing import CliRunner

    from coord import state
    from coord.commands.milestone import milestone_drive_cmd

    _stub_github(monkeypatch)
    _stub_dispatch(monkeypatch, [])
    cfg_path = str(_make_config(tmp_path))

    # Not a valid GateRecord: `gate` is outside GATE_SEQUENCE.
    state.save_milestone_gate(
        {"repo_name": "api", "tracking_issue": 100, "gate": "not-a-real-gate"}
    )

    result = CliRunner().invoke(milestone_drive_cmd, ["api", "100", "--config", cfg_path])
    assert result.exit_code == 0, result.output
    assert "warning: existing gate record" in result.output
    assert "could not be parsed" in result.output

    saved = mg.GateRecord.from_dict(
        state.get_milestone_gate(repo_name="api", tracking_issue=100)
    )
    assert saved.gate == mg.GATE_A
