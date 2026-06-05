"""Tests for #426 — PTY completion detection, termination, and log parsing.

Three layers:

* ``ClaudePtyProvider.parse_log`` / :func:`_parse_pty_log` on canned and
  REAL captured TTY logs.  The real captures (``tests/fixtures/
  pty_clean_finish.log`` and ``tests/fixtures/pty_cost_command.log``) were
  produced by spawning the live ``claude`` v2.1.160 binary in a PTY and
  driving it through a trivial turn — they exercise the ANSI-strip + cost
  extraction codepaths against the actual byte stream a real PTY worker
  produces.
* :func:`coord.agent._pty_completion_watcher` — mocked process + log
  fixtures, asserts the sentinel / STUCK / idle-backstop / max-wait
  paths all kill the process group.
* The ``claude -p`` path is unchanged — no completion watcher is started
  for the legacy spawn route (verified indirectly: the watcher is only
  threaded out of :meth:`AgentServer._spawn_pty`, not from
  :meth:`AgentServer._spawn`).
"""

from __future__ import annotations

import signal
import threading
import time
from pathlib import Path

import pytest

from coord.agent import (
    _PTY_COMPLETION_MAX_WAIT_S,
    _PTY_IDLE_PROMPT_GLYPH,
    _pty_completion_watcher,
)
from coord.providers.claude_pty import (
    COMPLETION_SENTINEL,
    ClaudePtyProvider,
    _AUTONOMOUS_SPEC_TYPES,
    _parse_pty_log,
    _strip_ansi,
)


# ── Fixtures ────────────────────────────────────────────────────────────────


FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _write_log(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# A tiny fake of subprocess.Popen for the watcher tests.  Only exposes the
# attributes / methods the watcher touches: ``pid``, ``poll()``, ``wait()``.
class _FakeProc:
    def __init__(self, pid: int = 9999) -> None:
        self.pid = pid
        self._exited = False
        self._exit_code: int | None = None
        # `wait()` is called after killpg; tests use `set_exit` to make it
        # return promptly, modelling a process that died on SIGTERM.
        self._wait_returns: int | None = None

    def poll(self) -> int | None:
        return self._exit_code if self._exited else None

    def wait(self, timeout: float | None = None) -> int:
        if self._wait_returns is not None:
            self._exited = True
            self._exit_code = self._wait_returns
            return self._wait_returns
        raise TimeoutError("not exiting in this test")

    def set_exit(self, code: int = 0) -> None:
        self._wait_returns = code


# ── _strip_ansi unit ────────────────────────────────────────────────────────


def test_strip_ansi_removes_csi_sequences() -> None:
    """Strips ESC[…m colour codes and other CSI families."""
    raw = "\x1b[1mhello\x1b[0m world\x1b[32m!\x1b[0m"
    assert _strip_ansi(raw) == "hello world!"


def test_strip_ansi_removes_osc_sequences() -> None:
    """Strips ESC]…BEL OSC sequences (terminal title-setting etc.)."""
    raw = "before\x1b]0;some title\x07after"
    assert _strip_ansi(raw) == "beforeafter"


def test_strip_ansi_preserves_plain_text() -> None:
    assert _strip_ansi("plain text\nwith newlines\n") == "plain text\nwith newlines\n"


def test_strip_ansi_handles_save_restore_cursor() -> None:
    """ESC7/ESC8 (save/restore cursor) and other single-char escapes are stripped."""
    raw = "\x1b7stuff\x1b8more"
    assert _strip_ansi(raw) == "stuffmore"


# ── _parse_pty_log: hand-built fixtures ─────────────────────────────────────


def test_parse_pty_log_missing_file_returns_blank(tmp_path: Path) -> None:
    """A missing log file degrades to a blank WorkerSummary."""
    summary = _parse_pty_log(tmp_path / "nope.log")
    assert summary.stop_reason is None
    assert summary.total_cost_usd == 0.0


def test_parse_pty_log_clean_finish_with_sentinel(tmp_path: Path) -> None:
    """A log containing the sentinel sets stop_reason='end_turn'."""
    log = tmp_path / "w.log"
    _write_log(log, "● OK\n" + COMPLETION_SENTINEL + "\n")
    summary = _parse_pty_log(log)
    assert summary.stop_reason == "end_turn"
    assert summary.total_cost_usd == 0.0


def test_parse_pty_log_stuck_takes_precedence_over_sentinel(tmp_path: Path) -> None:
    """STUCK: overrides the sentinel (worker explicitly bailed).

    Even when the sentinel string appears elsewhere, an explicit STUCK
    marker reads as the more specific signal.
    """
    log = tmp_path / "w.log"
    _write_log(
        log,
        "● working\n"
        "STUCK: cannot resolve the dependency\n"
        + COMPLETION_SENTINEL
        + "\n",
    )
    summary = _parse_pty_log(log)
    assert summary.stop_reason == "stuck"


def test_parse_pty_log_review_verdict_approve(tmp_path: Path) -> None:
    """VERDICT: approve maps to stop_reason='approve' (canonical PR-review verb)."""
    log = tmp_path / "w.log"
    _write_log(
        log,
        "● Looks good!\n"
        "VERDICT: approve\n"
        + COMPLETION_SENTINEL
        + "\n",
    )
    summary = _parse_pty_log(log)
    assert summary.stop_reason == "approve"


def test_parse_pty_log_review_verdict_request_changes(tmp_path: Path) -> None:
    """VERDICT: request-changes maps to stop_reason='request-changes'."""
    log = tmp_path / "w.log"
    _write_log(
        log,
        "● Concerns:\n"
        "VERDICT: request-changes\n"
        + COMPLETION_SENTINEL
        + "\n",
    )
    summary = _parse_pty_log(log)
    assert summary.stop_reason == "request-changes"


def test_parse_pty_log_verdict_beats_sentinel_precedence(tmp_path: Path) -> None:
    """When VERDICT and sentinel both appear, VERDICT wins."""
    log = tmp_path / "w.log"
    _write_log(
        log,
        "VERDICT: approve\n" + COMPLETION_SENTINEL + "\n",
    )
    assert _parse_pty_log(log).stop_reason == "approve"


def test_parse_pty_log_cost_from_total_cost_line(tmp_path: Path) -> None:
    """Total cost: $X.XX is captured into total_cost_usd."""
    log = tmp_path / "w.log"
    _write_log(log, "Total cost: $0.1516\n" + COMPLETION_SENTINEL + "\n")
    summary = _parse_pty_log(log)
    assert summary.total_cost_usd == pytest.approx(0.1516)


def test_parse_pty_log_cost_short_dollar_form(tmp_path: Path) -> None:
    """The ``Cost: $.09`` short form (no leading zero) is recognised.

    The issue body explicitly calls out this form as the one that appears
    in some configurations of the status footer.
    """
    log = tmp_path / "w.log"
    _write_log(log, "Cost: $.09\n" + COMPLETION_SENTINEL + "\n")
    summary = _parse_pty_log(log)
    assert summary.total_cost_usd == pytest.approx(0.09)


def test_parse_pty_log_skips_stale_zero_cost(tmp_path: Path) -> None:
    """A stale Cost: $0.00 earlier in the log is ignored in favour of the latest non-zero."""
    log = tmp_path / "w.log"
    _write_log(
        log,
        "Cost: $0.00\nmore output\nTotal cost: $0.42\n"
        + COMPLETION_SENTINEL
        + "\n",
    )
    summary = _parse_pty_log(log)
    assert summary.total_cost_usd == pytest.approx(0.42)


def test_parse_pty_log_strips_ansi_before_matching(tmp_path: Path) -> None:
    """Sentinel embedded among ANSI escapes is still detected.

    The PTY log is raw TTY bytes (ANSI escapes between characters); the
    parser must strip them before substring search.
    """
    log = tmp_path / "w.log"
    _write_log(
        log,
        "\x1b[1m●\x1b[0m OK\n\x1b[32m" + COMPLETION_SENTINEL + "\x1b[0m\n",
    )
    summary = _parse_pty_log(log)
    assert summary.stop_reason == "end_turn"


def test_parse_pty_log_provider_delegates(tmp_path: Path) -> None:
    """ClaudePtyProvider.parse_log returns the same data as the helper."""
    log = tmp_path / "w.log"
    _write_log(log, COMPLETION_SENTINEL + "\nTotal cost: $0.25\n")
    via_helper = _parse_pty_log(log)
    via_provider = ClaudePtyProvider().parse_log(log)
    assert via_helper.stop_reason == via_provider.stop_reason
    assert via_helper.total_cost_usd == via_provider.total_cost_usd


# ── Real-capture fixtures ───────────────────────────────────────────────────
#
# These exist because #425's live testing proved hand-authored fixtures
# CAN miss interactive-TTY bugs.  The captures were produced by spawning
# the real ``claude`` v2.1.160 binary against trivial briefings (see
# ``/tmp/pty-capture/capture2.py`` and ``capture_cost.py`` in the worker
# session that produced #426).


def test_parse_pty_log_real_capture_clean_finish() -> None:
    """Parse a REAL TTY log produced by interactive claude v2.1.160.

    The fixture log contains a single short turn ("Reply OK and end your
    turn") with the system prompt instructing the worker to emit
    ``COORD_PTY_DONE`` on its own line.  The captured log shows:

    * ``● OK`` — the assistant bullet response
    * ``COORD_PTY_DONE`` on the next rendered line — the completion sentinel
    * ``✻ Sautéed for 1s`` — the turn-finished marker

    The parser must surface ``stop_reason='end_turn'`` from the embedded
    sentinel after stripping the surrounding ANSI / kitty-keyboard / OSC
    sequences that make up the TUI frame.
    """
    fixture = FIXTURES_DIR / "pty_clean_finish.log"
    if not fixture.exists():
        pytest.skip("real-capture fixture missing (see capture2.py)")
    summary = _parse_pty_log(fixture)
    assert summary.stop_reason == "end_turn"


def test_parse_pty_log_real_capture_cost_extraction() -> None:
    """Parse a REAL TTY log that includes the ``/cost`` slash-command output.

    The fixture was captured by sending ``/cost\\r`` to the PTY master fd
    after sentinel detection — the same mechanism the production watcher
    uses to surface cost before terminating the worker.  The captured
    log contains ``Total cost: $0.1516`` (verified via direct inspection
    after ANSI stripping), so the parser must surface a non-zero
    total_cost_usd.
    """
    fixture = FIXTURES_DIR / "pty_cost_command.log"
    if not fixture.exists():
        pytest.skip("real-capture fixture missing (see capture_cost.py)")
    summary = _parse_pty_log(fixture)
    assert summary.total_cost_usd > 0.0
    # Sanity: should not be obviously wrong (we know it's $0.1516).
    assert 0.10 < summary.total_cost_usd < 1.0


# ── Completion watcher: termination paths ───────────────────────────────────


def _make_watcher_args(log_path: Path):
    """Build a kwargs dict that runs the watcher with fast test timings."""
    return dict(
        poll_interval=0.01,
        idle_quiesce=0.05,
        idle_min_post_submit=0.0,
        max_wait=2.0,
    )


def test_watcher_terminates_on_sentinel(tmp_path: Path) -> None:
    """Sentinel in the log → SIGTERM the process group."""
    log = tmp_path / "w.log"
    _write_log(log, COMPLETION_SENTINEL + "\n")
    proc = _FakeProc(pid=12345)
    proc.set_exit(0)
    killed: list[tuple[int, int]] = []

    def killpg(pid: int, sig: int) -> None:
        killed.append((pid, sig))

    reason = _pty_completion_watcher(
        proc,  # type: ignore[arg-type]
        str(log),
        COMPLETION_SENTINEL,
        killpg=killpg,
        **_make_watcher_args(log),
    )
    assert "sentinel" in reason
    assert killed and killed[0] == (12345, signal.SIGTERM)


def test_watcher_terminates_on_stuck_marker(tmp_path: Path) -> None:
    """``STUCK:`` line on its own → SIGTERM."""
    log = tmp_path / "w.log"
    _write_log(log, "doing things\nSTUCK: I cannot proceed\nmore output\n")
    proc = _FakeProc(pid=22222)
    proc.set_exit(0)
    killed: list[tuple[int, int]] = []
    reason = _pty_completion_watcher(
        proc,  # type: ignore[arg-type]
        str(log),
        COMPLETION_SENTINEL,
        killpg=lambda p, s: killed.append((p, s)),
        **_make_watcher_args(log),
    )
    assert "STUCK" in reason
    assert killed and killed[0][1] == signal.SIGTERM


def test_watcher_idle_backstop_fires_on_quiescence_with_prompt(tmp_path: Path) -> None:
    """When sentinel missing but log is idle AND idle prompt visible → SIGTERM.

    Models a worker that forgot to emit the sentinel: log size stops
    growing AND the ❯ glyph is visible in the tail.  The watcher should
    fire after idle_quiesce seconds and SIGTERM the group.
    """
    log = tmp_path / "w.log"
    # Idle prompt glyph in the tail, no sentinel, no STUCK.
    _write_log(log, "some output\n" + _PTY_IDLE_PROMPT_GLYPH + " Try \"foo...\"\n")
    proc = _FakeProc(pid=33333)
    proc.set_exit(0)
    killed: list[tuple[int, int]] = []
    reason = _pty_completion_watcher(
        proc,  # type: ignore[arg-type]
        str(log),
        COMPLETION_SENTINEL,
        killpg=lambda p, s: killed.append((p, s)),
        poll_interval=0.01,
        idle_quiesce=0.05,
        idle_min_post_submit=0.0,
        max_wait=2.0,
    )
    assert "idle backstop" in reason
    assert killed and killed[0][1] == signal.SIGTERM


def test_watcher_no_backstop_without_idle_prompt(tmp_path: Path) -> None:
    """Without the ❯ glyph in the tail, the backstop should NOT fire.

    Even when log size is stable, the absence of the idle prompt means
    the TUI hasn't returned to its prompt — the worker may still be
    rendering.  The watcher falls back to the max_wait safety net.
    """
    log = tmp_path / "w.log"
    _write_log(log, "spinner output... no idle prompt here\n")
    proc = _FakeProc(pid=44444)
    proc.set_exit(0)
    killed: list[tuple[int, int]] = []
    reason = _pty_completion_watcher(
        proc,  # type: ignore[arg-type]
        str(log),
        COMPLETION_SENTINEL,
        killpg=lambda p, s: killed.append((p, s)),
        poll_interval=0.01,
        idle_quiesce=0.02,
        idle_min_post_submit=0.0,
        max_wait=0.25,  # short safety net for test
    )
    # Backstop should NOT have fired (no idle prompt) — the max_wait did.
    assert "max_wait" in reason
    assert killed and killed[0][1] == signal.SIGTERM


def test_watcher_returns_when_process_already_exited(tmp_path: Path) -> None:
    """When the process already exited (cancel path), the watcher returns silently."""
    log = tmp_path / "w.log"
    _write_log(log, "")
    proc = _FakeProc(pid=55555)
    proc._exited = True
    proc._exit_code = 0
    killed: list[tuple[int, int]] = []
    reason = _pty_completion_watcher(
        proc,  # type: ignore[arg-type]
        str(log),
        COMPLETION_SENTINEL,
        killpg=lambda p, s: killed.append((p, s)),
        **_make_watcher_args(log),
    )
    assert reason == "process already exited"
    assert killed == []  # no kill — already gone.


def test_watcher_sigkills_when_sigterm_ignored(tmp_path: Path) -> None:
    """SIGTERM is escalated to SIGKILL when the process refuses to exit."""
    import subprocess

    log = tmp_path / "w.log"
    _write_log(log, COMPLETION_SENTINEL + "\n")

    class _NeverExits(_FakeProc):
        def wait(self, timeout: float | None = None) -> int:
            # The watcher catches subprocess.TimeoutExpired specifically
            # (the type Popen.wait raises) — raise that to model a process
            # that ignores SIGTERM and forces the SIGKILL escalation.
            raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout)

    proc = _NeverExits(pid=66666)
    killed: list[tuple[int, int]] = []
    _pty_completion_watcher(
        proc,  # type: ignore[arg-type]
        str(log),
        COMPLETION_SENTINEL,
        killpg=lambda p, s: killed.append((p, s)),
        **_make_watcher_args(log),
    )
    # Both SIGTERM and SIGKILL should have been issued.
    assert (66666, signal.SIGTERM) in killed
    assert (66666, signal.SIGKILL) in killed


