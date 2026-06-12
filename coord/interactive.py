"""Human-attended interactive `claude` launcher (#437).

This module owns the **only** path that may launch a provider whose
:attr:`~coord.providers.base.Capabilities.human_attended_only` flag is
``True`` — interactive Claude Code on a Max/Pro subscription.  It is
invoked from ``coord assign --interactive`` and attaches the child to
the operator's local TTY via one of two strategies:

**PTY relay (fallback, no tmux)**

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

**tmux session (preferred, when tmux is available and assignment_id is
provided — #487)**

* A named tmux session ``coord-<assignment_id>`` is created (or reused).
* ``claude`` runs directly inside the tmux session; tmux provides the pty.
* The briefing is injected via :func:`_inject_briefing_into_tmux_session`:
  a quiescence-based poll on the pane output followed by
  ``tmux paste-buffer -p`` (bracketed-paste mode).
* The operator's terminal ATTACHES to the tmux session
  (``tmux attach-session -t coord-<aid>``); if the TUI crashes the
  attachment disconnects but **the tmux session and claude keep running**.
* On TUI restart the user can reattach with ``coord reattach <aid>`` or
  via the TUI's Pipeline→Terminal tab (which checks for a live
  ``coord-*`` session for the open issue and offers reattach).

The session is HUMAN-CLOSED — the relay loop / tmux session exits when
the child exits and the launcher reports the child's exit code to the
caller.  This module deliberately contains **no** content-based
completion detection and **no** TTY scraper.  The structural
ToS-compliance posture the abandoned #426 was missing.

After exit, the CLI caller invokes :func:`finalize_interactive_exit`
(the #466 git-floor backstop) which computes the worktree's
commits-ahead using the #448 ``_commits_ahead`` primitive, pushes any
local commits, and writes the terminal completion through the
:mod:`coord.issue_store` seam.  That guarantees the board always gets a
recorded completion regardless of whether the interactive agent
remembered to call ``coord report-result`` first.

When the tmux path is used and the TUI crashes (the attach disconnects
while the session is still live), the CLI caller skips
``finalize_interactive_exit`` and instead shows a reattach hint.
Finalize runs when the user reattaches via ``coord reattach`` and the
session eventually ends.

Imports are deferred / Unix-only — the stdlib ``pty`` / ``termios`` /
``fcntl`` modules are not present on Windows, but agent machines are
Linux/macOS only.
"""

from __future__ import annotations

import fcntl
import os
import select
import shlex
import shutil
import signal
import socket
import struct
import subprocess
import sys
import termios
import time
import tty
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Sequence

if TYPE_CHECKING:
    from coord.config import Config
    from coord.models import Board

from coord.providers.claude_pty import (
    BRACKETED_PASTE_ENABLE,
    BRACKETED_PASTE_END,
    BRACKETED_PASTE_START,
)

__all__ = [
    "launch_human_attended_interactive",
    "finalize_interactive_exit",
    "reap_stale_interactive_sessions",
    "InteractiveFinalizeResult",
    "TMUX_SESSION_PREFIX",
    "TmuxHost",
    "tmux_session_name",
    "tmux_available",
    "tmux_session_alive",
    "list_coord_tmux_sessions",
]


# ── tmux session management (#487) ───────────────────────────────────────────

#: Prefix for all coordinator-managed tmux sessions.  The session name is
#: ``coord-<assignment_id>`` so that a ``tmux ls`` output can be filtered
#: cheaply and the assignment_id is directly recoverable.
TMUX_SESSION_PREFIX = "coord-"

#: SSH connection-multiplexing options for remote (#486/#494) tmux calls.
#: One interactive launch fires ~5+ separate ssh invocations (has-session →
#: new-session → a 50 ms ``capture-pane`` readiness poll → load/paste-buffer →
#: attach → alive-check).  Without multiplexing each is a fresh SSH auth, so a
#: passphrase-protected key prompts once *per call* — a wall of prompts the
#: operator has to clear.  ``ControlMaster=auto`` opens the connection once and
#: every subsequent call reuses it over the same socket (one auth per launch,
#: and zero within ``ControlPersist`` of the previous launch).  ``%C`` hashes
#: host/port/user so the socket path stays short and per-destination.  Applied
#: only on the remote path; the local (``ssh_target=None``) argv is unchanged.
_SSH_MUX_OPTS = [
    "-o", "ControlMaster=auto",
    "-o", "ControlPath=~/.ssh/coord-mux-%C",
    "-o", "ControlPersist=120s",
]


def _get_local_short_hostname() -> str:
    """Return the short hostname of the local machine (split on '.' and lowercased).

    Isolated into a helper so tests can patch ``coord.interactive._get_local_short_hostname``
    without monkey-patching the global ``socket`` module.
    """
    return socket.gethostname().split(".")[0].lower()


