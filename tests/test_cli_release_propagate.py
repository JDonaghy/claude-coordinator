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
    """#2067: a deferral is per-host now — a single busy host no longer
    defers the whole run (see the tests below), so this exercises the case
    that genuinely must still defer everything: EVERY configured host
    (`laptop` and `server`, per `valid_config_path`) is occupied."""
    monkeypatch.setattr(
        release_cmd, "_fetch_board",
        lambda: ({"assignments": [{"machine_name": "laptop", "issue_number": 9,
                                   "status": "RUNNING"},
                                  {"machine_name": "server", "issue_number": 10,
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


# ── #2110: a stale `running` row must not defer the roll ──────────────────
#
# The exact 2026-08-10 incident, reproduced through the real CLI: a
# drive-queue row still reads `running` for an issue that has since closed
# and whose PR has merged — the reconciler that would normally have caught
# this lives inside `coord drive-queue tick`, and the timer can be stopped
# (that is the whole scenario `docs/AGENT_OPERATIONS.md` documents). Before
# #2110 this deferred every run, forever, on a row describing work that
# ended hours ago. It must not anymore.


def test_a_stale_running_row_does_not_defer_and_is_surfaced(
    valid_config_path, state_dir, no_network, monkeypatch
):
    monkeypatch.setattr(
        release_cmd, "_fetch_board",
        lambda: (
            {
                "drive_queue": [
                    {"repo_name": "api", "issue_number": 7,
                     "state": STATE_RUNNING, "launch_host": "server"},
                ],
                "assignments": [
                    {"repo_name": "api", "issue_number": 7, "type": "work",
                     "status": "merged"},
                ],
                "issues": [
                    {"repo_name": "api", "number": 7, "state": "closed"},
                ],
            },
            None,
        ),
    )
    _stub_verify(monkeypatch, versions={"laptop": ["0.4.111"], "server": ["0.4.111"]})
    result = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.4.111"],
    )
    assert result.exit_code == 0, result.output
    assert rp.STATUS_DEFERRED not in result.output
    assert rp.STATUS_UP_TO_DATE in result.output
    # Surfaced, not silently dropped — the operator can see the fleet
    # self-corrected a stale row instead of it just quietly not blocking.
    assert "stale" in result.output
    assert "api#7" in result.output
    records = _records(state_dir)
    assert records[-1]["quiescence"]["stale"] == ["api#7"]


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


# ──────────────────────────────────────────────────────────────────────────
# #2067: quiescence is per host — a busy host defers on its own, and does
# not hold the rest of the fleet hostage
# ──────────────────────────────────────────────────────────────────────────


def test_a_busy_non_daemon_host_defers_alone_while_the_daemon_rolls(
    valid_config_path, state_dir, no_network, monkeypatch
):
    """The regression, end to end: `laptop` has a live assignment; `server`
    (the daemon) is free. Under the old fleet-wide reading this deferred
    everything, forever, on a fleet whose drive queue never goes idle.
    `server` must roll and verify while `laptop`'s lanes are recorded as a
    per-host deferral, not attempted."""
    monkeypatch.setattr(
        release_cmd, "_fetch_board",
        lambda: ({"assignments": [{"machine_name": "laptop", "issue_number": 9,
                                   "status": "RUNNING"}]}, None),
    )
    calls = _stub_lanes(monkeypatch)
    _stub_verify(monkeypatch, versions={"laptop": ["0.4.110"], "server": ["0.4.110"]},
                 daemon="server")
    result = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.4.111"],
    )
    assert result.exit_code == 0, result.output
    # `server`'s lanes were attempted; `laptop`'s never were.
    assert any(host == "server" for _lane, host in calls)
    assert not any(host == "laptop" for _lane, host in calls)

    record = _records(state_dir)[0]
    assert record["status"] == rp.STATUS_VERIFIED
    laptop_lane = next(l for l in record["lanes"] if l["host"] == "laptop")
    assert laptop_lane["lane"] == "-"
    assert laptop_lane["ok"] is None
    assert "deferred" in laptop_lane["detail"]
    assert "laptop:9" in laptop_lane["detail"]


def test_a_busy_daemon_host_defers_the_whole_run(
    valid_config_path, state_dir, no_network, monkeypatch
):
    """The one case per-host quiescence still has to defer everything: the
    DAEMON is occupied. Rolling `laptop` ahead of an unrolled `server` would
    put a caller on a newer `coord` than the daemon it talks to — the
    documented 405 — so nothing may roll until `server` itself is free."""
    monkeypatch.setattr(
        release_cmd, "_fetch_board",
        lambda: ({"assignments": [{"machine_name": "server", "issue_number": 9,
                                   "status": "RUNNING"}]}, None),
    )
    monkeypatch.setattr(
        release_cmd, "_roll_python",
        lambda *a, **k: pytest.fail("a busy daemon must roll nothing, anywhere"),
    )
    _stub_verify(monkeypatch, versions={"laptop": ["0.4.110"], "server": ["0.4.110"]},
                 daemon="server")
    result = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.4.111"],
    )
    assert result.exit_code == 0, result.output
    record = _records(state_dir)[0]
    assert record["status"] == rp.STATUS_DEFERRED
    assert record["lanes"] == []


def test_force_rolls_over_per_host_busyness_too(
    valid_config_path, state_dir, no_network, monkeypatch
):
    """`--force` already killed in-flight work fleet-wide before #2067; it
    must still roll every host, busy or not, rather than defer any of them."""
    monkeypatch.setattr(
        release_cmd, "_fetch_board",
        lambda: ({"assignments": [{"machine_name": "laptop", "issue_number": 9,
                                   "status": "RUNNING"},
                                  {"machine_name": "server", "issue_number": 10,
                                   "status": "RUNNING"}]}, None),
    )
    calls = _stub_lanes(monkeypatch)
    _stub_verify(monkeypatch, versions={"laptop": ["0.4.110"], "server": ["0.4.110"]},
                 daemon="server")
    result = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.4.111", "--force"],
    )
    assert result.exit_code == 0, result.output
    assert "--force" in result.output  # the kill warning
    assert {host for _lane, host in calls} == {"laptop", "server"}
    assert _records(state_dir)[0]["status"] == rp.STATUS_VERIFIED


def test_a_busy_host_is_visible_in_a_dry_run_plan(
    valid_config_path, state_dir, no_network, monkeypatch
):
    monkeypatch.setattr(
        release_cmd, "_fetch_board",
        lambda: ({"assignments": [{"machine_name": "laptop", "issue_number": 9,
                                   "status": "RUNNING"}]}, None),
    )
    _stub_verify(monkeypatch, versions={"laptop": ["0.4.110"], "server": ["0.4.110"]},
                 daemon="server")
    result = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.4.111", "--dry-run"],
    )
    assert result.exit_code == 0, result.output
    assert "would roll" in result.output
    assert "server" in "\n".join(
        l for l in result.output.splitlines() if "would roll" in l
    )
    assert "laptop" not in "\n".join(
        l for l in result.output.splitlines() if "would roll" in l
    )
    assert "laptop:9" in result.output