def test_watcher_cost_capture_writes_slash_cost(tmp_path: Path) -> None:
    """When cost_capture_fd is provided, the watcher writes ``/cost\\r`` before kill.

    Uses a pipe — writes the slash command to the pipe's write end and
    reads it back from the read end to verify the bytes the TUI would
    see.  In production this fd is the PTY master, so the TUI runs the
    slash command and renders ``Total cost: $X.XX`` into the log.
    """
    import os

    log = tmp_path / "w.log"
    _write_log(log, COMPLETION_SENTINEL + "\n")
    r_fd, w_fd = os.pipe()
    proc = _FakeProc(pid=77777)
    proc.set_exit(0)
    try:
        _pty_completion_watcher(
            proc,  # type: ignore[arg-type]
            str(log),
            COMPLETION_SENTINEL,
            killpg=lambda p, s: None,
            cost_capture_fd=w_fd,
            cost_capture_settle_s=0.0,
            **_make_watcher_args(log),
        )
        # Read what the watcher sent to the PTY.
        bytes_written = os.read(r_fd, 64)
    finally:
        os.close(r_fd)
        try:
            os.close(w_fd)
        except OSError:
            pass
    assert bytes_written == b"/cost\r"


def test_watcher_appends_diagnostic_log_lines(tmp_path: Path) -> None:
    """The watcher annotates the log with a ``# pty-watcher: ...`` marker.

    These lines surface the termination reason in ``coord log <id>`` so
    operators can tell whether a worker hit the sentinel, STUCK, idle
    backstop, or max_wait.
    """
    log = tmp_path / "w.log"
    _write_log(log, COMPLETION_SENTINEL + "\n")
    proc = _FakeProc(pid=88888)
    proc.set_exit(0)
    _pty_completion_watcher(
        proc,  # type: ignore[arg-type]
        str(log),
        COMPLETION_SENTINEL,
        killpg=lambda p, s: None,
        **_make_watcher_args(log),
    )
    body = log.read_text()
    assert "# pty-watcher:" in body
    assert "sentinel" in body


