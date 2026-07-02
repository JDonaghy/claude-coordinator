"""Unit tests for ``coord.interactive``'s pre-fill/verify/retry state machine (#865).

#865 root cause: both briefing-injection paths pasted once after a bare
render-quiescence heuristic and never verified the paste actually landed —
an async startup banner ("Fable 5 is back", MCP/auth notices) painting over
several seconds could silently discard a mistimed paste, and there was no
retry.  ``_PrefillState`` / ``_prefill_on_master_data`` / ``_prefill_step``
are the PTY-relay path's (``_launch_via_pty``) pure decision logic, split out
specifically so this behaviour can be tested without a real pty or any
``time.sleep()`` — every check below drives the state machine with explicit
``now`` values.

The tmux path's equivalent behaviour (``_inject_briefing_into_tmux_session``)
is covered separately in ``tests/test_cli_reattach_sessions.py``; the
agent-side PTY spawn path (``coord.agent._spawn_pty``) has a real-pty
black-box fixture in ``tests/test_providers_pty.py``
(``test_pty_spawn_retries_after_banner_interrupted_dropped_paste``).
"""

from __future__ import annotations

import logging
import os
import re
import sys

import pytest

from coord.interactive import (
    _INJECT_MAX_ATTEMPTS,
    _READY_QUIESCE_CAP_S,
    _READY_QUIESCE_NO_MARKER_S,
    _READY_QUIESCE_S,
    _PrefillState,
    _launch_via_pty,
    _prefill_on_master_data,
    _prefill_step,
)
from coord.providers.claude_pty import (
    BRACKETED_PASTE_END,
    BRACKETED_PASTE_ENABLE,
    BRACKETED_PASTE_START,
    INPUT_BOX_MARKER,
    INPUT_BOX_MARKER_BYTES,
    briefing_fingerprint,
)


def _state(fingerprint: str = "hello world", started: float = 1000.0) -> _PrefillState:
    return _PrefillState(fingerprint=fingerprint, started=started, last_master_activity=started)


# ── Readiness: bracketed-paste-enable + marker + quiescence ──────────────────


def test_no_paste_before_bracketed_paste_enable_seen() -> None:
    """Without the enable DECSET, _prefill_step never says "paste" — even
    with the marker present and plenty of elapsed quiescence."""
    state = _state(started=0.0)
    _prefill_on_master_data(state, f"{INPUT_BOX_MARKER} box".encode(), now=0.1)
    assert _prefill_step(state, now=0.1 + _READY_QUIESCE_S + 1.0) is False
    assert not state.done


def test_no_paste_before_marker_or_extended_quiescence() -> None:
    """Enable seen, but no marker AND not yet past the no-marker fallback
    window — still not ready."""
    state = _state(started=0.0)
    _prefill_on_master_data(state, BRACKETED_PASTE_ENABLE + b"loading...", now=0.1)
    assert _prefill_step(state, now=0.1 + 0.5) is False  # short of both thresholds


def test_pastes_quickly_once_marker_present_and_quiescent() -> None:
    """Marker present + enable seen + quiescent for _READY_QUIESCE_S → paste now."""
    state = _state(started=0.0)
    data = BRACKETED_PASTE_ENABLE + f"{INPUT_BOX_MARKER} placeholder".encode()
    _prefill_on_master_data(state, data, now=0.1)
    assert _prefill_step(state, now=0.1 + _READY_QUIESCE_S + 0.01) is True
    assert state.paste_attempts == 1
    assert state.verify_deadline is not None


def test_no_marker_falls_back_to_longer_quiescence_window() -> None:
    """No marker ever appears (older CLI / unusual render / dead process) —
    still pastes, but only after the LONGER no-marker window, not the short
    marker-confident one and not stuck until the overall cap (#865
    regression guard: this must NOT take _READY_QUIESCE_CAP_S)."""
    state = _state(started=0.0)
    _prefill_on_master_data(state, BRACKETED_PASTE_ENABLE + b"static, no box", now=0.1)

    # Short window: not ready yet (no marker).
    assert _prefill_step(state, now=0.1 + _READY_QUIESCE_S + 0.01) is False
    # Long no-marker window: ready.
    assert _prefill_step(state, now=0.1 + _READY_QUIESCE_NO_MARKER_S + 0.01) is True
    assert state.paste_attempts == 1


def test_overall_cap_pastes_even_with_no_readiness_signal_at_all() -> None:
    """Degraded fallback: even with no enable, no marker, no quiescence,
    the overall cap still forces a paste rather than hanging forever."""
    state = _state(started=0.0)
    assert _prefill_step(state, now=_READY_QUIESCE_CAP_S + 0.01) is True
    assert state.paste_attempts == 1