# ── the roll, the final gate, and the rollback on red ────────────────────


def _stub_lanes(monkeypatch, *, python_ok=True, calls=None, tui_local=None):
    """Replace the three per-lane executors with recorders.

    *python_ok* is either a single bool applied to every host, or a
    ``{host: bool}`` mapping for tests that need one host's python lane to
    fail while another's succeeds (e.g. the daemon-leads invariant).
    """
    log = calls if calls is not None else []

    def _ok_for(host: str) -> bool:
        if isinstance(python_ok, dict):
            return python_ok.get(host, True)
        return python_ok

    def _python(machine, **kwargs):
        log.append(("python", machine.name))
        ok = _ok_for(machine.name)
        return ok, "now v0.4.111" if ok else "pip failed"

    def _units(machine, **kwargs):
        log.append(("units", machine.name))
        return True, "1 unit(s) refreshed; daemon-reload ok"

    def _tui(machine, **kwargs):
        log.append(("tui", machine.name))
        if tui_local is not None and machine.name != tui_local:
            # #2052: no channel for this lane here — `ok=None`, never False.
            return None, "coord-tui is a per-host binary with no remote install path"
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


def test_a_failed_daemon_python_roll_skips_other_hosts_python_lane(
    valid_config_path, state_dir, no_network, monkeypatch
):
    """#1835 review: plan_lanes() puts the daemon host's python lane first so
    that 'a caller must never reach an endpoint its daemon predates' holds —
    but that invariant is only real if a failure there actually stops the
    rest of the python lane. If `server` (the daemon) fails its own python
    roll, `laptop` must never be advanced to target_version anyway; doing so
    would reproduce the documented 405 skew for the rest of this run."""
    calls = _stub_lanes(monkeypatch, python_ok={"server": False, "laptop": True})
    # The stubbed verify gate reports whatever `versions` says regardless of
    # what the roll loop actually did — it is not the seam under test here.
    # What's under test is that the loop itself never advances `laptop` past
    # the daemon, independent of what the final gate later decides.
    _stub_verify(monkeypatch, versions={"laptop": ["0.4.110"], "server": ["0.4.110"]})
    result = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.4.111", "--daemon-host", "server"],
    )
    assert result.exit_code == 0, result.output
    # The daemon's own python lane was attempted and failed...
    assert ("python", "server") in calls
    # ...but laptop's python lane was never attempted at all — not
    # attempted and failed, simply skipped outright.
    assert ("python", "laptop") not in calls

    record = _records(state_dir)[0]
    laptop_python = next(
        l for l in record["lanes"] if l["lane"] == "python" and l["host"] == "laptop"
    )
    # "not attempted" is recorded as ok=None, distinct from ok=False (a real
    # failure) — a re-run should resume this host, not treat it as needing
    # a rollback.
    assert laptop_python["ok"] is None
    assert "not attempted" in laptop_python["detail"]
    # No lane record claims laptop's python roll succeeded.
    assert not any(
        l["lane"] == "python" and l["host"] == "laptop" and l["ok"] is True
        for l in record["lanes"]
    )


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
    monkeypatch.setattr(release_cmd, "_get",
                        lambda url, *, timeout: (200, {"version": "0.4.110"}))
    result = CliRunner().invoke(
        main, ["release", "rollback", "--config", str(valid_config_path), "--yes"]
    )
    assert result.exit_code == 0, result.output
    assert len(hit) == 2
    assert all(u.endswith("/rollback") for u in hit)
    # #2052 fault 1: "rolling back" is a statement about the request. The
    # outcome is whether the service is serving again.
    assert "serving again" in result.output