# ── Wiring guards: claude -p path unchanged ─────────────────────────────────


def test_completion_watcher_gate_excludes_chat_types() -> None:
    """Chat-style spec types are NOT in _AUTONOMOUS_SPEC_TYPES.

    The watcher only runs for autonomous workers — chat sessions
    (refinement, test-chat, new-issue-chat) are driven by the developer
    and end via ``cancel``.  Running the watcher for them would cut a
    live conversation off at the idle prompt.
    """
    assert "refinement" not in _AUTONOMOUS_SPEC_TYPES
    assert "test-chat" not in _AUTONOMOUS_SPEC_TYPES
    assert "new-issue-chat" not in _AUTONOMOUS_SPEC_TYPES
    # And the autonomous types ARE included.
    assert "plan" in _AUTONOMOUS_SPEC_TYPES
    assert "work" in _AUTONOMOUS_SPEC_TYPES
    assert "review" in _AUTONOMOUS_SPEC_TYPES
    assert "smoke" in _AUTONOMOUS_SPEC_TYPES
    assert "conflict-fix" in _AUTONOMOUS_SPEC_TYPES


def test_completion_instruction_includes_sentinel() -> None:
    """The instruction text appended to the system prompt names the sentinel."""
    from coord.providers.claude_pty import _completion_instruction

    instr = _completion_instruction("plan")
    assert COMPLETION_SENTINEL in instr


