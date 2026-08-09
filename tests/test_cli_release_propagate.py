"""Black-box tests for `coord release propagate` / `history` (#1835, PKG-7).

These drive the *running command* through Click and assert on what it
printed and what it wrote to the journal — the CLAUDE.md bar for a
behaviour-changing PR. The pure judgement is covered in
`tests/test_release_propagate.py`; what is tested here is the wiring that
only exists in the shell, and the two behaviours a timer depends on:

* **A busy fleet is exit 0, with a record.** This command runs unattended
  every 20 minutes and defers most of the time. If a deferral exited
  non-zero, systemd would mark the unit failed and an operator would learn
  to ignore it — and the one night it genuinely broke would look identical.

* **Every attempt is journalled.** #1835: "a silent success is
  indistinguishable from a silent no-op, which is precisely how 2026-08-04
  stayed invisible."

Nothing here touches a real fleet: the board fetch, the PyPI lookup and the
per-host HTTP calls are all seams.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from coord import release_propagate as rp
from coord.cli import main
from coord.commands import release as release_cmd
from coord.drive_queue import HOLD_FIRED, STATE_RUNNING


@pytest.fixture()
def state_dir(tmp_path, monkeypatch):
    """Point the propagation journal at a tmp dir, never the real ~/.coord."""
    d = tmp_path / "state"
    d.mkdir()
    monkeypatch.setattr(release_cmd, "_state_dir", lambda: d)
    return d


@pytest.fixture()
def no_network(monkeypatch):
    """No PyPI lookup, no /board read, no agent POST unless a test says so."""
    monkeypatch.setattr(release_cmd, "_fetch_board", lambda: ({}, None))
    monkeypatch.setattr(
        release_cmd, "_post",
        lambda *a, **k: pytest.fail("no test should POST without saying so"),
    )


def _records(state_dir):
    return rp.read_records(state_dir)


# ── deferral: the common case, and the one a timer depends on ────────────


def test_a_busy_fleet_defers_at_exit_zero(valid_config_path, state_dir, no_network,
                                          monkeypatch):
    monkeypatch.setattr(
        release_cmd, "_fetch_board",
        lambda: ({"drive_queue": [{"repo_name": "api", "issue_number": 7,
                                   "state": STATE_RUNNING}],
                  "assignments": []}, None),
    )
    result = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.4.111"],
    )
    assert result.exit_code == 0, result.output
    assert "deferred" in result.output
    # The reason names the entry — a deferral nobody can explain is
    # indistinguishable from a wedged timer.
    assert "api#7" in result.output


def test_a_deferral_is_journalled(valid_config_path, state_dir, no_network, monkeypatch):
    monkeypatch.setattr(
        release_cmd, "_fetch_board",
        lambda: ({"assignments": [{"machine_name": "laptop", "issue_number": 9,
                                   "status": "RUNNING"}]}, None),
    )
    CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "v0.4.111"],
    )
    records = _records(state_dir)
    assert len(records) == 1
    assert records[0]["status"] == rp.STATUS_DEFERRED
    assert records[0]["target_version"] == "0.4.111"  # leading v normalised
    assert not records[0]["quiescence"]["quiescent"]


def test_an_unreadable_board_defers_rather_than_crashing(valid_config_path, state_dir,
                                                         no_network, monkeypatch):
    """The safe move when we cannot prove the fleet is idle is to do nothing
    and say so — never to assume idle and start restarting agents."""
    monkeypatch.setattr(
        release_cmd, "_fetch_board", lambda: ({}, "ConnectError: refused")
    )
    result = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.4.111"],
    )
    assert result.exit_code == 0, result.output
    assert "board unreadable" in result.output


def test_no_resolvable_target_fails_loudly(valid_config_path, state_dir, no_network,
                                           monkeypatch):
    monkeypatch.setattr(
        release_cmd, "_resolve_expected", lambda *a, **k: (None, "PyPI unreachable")
    )
    result = CliRunner().invoke(
        main, ["release", "propagate", "--config", str(valid_config_path)]
    )
    assert result.exit_code == 1
    assert _records(state_dir)[0]["status"] == rp.STATUS_FAILED


# ── dry run: the plan, without touching a host ───────────────────────────


def test_a_dry_run_prints_the_plan_and_writes_nothing(valid_config_path, state_dir,
                                                      no_network, monkeypatch):
    _stub_verify(monkeypatch, versions={"laptop": ["0.4.110"], "server": ["0.4.110"]})
    result = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.4.111", "--dry-run"],
    )
    assert result.exit_code == 0, result.output
    assert "would roll" in result.output
    assert "[dry-run]" in result.output
    # A dry run must not append to the journal — otherwise a rehearsal is
    # indistinguishable from the real thing in the history.
    assert _records(state_dir) == []


def test_a_dry_run_puts_the_daemon_host_first(valid_config_path, state_dir, no_network,
                                              monkeypatch):
    """The 405 invariant, visible end to end: `server` is the daemon host, so
    its python lane must be the first thing the plan names."""
    _stub_verify(monkeypatch, versions={"laptop": ["0.4.110"], "server": ["0.4.110"]})
    result = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.4.111", "--dry-run", "--daemon-host", "server",
         "--lane", "python"],
    )
    lines = [l for l in result.output.splitlines() if "would roll" in l]
    assert lines
    assert "server" in lines[0]


def test_hosts_already_on_the_target_are_reported_not_rolled(valid_config_path,
                                                             state_dir, no_network,
                                                             monkeypatch):
    _stub_verify(monkeypatch, versions={"laptop": ["0.4.111"], "server": ["0.4.110"]})
    result = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.4.111", "--dry-run", "--lane", "python"],
    )
    assert "already on v0.4.111" in result.output
    assert "laptop" not in "\n".join(
        l for l in result.output.splitlines() if "would roll" in l
    )


def test_a_fleet_already_on_the_target_is_up_to_date(valid_config_path, state_dir,
                                                     no_network, monkeypatch):
    _stub_verify(monkeypatch, versions={"laptop": ["0.4.111"], "server": ["0.4.111"]})
    result = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.4.111"],
    )
    assert result.exit_code == 0, result.output
    assert rp.STATUS_UP_TO_DATE in result.output


# ── history ──────────────────────────────────────────────────────────────


def test_history_of_an_empty_journal_names_the_timer(state_dir):
    result = CliRunner().invoke(main, ["release", "history"])
    assert result.exit_code == 0
    assert "no propagation attempts recorded" in result.output


def test_history_renders_what_propagate_wrote(valid_config_path, state_dir, no_network,
                                              monkeypatch):
    monkeypatch.setattr(
        release_cmd, "_fetch_board",
        lambda: ({"drive_queue": [{"repo_name": "api", "issue_number": 7,
                                   "state": STATE_RUNNING}]}, None),
    )
    CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.4.111"],
    )
    result = CliRunner().invoke(main, ["release", "history"])
    assert result.exit_code == 0
    assert "api#7" in result.output


def test_history_json_is_machine_readable(valid_config_path, state_dir, no_network,
                                          monkeypatch):
    monkeypatch.setattr(release_cmd, "_fetch_board",
                        lambda: ({}, "ConnectError: refused"))
    CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.4.111"],
    )
    result = CliRunner().invoke(main, ["release", "history", "--json"])
    payload = json.loads(result.output)
    assert payload[0]["status"] == rp.STATUS_DEFERRED


def test_propagate_json_output_is_the_record(valid_config_path, state_dir, no_network,
                                             monkeypatch):
    monkeypatch.setattr(release_cmd, "_fetch_board",
                        lambda: ({}, "ConnectError: refused"))
    result = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.4.111", "--json"],
    )
    payload = json.loads(result.output)
    assert payload["target_version"] == "0.4.111"
    assert payload["status"] == rp.STATUS_DEFERRED


# ── a fired deploy gate is a window, not a blocker ───────────────────────


def test_a_fired_deploy_gate_does_not_defer(valid_config_path, state_dir, no_network,
                                            monkeypatch):
    """#1757's gate stops the queue waiting for a deploy; propagation IS that
    deploy. If this deferred, the fleet would deadlock."""
    monkeypatch.setattr(
        release_cmd, "_fetch_board",
        lambda: ({"drive_queue": [{"repo_name": "api", "issue_number": 1543,
                                   "state": "done", "hold_state": HOLD_FIRED}]}, None),
    )
    _stub_verify(monkeypatch, versions={"laptop": ["0.4.111"], "server": ["0.4.111"]})
    result = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.4.111"],
    )
    assert result.exit_code == 0, result.output
    assert rp.STATUS_DEFERRED not in result.output
    assert "waiting on exactly this deploy" in result.output


# ── the roll, the final gate, and the rollback on red ────────────────────


def _stub_lanes(monkeypatch, *, python_ok=True, calls=None):
    """Replace the three per-lane executors with recorders."""
    log = calls if calls is not None else []

    def _python(machine, **kwargs):
        log.append(("python", machine.name))
        return python_ok, "now v0.4.111" if python_ok else "pip failed"

    def _units(machine, **kwargs):
        log.append(("units", machine.name))
        return True, "1 unit(s) refreshed; daemon-reload ok"

    def _tui(machine, **kwargs):
        log.append(("tui", machine.name))
        return True, "coord-tui now v0.4.111"

    monkeypatch.setattr(release_cmd, "_roll_python", _python)
    monkeypatch.setattr(release_cmd, "_roll_units", _units)
    monkeypatch.setattr(release_cmd, "_roll_tui", _tui)
    return log


def test_a_green_roll_is_verified_and_journalled(valid_config_path, state_dir,
                                                 no_network, monkeypatch):
    calls = _stub_lanes(monkeypatch)
    _stub_verify(monkeypatch, versions={"laptop": ["0.4.110"], "server": ["0.4.110"]})
    result = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.4.111", "--daemon-host", "server"],
    )
    assert result.exit_code == 0, result.output
    record = _records(state_dir)[0]
    assert record["status"] == rp.STATUS_VERIFIED
    # The daemon leads — the 405 invariant, end to end.
    assert calls[0] == ("python", "server")
    # Every lane and host is in the record: #1835's observability gate is
    # "when each lane rolled", not "something happened".
    assert {(l["lane"], l["host"]) for l in record["lanes"]} >= {
        ("python", "server"), ("python", "laptop"),
        ("units", "server"), ("tui", "laptop"),
    }
    assert record["verification"]["severity"] == "ok"


def test_a_red_verification_rolls_every_updated_host_back(valid_config_path, state_dir,
                                                          no_network, monkeypatch):
    """#1835: 'a red post-deploy verification must roll back, not just
    report.' Exit 2 so the timer's failure is distinguishable from a
    deferral (0) and from a plain failure (1)."""
    _stub_lanes(monkeypatch)
    _stub_verify(monkeypatch, versions={"laptop": ["0.4.110"], "server": ["0.4.110"]},
                 severity="crit")
    rolled_back: list[str] = []
    monkeypatch.setattr(
        release_cmd, "_rollback_host",
        lambda machine, **k: (rolled_back.append(machine.name), (True, "rolling back"))[1],
    )
    result = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.4.111", "--daemon-host", "server"],
    )
    assert result.exit_code == 2, result.output
    assert sorted(rolled_back) == ["laptop", "server"]
    assert _records(state_dir)[0]["status"] == rp.STATUS_ROLLED_BACK


def test_a_host_whose_python_lane_failed_is_not_rolled_back(valid_config_path,
                                                            state_dir, no_network,
                                                            monkeypatch):
    """Rolling back a host this run never successfully updated would undo
    somebody else's deliberate state."""
    _stub_lanes(monkeypatch, python_ok=False)
    _stub_verify(monkeypatch, versions={"laptop": ["0.4.110"], "server": ["0.4.110"]},
                 severity="crit")
    rolled_back: list[str] = []
    monkeypatch.setattr(
        release_cmd, "_rollback_host",
        lambda machine, **k: (rolled_back.append(machine.name), (True, "x"))[1],
    )
    CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.4.111", "--daemon-host", "server"],
    )
    assert rolled_back == []


