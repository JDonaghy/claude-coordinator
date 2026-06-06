"""Human-attended interactive `claude` launcher (#437).

This module owns the **only** path that may launch a provider whose
:attr:`~coord.providers.base.Capabilities.human_attended_only` flag is
``True`` — interactive Claude Code on a Max/Pro subscription.  It is
invoked from ``coord assign --interactive`` and attaches the child to
the operator's local TTY:

* A new pty pair is opened; the child execs ``claude`` with the slave fd
  as its stdin/stdout/stderr.
* The parent relays bytes between the operator's TTY and the master fd
  (raw-mode stdin → master, master → stdout), so the human drives the
  session as if they had typed ``claude`` themselves.
* Once the TUI emits the bracketed-paste-enable DECSET (``ESC[?2004h``)
  AND its initial render has gone quiet, the briefing is PRE-FILLED into
  the input box as a single bracketed paste — NO trailing carriage
  return.  The operator presses Enter to submit.
* Window-size changes (SIGWINCH) are forwarded to the child via
  ``TIOCSWINSZ`` so the TUI re-flows correctly when the operator
  resizes their terminal.

This module deliberately contains **no** content-based completion
detection, **no** TTY scraper, and **no** mechanism to advance pipeline
state from session output.  The session is HUMAN-CLOSED — the relay
loop exits when the child exits and the launcher reports the child's
exit code to the caller.  This is the structural ToS-compliance posture
the abandoned #426 was missing.

Imports are deferred / Unix-only — the stdlib ``pty`` / ``termios`` /
``fcntl`` modules are not present on Windows, but agent machines are
Linux/macOS only.
"""

from __future__ import annotations

import fcntl
import os
import select
import signal
import struct
import sys
import termios
import time
import tty
from typing import Sequence

from coord.providers.claude_pty import (
    BRACKETED_PASTE_ENABLE,
    BRACKETED_PASTE_END,
    BRACKETED_PASTE_START,
)

__all__ = ["launch_human_attended_interactive"]


# Match the agent-side readiness constants so the operator-launched path
# behaves the same way as the agent-spawned one once #437 lands the
# guardrail on the unattended side.  Bracketed-paste-enable fires while
# the TUI is still drawing; we additionally wait for render quiescence
# before pasting because a paste at the enable instant is silently
# dropped (verified live against interactive ``claude``).
_READY_QUIESCE_S = 0.8
_READY_QUIESCE_CAP_S = 8.0