def test_completion_instruction_review_adds_verdict() -> None:
    """For review specs, the instruction also mentions the VERDICT line."""
    from coord.providers.claude_pty import _completion_instruction

    instr_plan = _completion_instruction("plan")
    instr_rev = _completion_instruction("review")
    assert "VERDICT:" in instr_rev
    assert "VERDICT:" not in instr_plan


def test_build_command_chat_types_omit_sentinel() -> None:
    """Chat-style PTY workers do NOT get the completion sentinel in their prompt.

    They are interactive sessions with the developer — the sentinel
    instruction would confuse them into self-terminating mid-conversation.
    """
    from coord.providers.claude_pty import ClaudePtyProvider

    for spec_type in ("refinement", "test-chat", "new-issue-chat"):
        spec_kwargs = {
            "repo_name": "r", "repo_path": "/p", "issue_number": 1,
            "issue_title": "t", "briefing": "b", "type": spec_type,
        }
        from coord.agent import AssignmentSpec
        argv = ClaudePtyProvider().build_command(AssignmentSpec(**spec_kwargs))
        sp = argv[argv.index("--system-prompt") + 1]
        assert COMPLETION_SENTINEL not in sp, (
            f"chat type {spec_type!r} unexpectedly carries completion sentinel"
        )


def test_build_command_autonomous_types_include_sentinel() -> None:
    """Autonomous PTY workers get the sentinel instruction appended."""
    from coord.providers.claude_pty import ClaudePtyProvider
    from coord.agent import AssignmentSpec

    for spec_type in sorted(_AUTONOMOUS_SPEC_TYPES):
        spec = AssignmentSpec(
            repo_name="r", repo_path="/p", issue_number=1,
            issue_title="t", briefing="b", type=spec_type,
            # review_target is required by the legacy semantics for review
            # specs but the PTY build_command does not enforce it; pass a
            # placeholder so the dataclass is consistent.
            review_target="123" if spec_type == "review" else None,
        )
        argv = ClaudePtyProvider().build_command(spec)
        sp = argv[argv.index("--system-prompt") + 1]
        assert COMPLETION_SENTINEL in sp, (
            f"autonomous type {spec_type!r} missing completion sentinel"
        )


