"""Black-box tests for `coord release nightly-window` (#2112).

`coord release propagate` cannot roll the daemon host past a busy fleet on
its own: the daemon leads every roll (the documented 405), and dellserver's
own drive-queue tick charges itself busy for essentially any queued drive —
see `coord/release_window.py`'s module docstring for the full mechanism.
This command manufactures the window propagate can't: stop the queue, drain
what's already running (bounded), roll, ALWAYS restart the queue.

These tests drive the running command through Click, per this repo's bar
for a behaviour-changing PR — the pure judgement (`needs_roll`, the journal
shape) is covered in `tests/test_release_window.py`. What's tested here is
the wiring, and #2112's five acceptance criteria directly:

1. idle fleet at the window -> rolled, timer running again.
2. a drive still running at the drain deadline -> timer restarted, nothing
   rolled, the reason is in the surfaced message.
3. the fleet already current -> the queue is never touched at all.
4. the queue timer is running after the job in EVERY path, including a
   crash mid-window.
5. success is never reported for a roll the job did not confirm.

Nothing here touches a real fleet or a real systemd: `_systemctl`,
`_run_reconcile_tick`, `_run_propagate`, and `coord.release_verify.gather`/
`.verify` are all seams.
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from coord import release_propagate as rp
from coord import release_window as rw
from coord.cli import main
from coord.commands import release as release_cmd


@pytest.fixture(autouse=True)
def _own_pause_store(tmp_path, monkeypatch):
    """Give every test in this module its own pause store (#2174).

    `test_drain_is_blocked_by_a_paused_daemon_host` calls
    `mp.local_pause("server")`, and that store is per-`$HOME`, not
    per-test. `conftest._no_real_pause_store` redirects only when the
    resolved path lands under the REAL home, so under
    `scripts/run_tests_in_populated_home.sh` (#2170) — where `$HOME` is one
    throwaway directory shared by the whole run — the pause survives this
    test and every later one reads a machine it never paused. See
    `tests/test_cli_release_propagate.py::_own_pause_store` for the full
    write-up; this is the same hazard in the sibling module that shares the
    seam.
    """
    home = tmp_path / "home"
    (home / ".coord").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return home


@pytest.fixture()
def state_dir(tmp_path, monkeypatch):
    """Point the window journal at a tmp dir, never the real ~/.coord."""
    d = tmp_path / "state"
    d.mkdir()
    monkeypatch.setattr(release_cmd, "_state_dir", lambda: d)
    return d


@pytest.fixture()
def no_network(monkeypatch):
    """No PyPI lookup, no /board read unless a test says so."""
    monkeypatch.setattr(release_cmd, "_fetch_board", lambda: ({}, None))


@pytest.fixture()
def escalations(monkeypatch):
    """Capture every `record_drive_escalation` call `_escalate_window` makes."""
    calls: list[dict] = []

    def _fake(repo, issue, *, stage, reason, gate_readings, proposed_command,
             assignment_id=None):
        calls.append({"repo": repo, "issue": issue, "stage": stage, "reason": reason,
                      "gate_readings": gate_readings, "proposed_command": proposed_command})
        return 1

    monkeypatch.setattr("coord.state.record_drive_escalation", _fake)
    return calls


def _records(state_dir):
    return rw.read_records(state_dir)


def _serve_health(host: str) -> dict:
    return {
        "version": "0.5.31",
        "health": {"schema": 1, "results": [
            {"check_id": "spawned_coord", "subject": "coord-serve",
             "severity": "ok", "values": {"unit": "coord-serve", "pid": 1,
                                          "version": "0.5.31"}},
        ]},
    }


def _stub_verify(monkeypatch, *, daemon_version: str | None, daemon: str = "server"):
    """Replace `coord.release_verify`'s fleet sweep with a canned daemon-host
    python lane — same seam `tests/test_cli_release_propagate.py` uses."""
    from coord import release_verify as rv

    lanes = [rv.Lane(host=daemon, lane="~/.coord-venv", version=daemon_version)]
    machine_health = {daemon: _serve_health(daemon)}
    monkeypatch.setattr(rv, "gather",
                        lambda *a, **k: (machine_health, {}, None, daemon))
    monkeypatch.setattr(
        rv, "verify",
        lambda **kwargs: rv.VerifyReport(expected=kwargs.get("expected"), lanes=lanes,
                                         findings=[]),
    )


def _stub_systemctl(monkeypatch, *, stop_ok=True, start_ok=True):
    calls: list[tuple[str, str]] = []

    def _fake(unit, action, **kwargs):
        calls.append((unit, action))
        ok = stop_ok if action == "stop" else start_ok
        return ok, f"{action} {'ok' if ok else 'failed'}"

    monkeypatch.setattr(release_cmd, "_systemctl", _fake)
    return calls


def _stub_drain(monkeypatch, *, drained: bool, elapsed: float = 5.0, detail: str = ""):
    calls = []

    def _fake(**kwargs):
        calls.append(kwargs)
        return rw.DrainOutcome(drained=drained, elapsed_seconds=elapsed, detail=detail)

    monkeypatch.setattr(release_cmd, "_drain", _fake)
    return calls


def _stub_propagate(monkeypatch, *, status: str, exit_code: int, output: str = "ok",
                    started_at: float | None = None):
    calls = []

    def _fake(**kwargs):
        calls.append(kwargs)
        return status, exit_code, output, started_at

    monkeypatch.setattr(release_cmd, "_run_propagate", _fake)
    return calls


# ── acceptance 3: already current -> the queue is never touched ──────────


def test_an_already_current_daemon_never_touches_the_queue(
    valid_config_path, state_dir, no_network, escalations, monkeypatch
):
    _stub_verify(monkeypatch, daemon_version="0.5.31")
    systemctl_calls = _stub_systemctl(monkeypatch)
    drain_calls = _stub_drain(monkeypatch, drained=True)

    result = CliRunner().invoke(
        main,
        ["release", "nightly-window", "--config", str(valid_config_path),
         "--target", "0.5.31", "--daemon-host", "server"],
    )
    assert result.exit_code == 0, result.output
    assert systemctl_calls == []
    assert drain_calls == []
    assert not escalations
    record = _records(state_dir)[0]
    assert record["status"] == rw.STATUS_UP_TO_DATE
    assert record["queue_stopped"] is None
    assert record["queue_restarted"] is None


# ── acceptance 1: idle fleet -> rolled, timer running again ──────────────


def test_an_idle_fleet_rolls_and_the_timer_ends_up_running(
    valid_config_path, state_dir, no_network, escalations, monkeypatch
):
    _stub_verify(monkeypatch, daemon_version="0.5.30")
    systemctl_calls = _stub_systemctl(monkeypatch)
    drain_calls = _stub_drain(monkeypatch, drained=True, elapsed=2.0)
    prop_calls = _stub_propagate(monkeypatch, status=rp.STATUS_VERIFIED, exit_code=0)

    result = CliRunner().invoke(
        main,
        ["release", "nightly-window", "--config", str(valid_config_path),
         "--target", "0.5.31", "--daemon-host", "server"],
    )
    assert result.exit_code == 0, result.output
    # stop, THEN start — in that order, and start always happens.
    assert systemctl_calls == [
        ("coord-drive-queue.timer", "stop"),
        ("coord-drive-queue.timer", "start"),
    ]
    assert len(drain_calls) == 1
    assert drain_calls[0]["daemon_host"] == "server"
    assert len(prop_calls) == 1
    assert prop_calls[0]["daemon_host"] == "server"
    assert prop_calls[0]["target_version"] == "0.5.31"
    assert not escalations
    record = _records(state_dir)[0]
    assert record["status"] == rw.STATUS_ROLLED
    assert record["queue_stopped"] is True
    assert record["drained"] is True
    assert record["queue_restarted"] is True


def test_propagate_reporting_up_to_date_after_a_drain_is_still_a_success(
    valid_config_path, state_dir, no_network, monkeypatch
):
    """A race: the daemon host became current between this run's own check
    and the drained roll attempt (e.g. a human rolled it by hand mid-drain).
    Not a defect — `coord release propagate` itself said up-to-date."""
    _stub_verify(monkeypatch, daemon_version="0.5.30")
    _stub_systemctl(monkeypatch)
    _stub_drain(monkeypatch, drained=True)
    _stub_propagate(monkeypatch, status=rp.STATUS_UP_TO_DATE, exit_code=0)

    result = CliRunner().invoke(
        main,
        ["release", "nightly-window", "--config", str(valid_config_path),
         "--target", "0.5.31", "--daemon-host", "server"],
    )
    assert result.exit_code == 0, result.output
    assert _records(state_dir)[0]["status"] == rw.STATUS_UP_TO_DATE


# ── acceptance 2: drain deadline hit -> timer restarted, nothing rolled,
#    reason surfaced ──────────────────────────────────────────────────────


def test_a_blown_drain_deadline_restarts_the_queue_rolls_nothing_and_says_why(
    valid_config_path, state_dir, no_network, escalations, monkeypatch
):
    _stub_verify(monkeypatch, daemon_version="0.5.30")
    systemctl_calls = _stub_systemctl(monkeypatch)
    _stub_drain(monkeypatch, drained=False, elapsed=3600.0,
               detail="claude-coordinator#2054 still running on server")
    prop_calls = _stub_propagate(monkeypatch, status=rp.STATUS_VERIFIED, exit_code=0)

    result = CliRunner().invoke(
        main,
        ["release", "nightly-window", "--config", str(valid_config_path),
         "--target", "0.5.31", "--daemon-host", "server",
         "--drain-deadline", "3600"],
    )
    assert result.exit_code != 0, result.output
    # Never rolled: propagate must not even be invoked once the drain gave up.
    assert prop_calls == []
    # The timer comes back regardless.
    assert systemctl_calls == [
        ("coord-drive-queue.timer", "stop"),
        ("coord-drive-queue.timer", "start"),
    ]
    record = _records(state_dir)[0]
    assert record["status"] == rw.STATUS_DRAIN_TIMEOUT
    assert record["queue_restarted"] is True
    # The reason is asserted on the SURFACED message (#2112's own wording),
    # not just an internal flag.
    assert "claude-coordinator#2054" in record["error"]
    assert "claude-coordinator#2054" in result.output
    assert len(escalations) == 1
    assert "claude-coordinator#2054" in escalations[0]["reason"]
    assert escalations[0]["stage"] == "release-window"


# ── acceptance 4: the queue timer is running after the job in EVERY path,
#    including a crash mid-window ─────────────────────────────────────────


def test_stopping_the_queue_failing_still_restarts_it_and_rolls_nothing(
    valid_config_path, state_dir, no_network, escalations, monkeypatch
):
    _stub_verify(monkeypatch, daemon_version="0.5.30")
    systemctl_calls = _stub_systemctl(monkeypatch, stop_ok=False, start_ok=True)
    drain_calls = _stub_drain(monkeypatch, drained=True)
    prop_calls = _stub_propagate(monkeypatch, status=rp.STATUS_VERIFIED, exit_code=0)

    result = CliRunner().invoke(
        main,
        ["release", "nightly-window", "--config", str(valid_config_path),
         "--target", "0.5.31", "--daemon-host", "server"],
    )
    assert result.exit_code != 0, result.output
    assert drain_calls == []  # never even tried to drain without a stopped queue
    assert prop_calls == []
    assert systemctl_calls == [
        ("coord-drive-queue.timer", "stop"),
        ("coord-drive-queue.timer", "start"),
    ]
    assert _records(state_dir)[0]["status"] == rw.STATUS_ERROR
    assert len(escalations) == 1


def test_a_crash_mid_drain_still_restarts_the_queue(
    valid_config_path, state_dir, no_network, monkeypatch
):
    """Simulates the job being killed mid-window (#2112 acceptance 4): make
    the drain step itself blow up instead of returning. A Python `finally`
    still runs while the stack unwinds through a real exception — this is
    the in-process half of the guarantee; `--ensure-queue-running` wired as
    the unit's ExecStopPost= (tested separately below) is the SIGKILL-safe
    half a `finally` cannot be."""
    _stub_verify(monkeypatch, daemon_version="0.5.30")
    systemctl_calls = _stub_systemctl(monkeypatch)

    def _boom(**kwargs):
        raise RuntimeError("simulated kill mid-drain")

    monkeypatch.setattr(release_cmd, "_drain", _boom)

    result = CliRunner().invoke(
        main,
        ["release", "nightly-window", "--config", str(valid_config_path),
         "--target", "0.5.31", "--daemon-host", "server"],
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, RuntimeError)
    # The worst outcome this mechanism exists to prevent did NOT happen:
    # the timer was stopped, and — despite the crash — restarted too.
    assert systemctl_calls == [
        ("coord-drive-queue.timer", "stop"),
        ("coord-drive-queue.timer", "start"),
    ]


def test_ensure_queue_running_only_starts_the_timer_and_exits(
    valid_config_path, monkeypatch
):
    """The SIGKILL-safe half (ExecStopPost=, deploy/coord-release-window.
    service): does ONLY `systemctl --user start <timer>`, regardless of
    anything else — no board read, no version resolution, no daemon lookup."""
    monkeypatch.setattr(
        release_cmd, "_fetch_board",
        lambda: pytest.fail("--ensure-queue-running must not touch the board"),
    )
    calls = _stub_systemctl(monkeypatch, start_ok=True)
    result = CliRunner().invoke(
        main,
        ["release", "nightly-window", "--ensure-queue-running",
         "--config", str(valid_config_path)],
    )
    assert result.exit_code == 0, result.output
    assert calls == [("coord-drive-queue.timer", "start")]


def test_ensure_queue_running_reports_failure_honestly(valid_config_path, monkeypatch):
    _stub_systemctl(monkeypatch, start_ok=False)
    result = CliRunner().invoke(
        main,
        ["release", "nightly-window", "--ensure-queue-running",
         "--config", str(valid_config_path)],
    )
    assert result.exit_code == 1


# ── acceptance 5: never report success for a roll that was not confirmed ─


def test_propagate_still_deferring_after_a_clean_drain_is_reported_not_hidden(
    valid_config_path, state_dir, no_network, escalations, monkeypatch
):
    """The daemon host drained clean, but `coord release propagate` itself
    still deferred (e.g. some OTHER host is busy, unattributable to the
    daemon). This must not be reported as a roll."""
    _stub_verify(monkeypatch, daemon_version="0.5.30")
    _stub_systemctl(monkeypatch)
    _stub_drain(monkeypatch, drained=True)
    _stub_propagate(monkeypatch, status=rp.STATUS_DEFERRED, exit_code=0,
                    output='{"status": "deferred"}')

    result = CliRunner().invoke(
        main,
        ["release", "nightly-window", "--config", str(valid_config_path),
         "--target", "0.5.31", "--daemon-host", "server"],
    )
    assert result.exit_code != 0, result.output
    record = _records(state_dir)[0]
    assert record["status"] == rw.STATUS_PROPAGATE_DEFERRED
    assert record["status"] not in rw.OK_STATUSES
    assert len(escalations) == 1


def test_propagate_failing_is_reported_with_its_own_exit_code(
    valid_config_path, state_dir, no_network, escalations, monkeypatch
):
    _stub_verify(monkeypatch, daemon_version="0.5.30")
    _stub_systemctl(monkeypatch)
    _stub_drain(monkeypatch, drained=True)
    _stub_propagate(monkeypatch, status=rp.STATUS_ROLLED_BACK, exit_code=2,
                    output='{"status": "rolled-back"}')

    result = CliRunner().invoke(
        main,
        ["release", "nightly-window", "--config", str(valid_config_path),
         "--target", "0.5.31", "--daemon-host", "server"],
    )
    assert result.exit_code == 2, result.output
    record = _records(state_dir)[0]
    assert record["status"] == rw.STATUS_PROPAGATE_FAILED
    assert len(escalations) == 1


def test_a_propagate_subprocess_that_cannot_even_run_is_reported_not_a_success(
    valid_config_path, state_dir, no_network, escalations, monkeypatch
):
    _stub_verify(monkeypatch, daemon_version="0.5.30")
    _stub_systemctl(monkeypatch)
    _stub_drain(monkeypatch, drained=True)

    def _boom(**kwargs):
        raise TimeoutError("propagate subprocess timed out")

    monkeypatch.setattr(release_cmd, "_run_propagate",
                        lambda **k: ("error: TimeoutError: propagate subprocess timed out",
                                    1, "TimeoutError: propagate subprocess timed out", None))
    result = CliRunner().invoke(
        main,
        ["release", "nightly-window", "--config", str(valid_config_path),
         "--target", "0.5.31", "--daemon-host", "server"],
    )
    assert result.exit_code != 0
    assert _records(state_dir)[0]["status"] == rw.STATUS_PROPAGATE_FAILED


# ── no resolvable target / no daemon host: setup failures are loud too ───


def test_no_resolvable_target_fails_loudly(
    valid_config_path, state_dir, no_network, escalations, monkeypatch
):
    monkeypatch.setattr(
        release_cmd, "_resolve_expected", lambda *a, **k: (None, "PyPI unreachable")
    )
    result = CliRunner().invoke(
        main, ["release", "nightly-window", "--config", str(valid_config_path)]
    )
    assert result.exit_code == 1
    assert _records(state_dir)[0]["status"] == rw.STATUS_ERROR
    assert len(escalations) == 1


def test_an_unidentifiable_daemon_host_refuses_instead_of_guessing(
    valid_config_path, state_dir, no_network, escalations, monkeypatch
):
    monkeypatch.setattr(release_cmd, "_daemon_machine_name", lambda *a, **k: None)
    _stub_verify(monkeypatch, daemon_version="0.5.30", daemon="server")
    systemctl_calls = _stub_systemctl(monkeypatch)

    result = CliRunner().invoke(
        main,
        ["release", "nightly-window", "--config", str(valid_config_path),
         "--target", "0.5.31"],
    )
    assert result.exit_code == 1, result.output
    assert systemctl_calls == []  # never stopped a queue it couldn't safely resume
    record = _records(state_dir)[0]
    assert record["status"] == rw.STATUS_ERROR
    assert len(escalations) == 1


# ── --dry-run: the plan, without touching a host ──────────────────────────


def test_a_dry_run_prints_the_plan_and_touches_nothing(
    valid_config_path, state_dir, no_network, escalations, monkeypatch
):
    _stub_verify(monkeypatch, daemon_version="0.5.30")
    systemctl_calls = _stub_systemctl(monkeypatch)
    drain_calls = _stub_drain(monkeypatch, drained=True)
    prop_calls = _stub_propagate(monkeypatch, status=rp.STATUS_VERIFIED, exit_code=0)

    result = CliRunner().invoke(
        main,
        ["release", "nightly-window", "--config", str(valid_config_path),
         "--target", "0.5.31", "--daemon-host", "server", "--dry-run"],
    )
    assert result.exit_code == 0, result.output
    assert systemctl_calls == []
    assert drain_calls == []
    assert prop_calls == []
    assert not escalations
    assert _records(state_dir) == []  # dry-run writes nothing


# ── the queue restart failing is itself escalated ─────────────────────────


def test_a_failed_queue_restart_after_a_successful_roll_is_still_escalated(
    valid_config_path, state_dir, no_network, escalations, monkeypatch
):
    """The worst outcome this mechanism exists to prevent: even a CLEAN roll
    must not go quiet if the timer fails to come back."""
    _stub_verify(monkeypatch, daemon_version="0.5.30")
    _stub_systemctl(monkeypatch, stop_ok=True, start_ok=False)
    _stub_drain(monkeypatch, drained=True)
    _stub_propagate(monkeypatch, status=rp.STATUS_VERIFIED, exit_code=0)

    result = CliRunner().invoke(
        main,
        ["release", "nightly-window", "--config", str(valid_config_path),
         "--target", "0.5.31", "--daemon-host", "server"],
    )
    assert result.exit_code == 0, result.output  # the ROLL itself succeeded
    record = _records(state_dir)[0]
    assert record["status"] == rw.STATUS_ROLLED
    assert record["queue_restarted"] is False
    assert len(escalations) == 1
    assert "restart FAILED" in escalations[0]["reason"]


# ── window-history ─────────────────────────────────────────────────────


def test_window_history_reads_back_what_was_journalled(
    valid_config_path, state_dir, no_network, monkeypatch
):
    _stub_verify(monkeypatch, daemon_version="0.5.30")
    _stub_systemctl(monkeypatch)
    _stub_drain(monkeypatch, drained=True)
    _stub_propagate(monkeypatch, status=rp.STATUS_VERIFIED, exit_code=0)
    CliRunner().invoke(
        main,
        ["release", "nightly-window", "--config", str(valid_config_path),
         "--target", "0.5.31", "--daemon-host", "server"],
    )

    result = CliRunner().invoke(main, ["release", "window-history"])
    assert result.exit_code == 0, result.output
    assert rw.STATUS_ROLLED in result.output
    assert "v0.5.31" in result.output


def test_window_history_on_an_empty_journal_says_so(state_dir):
    result = CliRunner().invoke(main, ["release", "window-history"])
    assert result.exit_code == 0
    assert "no nightly-window attempts" in result.output


# ── _drain itself: the bounded loop, with an injected clock ──────────────


def test_drain_stops_as_soon_as_the_daemon_host_is_free():
    reconcile_calls = []
    boards = iter([
        {"drive_queue": [{"repo_name": "api", "issue_number": 1,
                          "state": "running", "machine": "server"}]},
        {"drive_queue": []},
    ])
    times = iter([0.0, 0.0, 10.0, 10.0])

    outcome = release_cmd._drain(
        daemon_host="server",
        config_path=None,
        deadline=3600.0,
        poll_interval=30.0,
        reconcile=lambda: reconcile_calls.append(1),
        board_fetch=lambda: (next(boards), None),
        now=lambda: next(times),
        sleep=lambda s: None,
    )
    assert outcome.drained is True
    assert outcome.elapsed_seconds == 10.0
    assert len(reconcile_calls) == 2


def test_drain_gives_up_at_the_deadline():
    times = [0.0]

    def _now():
        return times[0]

    def _sleep(s):
        times[0] += s

    outcome = release_cmd._drain(
        daemon_host="server",
        config_path=None,
        deadline=90.0,
        poll_interval=30.0,
        reconcile=lambda: None,
        board_fetch=lambda: (
            {"drive_queue": [{"repo_name": "api", "issue_number": 1,
                              "state": "running", "machine": "server"}]},
            None,
        ),
        now=_now,
        sleep=_sleep,
    )
    assert outcome.drained is False
    assert outcome.elapsed_seconds >= 90.0
    assert "api#1" in outcome.detail


def test_drain_treats_an_unreadable_board_as_fleet_wide_busy():
    """Same rule `coord release propagate` itself applies: a board this run
    cannot read is not proof of anything, least of all that the daemon host
    is free."""
    outcome = release_cmd._drain(
        daemon_host="server",
        config_path=None,
        deadline=10.0,
        poll_interval=5.0,
        reconcile=lambda: None,
        board_fetch=lambda: ({}, "ConnectError: refused"),
        now=(lambda ts=[0.0]: (ts.__setitem__(0, ts[0] + 5.0), ts[0])[1]),
        sleep=lambda s: None,
    )
    assert outcome.drained is False
    assert "board unreadable" in outcome.detail


def test_drain_is_blocked_by_a_paused_daemon_host(valid_config_path, monkeypatch):
    """#2174: `_drain`'s default `extra_busy_fetch` — the one the real
    `coord release nightly-window` call site uses (it passes `config=`) —
    must also see `coord pause`/quiet-hours state, not just tmux. A paused
    daemon host must never read as 'drained' just because the board and
    tmux are both quiet; before the fix nothing here ever consulted the
    pause store at all."""
    from coord import machine_pause as mp
    from coord.config import load as load_config

    config = load_config(str(valid_config_path))
    monkeypatch.setattr(release_cmd, "_interactive_session_busy", lambda config: [])
    mp.local_pause("server")

    outcome = release_cmd._drain(
        daemon_host="server",
        config_path=None,
        config=config,
        deadline=10.0,
        poll_interval=5.0,
        reconcile=lambda: None,
        board_fetch=lambda: ({}, None),
        now=(lambda ts=[0.0]: (ts.__setitem__(0, ts[0] + 5.0), ts[0])[1]),
        sleep=lambda s: None,
    )
    assert outcome.drained is False
    assert "machine paused" in outcome.detail
    assert "server" in outcome.detail


# ── #2187: a VERIFIED, exit-0 propagate must never be reported as
#    `propagate-failed` — the whole bug this issue is about ─────────────────
#
# The tests above all stub `_run_propagate` wholesale via `_stub_propagate`,
# which bypasses the exact code that was broken: parsing what a REAL
# `coord release propagate --json` subprocess prints. These exercise
# `_parse_trailing_json`, `_latest_propagate_record_since` and
# `_run_propagate` itself directly, then drive the full CLI command with a
# faked subprocess boundary (not `_run_propagate` itself) so the fix is
# proven end to end, the same way the real bug reached production.


def test_parse_trailing_json_reads_a_pretty_printed_indent2_payload():
    """The exact shape `coord release propagate --json` prints
    (`json.dumps(..., indent=2, sort_keys=True)`) — this is the shape the
    old single-line heuristic never matched (#2187's root cause)."""
    import json as _json

    payload = {"status": "verified", "target_version": "0.5.50"}
    stdout = (
        "note: some warning on stdout\n"
        + _json.dumps(payload, indent=2, sort_keys=True)
        + "\n"
    )
    parsed = release_cmd._parse_trailing_json(stdout)
    assert parsed == payload


def test_parse_trailing_json_still_reads_a_compact_single_line_payload():
    import json as _json

    stdout = "some preamble\n" + _json.dumps({"status": "deferred"}, sort_keys=True)
    assert release_cmd._parse_trailing_json(stdout) == {"status": "deferred"}


def test_parse_trailing_json_on_no_json_at_all_is_none():
    assert release_cmd._parse_trailing_json("just some plain log output\nnothing here") is None


def test_parse_trailing_json_on_empty_stdout_is_none():
    assert release_cmd._parse_trailing_json("") is None


def test_latest_propagate_record_since_finds_the_run_just_launched(tmp_path, monkeypatch):
    from coord import release_propagate as rp

    rp.append_record(tmp_path, rp.PropagationRecord(started_at=100.0, status=rp.STATUS_FAILED))
    rp.append_record(tmp_path, rp.PropagationRecord(started_at=200.0, status=rp.STATUS_VERIFIED))
    record = release_cmd._latest_propagate_record_since(tmp_path, 150.0)
    assert record["started_at"] == 200.0
    assert record["status"] == rp.STATUS_VERIFIED


def test_latest_propagate_record_since_ignores_older_runs(tmp_path):
    from coord import release_propagate as rp

    rp.append_record(tmp_path, rp.PropagationRecord(started_at=100.0, status=rp.STATUS_VERIFIED))
    assert release_cmd._latest_propagate_record_since(tmp_path, 150.0) is None


def test_latest_propagate_record_since_on_no_journal_is_none(tmp_path):
    assert release_cmd._latest_propagate_record_since(tmp_path, 0.0) is None


def test_run_propagate_prefers_the_journal_over_stdout(tmp_path):
    """Ground truth (#2187 proposal 1): even if stdout were unparseable, a
    matching journal entry is what decides the status — and it stamps
    `propagate_started_at`, the join key (#2187 proposal 2)."""
    from coord import release_propagate as rp

    def _fake_runner(argv, **kwargs):
        import subprocess as _subprocess

        rp.append_record(
            tmp_path,
            rp.PropagationRecord(started_at=500.0, status=rp.STATUS_VERIFIED,
                                 target_version="0.5.50", finished_at=505.0),
        )
        return _subprocess.CompletedProcess(argv, 0, stdout="not json at all", stderr="")

    status, exit_code, output, started_at = release_cmd._run_propagate(
        daemon_host="dellserver", target_version="0.5.50",
        config_path=tmp_path / "coordinator.yml", state_dir=tmp_path,
        runner=_fake_runner, now_fn=lambda: 499.0,
    )
    assert status == rp.STATUS_VERIFIED
    assert exit_code == 0
    assert started_at == 500.0


def test_run_propagate_falls_back_to_pretty_printed_stdout_when_no_journal_entry(tmp_path):
    """#2187's exact root-cause repro: no journal record can be found (the
    write races or fails), but stdout carries the SAME pretty-printed
    (`indent=2`) `--json` payload the real command emits. The old
    single-line heuristic returned the `f"exit {code}"` placeholder here for
    every successful, exit-0 roll — this must now read `verified` instead."""
    import json as _json
    import subprocess as _subprocess

    def _fake_runner(argv, **kwargs):
        payload = {"status": "verified", "target_version": "0.5.50"}
        stdout = _json.dumps(payload, indent=2, sort_keys=True)
        return _subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    status, exit_code, output, started_at = release_cmd._run_propagate(
        daemon_host="dellserver", target_version="0.5.50",
        config_path=tmp_path / "coordinator.yml", state_dir=tmp_path,
        runner=_fake_runner, now_fn=lambda: 0.0,
    )
    assert status == "verified"
    assert exit_code == 0
    assert started_at is None  # nothing to join to — no journal entry found


def test_run_propagate_with_no_journal_and_no_parseable_stdout_names_the_gap(tmp_path):
    """Neither ground truth is available: falls back to the honest
    `f"exit {code}"` placeholder — the CALLER (below) is responsible for
    turning that into a message that names what's missing, not one that
    misreports it as a real, examined status."""
    import subprocess as _subprocess

    def _fake_runner(argv, **kwargs):
        return _subprocess.CompletedProcess(argv, 0, stdout="garbage, no json", stderr="")

    status, exit_code, output, started_at = release_cmd._run_propagate(
        daemon_host="dellserver", target_version="0.5.50",
        config_path=tmp_path / "coordinator.yml", state_dir=tmp_path,
        runner=_fake_runner, now_fn=lambda: 0.0,
    )
    assert status == "exit 0"
    assert exit_code == 0
    assert started_at is None


def _fake_propagate_subprocess(monkeypatch, state_dir, *, status: str, exit_code: int,
                               target_version: str = "0.5.50", write_journal: bool = True,
                               stderr: str = ""):
    """Stands in for a REAL `python -m coord.cli release propagate --json`
    subprocess: appends the same journal record `_finish` would (#2187's
    ground truth) and returns the SAME pretty-printed (`indent=2`) --json
    stdout shape the real command emits, so the whole `_run_propagate`
    boundary — not just its already-stubbed replacement — is exercised.

    *stderr* stands in for the advisory-finding lines the real command
    writes with `click.echo(..., err=True)` (`release.py` lines 996-1002) —
    #2178 acceptance arm 3 needs those present in `propagate_output` even
    though they never affect *status* or *exit_code*."""
    import json as _json
    import subprocess as _subprocess
    import time as _time

    from coord import release_propagate as rp

    calls = []

    def _fake_run(argv, **kwargs):
        calls.append(argv)
        if write_journal:
            # A REAL `started_at` — not a fixed constant — because
            # `_run_propagate` compares this against `time.time()` captured
            # right before launch (`_latest_propagate_record_since`'s
            # `since`); a hardcoded past timestamp would look like an OLDER,
            # unrelated run and be filtered out exactly like a real stale
            # entry would be.
            rp.append_record(
                state_dir,
                rp.PropagationRecord(
                    started_at=_time.time(), target_version=target_version,
                    status=status, finished_at=_time.time(),
                ),
            )
        payload = {"status": status, "target_version": target_version}
        stdout = _json.dumps(payload, indent=2, sort_keys=True)
        return _subprocess.CompletedProcess(argv, exit_code, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(_subprocess, "run", _fake_run)
    return calls


def test_window_end_to_end_a_verified_roll_is_never_reported_as_failed(
    valid_config_path, state_dir, no_network, escalations, monkeypatch
):
    """#2187 acceptance arm 1: a propagate that exits 0 and records
    `verified` produces a clean window-history entry and a clean exit —
    through the REAL `_run_propagate`, not a stub of it."""
    _stub_verify(monkeypatch, daemon_version="0.5.49")
    _stub_systemctl(monkeypatch)
    _stub_drain(monkeypatch, drained=True)
    _fake_propagate_subprocess(monkeypatch, state_dir, status=rp.STATUS_VERIFIED,
                               exit_code=0, target_version="0.5.50")

    result = CliRunner().invoke(
        main,
        ["release", "nightly-window", "--config", str(valid_config_path),
         "--target", "0.5.50", "--daemon-host", "server"],
    )
    assert result.exit_code == 0, result.output
    record = _records(state_dir)[0]
    assert record["status"] == rw.STATUS_ROLLED
    assert record["status"] in rw.OK_STATUSES
    assert record["propagate_status"] == rp.STATUS_VERIFIED
    # The join key (#2187 proposal 2): stamped from the propagation
    # journal's OWN `started_at`, proving `window-history` can now be
    # correlated to `history` for this exact run.
    assert record["propagate_started_at"] is not None
    assert record["propagate_started_at"] > 0
    assert not record["error"]
    assert not escalations


def test_window_end_to_end_a_genuine_failure_is_still_reported_failed(
    valid_config_path, state_dir, no_network, escalations, monkeypatch
):
    """#2187 acceptance arm 2: a propagate that genuinely fails still
    produces `propagate-failed` and a non-zero exit — the fix must not turn
    every outcome green."""
    _stub_verify(monkeypatch, daemon_version="0.5.49")
    _stub_systemctl(monkeypatch)
    _stub_drain(monkeypatch, drained=True)
    _fake_propagate_subprocess(monkeypatch, state_dir, status=rp.STATUS_FAILED,
                               exit_code=1, target_version="0.5.50")

    result = CliRunner().invoke(
        main,
        ["release", "nightly-window", "--config", str(valid_config_path),
         "--target", "0.5.50", "--daemon-host", "server"],
    )
    assert result.exit_code == 1, result.output
    record = _records(state_dir)[0]
    assert record["status"] == rw.STATUS_PROPAGATE_FAILED
    assert record["status"] not in rw.OK_STATUSES
    assert record["propagate_status"] == rp.STATUS_FAILED
    assert len(escalations) == 1


def test_window_end_to_end_an_unconfirmable_exit_0_names_the_missing_evidence(
    valid_config_path, state_dir, no_network, escalations, monkeypatch
):
    """No journal entry AND no parseable stdout, despite exit 0 (#2187
    proposal 3): the error must name the specific missing artifacts, never
    read as `status=exit 0, exit=0` with nothing further explained."""
    import subprocess as _subprocess

    _stub_verify(monkeypatch, daemon_version="0.5.49")
    _stub_systemctl(monkeypatch)
    _stub_drain(monkeypatch, drained=True)

    def _fake_run(argv, **kwargs):
        return _subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(_subprocess, "run", _fake_run)

    result = CliRunner().invoke(
        main,
        ["release", "nightly-window", "--config", str(valid_config_path),
         "--target", "0.5.50", "--daemon-host", "server"],
    )
    assert result.exit_code != 0, result.output
    record = _records(state_dir)[0]
    assert record["status"] == rw.STATUS_PROPAGATE_FAILED
    assert "status=exit 0, exit=0" not in (record["error"] or "")
    assert "no matching entry" in (record["error"] or "")
    assert "no parseable" in (record["error"] or "")
    assert len(escalations) == 1


def test_window_end_to_end_an_advisory_only_gate_is_still_a_success(
    valid_config_path, state_dir, no_network, escalations, monkeypatch
):
    """#2178 acceptance arm 3: a lane propagation structurally cannot roll
    (`~/.coord-cli-venv`, stale on some OTHER host) makes `coord release
    verify` read CRIT, but `release_propagate.scope_verification` classifies
    that finding as ADVISORY rather than blocking (`release_propagate.py`
    lines 404-455) — so the real subprocess exits 0 with `status=verified`
    regardless. The window must record that as a plain success, not pin the
    fleet's release status to failed for as long as that one lane stays
    stale (#2178's point 2: "advisory lanes must not fail the run").

    The advisory finding itself must not be swallowed either — it travels
    in `propagate_output`, exactly as the real subprocess's own stderr
    would carry it, so a human reading `window-history` sees the lane that
    needs fixing by hand without needing to cross-reference `coord release
    history` separately."""
    advisory_line = (
        "  ~ advisory [crit] elitebook ~/.coord-cli-venv: on 0.5.46, expected "
        "0.5.49 — outside propagation's reach, fix by hand"
    )
    _stub_verify(monkeypatch, daemon_version="0.5.49")
    _stub_systemctl(monkeypatch)
    _stub_drain(monkeypatch, drained=True)
    _fake_propagate_subprocess(monkeypatch, state_dir, status=rp.STATUS_VERIFIED,
                               exit_code=0, target_version="0.5.50", stderr=advisory_line)

    result = CliRunner().invoke(
        main,
        ["release", "nightly-window", "--config", str(valid_config_path),
         "--target", "0.5.50", "--daemon-host", "server"],
    )
    assert result.exit_code == 0, result.output
    record = _records(state_dir)[0]
    assert record["status"] == rw.STATUS_ROLLED
    assert record["status"] in rw.OK_STATUSES
    assert record["propagate_status"] == rp.STATUS_VERIFIED
    assert not record["error"]
    assert advisory_line.strip() in record["propagate_output"]
    assert not escalations


def test_window_never_prints_a_zero_exit_code_next_to_a_failure_assertion(
    valid_config_path, state_dir, no_network, escalations, monkeypatch
):
    """#2178 point 3: whatever the window says about a run, `exit 0` must
    never appear next to language asserting the roll didn't happen — that
    exact contradiction ("did not verify a roll ... exit=0") is what made
    diagnosing #2178 take real time on a verified, successful roll.

    Drives the one arm where a real `exit 0` and a FAILED window status
    legitimately coexist — no journal entry and no parseable stdout, so the
    outcome is genuinely unconfirmable rather than known-bad — and checks
    it two ways: the specific honest wording is present, and (generically,
    scanning every line of output) no line anywhere pairs an exit-0 mention
    with either failure phrase."""
    import subprocess as _subprocess

    _stub_verify(monkeypatch, daemon_version="0.5.49")
    _stub_systemctl(monkeypatch)
    _stub_drain(monkeypatch, drained=True)

    def _fake_run(argv, **kwargs):
        return _subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(_subprocess, "run", _fake_run)

    result = CliRunner().invoke(
        main,
        ["release", "nightly-window", "--config", str(valid_config_path),
         "--target", "0.5.50", "--daemon-host", "server"],
    )
    assert result.exit_code != 0, result.output
    record = _records(state_dir)[0]
    assert record["status"] == rw.STATUS_PROPAGATE_FAILED
    error = record["error"] or ""
    # The only sanctioned way for "exit 0" and a failed status to appear
    # together: naming the exact missing evidence, never asserting the roll
    # itself didn't happen.
    assert "exited 0, but its outcome could not be confirmed" in error
    assert "did not verify a roll" not in error
    for line in (result.output or "").splitlines():
        if "exit 0" in line or "exit=0" in line:
            assert "did not verify a roll" not in line
            assert "propagate-failed" not in line