@dataclass(frozen=True)
class TmuxHost:
    """Seam that resolves tmux subprocess commands for local or remote hosts.

    ``ssh_target=None`` (the default) means the local machine; subprocess
    calls are plain ``["tmux", ...]``.  When ``ssh_target`` is set, calls
    become ``["ssh", ssh_target, "tmux", ...]`` — optionally with ``-t``
    for commands that need a TTY (e.g. ``attach-session``).

    This seam is introduced in #493 to unblock #486b (remote tmux).  No
    remote callers exist yet; all production call-sites use the default
    ``TmuxHost(None)`` which preserves the exact same subprocess argv as
    before.
    """

    ssh_target: str | None  # None => local

    def cmd(self, tmux_args: list[str], *, tty: bool = False) -> list[str]:
        """Build the full subprocess argv for a tmux invocation.

        Args:
            tmux_args: The tmux sub-command and its arguments, *without*
                the leading ``"tmux"`` token.
            tty: When ``True`` and the host is remote, ``-t`` is inserted
                after ``"ssh"`` so the remote side allocates a pseudo-TTY.
                Use ``True`` only for interactive commands like
                ``attach-session``; leave ``False`` (the default) for all
                control commands (has-session, ls, capture-pane, etc.).

        Returns:
            A complete ``subprocess.run``-ready command list.
        """
        if self.ssh_target is None:
            return ["tmux", *tmux_args]
        return [
            "ssh",
            *(["-t"] if tty else []),
            *_SSH_MUX_OPTS,
            self.ssh_target,
            "tmux",
            *tmux_args,
        ]


def tmux_session_name(assignment_id: str) -> str:
    """Return the canonical tmux session name for *assignment_id*."""
    return f"{TMUX_SESSION_PREFIX}{assignment_id}"


def tmux_available() -> bool:
    """Return ``True`` when ``tmux`` is on the current ``PATH``."""
    return shutil.which("tmux") is not None


def tmux_session_alive(
    session_name: str,
    *,
    host: TmuxHost = TmuxHost(None),
) -> bool:
    """Return ``True`` when the named tmux session exists and is running.

    Uses ``tmux has-session`` which exits 0 when the session is alive and 1
    when it does not exist.  Subprocess / OS errors are treated as
    "not alive" so callers don't need try/except.

    Args:
        session_name: The tmux session name to probe.
        host: Target host.  Defaults to ``TmuxHost(None)`` (local).
    """
    try:
        result = subprocess.run(
            host.cmd(["has-session", "-t", session_name]),
            capture_output=True,
            timeout=5.0,
        )
        return result.returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False


def list_coord_tmux_sessions(
    *,
    host: TmuxHost = TmuxHost(None),
) -> list[dict[str, str]]:
    """Return a list of live ``coord-*`` tmux sessions.

    Each entry is a ``dict`` with at least the key ``"session_name"``.
    Returns an empty list when tmux is not available, not running, or has
    no matching sessions.

    Args:
        host: Target host.  Defaults to ``TmuxHost(None)`` (local).

    Example::

        [{"session_name": "coord-abc123def456"}]
    """
    try:
        result = subprocess.run(
            host.cmd(["ls", "-F", "#{session_name}"]),
            capture_output=True,
            text=True,
            timeout=5.0,
        )
        if result.returncode != 0:
            return []
        sessions: list[dict[str, str]] = []
        for name in result.stdout.splitlines():
            name = name.strip()
            if name.startswith(TMUX_SESSION_PREFIX):
                sessions.append({"session_name": name})
        return sessions
    except (subprocess.SubprocessError, OSError):
        return []


def _inject_briefing_into_tmux_session(
    session_name: str,
    briefing: str,
    *,
    timeout: float = 12.0,
    host: TmuxHost = TmuxHost(None),
) -> bool:
    """Wait for the tmux pane to stabilise then inject *briefing* via bracketed paste.

    The injection strategy mirrors the PTY-relay path:

    1. Poll ``tmux capture-pane -p`` every 50 ms.
    2. Once the pane has been non-empty AND stable for
       :data:`_READY_QUIESCE_S` seconds (or the overall *timeout* lapses),
       load the briefing text into a tmux named buffer (``coord-brief``)
       via **stdin** (``tmux load-buffer -b coord-brief -``) and invoke
       ``tmux paste-buffer -p``.
    3. The ``-p`` flag makes tmux send the content wrapped in bracketed-paste
       markers (``ESC[200~`` … ``ESC[201~``) **if** the target application
       has requested bracketed-paste mode.  Since ``claude``'s TUI always
       enables bracketed paste, this is equivalent to the PTY relay's
       manual bracketed-paste block.

    The stdin-based ``load-buffer -`` approach avoids creating a local
    temporary file, which is important for the remote-host path (#486b):
    no ``scp`` or ``ssh`` file-transfer is needed.

    Args:
        session_name: The tmux session to inject into.
        briefing: Text to pre-fill in the TUI input box.
        timeout: Quiescence-wait deadline in seconds.
        host: Target host.  Defaults to ``TmuxHost(None)`` (local).

    Returns:
        ``True`` when the briefing was injected, ``False`` on timeout
        or subprocess error.
    """
    if not briefing.strip():
        return True  # nothing to inject — trivially OK

    try:
        deadline = time.monotonic() + timeout
        prev_content: str | None = None
        quiescent_since: float | None = None

        while time.monotonic() < deadline:
            time.sleep(0.05)
            try:
                cap = subprocess.run(
                    host.cmd(["capture-pane", "-p", "-t", session_name]),
                    capture_output=True,
                    text=True,
                    timeout=2.0,
                )
            except (subprocess.SubprocessError, OSError):
                break
            if cap.returncode != 0:
                break  # session gone

            content = cap.stdout
            now = time.monotonic()
            if content != prev_content:
                prev_content = content
                quiescent_since = now if content.strip() else None
            elif content.strip() and quiescent_since is not None:
                if now - quiescent_since >= _READY_QUIESCE_S:
                    break  # stable and non-empty — inject

        # Load briefing into a named tmux buffer via stdin.
        # Using "-" as the source tells tmux to read from stdin, which
        # works for both local and remote hosts without temporary files.
        subprocess.run(
            host.cmd(["load-buffer", "-b", "coord-brief", "-"]),
            input=briefing.rstrip("\n"),
            text=True,
            capture_output=True,
            timeout=5.0,
        )
        # Paste with bracketed-paste enabled (``-p``).
        subprocess.run(
            host.cmd(["paste-buffer", "-p", "-t", session_name, "-b", "coord-brief"]),
            capture_output=True,
            timeout=5.0,
        )
        return True
    except (subprocess.SubprocessError, OSError):
        return False