def test_default_worker_command_unchanged_by_426() -> None:
    """The claude -p path's argv is identical before / after #426.

    The completion instruction is appended only by
    :meth:`ClaudePtyProvider.build_command` — :func:`default_worker_command`
    builds the legacy ``claude -p`` argv and must be untouched.  We assert
    the sentinel does NOT appear in the legacy --system-prompt value.
    """
    from coord.agent import AssignmentSpec, default_worker_command

    spec = AssignmentSpec(
        repo_name="r", repo_path="/p", issue_number=1,
        issue_title="t", briefing="b", type="work",
    )
    argv = default_worker_command(spec)
    sp = argv[argv.index("--system-prompt") + 1]
    assert COMPLETION_SENTINEL not in sp


# ── End-to-end: real watcher thread against a synthetic log ─────────────────


def test_watcher_thread_kills_when_sentinel_written_after_start(tmp_path: Path) -> None:
    """Start the watcher thread on an empty log; write sentinel later → kill fires.

    Models the production lifecycle: the worker writes for a while, then
    eventually emits the sentinel.  The watcher must wake up, see the
    sentinel during a poll cycle, and SIGTERM the process group — all
    without our test thread polling for completion in a busy loop.
    """
    log = tmp_path / "w.log"
    _write_log(log, "")
    proc = _FakeProc(pid=99999)
    proc.set_exit(0)
    killed: list[tuple[int, int]] = []
    done = threading.Event()

    def run() -> None:
        _pty_completion_watcher(
            proc,  # type: ignore[arg-type]
            str(log),
            COMPLETION_SENTINEL,
            killpg=lambda p, s: killed.append((p, s)),
            poll_interval=0.02,
            idle_quiesce=10.0,
            idle_min_post_submit=10.0,
            max_wait=5.0,
        )
        done.set()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    # Brief sleep, then plant the sentinel — watcher should fire on next poll.
    time.sleep(0.1)
    log.write_text("output...\n" + COMPLETION_SENTINEL + "\n")
    assert done.wait(timeout=3.0), "watcher did not detect sentinel within budget"
    assert killed and killed[0][1] == signal.SIGTERM