# ── Verify + retry ────────────────────────────────────────────────────────────


def test_verified_success_stops_the_machine() -> None:
    """After a paste, if the fingerprint shows up in the screen buffer by
    the verify deadline, the machine reports done with no further pastes."""
    state = _state(fingerprint="hello world", started=0.0)
    state.verify_deadline = 5.0
    state.paste_attempts = 1
    _prefill_on_master_data(state, b"hello world rendered", now=4.9)
    assert _prefill_step(state, now=5.01) is False
    assert state.done is True
    assert state.paste_attempts == 1  # no retry needed


def test_verified_success_via_paste_chip_not_just_literal_fingerprint() -> None:
    """#896 review follow-up: ``_prefill_step`` (the third of the three #896
    call-sites — tmux and the agent.py PTY relay were fixed first, this one
    was missed) must use the broadened ``paste_landed_bytes`` predicate, not
    the bare literal-fingerprint check.  A large briefing that Claude Code
    collapses into a paste-chip (``[Pasted text #1 +NNN lines]``) never
    renders its literal fingerprint text at all — only ``paste_landed_bytes``
    (chip-aware) reports this as landed; the old ``fingerprint_in_bytes``
    would spin through all retries and give up (the exact #896 bug)."""
    state = _state(fingerprint=briefing_fingerprint("x" * 200), started=0.0)
    state.verify_deadline = 5.0
    state.paste_attempts = 1
    chip = INPUT_BOX_MARKER_BYTES + b" [Pasted text #1 +58 lines]\r\n"
    _prefill_on_master_data(state, chip, now=4.9)
    assert _prefill_step(state, now=5.01) is False
    assert state.done is True
    assert state.paste_attempts == 1  # verified on the first attempt, no retry


def test_verification_miss_schedules_a_retry() -> None:
    """A miss (fingerprint absent) schedules another attempt after the
    backoff — it does NOT give up after just one try."""
    state = _state(fingerprint="hello world", started=0.0)
    state.verify_deadline = 5.0
    state.paste_attempts = 1
    # No matching data written — the screen buffer never gets the fingerprint.
    assert _prefill_step(state, now=5.01) is False  # resolves the verify window
    assert state.done is False
    assert state.next_paste_at is not None
    assert state.verify_deadline is None

    # The retry fires once next_paste_at elapses.
    assert _prefill_step(state, now=state.next_paste_at + 0.001) is True
    assert state.paste_attempts == 2
    assert state.verify_deadline is not None


def test_exhausting_all_attempts_gives_up_and_logs(caplog) -> None:
    """After _INJECT_MAX_ATTEMPTS misses, the machine gives up (marks
    ``done``) and logs a hard failure — #865's "never silent" requirement."""
    state = _state(fingerprint="hello world", started=0.0)
    # Skip past readiness (covered by the earlier tests) — force the first
    # attempt to fire immediately so this test is purely about the
    # verify/retry/give-up sequence.
    state.next_paste_at = 0.0
    now = 0.0
    for attempt in range(1, _INJECT_MAX_ATTEMPTS + 1):
        # Fire the (attempt)th paste.
        assert _prefill_step(state, now=now) is True
        assert state.paste_attempts == attempt
        now = state.verify_deadline
        with caplog.at_level(logging.ERROR):
            fired = _prefill_step(state, now=now + 0.001)
        if attempt < _INJECT_MAX_ATTEMPTS:
            assert fired is False
            assert state.done is False
            now = state.next_paste_at
        else:
            assert fired is False
            assert state.done is True

    assert state.paste_attempts == _INJECT_MAX_ATTEMPTS
    assert any(
        "unverified" in r.message and "PTY relay" in r.message for r in caplog.records
    ), f"expected a hard-failure log record, got: {[r.message for r in caplog.records]}"


def test_done_state_machine_is_inert() -> None:
    """Once ``done``, _prefill_step never says paste again, regardless of
    what further master data arrives."""
    state = _state(started=0.0)
    state.done = True
    _prefill_on_master_data(state, b"anything at all", now=1.0)
    assert _prefill_step(state, now=1000.0) is False


# ── screen_buf cap ────────────────────────────────────────────────────────────


def test_screen_buf_is_capped_not_unbounded() -> None:
    """The rolling screen buffer used for readiness/verification is capped —
    a long-lived relay session must not grow it unboundedly."""
    from coord.interactive import _PREFILL_SCREEN_BUF_CAP

    state = _state(started=0.0)
    chunk = b"x" * 4096
    for i in range(20):  # 20 * 4096 = 81920 bytes, well over the cap
        _prefill_on_master_data(state, chunk, now=float(i))
    assert len(state.screen_buf) <= _PREFILL_SCREEN_BUF_CAP