def _launch_via_tmux(
    argv: Sequence[str],
    briefing: str,
    session_name: str,
    *,
    cwd: str | None = None,
    host: TmuxHost = TmuxHost(None),
    raw_shell_cmd: str | None = None,
) -> int | None:
    """Create (or reuse) a named tmux session running *argv* and attach.

    This is the preferred path when ``tmux`` is available and
    *session_name* is provided (#487).  The strategy:

    1. If the session does not already exist, create it with
       ``tmux new-session -d`` running ``argv`` as the session command.
       The briefing is then injected via
       :func:`_inject_briefing_into_tmux_session`.
    2. If the session already exists (reattach after TUI crash), skip
       creation and injection — the session is already running.
    3. Attach the current terminal with
       ``tmux attach-session -t <session_name>``.  If the TUI process is
       killed (e.g. SIGHUP on TUI crash), only the *attach* subprocess
       dies; the tmux session and ``claude`` inside it keep running.

    Returns ``tmux attach-session``'s exit code (typically ``0`` for both
    clean exits and user-initiated detach with ``Ctrl-b d``).  Callers
    should check :func:`tmux_session_alive` after this returns to
    distinguish "session ended" from "user detached — session still live".

    Returns ``None`` when session creation fails so that the caller
    (:func:`launch_human_attended_interactive`) can fall back to the
    PTY relay without double-echoing the briefing.

    Args:
        argv: Worker command.  Used as-is when *raw_shell_cmd* is ``None``.
        briefing: Text to pre-fill in the TUI input box.
        session_name: tmux session name (``coord-<assignment_id>``).
        cwd: Starting directory for the session (local) or the tmux ``-c``
            start-directory (remote).  For remote worktree launches
            (#486b) this is typically ``None`` because the *raw_shell_cmd*
            itself ``cd``s into the worktree.
        host: Target host.  ``TmuxHost(None)`` means local; a non-``None``
            ``ssh_target`` triggers the remote-safe single-string SSH path.
        raw_shell_cmd: When provided, use this string verbatim as the
            tmux session command instead of ``shlex.join(argv)``.  Intended
            for the remote launch path (#486b) where the command includes
            ``$HOME`` paths and shell operators (``&&``, ``||``) that must
            survive round-tripping through SSH and the remote shell without
            being re-split or having ``~`` masked by single-quoting.
    """
    # Determine terminal dimensions for the new session.
    try:
        cols, rows = os.get_terminal_size(sys.stdout.fileno())
    except (OSError, AttributeError, ValueError):
        cols, rows = 220, 50

    already_alive = tmux_session_alive(session_name, host=host)

    if not already_alive:
        # ``raw_shell_cmd`` is used by the remote launch path (#486b) so that
        # paths containing ``$HOME`` / ``~`` and shell operators are passed
        # verbatim to the remote shell rather than being re-quoted by
        # ``shlex.join``.  When not provided, fall back to the default
        # behaviour: quote the argv list.
        shell_cmd = raw_shell_cmd if raw_shell_cmd is not None else shlex.join(list(argv))

        if host.ssh_target is not None:
            # Remote: build the entire ``tmux new-session`` invocation as a
            # single shell string and pass it to SSH as ONE argument.  If we
            # used ``host.cmd([..., shell_cmd])`` (multiple list elements),
            # SSH would concatenate everything after the hostname with spaces
            # and the remote shell would split ``shell_cmd`` at operator
            # tokens (``&&``, ``||``, …), breaking the intended multi-step
            # command.  Sending the whole tmux invocation as one string
            # preserves quoting and lets the remote shell hand the
            # properly-quoted ``shell_cmd`` to tmux as a single argument.
            # Tmux then runs ``$SHELL -c <shell_cmd>`` where ``$HOME`` and
            # other shell expansions work correctly.
            _parts: list[str] = [
                "tmux", "new-session", "-d",
                "-s", shlex.quote(session_name),
                "-x", str(max(cols, 40)),
                "-y", str(max(rows, 10)),
            ]
            if cwd:
                _parts += ["-c", shlex.quote(cwd)]
            # shlex.quote wraps shell_cmd so the remote shell treats it as
            # exactly one argument to tmux new-session.
            _parts.append(shlex.quote(shell_cmd))
            create_cmd = ["ssh", *_SSH_MUX_OPTS, host.ssh_target, " ".join(_parts)]
        else:
            # Local: pass args directly to tmux via subprocess list form.
            # No shell is involved, so shell_cmd is passed as ONE argument
            # without any re-quoting or special-character issues.
            create_cmd = host.cmd([
                "new-session", "-d",
                "-s", session_name,
                "-x", str(max(cols, 40)),
                "-y", str(max(rows, 10)),
            ])
            if cwd:
                create_cmd += ["-c", cwd]
            create_cmd.append(shell_cmd)

        try:
            result = subprocess.run(
                create_cmd,
                capture_output=True,
                text=True,
                timeout=15.0,
            )
        except (subprocess.SubprocessError, OSError):
            return None  # signal caller to fall back to PTY relay

        if result.returncode != 0:
            # Session creation failed (name collision, tmux daemon error, …).
            return None  # signal caller to fall back to PTY relay

        # Inject briefing (best-effort; a failure here is non-fatal).
        if briefing.strip():
            _inject_briefing_into_tmux_session(session_name, briefing, host=host)

    # Attach.  ``subprocess.run`` (not ``os.execvp``) is intentional: we
    # need this process to continue after the operator detaches so that
    # the CLI caller (``coord assign``) can check whether the session is
    # still alive and decide whether to run ``finalize_interactive_exit``.
    # The attach-session call uses tty=True because it needs a pseudo-TTY
    # on the remote side when the host is remote.
    try:
        if host.ssh_target is None and os.environ.get("TMUX"):
            # The operator is already inside a tmux client (nested).
            # ``attach-session`` refuses to nest ("sessions should be nested
            # with care, unset $TMUX to force") and exits 1, orphaning the
            # session so the terminal never opens.  ``switch-client`` switches
            # the current client to the new session instead — it works from
            # inside tmux and lands the operator in the claude session.  It
            # returns immediately (unlike ``attach-session`` it does not block
            # until detach), so the caller's ``tmux_session_alive`` check then
            # treats the live session as "operator is now in it".
            cmd: list[str] = ["tmux", "switch-client", "-t", session_name]
        else:
            cmd = list(host.cmd(["attach-session", "-t", session_name], tty=True))
        attach_result = subprocess.run(cmd)
        return attach_result.returncode
    except (subprocess.SubprocessError, OSError):
        return 1