def test_pty_completion_max_wait_constant_is_reasonable() -> None:
    """Sanity: the default max wait is below the outer 2-hour reap safety net."""
    from coord.agent import _REAP_MAX_WAIT
    assert _PTY_COMPLETION_MAX_WAIT_S < _REAP_MAX_WAIT


# ── Regression: spawn-header false-positives ────────────────────────────────


def test_watcher_ignores_sentinel_in_spawn_header(tmp_path: Path) -> None:
    """Regression for the live-E2E bug: the spawn header includes the
    worker's ``--system-prompt`` value, which (post-#426) contains the
    literal sentinel string.  Without the comment-line filter the watcher
    fires within the first poll cycle and kills the worker before any
    real output is produced.

    Models a worker that has been spawned (header written) but has not
    yet emitted anything beyond the agent-authored ``# agent=...
    argv=...`` line.  The watcher must NOT terminate.
    """
    log = tmp_path / "w.log"
    # Simulate the spawn header: a single ``#``-comment line containing
    # the sentinel embedded inside the rendered --system-prompt argv.
    _write_log(
        log,
        f"# agent=x repo=r issue=#1 provider=claude-pty argv=claude "
        f"--system-prompt 'do work; emit {COMPLETION_SENTINEL}'\n",
    )
    proc = _FakeProc(pid=11111)
    proc.set_exit(0)
    killed: list[tuple[int, int]] = []
    reason = _pty_completion_watcher(
        proc,  # type: ignore[arg-type]
        str(log),
        COMPLETION_SENTINEL,
        killpg=lambda p, s: killed.append((p, s)),
        poll_interval=0.01,
        idle_quiesce=10.0,           # don't trigger idle backstop
        idle_min_post_submit=10.0,
        max_wait=0.2,                # fall through to max_wait safety net
    )
    # The watcher must NOT have flagged the sentinel from the header.
    assert "sentinel" not in reason, (
        f"watcher false-positived on spawn header sentinel: reason={reason!r}"
    )
    # It should have hit max_wait instead (proves no other signal fired).
    assert "max_wait" in reason


def test_parse_log_ignores_sentinel_in_spawn_header(tmp_path: Path) -> None:
    """Same regression for ``_parse_pty_log``: the spawn header literal must
    not yield a false ``stop_reason='end_turn'``."""
    log = tmp_path / "w.log"
    _write_log(
        log,
        f"# agent=x argv=claude --system-prompt 'emit {COMPLETION_SENTINEL} on done'\n"
        "some intermediate output (no sentinel here)\n",
    )
    summary = _parse_pty_log(log)
    assert summary.stop_reason is None


def test_parse_log_picks_sentinel_on_worker_output_line(tmp_path: Path) -> None:
    """The sentinel IS detected when it appears on a non-comment worker line."""
    log = tmp_path / "w.log"
    _write_log(
        log,
        f"# agent=x argv=claude --system-prompt 'emit {COMPLETION_SENTINEL} on done'\n"
        "OK\n"
        f"{COMPLETION_SENTINEL}\n",
    )
    summary = _parse_pty_log(log)
    assert summary.stop_reason == "end_turn"


def test_sentinel_helper_filters_comment_lines() -> None:
    """Direct test for the comment-aware helper."""
    from coord.providers.claude_pty import _sentinel_in_worker_output

    s = COMPLETION_SENTINEL
    assert not _sentinel_in_worker_output(f"# header {s}\n", s)
    assert not _sentinel_in_worker_output(f"   # indented comment {s}\n", s)
    assert _sentinel_in_worker_output(f"# header {s}\nreal output {s}\n", s)
    assert _sentinel_in_worker_output(f"{s}\n", s)
    assert not _sentinel_in_worker_output("nothing here\n", s)


# ── Reap success override (sentinel → DONE, others → FAILED) ────────────────


