"""Unit tests for the #485 briefing-echo feature.

:func:`coord.interactive.launch_human_attended_interactive` must write the
seeded briefing as plain human-readable text to the operator's terminal
(``fd_out`` / ``sys.stdout``) **before** ``pty.fork()`` is called.

Strategy
--------
* Redirect ``sys.stdout`` so its ``fileno()`` returns the write-end of an
  ``os.pipe()`` pair that we control.
* Patch ``pty.fork`` to raise immediately after the echo (so no real fork
  happens in the test process).
* Read back what was written to the pipe and assert the briefing content.
"""
from __future__ import annotations

import os
import sys
from typing import Generator

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _PipedStdout:
    """Minimal stdout shim whose ``fileno()`` returns a caller-supplied fd."""

    def __init__(self, fd: int) -> None:
        self._fd = fd

    def fileno(self) -> int:
        return self._fd


class _PipedStdin:
    """Minimal stdin shim whose ``fileno()`` returns a caller-supplied fd.

    Not a TTY, so ``termios.tcgetattr`` will fail with ``termios.error``
    and ``orig_attr`` will be ``None`` — that code-path is already tested
    by the module's own comment ("may fail if stdin isn't a TTY").
    """

    def __init__(self, fd: int) -> None:
        self._fd = fd

    def fileno(self) -> int:
        return self._fd


def _fake_fork_raiser() -> tuple[int, int]:
    """Substitute for ``pty.fork`` — raises immediately to abort the session."""
    raise OSError("test sentinel — pty.fork intentionally blocked in unit tests")