def test_a_rollback_that_leaves_the_agent_dead_says_so(valid_config_path, monkeypatch):
    """#2052 fault 1: precision's coord-agent went `inactive (dead)` at the
    moment of the rollback and was never restarted — recovery needed a human.
    A rollback that stops a service and does not restore it leaves the fleet
    WORSE off than the failed roll did, so it must escalate, and then shout."""
    monkeypatch.setattr(release_cmd, "_post", lambda *a, **k: (202, {}, ""))
    monkeypatch.setattr(release_cmd, "_get", lambda url, *, timeout: (None, {}))
    escalated: list[str] = []
    monkeypatch.setattr(
        "coord.commands.agent_ops._escalate_restart",
        lambda machine: (escalated.append(machine.name), False)[1],
    )
    result = CliRunner().invoke(
        main, ["release", "rollback", "--config", str(valid_config_path), "--yes",
               "--wait", "1"]
    )
    assert result.exit_code == 1, result.output
    assert "DOWN" in result.output
    # The documented systemd-stall fix is APPLIED, not merely suggested.
    assert escalated, "a dead agent must be restarted, not just reported"


def test_a_rollback_rescued_by_the_ssh_restart_is_a_success(valid_config_path,
                                                            monkeypatch):
    """#404/#1568: `os.execv` does not always take under systemd. The
    documented fix is an SSH `systemctl --user restart coord-agent` — and a
    host that came back that way is genuinely back."""
    monkeypatch.setattr(release_cmd, "_post", lambda *a, **k: (202, {}, ""))
    answers = iter([(None, {})] * 200)
    revived = {"yes": False}

    def _fake_get(url, *, timeout):
        if revived["yes"]:
            return 200, {"version": "0.4.110"}
        return next(answers)

    monkeypatch.setattr(release_cmd, "_get", _fake_get)
    monkeypatch.setattr(
        "coord.commands.agent_ops._escalate_restart",
        lambda machine: revived.__setitem__("yes", True) or True,
    )
    result = CliRunner().invoke(
        main, ["release", "rollback", "--config", str(valid_config_path), "--yes",
               "--wait", "1"]
    )
    assert result.exit_code == 0, result.output
    assert "systemctl --user restart" in result.output


