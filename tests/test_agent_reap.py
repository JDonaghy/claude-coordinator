"""Tests for the reap-thread helpers added in #228.

Covers `_log_has_result` and the `_wait_for_proc_or_result` polling loop
that handles claude-cli sessions where `proc.wait()` hangs after the
worker has emitted its final result event.
"""

from __future__ import annotations

import signal
import subprocess
from pathlib import Path
from typing import Callable, List, Tuple

from coord.agent import (
    _log_has_result,
    _wait_for_proc_or_result,
    _RESULT_LINE_MARKER,
)


# ── _log_has_result ──────────────────────────────────────────────────────────

def test_log_has_result_finds_marker(tmp_path: Path) -> None:
    log = tmp_path / "log"
    log.write_text(
        '{"type":"assistant","message":{}}\n'
        '{"type":"result","subtype":"success","is_error":false}\n'
    )
    assert _log_has_result(str(log))


def test_log_has_result_false_when_marker_absent(tmp_path: Path) -> None:
    log = tmp_path / "log"
    log.write_text(
        '{"type":"assistant","message":{}}\n'
        '{"type":"user","message":{}}\n'
    )
    assert not _log_has_result(str(log))


def test_log_has_result_false_for_missing_file(tmp_path: Path) -> None:
    assert not _log_has_result(str(tmp_path / "nonexistent"))


def test_log_has_result_handles_binary_safe_scan(tmp_path: Path) -> None:
    """The marker is a byte-string; binary scan must not mis-decode utf-8."""
    log = tmp_path / "log"
    log.write_bytes(b'\xff\xfe' + _RESULT_LINE_MARKER + b'\xff')
    assert _log_has_result(str(log))


# ── _wait_for_proc_or_result ─────────────────────────────────────────────────

class _FakeProc:
    """Minimal Popen stand-in: exits after N wait() calls or never exits.

    Also accepts kill signals via the recorded `killpg` callable so tests can
    assert what was sent without invoking the OS.
    """

    def __init__(
        self,
        *,
        exit_after_calls: int | None = None,
        exit_code: int = 0,
        exit_after_kill: bool = True,
    ) -> None:
        self.pid = 99999
        self._wait_calls = 0
        self._exit_after_calls = exit_after_calls
        self._exit_code = exit_code
        self._exit_after_kill = exit_after_kill
        self._was_killed = False

    def wait(self, timeout: float | None = None) -> int:
        self._wait_calls += 1
        # If we've been killed and the proc honors kill, return on next wait.
        if self._was_killed and self._exit_after_kill:
            return self._exit_code
        # Otherwise return when call count is reached, else TimeoutExpired.
        if self._exit_after_calls is not None and self._wait_calls >= self._exit_after_calls:
            return self._exit_code
        raise subprocess.TimeoutExpired("fake", timeout)

    def mark_killed(self) -> None:
        self._was_killed = True


def _make_killpg_recorder() -> Tuple[Callable[[int, int], None], List[Tuple[int, int]], _FakeProc | None]:
    """Build a killpg callable that records (pid, sig) calls and, if a proc is
    set later, notifies it so its next `wait()` returns."""
    calls: List[Tuple[int, int]] = []
    proc_holder: List[_FakeProc] = []

    def record(pid: int, sig: int) -> None:
        calls.append((pid, sig))
        if proc_holder:
            proc_holder[0].mark_killed()

    def set_proc(p: _FakeProc) -> None:
        proc_holder.append(p)

    return record, calls, set_proc  # type: ignore[return-value]


def _fake_clock() -> Callable[[], float]:
    """A controllable clock — call returned ticker to advance time."""
    state = {"t": 0.0}

    def now() -> float:
        return state["t"]

    def advance(delta: float) -> None:
        state["t"] += delta

    now.advance = advance  # type: ignore[attr-defined]
    return now


def test_clean_exit_returns_proc_exit_code(tmp_path: Path) -> None:
    """When proc exits normally on the first wait, return its exit code."""
    proc = _FakeProc(exit_after_calls=1, exit_code=42)
    killpg = lambda pid, sig: None  # noqa: E731 — should never fire
    code = _wait_for_proc_or_result(
        proc,  # type: ignore[arg-type]
        str(tmp_path / "log"),
        poll_interval=0.01,
        grace_after_result=0.5,
        max_wait=10.0,
        killpg=killpg,
        log_has_result=lambda _: False,
    )
    assert code == 42