# ── briefing_fingerprint / whitespace normalization sanity ───────────────────


def test_fingerprint_matches_rewrapped_whitespace() -> None:
    """The fingerprint check tolerates the terminal re-wrapping the pasted
    text across lines — whitespace is collapsed on both sides."""
    from coord.providers.claude_pty import fingerprint_in_bytes

    fp = briefing_fingerprint("please review\nPR 42 carefully")
    rewrapped = b"please review\r\n   PR 42   carefully rendered on screen"
    assert fingerprint_in_bytes(rewrapped, fp) is True


# ── Real-pty wiring smoke test ────────────────────────────────────────────────
#
# The unit tests above prove the state machine's DECISIONS are correct.  This
# test proves those decisions are wired correctly into the actual
# ``_launch_via_pty`` relay loop — a real ``pty.fork()``, a real child
# process, no mocking of ``_prefill_step``/``_prefill_on_master_data``
# themselves.


class _PipedStdout:
    """Minimal stdout shim whose ``fileno()`` returns a caller-supplied fd
    (mirrors ``tests/test_interactive_briefing_echo.py``)."""

    def __init__(self, fd: int) -> None:
        self._fd = fd

    def fileno(self) -> int:
        return self._fd


class _PipedStdin:
    """Minimal stdin shim — not a TTY, so ``termios.tcgetattr`` fails and
    ``_launch_via_pty`` skips raw-mode setup, same as piped-input tests
    elsewhere in this suite."""

    def __init__(self, fd: int) -> None:
        self._fd = fd

    def fileno(self) -> int:
        return self._fd


def _drain_pipe(r_fd: int) -> bytes:
    chunks: list[bytes] = []
    while True:
        chunk = os.read(r_fd, 4096)
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks)


# Mock ``claude``: renders the input box, then (after it's already up) an
# async banner arrives — the #865 scenario — before finally accepting the
# pre-fill.  tlen is hardcoded (not derived from the literal briefing text)
# so the briefing never leaks into the script's own source, which would
# otherwise produce a false-positive fingerprint match (see the analogous
# comment on ``_BANNER_INTERRUPT_PTY_MOCK`` in test_providers_pty.py).
_CHILD_SCRIPT = (
    "import sys, os, select, time, tty\n"
    "tty.setraw(0)\n"
    "sys.stdout.write('\\x1b[?2004h')\n"
    f"sys.stdout.write('{INPUT_BOX_MARKER} placeholder\\n')\n"
    "sys.stdout.flush()\n"
    "time.sleep(0.2)\n"
    "sys.stdout.write('late banner notice\\n')\n"
    "sys.stdout.flush()\n"
    "tlen = 19\n"
    "buf = b''\n"
    "deadline = time.monotonic() + 5.0\n"
    "while len(buf) < tlen and time.monotonic() < deadline:\n"
    "    r, _, _ = select.select([0], [], [], 0.05)\n"
    "    if r:\n"
    "        chunk = os.read(0, tlen - len(buf))\n"
    "        if not chunk:\n"
    "            break\n"
    "        buf += chunk\n"
    "sys.stdout.write('got=' + buf.decode('utf-8', errors='replace') + '\\n')\n"
    "sys.stdout.flush()\n"
)