def test_release_rollback_reports_a_host_with_no_previous_generation(valid_config_path,
                                                                     monkeypatch):
    monkeypatch.setattr(release_cmd, "_post", lambda *a, **k: (404, {}, ""))
    result = CliRunner().invoke(
        main, ["release", "rollback", "--config", str(valid_config_path), "--yes"]
    )
    assert result.exit_code == 1
    assert "no previous generation" in result.output


# ──────────────────────────────────────────────────────────────────────────
# #2052: the gate cannot fail for reasons propagation cannot influence
# ──────────────────────────────────────────────────────────────────────────


def test_2026_08_09_a_good_roll_is_not_reverted_by_lanes_it_cannot_roll(
    valid_config_path, state_dir, no_network, monkeypatch
):
    """The regression, end to end. #2052: every lane propagation *can* roll,
    rolled — three python lanes, three unit lanes, the one coord-tui it could
    reach. Verification then came back crit on `~/.coord-cli-venv` (a lane
    this command has no model of) and on the remote `coord-tui` binary (which
    it reports itself has NO remote install path), plus a stale `webapp
    bundle` (SHA-versioned off its own timer, never pip-versioned at all) —
    and `--rollback-on-red` reverted the lot. Not a transient failure: it
    would have happened on every run, forever.

    #2069 closed the fourth lane this incident actually hit — `coord-serve
    process` — by having the python lane restart coord-serve itself; see
    `tests/test_release_propagate.py::
    test_a_sibling_unit_finding_blocks_when_its_host_python_lane_rolled` for
    that lane now correctly blocking instead of being advisory forever."""
    from coord import release_verify as rv

    # `server` is the host this command runs on, so it is the only host whose
    # coord-tui binary has any install path at all — exactly the shape of the
    # real run, where 1 of 3 tui lanes could roll.
    _stub_lanes(monkeypatch, tui_local="server")
    _stub_verify(
        monkeypatch,
        versions={"laptop": ["0.4.110"], "server": ["0.4.110"]},
        findings=[
            rv.Finding(severity="crit", host="laptop",
                       lane="~/.coord-cli-venv (laptop)",
                       summary="on 0.4.104, expected 0.4.111"),
            rv.Finding(severity="warn", host="server",
                       lane="webapp bundle",
                       summary="webapp bundle is stale"),
            rv.Finding(severity="warn", host="laptop", lane="coord-tui",
                       summary="tui binary is stale"),
        ],
    )
    monkeypatch.setattr(
        release_cmd, "_rollback_host",
        lambda *a, **k: pytest.fail(
            "reverting a good python roll because a per-host binary could "
            "not be installed remotely is a category error"
        ),
    )
    result = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.4.111", "--daemon-host", "server"],
    )
    assert result.exit_code == 0, result.output
    record = _records(state_dir)[0]
    assert record["status"] == rp.STATUS_VERIFIED
    # The full report is still journalled verbatim — scoping the gate must
    # never shrink the record.
    assert record["verification"]["severity"] == "crit"
    assert len(record["verification"]["findings"]) == 3
    # ...and the scoping itself is legible, so a gate that stopped gating
    # would be visible rather than silent.
    assert record["gate"]["severity"] == "ok"
    assert len(record["gate"]["advisory"]) == 3
    assert "advisory" in result.output


def test_a_crit_on_a_lane_this_run_rolled_still_reverts(valid_config_path, state_dir,
                                                        no_network, monkeypatch):
    """Scoping the gate is not removing it."""
    from coord import release_verify as rv

    _stub_lanes(monkeypatch)
    _stub_verify(
        monkeypatch,
        versions={"laptop": ["0.4.110"], "server": ["0.4.110"]},
        findings=[rv.Finding(severity="crit", host="laptop",
                             lane="~/.coord-venv (laptop)",
                             summary="on 0.4.110, expected 0.4.111")],
    )
    rolled_back: list[str] = []
    monkeypatch.setattr(
        release_cmd, "_rollback_host",
        lambda machine, **k: (rolled_back.append(machine.name), (True, "back up"))[1],
    )
    result = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.4.111", "--daemon-host", "server"],
    )
    assert result.exit_code == 2, result.output
    assert sorted(rolled_back) == ["laptop", "server"]