# ── Quiescence constants for the PTY relay path ───────────────────────────────

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
    assignment_id: str | None = None,
    cwd: str | None = None,
) -> int:
    """Run *argv* attached to the current TTY with *briefing* pre-filled.

    Used exclusively by ``coord assign --interactive``.  The function
    returns the child's exit code; the caller decides what (if anything)
    to record on the board.  No GitHub comments are posted from inside
    this function — the human owns the session lifecycle.

    When *assignment_id* is provided and ``tmux`` is available, the session
    is hosted in a named tmux session ``coord-<assignment_id>`` (#487).
    This means the session survives a TUI crash — the operator can reattach
    later with ``coord reattach <assignment_id>``.  If tmux is not available
    or *assignment_id* is omitted, falls back to the PTY relay path.

    Args:
        argv: The worker command (typically built via
            :meth:`ClaudePtyProvider.build_command`).
        briefing: The text to PRE-FILL in the TUI's input box.  Wrapped
            in a bracketed-paste block; the operator presses Enter to
            submit.  An empty string disables the pre-fill entirely.
        assignment_id: Optional coordinator assignment ID.  When provided
            and tmux is available, the session runs inside a persistent
            tmux session named ``coord-<assignment_id>``.
        cwd: Working directory for the child.  ``None`` keeps the
            parent's cwd.

    Returns:
        The child's exit status (``0`` on clean exit; ``128 + signum``
        on termination by signal).  For the tmux path, returns
        ``tmux attach-session``'s exit code — callers should check
        :func:`tmux_session_alive` afterwards to distinguish
        "session ended" from "user detached with Ctrl-b d".
    """
    # #487 — prefer the tmux path when assignment_id is available.
    if assignment_id and tmux_available():
        sname = tmux_session_name(assignment_id)
        # Echo briefing BEFORE the tmux session starts (same as PTY path).
        # Important: only echo here on the tmux path.  _launch_via_pty()
        # handles its own echo so that it works correctly when called
        # stand-alone (no assignment_id / no tmux).
        if briefing.strip():
            _hdr = (
                "--- seeded briefing -- review below; "
                "submit the pre-filled input in Claude to send ---"
            )
            _ftr = "-" * len(_hdr)
            _preview = f"\n{_hdr}\n{briefing.rstrip()}\n{_ftr}\n\n"
            try:
                os.write(sys.stdout.fileno(), _preview.encode("utf-8"))
            except OSError:
                pass
        rc = _launch_via_tmux(argv, briefing, sname, cwd=cwd)
        if rc is not None:
            return rc
        # tmux session creation failed — fall back to the PTY relay.
        # Note: _launch_via_pty will echo the briefing again; that is
        # acceptable for this rare fallback case.

    return _launch_via_pty(argv, briefing, cwd=cwd)