def test_no_rollback_on_red_reports_instead(valid_config_path, state_dir, no_network,
                                            monkeypatch):
    _stub_lanes(monkeypatch)
    _stub_verify(monkeypatch, versions={"laptop": ["0.4.110"], "server": ["0.4.110"]},
                 severity="crit")
    monkeypatch.setattr(
        release_cmd, "_rollback_host",
        lambda *a, **k: pytest.fail("--no-rollback-on-red must not roll back"),
    )
    result = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.4.111", "--no-rollback-on-red"],
    )
    assert result.exit_code == 1
    assert _records(state_dir)[0]["status"] == rp.STATUS_FAILED


def test_a_verified_roll_releases_the_deploy_gate_that_was_waiting(valid_config_path,
                                                                   state_dir,
                                                                   no_network,
                                                                   monkeypatch):
    """The loop closes: the gate stops the queue for the deploy, propagation
    performs the deploy, propagation restarts the queue."""
    _stub_lanes(monkeypatch)
    _stub_verify(monkeypatch, versions={"laptop": ["0.4.110"], "server": ["0.4.110"]})
    monkeypatch.setattr(
        release_cmd, "_fetch_board",
        lambda: ({"drive_queue": [{"repo_name": "api", "issue_number": 1543,
                                   "state": "done", "hold_state": HOLD_FIRED}]}, None),
    )
    released: list[str] = []
    monkeypatch.setattr(
        release_cmd, "_release_hold",
        lambda key: (released.append(key), (True, "queue released"))[1],
    )
    result = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.4.111"],
    )
    assert result.exit_code == 0, result.output
    assert released == ["api#1543"]
    assert _records(state_dir)[0]["released_holds"] == ["api#1543"]