def test_the_remote_tui_lane_is_recorded_as_unrollable_not_failed(
    valid_config_path, state_dir, no_network, monkeypatch
):
    """`coord-tui` is a per-host binary with no remote install path — the
    command says so in its own failure message. A lane that reports it cannot
    be rolled from here must not also count as this run going wrong."""
    _stub_lanes(monkeypatch, tui_local="server")
    _stub_verify(monkeypatch, versions={"laptop": ["0.4.110"], "server": ["0.4.110"]})
    result = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.4.111", "--daemon-host", "server"],
    )
    assert result.exit_code == 0, result.output
    record = _records(state_dir)[0]
    laptop_tui = next(
        l for l in record["lanes"] if l["lane"] == "tui" and l["host"] == "laptop"
    )
    assert laptop_tui["ok"] is None
    assert laptop_tui["unrollable"] is True
    assert "tui@laptop" in record["gate"]["unrollable"]


def test_an_agent_without_deploy_units_is_a_next_run_fact_not_a_red_gate(
    valid_config_path, state_dir, no_network, monkeypatch
):
    """Bootstrap: an agent that predates /deploy-units gets the endpoint once
    the python lane lands. That is a fact about the next run, and it must not
    revert this one."""
    _stub_lanes(monkeypatch)
    monkeypatch.setattr(
        release_cmd, "_roll_units",
        lambda machine, **k: (None, "agent has no /deploy-units yet"),
    )
    _stub_verify(monkeypatch, versions={"laptop": ["0.4.110"], "server": ["0.4.110"]})
    result = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.4.111", "--daemon-host", "server"],
    )
    assert result.exit_code == 0, result.output
    record = _records(state_dir)[0]
    assert all(
        l["unrollable"] is True
        for l in record["lanes"] if l["lane"] == "units"
    )


# ──────────────────────────────────────────────────────────────────────────
# #2052 fault 2: the daemon host is derived, or the run refuses
# ──────────────────────────────────────────────────────────────────────────


def test_the_daemon_host_is_derived_from_the_fleets_own_health(
    valid_config_path, state_dir, no_network, monkeypatch
):
    """No --daemon-host flag, and it still leads: `server` is the machine
    whose /health reports a running coord-serve."""
    calls = _stub_lanes(monkeypatch)
    _stub_verify(monkeypatch, versions={"laptop": ["0.4.110"], "server": ["0.4.110"]},
                 daemon="server")
    result = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.4.111"],
    )
    assert result.exit_code == 0, result.output
    assert calls[0] == ("python", "server")


def test_an_unidentifiable_daemon_host_refuses_instead_of_guessing(
    valid_config_path, state_dir, no_network, monkeypatch
):
    """#2052 fault 2: this used to warn and roll in coordinator.yml order,
    which during a partial revert briefly left the daemon host BEHIND both
    its callers — the documented 405 hazard the warning itself named.
    Ordering is the one thing protecting against that."""
    _stub_lanes(
        monkeypatch,
        calls=None,
    )
    monkeypatch.setattr(
        release_cmd, "_roll_python",
        lambda *a, **k: pytest.fail("an unorderable run must not roll anything"),
    )
    monkeypatch.setattr(release_cmd, "_daemon_machine_name", lambda *a, **k: None)
    _stub_verify(monkeypatch, versions={"laptop": ["0.4.110"], "server": ["0.4.110"]},
                 daemon=None)
    result = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.4.111"],
    )
    assert result.exit_code == 1, result.output
    record = _records(state_dir)[0]
    assert record["status"] == rp.STATUS_FAILED
    assert "REFUSING" in record["error"]
    assert "405" in record["error"]


# ── helpers ──────────────────────────────────────────────────────────────


def _serve_health(host: str) -> dict:
    """A ``/health`` body whose ``spawned_coord`` rows name a live coord-serve.

    #2052 fault 2: this is how the daemon host is *derived* rather than
    guessed. Stubbing `gather` with an empty machine_health used to leave
    propagation unable to name the daemon at all, which is precisely the
    state that let it roll in coordinator.yml order and briefly put the
    daemon behind both its callers.
    """
    return {
        "version": "0.4.111",
        "health": {"schema": 1, "results": [
            {"check_id": "spawned_coord", "subject": "coord-serve",
             "severity": "ok", "values": {"unit": "coord-serve", "pid": 1,
                                          "version": "0.4.111"}},
        ]},
    }