def _drain_pipe(r_fd: int) -> bytes:
    """Read all available bytes from a pipe read-end (after write-end is closed)."""
    chunks: list[bytes] = []
    while True:
        chunk = os.read(r_fd, 4096)
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestBriefingEchoBeforeFork:
    """Briefing text is echoed to ``fd_out`` before ``pty.fork()`` runs."""

    def test_briefing_written_to_stdout_before_fork(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Plain briefing text and the delimiter header appear in fd_out output."""
        import pty  # noqa: PLC0415 — same deferred import as the module under test

        from coord.interactive import launch_human_attended_interactive

        r_fd, w_fd = os.pipe()
        try:
            monkeypatch.setattr(sys, "stdout", _PipedStdout(w_fd))
            monkeypatch.setattr(sys, "stdin", _PipedStdin(r_fd))
            monkeypatch.setattr(pty, "fork", _fake_fork_raiser)

            with pytest.raises(OSError, match="test sentinel"):
                launch_human_attended_interactive(
                    argv=["claude"],
                    briefing="This briefing must be readable before the operator submits.",
                )

            os.close(w_fd)
            w_fd = -1
            captured = _drain_pipe(r_fd)
        finally:
            if w_fd != -1:
                try:
                    os.close(w_fd)
                except OSError:
                    pass
            try:
                os.close(r_fd)
            except OSError:
                pass

        assert b"This briefing must be readable" in captured, (
            "briefing body should appear in the text written to fd_out"
        )
        assert b"seeded briefing" in captured, (
            "delimiter header should appear so the operator knows what to look at"
        )

    def test_empty_briefing_produces_no_briefing_echo(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the briefing is empty (or whitespace-only), the briefing-echo
        block is skipped.  (The unconditional #1102 no-detach warning still
        fires regardless of briefing content — see TestNoDetachWarningPtyRelay
        — so this no longer asserts zero output overall.)"""
        import pty  # noqa: PLC0415

        from coord.interactive import launch_human_attended_interactive

        r_fd, w_fd = os.pipe()
        try:
            monkeypatch.setattr(sys, "stdout", _PipedStdout(w_fd))
            monkeypatch.setattr(sys, "stdin", _PipedStdin(r_fd))
            monkeypatch.setattr(pty, "fork", _fake_fork_raiser)

            with pytest.raises(OSError, match="test sentinel"):
                launch_human_attended_interactive(argv=["claude"], briefing="   ")

            os.close(w_fd)
            w_fd = -1
            captured = _drain_pipe(r_fd)
        finally:
            if w_fd != -1:
                try:
                    os.close(w_fd)
                except OSError:
                    pass
            try:
                os.close(r_fd)
            except OSError:
                pass

        assert b"seeded briefing" not in captured, (
            f"expected no briefing-echo block for a blank briefing, got {captured!r}"
        )

    def test_multiline_briefing_preserved_verbatim(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Every line of a multi-line briefing appears in the echoed output."""
        import pty  # noqa: PLC0415

        from coord.interactive import launch_human_attended_interactive

        r_fd, w_fd = os.pipe()
        briefing = "Line one\nLine two\nLine three with details"
        try:
            monkeypatch.setattr(sys, "stdout", _PipedStdout(w_fd))
            monkeypatch.setattr(sys, "stdin", _PipedStdin(r_fd))
            monkeypatch.setattr(pty, "fork", _fake_fork_raiser)

            with pytest.raises(OSError, match="test sentinel"):
                launch_human_attended_interactive(argv=["claude"], briefing=briefing)

            os.close(w_fd)
            w_fd = -1
            captured = _drain_pipe(r_fd)
        finally:
            if w_fd != -1:
                try:
                    os.close(w_fd)
                except OSError:
                    pass
            try:
                os.close(r_fd)
            except OSError:
                pass

        for line in briefing.splitlines():
            assert line.encode() in captured, (
                f"expected line {line!r} to appear in echoed output"
            )

    def test_briefing_echo_precedes_fork(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Confirm the echo happens before pty.fork() is reached.

        We record the call order: echo-observed then fork-called.  If the
        echo appeared AFTER fork, ``fork_called_first`` would be True.
        """
        import pty  # noqa: PLC0415

        from coord.interactive import launch_human_attended_interactive

        r_fd, w_fd = os.pipe()
        order: list[str] = []

        original_os_write = os.write

        def _spy_write(fd: int, data: bytes) -> int:
            if fd == w_fd and b"seeded briefing" in data:
                order.append("echo")
            return original_os_write(fd, data)

        def _spy_fork() -> tuple[int, int]:
            order.append("fork")
            raise OSError("test sentinel")

        try:
            monkeypatch.setattr(sys, "stdout", _PipedStdout(w_fd))
            monkeypatch.setattr(sys, "stdin", _PipedStdin(r_fd))
            monkeypatch.setattr(os, "write", _spy_write)
            monkeypatch.setattr(pty, "fork", _spy_fork)

            with pytest.raises(OSError, match="test sentinel"):
                launch_human_attended_interactive(
                    argv=["claude"],
                    briefing="Order check briefing.",
                )
        finally:
            try:
                os.close(w_fd)
            except OSError:
                pass
            try:
                os.close(r_fd)
            except OSError:
                pass

        assert order == ["echo", "fork"], (
            f"expected echo before fork, got call order: {order}"
        )


class TestNoDetachWarningPtyRelay:
    """#1102: the PTY-relay fallback has no tmux underneath it, so unlike the
    tmux path there is no detach to protect — closing the terminal or Ctrl-C
    ends the session immediately.  ``_launch_via_pty`` must say so, every
    time, regardless of whether a briefing was supplied.
    """

    def test_warning_written_before_fork(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import pty  # noqa: PLC0415

        from coord.interactive import launch_human_attended_interactive

        r_fd, w_fd = os.pipe()
        try:
            monkeypatch.setattr(sys, "stdout", _PipedStdout(w_fd))
            monkeypatch.setattr(sys, "stdin", _PipedStdin(r_fd))
            monkeypatch.setattr(pty, "fork", _fake_fork_raiser)

            with pytest.raises(OSError, match="test sentinel"):
                launch_human_attended_interactive(argv=["claude"], briefing="hi")

            os.close(w_fd)
            w_fd = -1
            captured = _drain_pipe(r_fd)
        finally:
            if w_fd != -1:
                try:
                    os.close(w_fd)
                except OSError:
                    pass
            try:
                os.close(r_fd)
            except OSError:
                pass

        assert b"no detach available" in captured
        assert b"Ctrl-C ends the claude process" in captured

    def test_warning_written_even_for_empty_briefing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unlike the briefing echo, the no-detach warning is unconditional —
        there's no detach whether or not a briefing was seeded."""
        import pty  # noqa: PLC0415

        from coord.interactive import launch_human_attended_interactive

        r_fd, w_fd = os.pipe()
        try:
            monkeypatch.setattr(sys, "stdout", _PipedStdout(w_fd))
            monkeypatch.setattr(sys, "stdin", _PipedStdin(r_fd))
            monkeypatch.setattr(pty, "fork", _fake_fork_raiser)

            with pytest.raises(OSError, match="test sentinel"):
                launch_human_attended_interactive(argv=["claude"], briefing="   ")

            os.close(w_fd)
            w_fd = -1
            captured = _drain_pipe(r_fd)
        finally:
            if w_fd != -1:
                try:
                    os.close(w_fd)
                except OSError:
                    pass
            try:
                os.close(r_fd)
            except OSError:
                pass

        assert b"no detach available" in captured