def test_a_rolled_back_run_leaves_the_deploy_gate_held(valid_config_path, state_dir,
                                                       no_network, monkeypatch):
    """Releasing the gate on a rolled-back roll would restart the overnight
    queue into the exact 'merged is not live' trap the gate exists for."""
    _stub_lanes(monkeypatch)
    _stub_verify(monkeypatch, versions={"laptop": ["0.4.110"], "server": ["0.4.110"]},
                 severity="crit")
    monkeypatch.setattr(release_cmd, "_rollback_host", lambda *a, **k: (True, "x"))
    monkeypatch.setattr(
        release_cmd, "_release_hold",
        lambda key: pytest.fail("a rolled-back run must never release the gate"),
    )
    monkeypatch.setattr(
        release_cmd, "_fetch_board",
        lambda: ({"drive_queue": [{"repo_name": "api", "issue_number": 1543,
                                   "state": "done", "hold_state": HOLD_FIRED}]}, None),
    )
    result = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.4.111"],
    )
    assert result.exit_code == 2


def test_no_verify_stops_before_the_gate(valid_config_path, state_dir, no_network,
                                         monkeypatch):
    _stub_lanes(monkeypatch)
    _stub_verify(monkeypatch, versions={"laptop": ["0.4.110"], "server": ["0.4.110"]})
    result = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.4.111", "--no-verify"],
    )
    assert result.exit_code == 0
    record = _records(state_dir)[0]
    assert record["status"] == rp.STATUS_ROLLED
    assert record["verification"] is None