def _stub_verify(monkeypatch, *, versions: dict[str, list[str]], severity: str = "ok",
                 daemon: str | None = "server", findings=None):
    """Replace `coord.release_verify`'s fleet sweep with a canned lane set.

    *daemon* is the machine whose ``/health`` reports a running coord-serve —
    the fact `_daemon_machine_name` derives the roll order from. Pass None to
    model a fleet nothing can name a daemon for (which now REFUSES to roll).
    """
    from coord import release_verify as rv

    lanes = [
        rv.Lane(host=host, lane="~/.coord-venv", version=v)
        for host, vs in versions.items()
        for v in vs
    ]
    if findings is None:
        findings = (
            # #2052: a crit the gate can actually attribute to this run. A
            # finding on a lane propagation cannot roll is advisory, and
            # tests that want THAT say so explicitly.
            [rv.Finding(severity="crit", host=host, lane=f"~/.coord-venv ({host})",
                        summary="stubbed")
             for host in sorted(versions)]
            if severity == "crit"
            else []
        )
    machine_health = {daemon: _serve_health(daemon)} if daemon else {}
    monkeypatch.setattr(rv, "gather",
                        lambda *a, **k: (machine_health, {}, None, daemon or "daemon"))
    monkeypatch.setattr(
        rv, "verify",
        lambda **kwargs: rv.VerifyReport(
            expected=kwargs.get("expected"), lanes=lanes, findings=findings
        ),
    )


# ──────────────────────────────────────────────────────────────────────────
# #2069: the python lane restarts coord-serve/coord-web/coord-drive-queue,
# not just coord-agent
# ──────────────────────────────────────────────────────────────────────────


def _machine(name="server", host="server.tailnet"):
    from coord.models import Machine

    return Machine(name=name, host=host)


def _stub_agent_update_ok(monkeypatch, *, target="0.4.111"):
    """Make `_roll_python`'s own `/update` half succeed without a real agent."""
    monkeypatch.setattr(release_cmd, "_post",
                        lambda url, payload, *, timeout: (202, {}, ""))
    monkeypatch.setattr(
        "coord.commands.agent_ops._fetch_pre_started_at", lambda machines: {}
    )
    monkeypatch.setattr(
        "coord.commands.agent_ops._wait_agents_updated",
        lambda machines, *, target_version, timeout, pre_started_at: {
            m.name: {"matched": True} for m in machines
        },
    )


def test_roll_python_restarts_sibling_services_after_the_venv_swap(monkeypatch):
    """The concrete cost this issue names: v0.5.13 carried a fix inside
    coord-serve, but only coord-agent got restarted, so the daemon kept
    serving v0.5.8's code under a v0.5.13 label. `_roll_python` must now call
    `/restart-services` right after `/update` reports success."""
    posts: list[tuple[str, dict]] = []

    def _fake_post(url, payload, *, timeout):
        posts.append((url, payload))
        if url.endswith("/update"):
            return 202, {}, ""
        if url.endswith("/restart-services"):
            return 200, {"units": {
                "coord-serve": {"restarted": True, "detail": "active"},
                "coord-web": {"restarted": None, "detail": "not running on this host"},
                "coord-drive-queue": {"restarted": None, "detail": "not running on this host"},
            }}, ""
        raise AssertionError(f"unexpected POST {url}")

    monkeypatch.setattr(release_cmd, "_post", _fake_post)
    monkeypatch.setattr(
        "coord.commands.agent_ops._fetch_pre_started_at", lambda machines: {}
    )
    monkeypatch.setattr(
        "coord.commands.agent_ops._wait_agents_updated",
        lambda machines, *, target_version, timeout, pre_started_at: {
            m.name: {"matched": True} for m in machines
        },
    )

    ok, detail = release_cmd._roll_python(
        _machine(), target_version="0.4.111", agent_port=7433, timeout=5.0, force=False
    )
    assert ok
    assert "now v0.4.111" in detail
    assert "restarted coord-serve" in detail
    urls = [u for u, _ in posts]
    assert urls == [
        "http://server.tailnet:7433/update",
        "http://server.tailnet:7433/restart-services",
    ], "restart-services must be called AFTER update, on the same host"


