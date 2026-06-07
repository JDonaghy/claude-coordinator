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

The session is HUMAN-CLOSED — the relay loop exits when the child exits
and the launcher reports the child's exit code to the caller.  This
module deliberately contains **no** content-based completion detection
and **no** TTY scraper.  The structural ToS-compliance posture the
abandoned #426 was missing.

After exit, the CLI caller invokes :func:`finalize_interactive_exit`
(the #466 git-floor backstop) which computes the worktree's
commits-ahead using the #448 ``_commits_ahead`` primitive, pushes any
local commits, and writes the terminal completion through the
:mod:`coord.issue_store` seam.  That guarantees the board always gets a
recorded completion regardless of whether the interactive agent
remembered to call ``coord report-result`` first.

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
import subprocess
import sys
import termios
import time
import tty
from pathlib import Path
from typing import Sequence

from coord.providers.claude_pty import (
    BRACKETED_PASTE_ENABLE,
    BRACKETED_PASTE_END,
    BRACKETED_PASTE_START,
)

__all__ = [
    "launch_human_attended_interactive",
    "finalize_interactive_exit",
    "InteractiveFinalizeResult",
]


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


# ── #466 git-floor backstop ─────────────────────────────────────────────────


# Result class for the backstop — small and dataclass-y for test ergonomics.
from dataclasses import dataclass  # noqa: E402  — co-located with the helper


@dataclass
class InteractiveFinalizeResult:
    """What :func:`finalize_interactive_exit` ended up writing.

    Returned for callers (and tests) so the chosen terminal state and
    commit count can be asserted without re-reading the DB.

    Attributes:
        terminal_status: ``done`` | ``advisory`` | ``failed`` — what the
            issue-store seam ultimately recorded.
        commits_ahead: Commits the worktree was ahead of *base* at exit
            time.  ``None`` means the commit-count primitive couldn't
            determine it (e.g. detached HEAD); the seam treats this as
            non-zero per #448 policy.
        push_ok: ``True`` if ``git push`` succeeded (or was skipped
            because there were zero commits to push).  ``False`` if a
            push was attempted and the remote rejected.
        push_error: Stderr from the failed push, when ``push_ok`` is
            ``False``.
        already_recorded: ``True`` when ``coord report-result`` already
            wrote a terminal status for this assignment before the
            backstop ran.  The backstop respects the agent's report and
            does NOT clobber it; this field tells the caller that.
        seam_outcome: The :class:`coord.issue_store.StoreOutcome` for
            the seam call (``None`` when the backstop deferred to an
            existing report).
    """

    terminal_status: str
    commits_ahead: int | None
    push_ok: bool
    push_error: str | None
    already_recorded: bool
    seam_outcome: object | None = None  # StoreOutcome | None


def _commits_ahead(wt_path: Path, base: str) -> int | None:
    """Mirror of :meth:`coord.agent.AgentServer._commits_ahead`.

    Kept as a thin wrapper so the launcher (which never imports the
    agent server) doesn't pull in the full :class:`AgentServer` graph
    just to count commits.  Logic is byte-identical to #448's primitive
    — same ref precedence (``origin/<base>`` first, then ``<base>``),
    same ``None`` semantics on git failure (callers must treat ``None``
    as "unknown, assume non-zero" to avoid false advisories).
    """
    for ref in (f"origin/{base}", base):
        try:
            raw = subprocess.run(
                ["git", "rev-list", "--count", f"{ref}..HEAD"],
                cwd=str(wt_path),
                capture_output=True,
                text=True,
                timeout=15.0,
                check=True,
            ).stdout
            return int(raw.strip())
        except (subprocess.SubprocessError, ValueError, OSError):
            continue
    return None


def _git_push(wt_path: Path, *, timeout: float = 60.0) -> tuple[bool, str | None]:
    """``git push -u origin HEAD`` with a clean (ok, error) return shape.

    Caller decides what to do with a failed push.  ``(True, None)`` is
    the happy path; ``(False, stderr)`` surfaces the remote's error.
    """
    try:
        result = subprocess.run(
            ["git", "push", "-u", "origin", "HEAD"],
            cwd=str(wt_path),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        return False, str(exc)
    if result.returncode != 0:
        return False, (result.stderr or "git push failed").strip()
    return True, None


def _current_branch(wt_path: Path) -> str | None:
    """Best-effort current-branch name for the worktree.  ``None`` on
    detached HEAD or git failure — the caller passes the value through
    to the seam where ``None`` is acceptable (the assignment row already
    has the dispatch-time branch slug)."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(wt_path),
            capture_output=True,
            text=True,
            timeout=10.0,
            check=True,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    branch = result.stdout.strip()
    if not branch or branch == "HEAD":
        return None
    return branch


def _assignment_already_recorded(assignment_id: str) -> bool:
    """Did ``coord report-result`` already write a terminal state for
    this assignment?  We check the assignments row's ``status`` column.
    The backstop runs after the human session exits, so if the agent
    invoked ``coord report-result`` before exiting we must NOT clobber
    that decision with the git-floor's heuristic verdict.
    """
    if not assignment_id:
        return False
    try:
        from coord.state import get_connection  # noqa: PLC0415

        conn = get_connection()
        row = conn.execute(
            "SELECT status FROM assignments WHERE assignment_id=?",
            (assignment_id,),
        ).fetchone()
    except Exception:  # noqa: BLE001
        return False
    if row is None:
        return False
    status = row["status"] if hasattr(row, "keys") else row[0]
    # Anything that's not 'running'/'pending' means a terminal write
    # already landed (done/advisory/failed/cancelled).  Respect it.
    return status not in (None, "", "running", "pending")


def finalize_interactive_exit(
    *,
    assignment_id: str,
    repo_name: str,
    repo_github: str,
    issue_number: int,
    machine_name: str,
    worktree_path: str,
    base_branch: str,
    exit_code: int,
    started_at: float | None = None,
    log_path: str | None = None,
) -> InteractiveFinalizeResult:
    """Git-floor backstop for the interactive launcher exit path (#466).

    Called AFTER :func:`launch_human_attended_interactive` returns.
    Performs three steps:

    1. Push any local commits with ``git push -u origin HEAD`` (the same
       discipline the agent-side reap uses).  Push errors are surfaced
       but never abort the backstop — the local DB write is still the
       authoritative record.
    2. Count commits ahead of *base_branch* using the #448 primitive
       (mirrored above to avoid importing the full AgentServer graph).
    3. Route the resulting ``(exit_code, commits_ahead)`` tuple through
       :func:`coord.issue_store.post_completion` — the seam decides
       whether this is ``done`` / ``advisory`` / ``failed``.

    If ``coord report-result`` already wrote a terminal state for this
    assignment, this function returns without re-writing anything.  The
    agent-reported result wins because the backstop's heuristic can't
    distinguish "0 commits because review session" from "0 commits
    because agent did nothing".
    """
    # Respect an explicit `coord report-result` from the agent.  Without
    # this check, every review session (which legitimately has 0 commits)
    # would have its agent-reported verdict overwritten with an advisory
    # the instant the human closed the TTY.
    if _assignment_already_recorded(assignment_id):
        return InteractiveFinalizeResult(
            terminal_status="report-result",  # informational only
            commits_ahead=None,
            push_ok=True,
            push_error=None,
            already_recorded=True,
            seam_outcome=None,
        )

    wt_path = Path(worktree_path)

    # Step 1 — push.  Failure is non-fatal but recorded.  Skip the push
    # entirely when the worktree directory doesn't exist (extremely
    # rare, but possible if the operator nuked it).
    push_ok = True
    push_error: str | None = None
    if wt_path.exists():
        push_ok, push_error = _git_push(wt_path)

    # Step 2 — count commits ahead of the base.  None = git failed; the
    # seam treats that as "unknown, assume non-zero" so a git outage
    # never falsely flags an advisory.
    commits = None
    if wt_path.exists():
        commits = _commits_ahead(wt_path, base_branch)

    branch_now = _current_branch(wt_path) if wt_path.exists() else None

    duration: float | None = None
    if started_at is not None:
        duration = max(0.0, time.time() - started_at)

    # Step 3 — write the terminal state through the seam.  Deferred import
    # so the launcher module stays importable in isolation (tests, agent
    # processes that just want the constants).
    from coord.issue_store import CompletionRecord, post_completion  # noqa: PLC0415

    record = CompletionRecord(
        assignment_id=assignment_id,
        machine_name=machine_name,
        repo_name=repo_name,
        repo_github=repo_github,
        issue_number=issue_number,
        exit_code=exit_code,
        commits_ahead=commits,
        branch=branch_now,
        duration_seconds=duration,
        log_path=log_path,
        summary="",
    )
    outcome = post_completion(record)

    return InteractiveFinalizeResult(
        terminal_status=outcome.status,
        commits_ahead=commits,
        push_ok=push_ok,
        push_error=push_error,
        already_recorded=False,
        seam_outcome=outcome,
    )