def test_launch_via_pty_lands_briefing_after_banner_interrupt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end (real ``pty.fork()``, real child process) through the
    ACTUAL ``_launch_via_pty`` relay loop: the input box renders, a late
    async banner arrives, and the briefing still lands.  This is the #865
    acceptance-criterion fixture for the interactive-launcher (no-tmux) PTY
    path — the agent-side twin lives in test_providers_pty.py."""
    out_r, out_w = os.pipe()
    in_r, in_w = os.pipe()  # write-end kept open, never written to
    try:
        monkeypatch.setattr(sys, "stdout", _PipedStdout(out_w))
        monkeypatch.setattr(sys, "stdin", _PipedStdin(in_r))

        rc = _launch_via_pty([sys.executable, "-c", _CHILD_SCRIPT], "ECHO_ME")

        os.close(out_w)
        out_w = -1
        captured = _drain_pipe(out_r)
    finally:
        if out_w != -1:
            try:
                os.close(out_w)
            except OSError:
                pass
        for fd in (out_r, in_r, in_w):
            try:
                os.close(fd)
            except OSError:
                pass

    assert rc == 0, f"child exited non-zero; captured={captured!r}"
    assert b"late banner notice" in captured, (
        f"banner line missing — readiness didn't observe it: {captured!r}"
    )
    assert b"got=" in captured and b"ECHO_ME" in captured, (
        f"briefing never landed in the (real) relay loop: {captured!r}"
    )


# Mock ``claude`` for the retry-clears-input-box fixture below: renders the
# input box but never echoes anything that would let ``paste_landed_bytes``
# verify a landed paste, so the relay is forced through every one of
# ``_INJECT_MAX_ATTEMPTS`` — deterministic, no timing race on when/whether
# the fingerprint happens to show up in the child's own output.  It just
# accumulates every byte it reads on stdin (across all 3 attempts) and dumps
# the lot back as hex once it's read enough (or a generous deadline elapses).
_RETRY_CHILD_SCRIPT = (
    "import sys, os, select, time, tty\n"
    "tty.setraw(0)\n"
    "sys.stdout.write('\\x1b[?2004h')\n"
    f"sys.stdout.write('{INPUT_BOX_MARKER} placeholder\\n')\n"
    "sys.stdout.flush()\n"
    "wanted = 64\n"
    "buf = b''\n"
    "deadline = time.monotonic() + 8.0\n"
    "while len(buf) < wanted and time.monotonic() < deadline:\n"
    "    r, _, _ = select.select([0], [], [], 0.05)\n"
    "    if r:\n"
    "        chunk = os.read(0, wanted - len(buf))\n"
    "        if not chunk:\n"
    "            break\n"
    "        buf += chunk\n"
    # Stay alive (don't exit yet) so the parent relay's WNOHANG child-exit
    # check doesn't race ahead of the 3rd verify_deadline / give-up log —
    # exiting immediately after the 3rd paste lands would let the relay
    # observe the exit and break out of its loop before ever re-driving
    # _prefill_step past that final verify window.
    "time.sleep(1.5)\n"
    "sys.stdout.write('got=' + buf.hex() + '\\n')\n"
    "sys.stdout.flush()\n"
)


def test_launch_via_pty_clears_input_box_before_retry_paste(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """#896 review follow-up: on a retry (the previous paste didn't verify),
    the PTY relay must clear the input box (Escape + Ctrl-U) before
    re-pasting — otherwise a false-negative on an already-landed paste
    stacks duplicate paste chips.  This mirrors the idempotent-retry fix
    already applied to the tmux path (``_paste_once`` in this module) and
    the remote PTY relay (``coord.agent``'s ``_spawn_pty``); this end-to-end
    fixture (real ``pty.fork()``, real child process, no mocking of
    ``_prefill_step``) proves it's wired into ``_launch_via_pty`` too.

    The mock child never echoes anything ``paste_landed_bytes`` would
    recognise, so all ``_INJECT_MAX_ATTEMPTS`` (3) fire deterministically —
    the test asserts the child received: paste, clear, paste, clear, paste.
    """
    briefing = "RETRY_ME"
    paste_block = BRACKETED_PASTE_START + briefing.encode("utf-8") + BRACKETED_PASTE_END
    clear_bytes = b"\x1b\x15"  # Escape + Ctrl-U
    expected = paste_block + clear_bytes + paste_block + clear_bytes + paste_block

    out_r, out_w = os.pipe()
    in_r, in_w = os.pipe()
    try:
        monkeypatch.setattr(sys, "stdout", _PipedStdout(out_w))
        monkeypatch.setattr(sys, "stdin", _PipedStdin(in_r))

        with caplog.at_level(logging.ERROR):
            rc = _launch_via_pty([sys.executable, "-c", _RETRY_CHILD_SCRIPT], briefing)

        os.close(out_w)
        out_w = -1
        captured = _drain_pipe(out_r)
    finally:
        if out_w != -1:
            try:
                os.close(out_w)
            except OSError:
                pass
        for fd in (out_r, in_r, in_w):
            try:
                os.close(fd)
            except OSError:
                pass

    assert rc == 0, f"child exited non-zero; captured={captured!r}"
    match = re.search(rb"got=([0-9a-f]*)\n", captured)
    assert match is not None, f"child never dumped what it received: {captured!r}"
    received = bytes.fromhex(match.group(1).decode())
    assert received == expected, (
        f"expected paste/clear/paste/clear/paste, got {received!r}"
    )
    assert any(
        "unverified after 3 attempt" in r.message and "PTY relay" in r.message
        for r in caplog.records
    ), f"expected the exhausted-retries hard-failure log, got: {[r.message for r in caplog.records]}"