def test_roll_python_fails_the_lane_when_a_sibling_restart_fails(monkeypatch):
    """#2095: this used to stay `ok=True` — "the venv swap itself succeeded"
    bleeding into "the lane succeeded" — and printed a leading `✓` over a
    line that itself said `FAILED to restart: coord-serve`. That is what
    happened for real during the 2026-08-10 0.5.15 -> 0.5.26 roll (coord-web,
    not coord-serve, but the same code path): the phone dashboard went
    offline and propagation reported success.

    The venv swap is still named in the detail string — that part really did
    happen and is still worth recording — but a sibling this run took down
    and never brought back is a real outage, not a footnote under a `✓`. The
    old justification for staying green was "`coord release verify` will
    catch the resulting skew"; it cannot, because verify grades versions, not
    liveness, and carries no lane for these units at all — see
    `tests/test_release_propagate.py`'s coord-web-liveness-adjacent tests
    (there is deliberately no such lane to test)."""
    _stub_agent_update_ok(monkeypatch)

    def _fake_post(url, payload, *, timeout):
        if url.endswith("/update"):
            return 202, {}, ""
        # #2069: the real endpoint (agent_app.py's restart_services) returns HTTP
        # 500 — not 200 — whenever any unit fails to restart, with the same
        # {"units": {...}} body shape either way. Mocking 200 here would let a
        # since-fixed bug (the caller discarding per-unit detail on a real 500)
        # regress silently.
        return 500, {"units": {
            "coord-serve": {"restarted": False, "detail": "still activating 30s after restart"},
        }}, ""

    monkeypatch.setattr(release_cmd, "_post", _fake_post)
    ok, detail = release_cmd._roll_python(
        _machine(), target_version="0.4.111", agent_port=7433, timeout=5.0, force=False
    )
    assert ok is False, (
        "a sibling this run took down and never brought back must not print "
        "a `✓` over the lane — see coord/commands/release.py's _roll_python"
    )
    assert "now v0.4.111" in detail, "the venv swap itself still happened and is still named"
    assert "FAILED to restart" in detail
    assert "coord-serve" in detail
    assert "verify" not in detail, (
        "must not claim `coord release verify` catches this — it has no "
        "liveness lane for these units at all (#2095)"
    )


def test_roll_python_tolerates_an_agent_that_predates_restart_services(monkeypatch):
    """A host on an old agent build has no /restart-services endpoint yet.
    That must not turn a successful venv swap into a failed python lane."""
    _stub_agent_update_ok(monkeypatch)
    monkeypatch.setattr(
        release_cmd, "_post",
        lambda url, payload, *, timeout: (
            (202, {}, "") if url.endswith("/update") else (404, {}, "")
        ),
    )
    ok, detail = release_cmd._roll_python(
        _machine(), target_version="0.4.111", agent_port=7433, timeout=5.0, force=False
    )
    assert ok
    assert "now v0.4.111" in detail


def test_restart_sibling_services_reports_a_mix_of_outcomes(monkeypatch):
    calls = []

    def _fake_post(url, payload, *, timeout):
        calls.append(url)
        # #2069: a mixed outcome with any failed unit is a real HTTP 500 from the
        # endpoint (agent_app.py's restart_services), not a 200 — see the comment
        # in test_roll_python_fails_the_lane_when_a_sibling_restart_fails above.
        return 500, {"units": {
            "coord-serve": {"restarted": True, "detail": "active"},
            "coord-web": {"restarted": False, "detail": "still deactivating"},
            "coord-drive-queue": {"restarted": None, "detail": "not running on this host"},
        }}, ""

    monkeypatch.setattr(release_cmd, "_post", _fake_post)
    ok, detail = release_cmd._restart_sibling_services(
        _machine(), agent_port=7433, timeout=5.0
    )
    assert not ok
    assert "restarted coord-serve" in detail
    assert "not running here: coord-drive-queue" in detail
    assert "FAILED to restart: coord-web" in detail
    assert calls == ["http://server.tailnet:7433/restart-services"]


def test_restart_sibling_services_tolerates_a_pre_2069_agent(monkeypatch):
    """#2095: HTTP 404 from `/restart-services` means this agent build
    predates the endpoint (#2069) — there was never a channel here to have
    restarted anything through, which is a different thing from a channel
    that existed and failed. Tri-state `None`, not `False`, is what tells
    `_roll_python` not to fail the lane over it (see
    test_roll_python_tolerates_an_agent_that_predates_restart_services)."""
    monkeypatch.setattr(
        release_cmd, "_post", lambda url, payload, *, timeout: (404, {}, "")
    )
    ok, detail = release_cmd._restart_sibling_services(
        _machine(), agent_port=7433, timeout=5.0
    )
    assert ok is None
    assert "404" in detail