def _launch_via_pty(
    argv: Sequence[str],
    briefing: str,
    *,
    cwd: str | None = None,
) -> int:
    """PTY relay implementation — the original pty.fork() path.

    Kept as a separate function so :func:`launch_human_attended_interactive`
    can delegate to it both directly (no tmux) and as a fallback from the
    tmux path.
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

    # ── #485 human-readable briefing preview ────────────────────────────────
    # Echo the briefing to the operator's terminal BEFORE pty.fork() while
    # the terminal is still in cooked mode (before tty.setraw).  This lands
    # in scrollback just above Claude's TUI so the operator can read the
    # briefing before pressing Enter to submit the pre-filled chip.
    #
    # IMPORTANT: do NOT move this into the relay ``while True`` loop.  At
    # that point Claude is actively drawing its screen; injecting bytes onto
    # fd_out there corrupts the display.
    if briefing.strip():
        _hdr = (
            "--- seeded briefing -- review below; "
            "submit the pre-filled input in Claude to send ---"
        )
        _ftr = "-" * len(_hdr)
        _preview = f"\n{_hdr}\n{briefing.rstrip()}\n{_ftr}\n\n"
        try:
            os.write(fd_out, _preview.encode("utf-8"))
        except OSError:
            pass

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


def _remove_worktree(repo_path: Path, wt_path: Path) -> bool:
    """Best-effort removal of a git worktree for an interactive session.

    Mirrors :meth:`coord.agent.AgentServer._cleanup_worktree` without pulling
    in the full AgentServer graph.  Returns ``True`` on success, ``False`` if
    every removal strategy fails.
    """
    removed = False
    if wt_path.exists():
        try:
            result = subprocess.run(
                ["git", "worktree", "remove", str(wt_path), "--force"],
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                timeout=15.0,
            )
            removed = result.returncode == 0
        except (subprocess.SubprocessError, OSError):
            pass

        if not removed:
            try:
                shutil.rmtree(wt_path, ignore_errors=True)
                removed = True
            except OSError:
                pass
    else:
        removed = True  # already gone

    # Prune the stale git admin entry regardless of whether the directory
    # was physically removed — a stale entry blocks the next dispatch on
    # the same branch.
    try:
        subprocess.run(
            ["git", "worktree", "prune"],
            cwd=str(repo_path),
            capture_output=True,
            timeout=10.0,
        )
    except (subprocess.SubprocessError, OSError):
        pass

    return removed


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
        worktree_removed: ``True`` when the interactive worktree was
            successfully removed by the backstop.  ``False`` when no
            *repo_path* was supplied (the caller owns cleanup) or when
            the removal failed.
    """

    terminal_status: str
    commits_ahead: int | None
    push_ok: bool
    push_error: str | None
    already_recorded: bool
    seam_outcome: object | None = None  # StoreOutcome | None
    worktree_removed: bool = field(default=False)


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
    worktree_path: str | None = None,
    base_branch: str,
    exit_code: int,
    started_at: float | None = None,
    log_path: str | None = None,
    repo_path: str | None = None,
) -> InteractiveFinalizeResult:
    """Git-floor backstop for the interactive launcher exit path (#466).

    Called AFTER :func:`launch_human_attended_interactive` returns.
    Performs three steps, then optionally removes the interactive worktree:

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

    When *repo_path* is supplied (the interactive launcher always provides
    it after the worktree-per-session fix), the function removes the
    worktree after recording the terminal state — matching the cleanup
    discipline of :meth:`coord.agent.AgentServer._cleanup_worktree`.
    """
    # Respect an explicit `coord report-result` from the agent.  Without
    # this check, every review session (which legitimately has 0 commits)
    # would have its agent-reported verdict overwritten with an advisory
    # the instant the human closed the TTY.
    if _assignment_already_recorded(assignment_id):
        worktree_removed = False
        if repo_path is not None and worktree_path is not None:
            worktree_removed = _remove_worktree(Path(repo_path), Path(worktree_path))
        return InteractiveFinalizeResult(
            terminal_status="report-result",  # informational only
            commits_ahead=None,
            push_ok=True,
            push_error=None,
            already_recorded=True,
            seam_outcome=None,
            worktree_removed=worktree_removed,
        )

    # worktree_path is None for a human-attended REVIEW (migration A1): the
    # review runs read-only in the LIVE checkout, so there is no session
    # worktree to push from, count commits in, or remove.  Guard every git
    # step on a real worktree path so the review backstop still records a
    # terminal state (commits_ahead=None) without ever touching the checkout.
    wt_path = Path(worktree_path) if worktree_path else None

    # Step 1 — push.  Failure is non-fatal but recorded.  Skip the push
    # entirely when there is no worktree (review) or its directory doesn't
    # exist (extremely rare, but possible if the operator nuked it).
    push_ok = True
    push_error: str | None = None
    if wt_path is not None and wt_path.exists():
        push_ok, push_error = _git_push(wt_path)

    # Step 2 — count commits ahead of the base.  None = git failed; the
    # seam treats that as "unknown, assume non-zero" so a git outage
    # never falsely flags an advisory.
    # Deferred import of the shared primitive from coord.agent (#466) so
    # this module stays importable in isolation without pulling in the full
    # AgentServer graph at module load time.
    from coord.agent import _commits_ahead  # noqa: PLC0415
    commits = None
    if wt_path is not None and wt_path.exists():
        commits = _commits_ahead(wt_path, base_branch)

    branch_now = (
        _current_branch(wt_path)
        if wt_path is not None and wt_path.exists()
        else None
    )

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

    # Step 4 — remove the interactive worktree when repo_path is provided.
    # Matches _cleanup_worktree discipline: always runs, best-effort.
    worktree_removed = False
    if repo_path is not None and wt_path is not None:
        worktree_removed = _remove_worktree(Path(repo_path), wt_path)

    return InteractiveFinalizeResult(
        terminal_status=outcome.status,
        commits_ahead=commits,
        push_ok=push_ok,
        push_error=push_error,
        already_recorded=False,
        seam_outcome=outcome,
        worktree_removed=worktree_removed,
    )