def test_force_kills_when_result_seen_then_proc_hangs(tmp_path: Path) -> None:
    """The fix for #228: worker emits result, proc.wait keeps timing out,
    so we SIGTERM the group and return 0."""
    log_path = str(tmp_path / "log")
    proc = _FakeProc(exit_after_calls=None, exit_after_kill=True, exit_code=0)
    record, calls, set_proc = _make_killpg_recorder()
    set_proc(proc)

    code = _wait_for_proc_or_result(
        proc,  # type: ignore[arg-type]
        log_path,
        poll_interval=0.01,
        grace_after_result=0.0,  # immediately escalate after result seen
        max_wait=10.0,
        killpg=record,
        log_has_result=lambda _: True,  # claim result already in log
    )
    assert code == 0
    # SIGTERM must have fired on the process group.
    assert calls and calls[0] == (proc.pid, signal.SIGTERM)
    # Log should mention the escalation so an operator can diagnose.
    text = Path(log_path).read_text()
    assert "SIGTERM" in text


def test_escalates_to_sigkill_when_sigterm_ignored(tmp_path: Path) -> None:
    """If SIGTERM doesn't bring the proc down, SIGKILL follows."""
    log_path = str(tmp_path / "log")
    # exit_after_kill=False → even after being killed, wait keeps timing out.
    proc = _FakeProc(exit_after_calls=None, exit_after_kill=False)
    calls: List[Tuple[int, int]] = []
    code = _wait_for_proc_or_result(
        proc,  # type: ignore[arg-type]
        log_path,
        poll_interval=0.01,
        grace_after_result=0.0,
        max_wait=10.0,
        killpg=lambda pid, sig: calls.append((pid, sig)),
        log_has_result=lambda _: True,
    )
    assert code == 0  # logical completion → success
    sigs = [s for _, s in calls]
    assert signal.SIGTERM in sigs
    assert signal.SIGKILL in sigs


def test_max_wait_safety_net_sigkills_and_marks_failed(tmp_path: Path) -> None:
    """If the worker never emits a result, max_wait elapses and we SIGKILL."""
    log_path = str(tmp_path / "log")
    proc = _FakeProc(exit_after_calls=None, exit_after_kill=True, exit_code=-9)
    record, calls, set_proc = _make_killpg_recorder()
    set_proc(proc)
    clock = _fake_clock()

    # Make poll_interval advance the clock so max_wait can be reached.
    real_wait = proc.wait

    def wait_advances(timeout: float | None = None) -> int:
        clock.advance(timeout or 0.0)  # type: ignore[attr-defined]
        return real_wait(timeout=timeout)

    proc.wait = wait_advances  # type: ignore[assignment]

    code = _wait_for_proc_or_result(
        proc,  # type: ignore[arg-type]
        log_path,
        poll_interval=1.0,
        grace_after_result=0.5,
        max_wait=2.0,  # → 3rd iteration trips the safety net
        killpg=record,
        log_has_result=lambda _: False,  # never emits result
        clock=clock,
    )
    assert code == 137  # SIGKILL convention
    assert (proc.pid, signal.SIGKILL) in calls
    assert "max-wait" in Path(log_path).read_text()


def test_result_detected_mid_wait_only_kills_after_grace(tmp_path: Path) -> None:
    """If the worker emits the result mid-wait but might still exit cleanly,
    we wait the grace period before escalating."""
    log_path = str(tmp_path / "log")
    proc = _FakeProc(exit_after_calls=None, exit_after_kill=True)
    record, calls, set_proc = _make_killpg_recorder()
    set_proc(proc)
    clock = _fake_clock()

    real_wait = proc.wait

    def wait_advances(timeout: float | None = None) -> int:
        clock.advance(timeout or 0.0)  # type: ignore[attr-defined]
        return real_wait(timeout=timeout)

    proc.wait = wait_advances  # type: ignore[assignment]

    # Trigger log_has_result True only after the 2nd iteration.
    state = {"calls": 0}

    def has_result(_path: str) -> bool:
        state["calls"] += 1
        return state["calls"] >= 2

    code = _wait_for_proc_or_result(
        proc,  # type: ignore[arg-type]
        log_path,
        poll_interval=0.5,
        grace_after_result=1.0,  # need 1s elapsed after result seen
        max_wait=10.0,
        killpg=record,
        log_has_result=has_result,
        clock=clock,
    )
    assert code == 0
    # The SIGTERM should not have fired on the first iteration where result
    # wasn't yet detected.
    assert calls, "expected SIGTERM to fire eventually"