def launch_human_attended_interactive(
    argv: Sequence[str],
    briefing: str,
    *,
    cwd: str | None = None,
) -> int:
    """Run *argv* attached to the current TTY with *briefing* pre-filled.

    Used exclusively by ``coord assign --interactive``.  The function
    returns the child's exit code; the caller decides what (if anything)
    to record on the board.  No GitHub comments are posted from inside
    this function — the human owns the session lifecycle.

    Args:
        argv: The worker command (typically built via
            :meth:`ClaudePtyProvider.build_command`).
        briefing: The text to PRE-FILL in the TUI's input box.  Wrapped
            in a bracketed-paste block; the operator presses Enter to
            submit.  An empty string disables the pre-fill entirely.
        cwd: Working directory for the child.  ``None`` keeps the
            parent's cwd.

    Returns:
        The child's exit status (``0`` on clean exit; ``128 + signum``
        on termination by signal).
    """
    import pty  # stdlib, Unix-only — deferred for platform safety  # noqa: PLC0415

    fd_in = sys.stdin.fileno()
    fd_out = sys.stdout.fileno()

    # Capture original TTY attrs so we can restore on exit.  ``setraw``
    # may fail if stdin isn't a TTY (e.g. piped input under tests); in
    # that case we proceed without raw mode — the pre-fill still works,
    # only line discipline is lost.
    try:
        orig_attr = termios.tcgetattr(fd_in)
    except termios.error:
        orig_attr = None

    pid, master_fd = pty.fork()
    if pid == 0:
        # child
        try:
            if cwd:
                os.chdir(cwd)
            os.execvp(argv[0], list(argv))
        except OSError as e:
            sys.stderr.write(f"exec failed: {e}\n")
            os._exit(127)

    # parent: forward initial terminal size to the child so the TUI
    # renders at the operator's actual viewport size.
    def _send_winsize() -> None:
        try:
            cols, rows = os.get_terminal_size(fd_out)
            fcntl.ioctl(
                master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0)
            )
        except (OSError, AttributeError, ValueError):
            pass

    _send_winsize()

    # Forward SIGWINCH so terminal resizes propagate to the child while
    # the session is live.  Restore default handler on exit.
    def _winch_handler(_signum, _frame) -> None:
        _send_winsize()

    prev_winch = signal.signal(signal.SIGWINCH, _winch_handler)

    if orig_attr is not None:
        try:
            tty.setraw(fd_in)
        except termios.error:
            pass

    paste_block = b""
    if briefing.strip():
        paste_block = (
            BRACKETED_PASTE_START
            + briefing.rstrip("\n").encode("utf-8")
            + BRACKETED_PASTE_END
        )

    seen_enable = False
    prefilled = not bool(paste_block)
    started = time.monotonic()
    last_master_activity = started
    # Capture the raw wait-status from the WNOHANG poll so that if the
    # child is already reaped when we reach the post-loop waitpid we can
    # still extract the correct exit code (see ChildProcessError handler
    # below).
    _reaped_status: int | None = None

    try:
        while True:
            try:
                r, _, _ = select.select([fd_in, master_fd], [], [], 0.05)
            except (OSError, ValueError):
                break

            if fd_in in r:
                try:
                    data = os.read(fd_in, 4096)
                except OSError:
                    data = b""
                if data:
                    try:
                        os.write(master_fd, data)
                    except OSError:
                        break

            if master_fd in r:
                try:
                    data = os.read(master_fd, 4096)
                except OSError:
                    break
                if not data:
                    break
                try:
                    os.write(fd_out, data)
                except OSError:
                    break
                if not prefilled:
                    if not seen_enable and BRACKETED_PASTE_ENABLE in data:
                        seen_enable = True
                    last_master_activity = time.monotonic()

            if not prefilled:
                now = time.monotonic()
                # Pre-fill once: bracketed-paste-enable seen AND render
                # quiescent for _READY_QUIESCE_S, OR the overall cap
                # has elapsed (degraded fallback — paste anyway).
                ready_quiet = (
                    seen_enable and now - last_master_activity >= _READY_QUIESCE_S
                )
                ready_cap = now - started >= _READY_QUIESCE_CAP_S
                if ready_quiet or ready_cap:
                    try:
                        os.write(master_fd, paste_block)
                    except OSError:
                        pass
                    prefilled = True

            # Poll child status without blocking.
            try:
                done_pid, _reaped_status = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                break
            if done_pid != 0:
                # Drain any final master output before exiting the loop.
                while True:
                    try:
                        data = os.read(master_fd, 4096)
                    except OSError:
                        break
                    if not data:
                        break
                    try:
                        os.write(fd_out, data)
                    except OSError:
                        break
                break
    finally:
        if orig_attr is not None:
            try:
                termios.tcsetattr(fd_in, termios.TCSAFLUSH, orig_attr)
            except termios.error:
                pass
        try:
            os.close(master_fd)
        except OSError:
            pass
        try:
            signal.signal(signal.SIGWINCH, prev_winch)
        except (OSError, ValueError, TypeError):
            pass

    try:
        _, status = os.waitpid(pid, 0)
    except ChildProcessError:
        # The zombie was already reaped by the WNOHANG poll inside the
        # relay loop.  Use the status captured there so non-zero exit
        # codes are correctly propagated (without this fix the function
        # would always return 0 for the normal-exit path).
        if _reaped_status is not None:
            if os.WIFEXITED(_reaped_status):
                return os.WEXITSTATUS(_reaped_status)
            if os.WIFSIGNALED(_reaped_status):
                return 128 + os.WTERMSIG(_reaped_status)
        return 0
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    if os.WIFSIGNALED(status):
        return 128 + os.WTERMSIG(status)
    return 0