def test_watcher_writes_success_marker_on_sentinel(tmp_path: Path) -> None:
    """When the sentinel triggers termination, watcher stamps the success marker.

    :meth:`AgentServer._reap` reads this marker after the SIGTERM exit
    code is captured and overrides it to 0, so the assignment ends as
    DONE and the existing branch-push code runs.
    """
    from coord.agent import _PTY_SUCCESS_LINE_MARKER

    log = tmp_path / "w.log"
    _write_log(log, COMPLETION_SENTINEL + "\n")
    proc = _FakeProc(pid=10101)
    proc.set_exit(0)
    _pty_completion_watcher(
        proc,  # type: ignore[arg-type]
        str(log),
        COMPLETION_SENTINEL,
        killpg=lambda p, s: None,
        **_make_watcher_args(log),
    )
    assert _PTY_SUCCESS_LINE_MARKER in log.read_bytes()


def test_watcher_does_not_write_success_marker_on_stuck(tmp_path: Path) -> None:
    """STUCK termination must NOT write the success marker (assignment fails).

    Without this, ``_reap`` would override a SIGTERM exit code to 0 and
    push the worktree, advertising a failed STUCK worker as a successful
    completion.
    """
    from coord.agent import _PTY_SUCCESS_LINE_MARKER

    log = tmp_path / "w.log"
    _write_log(log, "STUCK: cannot proceed\n")
    proc = _FakeProc(pid=10102)
    proc.set_exit(0)
    _pty_completion_watcher(
        proc,  # type: ignore[arg-type]
        str(log),
        COMPLETION_SENTINEL,
        killpg=lambda p, s: None,
        **_make_watcher_args(log),
    )
    assert _PTY_SUCCESS_LINE_MARKER not in log.read_bytes()


def test_watcher_does_not_write_success_marker_on_idle_backstop(tmp_path: Path) -> None:
    """Idle-backstop termination must NOT write the success marker."""
    from coord.agent import _PTY_SUCCESS_LINE_MARKER

    log = tmp_path / "w.log"
    _write_log(log, "intermediate output\n" + _PTY_IDLE_PROMPT_GLYPH + " ...\n")
    proc = _FakeProc(pid=10103)
    proc.set_exit(0)
    _pty_completion_watcher(
        proc,  # type: ignore[arg-type]
        str(log),
        COMPLETION_SENTINEL,
        killpg=lambda p, s: None,
        poll_interval=0.01,
        idle_quiesce=0.05,
        idle_min_post_submit=0.0,
        max_wait=2.0,
    )
    assert _PTY_SUCCESS_LINE_MARKER not in log.read_bytes()


def test_watcher_does_not_write_success_marker_on_max_wait(tmp_path: Path) -> None:
    """Max-wait termination must NOT write the success marker."""
    from coord.agent import _PTY_SUCCESS_LINE_MARKER

    log = tmp_path / "w.log"
    _write_log(log, "no signal at all\n")
    proc = _FakeProc(pid=10104)
    proc.set_exit(0)
    _pty_completion_watcher(
        proc,  # type: ignore[arg-type]
        str(log),
        COMPLETION_SENTINEL,
        killpg=lambda p, s: None,
        poll_interval=0.01,
        idle_quiesce=10.0,
        idle_min_post_submit=10.0,
        max_wait=0.15,
    )
    assert _PTY_SUCCESS_LINE_MARKER not in log.read_bytes()


def test_log_has_pty_success_returns_false_when_marker_absent(tmp_path: Path) -> None:
    """``_log_has_pty_success`` returns False on a log without the marker."""
    from coord.agent import _log_has_pty_success

    log = tmp_path / "w.log"
    log.write_text("just plain output\n")
    assert _log_has_pty_success(str(log)) is False


def test_log_has_pty_success_returns_true_when_marker_present(tmp_path: Path) -> None:
    """``_log_has_pty_success`` returns True on a log containing the marker."""
    from coord.agent import _PTY_SUCCESS_LINE_MARKER, _log_has_pty_success

    log = tmp_path / "w.log"
    log.write_bytes(b"some output\n" + _PTY_SUCCESS_LINE_MARKER + b"\n")
    assert _log_has_pty_success(str(log)) is True


def test_log_has_pty_success_returns_false_on_missing_file(tmp_path: Path) -> None:
    """Missing log files report False (defensive — OSError swallowed)."""
    from coord.agent import _log_has_pty_success

    assert _log_has_pty_success(str(tmp_path / "nope.log")) is False