# ── Remote (#486d) finalize for a remote interactive FIX ──────────────────────


def _remote_push_and_count(
    ssh_target: str,
    remote_worktree_sh: str,
    branch: str,
    base_branch: str,
    *,
    timeout: float = 90.0,
) -> tuple[bool, str | None, int | None, str | None]:
    """Over ssh: push the remote fix worktree's commits to ``origin/<branch>``
    (a fast-forward — the worktree started at ``origin/<branch>`` plus the
    session's commits), then read commits-ahead-of-base and the branch.

    Returns ``(push_ok, push_error, commits_ahead, branch)``.  Any push error
    (incl. a non-fast-forward) sets ``push_ok=False`` with the remote's
    stderr; the caller then PRESERVES the worktree instead of removing it, so
    the operator can recover the commits.  ``remote_worktree_sh`` is a
    ``$HOME``-form path the remote shell expands — it is NOT quoted (coord
    generates it: ``$HOME/.coord/worktrees/<hex>``).  *branch*/*base_branch*
    are shell-quoted since they come from issue data.
    """
    refspec = shlex.quote(f"HEAD:{branch}")
    base_ref = shlex.quote(f"origin/{base_branch}")
    remote_cmd = (
        f"cd {remote_worktree_sh} || exit 91; "
        f"git push origin {refspec}; echo \"__PUSH_RC=$?\"; "
        f"echo \"__COMMITS=$(git rev-list --count {base_ref}..HEAD 2>/dev/null)\"; "
        f"echo \"__BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null)\""
    )
    try:
        result = subprocess.run(
            ["ssh", *_SSH_MUX_OPTS, ssh_target, remote_cmd],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        return False, str(exc), None, None

    push_rc: int | None = None
    commits: int | None = None
    branch_now: str | None = None
    for raw in (result.stdout or "").splitlines():
        line = raw.strip()
        if line.startswith("__PUSH_RC="):
            try:
                push_rc = int(line.split("=", 1)[1])
            except ValueError:
                pass
        elif line.startswith("__COMMITS="):
            val = line.split("=", 1)[1].strip()
            if val.isdigit():
                commits = int(val)
        elif line.startswith("__BRANCH="):
            branch_now = line.split("=", 1)[1].strip() or None
    if branch_now in (None, "", "HEAD"):
        branch_now = None
    push_ok = push_rc == 0
    push_error = (
        None if push_ok
        else (result.stderr or result.stdout or "git push failed").strip()
    )
    return push_ok, push_error, commits, branch_now


def _remote_worktree_remove(
    ssh_target: str,
    remote_repo_sh: str,
    remote_worktree_sh: str,
    *,
    timeout: float = 30.0,
) -> bool:
    """Best-effort ``git worktree remove --force`` + ``prune`` over ssh.

    The worktree is a worktree OF the remote ``~/src/<repo>`` checkout, so
    removing it never touches the live checkout (the worker-worktree base).
    Returns ``True`` when the remote command ran to completion.
    """
    remote_cmd = (
        f"git -C {remote_repo_sh} worktree remove {remote_worktree_sh} --force"
        f" 2>/dev/null; git -C {remote_repo_sh} worktree prune 2>/dev/null;"
        f" echo __WT_DONE"
    )
    try:
        result = subprocess.run(
            ["ssh", *_SSH_MUX_OPTS, ssh_target, remote_cmd],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    return "__WT_DONE" in (result.stdout or "")


def finalize_remote_interactive_exit(
    *,
    assignment_id: str,
    repo_name: str,
    repo_github: str,
    issue_number: int,
    machine_name: str,
    ssh_target: str,
    remote_worktree_sh: str,
    remote_repo_sh: str,
    branch: str,
    base_branch: str,
    exit_code: int,
    started_at: float | None = None,
) -> InteractiveFinalizeResult:
    """Remote (#486d) analog of :func:`finalize_interactive_exit` for a remote
    interactive FIX.

    The fix session ran in a worktree ON the remote machine, so the local
    git-floor backstop can't see its commits.  This sshs in to:

    1. push the worktree's commits to ``origin/<branch>`` (fast-forward) and
       read commits-ahead-of-base + the branch;
    2. record the completion through the issue_store seam LOCALLY (coordinator
       DB + GitHub comment) so the pipeline sees a normal completion and the
       re-review fires;
    3. remove the remote worktree — but ONLY when the push succeeded, so
       unpushed commits are never silently destroyed.

    Respects an existing ``coord report-result`` (same as the local backstop):
    if the row already holds a terminal status, the verdict/result wins and
    this only attempts worktree cleanup.
    """
    if _assignment_already_recorded(assignment_id):
        removed = _remote_worktree_remove(
            ssh_target, remote_repo_sh, remote_worktree_sh,
        )
        return InteractiveFinalizeResult(
            terminal_status="report-result",
            commits_ahead=None,
            push_ok=True,
            push_error=None,
            already_recorded=True,
            seam_outcome=None,
            worktree_removed=removed,
        )

    push_ok, push_error, commits, branch_now = _remote_push_and_count(
        ssh_target, remote_worktree_sh, branch, base_branch,
    )

    duration = (
        max(0.0, time.time() - started_at) if started_at is not None else None
    )

    from coord.issue_store import CompletionRecord, post_completion  # noqa: PLC0415

    record = CompletionRecord(
        assignment_id=assignment_id,
        machine_name=machine_name,
        repo_name=repo_name,
        repo_github=repo_github,
        issue_number=issue_number,
        exit_code=exit_code,
        commits_ahead=commits,
        branch=branch_now or branch,
        duration_seconds=duration,
        log_path=None,
        summary="",
    )
    outcome = post_completion(record)

    # Clean up the remote worktree only on a successful push — a failed push
    # means the commits live nowhere but the worktree, so preserve it.
    worktree_removed = False
    if push_ok:
        worktree_removed = _remote_worktree_remove(
            ssh_target, remote_repo_sh, remote_worktree_sh,
        )

    return InteractiveFinalizeResult(
        terminal_status=outcome.status,
        commits_ahead=commits,
        push_ok=push_ok,
        push_error=push_error,
        already_recorded=False,
        seam_outcome=outcome,
        worktree_removed=worktree_removed,
    )


# ── Stale interactive-session reaper ─────────────────────────────────────────


def reap_stale_interactive_sessions(
    board: "Board",
    config: "Config",
    *,
    worktrees_dir: Path | None = None,
) -> list[str]:
    """Sweep *board.active* for dead interactive sessions and release their claims.

    Called from :func:`coord.reconcile.reconcile` on every pass so that
    ``coord resume`` / ``coord notify`` automatically clean up orphaned
    worktrees and stale ``running`` DB rows for interactive assignments whose
    tmux session died without going through the normal ``/exit`` → finalize
    path.

    **Detection strategy**: an assignment is stale when ALL of:

    * ``provider_name == "claude-pty"`` (i.e. dispatched via ``--interactive``)
    * ``status`` is ``"running"`` or ``"pending"`` in the in-memory board
    * tmux is available on the local machine AND ``coord-<assignment_id>``
      is NOT alive

    When tmux is NOT available the function returns immediately — the PTY
    relay is handled synchronously inside ``coord assign``, so no orphan
    accumulates there.

    **Remote sessions are skipped.** :func:`tmux_session_alive` probes only the
    local tmux server; a remote interactive session launched via ``coord assign
    --interactive <remote>`` stores ``machine_name=<remote>`` in the DB and
    runs its tmux session on that host.  If the reaper were allowed to proceed,
    it would see ``tmux_session_alive() == False`` (no local session) and
    falsely stamp the remote session ``failed`` while it is still live.  The
    reaper therefore skips any assignment whose machine does not resolve to the
    local host.

    For each stale **local** session the function:

    1. Counts commits the worktree is ahead of the base branch (before
       removing the worktree, so git can still run).
    2. Removes the interactive worktree at
       ``~/.coord/worktrees/<assignment_id>`` (best-effort via
       :func:`_remove_worktree`; falls back to ``shutil.rmtree``).
    3. Marks the assignment in the SQLite DB (only when the row is still
       ``running`` / ``pending`` — a ``coord report-result`` that raced the
       reaper is left untouched):

       - ``advisory`` when the worktree had 0 commits ahead of the base branch
         (the operator killed the session before producing any work).
       - ``failed`` otherwise (commits ≥ 1 or the count is unknown because the
         worktree was absent when the reaper ran).

    4. Moves the assignment from ``board.active`` to ``board.completed``
       and sets the in-memory status to match the DB value.

    Returns the assignment IDs that were reaped.  The caller should include
    these in its ``changed`` list so ``save_board`` is triggered.
    """
    if not tmux_available():
        return []

    from coord.state import COORD_DIR, get_connection  # noqa: PLC0415

    if worktrees_dir is None:
        worktrees_dir = COORD_DIR / "worktrees"

    machines_by_name = {m.name: m for m in config.machines}
    repos_by_name = {r.name: r for r in config.repos}
    _local_hn = _get_local_short_hostname()
    reaped: list[str] = []
    now = time.time()

    for a in board.active[:]:  # iterate a copy — we mutate board mid-loop
        if a.provider_name != "claude-pty":
            continue
        if a.status not in ("running", "pending"):
            continue
        if not a.assignment_id:
            continue

        sname = tmux_session_name(a.assignment_id)
        if tmux_session_alive(sname):
            continue  # session is live — leave it alone

        # ── Session is dead locally: check it's actually a LOCAL session ──

        # Reconstruct machine info for locality check and repo path lookup.
        machine = machines_by_name.get(a.machine_name or "")

        # Remote-machine guard (#515 follow-up): tmux_session_alive() only
        # checks the coordinator's local tmux server.  A remote session (coord
        # assign --interactive <remote_host>) runs on a different machine and
        # will always appear "not alive" locally even while it's running.
        # Skip the reap entirely so we don't falsely stamp a live remote
        # session as failed and release its dispatch claim.
        if machine is not None:
            _is_local = (
                machine.name.lower() == _local_hn
                or machine.host.split(".")[0].lower() == _local_hn
            )
            if not _is_local:
                continue  # remote session — leave it alone
        elif a.machine_name:
            # machine_name is set but not found in config — unknown host;
            # skip rather than risk a false-positive reap.
            continue
        # else: machine_name is None/empty → coordinator-local session, proceed

        # Reconstruct the repo root so ``git worktree remove`` can run
        # relative to it; fall back to shutil.rmtree when unavailable.
        repo_path_val: str | None = None
        if machine is not None and a.repo_name:
            rp = machine.repo_path(a.repo_name)
            if rp:
                repo_path_val = str(Path(rp).expanduser())

        # ── Determine terminal status from commit count ────────────────────
        # Count commits ahead of the base branch BEFORE removing the worktree
        # so that git can still run against it.
        #   advisory: 0 commits — session died before producing any work.
        #   failed:   ≥1 commits or unknown — abandoned work-in-progress.
        wt_path = worktrees_dir / a.assignment_id
        commits: int | None = None
        if wt_path.exists():
            repo = repos_by_name.get(a.repo_name or "")
            base_branch = repo.default_branch if repo is not None else "main"
            from coord.agent import _commits_ahead  # noqa: PLC0415
            commits = _commits_ahead(wt_path, base_branch)
        terminal_status = "advisory" if commits == 0 else "failed"

        # 1. Remove worktree (best-effort).
        if wt_path.exists():
            if repo_path_val is not None:
                try:
                    _remove_worktree(Path(repo_path_val), wt_path)
                except Exception:  # noqa: BLE001
                    # Fall back to a plain directory removal so we always
                    # make progress even when git is unavailable.
                    try:
                        shutil.rmtree(wt_path, ignore_errors=True)
                    except OSError:
                        pass
            else:
                try:
                    shutil.rmtree(wt_path, ignore_errors=True)
                except OSError:
                    pass

        # 2. Mark ``terminal_status`` in the DB if the row is still live.
        try:
            conn = get_connection()
            conn.execute(
                "UPDATE assignments SET status=?, finished_at=? "
                "WHERE assignment_id=? AND status IN ('running', 'pending')",
                (terminal_status, now, a.assignment_id),
            )
            conn.commit()
        except Exception:  # noqa: BLE001
            pass  # non-fatal — the board update below still releases the claim

        # 3. Update in-memory board so the claim is released immediately
        #    (without waiting for the next build_board() call).
        moved = board.mark_failed_by_id(a.assignment_id, finished_at=now)
        # mark_failed_by_id always sets status="failed"; upgrade to advisory
        # when the commit count shows no work was produced.
        if moved is not None and terminal_status == "advisory":
            moved.status = "advisory"
        reaped.append(a.assignment_id)

    return reaped