# ── rollback is one command (#1560) ──────────────────────────────────────


def test_release_rollback_hits_every_machine(valid_config_path, monkeypatch):
    hit: list[str] = []

    def _fake_post(url, payload, *, timeout):
        hit.append(url)
        return 202, {}, ""

    monkeypatch.setattr(release_cmd, "_post", _fake_post)
    result = CliRunner().invoke(
        main, ["release", "rollback", "--config", str(valid_config_path), "--yes"]
    )
    assert result.exit_code == 0, result.output
    assert len(hit) == 2
    assert all(u.endswith("/rollback") for u in hit)


def test_release_rollback_reports_a_host_with_no_previous_generation(valid_config_path,
                                                                     monkeypatch):
    monkeypatch.setattr(release_cmd, "_post", lambda *a, **k: (404, {}, ""))
    result = CliRunner().invoke(
        main, ["release", "rollback", "--config", str(valid_config_path), "--yes"]
    )
    assert result.exit_code == 1
    assert "no previous generation" in result.output


# ── helpers ──────────────────────────────────────────────────────────────


def _stub_verify(monkeypatch, *, versions: dict[str, list[str]], severity: str = "ok"):
    """Replace `coord.release_verify`'s fleet sweep with a canned lane set."""
    from coord import release_verify as rv

    lanes = [
        rv.Lane(host=host, lane="~/.coord-venv", version=v)
        for host, vs in versions.items()
        for v in vs
    ]
    findings = (
        [rv.Finding(severity="crit", host="?", lane="?", summary="stubbed")]
        if severity == "crit"
        else []
    )
    monkeypatch.setattr(rv, "gather", lambda *a, **k: ({}, {}, None, "daemon"))
    monkeypatch.setattr(
        rv, "verify",
        lambda **kwargs: rv.VerifyReport(
            expected=kwargs.get("expected"), lanes=lanes, findings=findings
        ),
    )
