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
* Once the TUI emits the bracketed-paste-enable DECSET (``ESC[?2004h``),
  the rendered input box (:data:`~coord.providers.claude_pty.INPUT_BOX_MARKER`)
  has appeared, AND the render has gone quiet, the briefing is PRE-FILLED
  into the input box as a single bracketed paste — NO trailing carriage
  return.  The operator presses Enter to submit.  The paste is then
  **verified** (re-observing the master output for a fingerprint of the
  briefing) and **retried** on a miss (#865: a mistimed paste — e.g. one
  that races an async startup banner like "Fable 5 is back" — used to be
  silently dropped with no way to tell).  See :class:`_PrefillState`.
* Window-size changes (SIGWINCH) are forwarded to the child via
  ``TIOCSWINSZ`` so the TUI re-flows correctly when the operator
  resizes their terminal.

**tmux session (preferred, when tmux is available and assignment_id is
provided — #487)**

* A named tmux session ``coord-<assignment_id>`` is created (or reused).
* ``claude`` runs directly inside the tmux session; tmux provides the pty.
* The briefing is injected via :func:`_inject_briefing_into_tmux_session`:
  a marker+quiescence-anchored poll on the pane output, then
  ``tmux paste-buffer -p`` (bracketed-paste mode), then a re-``capture-pane``
  verification with retry on a miss — same #865 rationale as the PTY path.
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
import json
import logging
import os
import re
import select
import shlex
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import termios
import time
import tty
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Sequence

if TYPE_CHECKING:
    from coord.agent import MergeVerify
    from coord.config import Config
    from coord.models import Board

from coord.providers.claude_pty import (
    BRACKETED_PASTE_ENABLE,
    BRACKETED_PASTE_END,
    BRACKETED_PASTE_START,
    INPUT_BOX_MARKER,
    INPUT_BOX_MARKER_BYTES,
    briefing_fingerprint,
    fingerprint_in_bytes,
    fingerprint_in_text,
)

__all__ = [
    "launch_human_attended_interactive",
    "finalize_interactive_exit",
    "reap_stale_interactive_sessions",
    "reap_stale_remote_interactive_sessions",
    "InteractiveFinalizeResult",
    "TMUX_SESSION_PREFIX",
    "TmuxHost",
    "tmux_session_name",
    "tmux_available",
    "tmux_session_alive",
    "tmux_pane_dead",
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

    ``batch=True`` adds ``BatchMode=yes`` (+ a short ``ConnectTimeout``) so the
    ssh call NEVER prompts for a passphrase/password — it fails fast instead.
    Use it for **non-interactive, background** probes (e.g. the `coord sessions
    --remote` fleet sweep the TUI runs at startup): without it, ssh grabs the
    controlling terminal to ask for the key passphrase, corrupting the TUI
    display (#486 Leg 4 regression).  Leave it ``False`` for human-attended
    paths (launch / reattach) that legitimately prompt once.
    """

    ssh_target: str | None  # None => local
    batch: bool = False  # True => BatchMode=yes (never prompt; fail fast)

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
        # BatchMode must precede the destination; it disables every interactive
        # auth prompt (passphrase/password) so a background probe degrades to a
        # fast failure instead of hijacking the terminal.  An existing
        # ControlMaster socket / ssh-agent key still authenticates silently.
        batch_opts = (
            ["-o", "BatchMode=yes", "-o", "ConnectTimeout=4"] if self.batch else []
        )
        # ssh space-joins the remote command args (NO escaping) and runs the
        # result through the remote LOGIN SHELL.  Passing a tmux format like
        # ``#{session_name}`` as a bare token means that shell sees ``#`` — a
        # comment marker — and silently truncates the command
        # (``tmux ls -F #{session_name}`` → ``tmux ls -F``, which errors and
        # returns nothing).  This broke remote session discovery for `coord
        # sessions --remote` and the TUI reattach sweep.  Shell-quote each tmux
        # arg so the remote shell receives it verbatim.  Plain tokens (``ls``,
        # ``-t``, ``coord-<id>``) quote to themselves, so the launch/attach
        # argv is byte-for-byte unchanged; only shell-special chars (``#{}``,
        # spaces) get protected.
        return [
            "ssh",
            *(["-t"] if tty else []),
            *batch_opts,
            *_SSH_MUX_OPTS,
            self.ssh_target,
            "tmux",
            *(shlex.quote(a) for a in tmux_args),
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


def tmux_pane_dead(
    session_name: str,
    *,
    host: TmuxHost = TmuxHost(None),
) -> bool:
    """Return ``True`` when the named session exists but its pane process has exited.

    A "dead pane" means the tmux session is still up (``tmux has-session``
    returns 0) but the child process that was running inside the pane —
    typically ``claude`` — has exited.  This happens when the operator detaches
    while the session is running and claude finishes in the background.

    Uses ``tmux list-panes -F "#{pane_dead}" -t <session>``, which emits
    ``1`` when a pane's child process has exited and ``0`` while it is still
    running.  Returns ``True`` only when **all** panes in the session report
    dead (i.e. at least one alive pane keeps the session active).

    Subprocess / OS errors return ``False`` (treat as alive to avoid spurious
    reaping of sessions we can't probe).

    Args:
        session_name: The tmux session name to probe.
        host: Target host.  Defaults to ``TmuxHost(None)`` (local).
    """
    try:
        result = subprocess.run(
            host.cmd(["list-panes", "-F", "#{pane_dead}", "-t", session_name]),
            capture_output=True,
            text=True,
            timeout=5.0,
        )
        if result.returncode != 0:
            return False
        lines = [ln.strip() for ln in result.stdout.splitlines() if ln.strip()]
        if not lines:
            return False
        # All panes must be dead for the session to be considered dead-pane.
        return all(ln == "1" for ln in lines)
    except (subprocess.SubprocessError, OSError):
        return False


def list_coord_tmux_sessions(
    *,
    host: TmuxHost = TmuxHost(None),
) -> list[dict[str, str]]:
    """Return a list of live ``coord-*`` tmux sessions.

    Each entry is a ``dict`` with keys:

    * ``"session_name"`` — the tmux session name (``coord-<assignment_id>``).
    * ``"pane_dead"`` — ``"1"`` when the session's pane process has exited
      (``claude`` finished but the tmux session is still up — the
      detach-and-abandon case), ``"0"`` while the pane is still running.

    Returns an empty list when tmux is not available, not running, or has
    no matching sessions.

    Uses ``tmux list-panes -a -F "#{session_name}\t#{pane_dead}"`` to fetch
    both the session name and pane-dead status in a single subprocess call.
    When a session has multiple panes the *most conservative* (alive=0) value
    wins — i.e. the session is only marked dead when every pane has exited.

    Args:
        host: Target host.  Defaults to ``TmuxHost(None)`` (local).

    Example::

        [
            {"session_name": "coord-abc123", "pane_dead": "0"},
            {"session_name": "coord-def456", "pane_dead": "1"},
        ]
    """
    try:
        result = subprocess.run(
            host.cmd(["list-panes", "-a", "-F", "#{session_name}\t#{pane_dead}"]),
            capture_output=True,
            text=True,
            timeout=5.0,
        )
        if result.returncode != 0:
            return []
        # Collect per-session: "0" (alive) beats "1" (dead) — any alive pane
        # keeps the session active.
        pane_dead_per_session: dict[str, str] = {}
        for raw_line in result.stdout.splitlines():
            parts = raw_line.split("\t", 1)
            if len(parts) < 2:
                continue
            name, pane_dead = parts[0].strip(), parts[1].strip()
            if not name.startswith(TMUX_SESSION_PREFIX):
                continue
            existing = pane_dead_per_session.get(name)
            # "0" (alive) wins over "1" (dead).
            if existing is None or pane_dead == "0":
                pane_dead_per_session[name] = pane_dead
        return [
            {"session_name": name, "pane_dead": pd}
            for name, pd in pane_dead_per_session.items()
        ]
    except (subprocess.SubprocessError, OSError):
        return []


def _inject_briefing_into_tmux_session(
    session_name: str,
    briefing: str,
    *,
    timeout: float = 12.0,
    host: TmuxHost = TmuxHost(None),
) -> bool:
    """Wait for the tmux pane to stabilise, inject *briefing*, then VERIFY it landed.

    #865 fix: quiescence alone can't tell "static startup banner" from "input
    box settled" — Claude Code's TUI paints async startup content (promo
    banners, MCP/auth notices) over several seconds, and a paste that lands
    mid-repaint used to be silently dropped.  The injection is now two
    phases:

    1. **Readiness wait** — poll ``tmux capture-pane -p`` every 50 ms until
       the pane both contains :data:`~coord.providers.claude_pty.INPUT_BOX_MARKER`
       (the rendered input box, not just any static content) AND has been
       unchanged for :data:`_READY_QUIESCE_S` seconds, or *timeout* lapses
       (degraded fallback: proceed anyway rather than hang forever on an
       unrecognised render).
    2. **Paste + verify + retry** — load the briefing into a tmux named
       buffer (``coord-brief``) via stdin and ``paste-buffer -p`` (bracketed
       paste), then re-capture the pane and check whether a fingerprint of
       the briefing (see :func:`~coord.providers.claude_pty.briefing_fingerprint`)
       actually rendered.  Retries up to :data:`_INJECT_MAX_ATTEMPTS` times
       with a short backoff between attempts.  If the pane can't be
       captured at all (tmux/session gone), verification is impossible and
       the single paste already issued is treated as best-effort success —
       there's nothing more to learn by retrying blind.  If every
       observable attempt misses, a hard failure is logged (never silent)
       and ``False`` is returned.

    The ``-p`` flag makes tmux send the content wrapped in bracketed-paste
    markers (``ESC[200~`` … ``ESC[201~``) **if** the target application has
    requested bracketed-paste mode.  Since ``claude``'s TUI always enables
    bracketed paste, this is equivalent to the PTY relay's manual
    bracketed-paste block.

    The stdin-based ``load-buffer -`` approach avoids creating a local
    temporary file, which is important for the remote-host path (#486b):
    no ``scp`` or ``ssh`` file-transfer is needed.

    Args:
        session_name: The tmux session to inject into.
        briefing: Text to pre-fill in the TUI input box.
        timeout: Readiness-wait deadline in seconds.  The paste+verify+retry
            phase spends additional (bounded) time beyond this.
        host: Target host.  Defaults to ``TmuxHost(None)`` (local).

    Returns:
        ``True`` when the briefing was injected (verified, or verification
        was impossible and the paste was issued).  ``False`` when every
        verifiable attempt confirmed the briefing did NOT land.
    """
    if not briefing.strip():
        return True  # nothing to inject — trivially OK

    fingerprint = briefing_fingerprint(briefing)

    def _capture() -> str | None:
        try:
            cap = subprocess.run(
                host.cmd(["capture-pane", "-p", "-t", session_name]),
                capture_output=True,
                text=True,
                timeout=2.0,
            )
        except (subprocess.SubprocessError, OSError):
            return None
        if cap.returncode != 0:
            return None
        return cap.stdout

    def _paste_once() -> None:
        # Load briefing into a named tmux buffer via stdin.  Using "-" as
        # the source tells tmux to read from stdin, which works for both
        # local and remote hosts without temporary files.
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

    try:
        # ── Phase 1: wait for the input box to render AND settle ───────────
        deadline = time.monotonic() + timeout
        prev_content: str | None = None
        quiescent_since: float | None = None

        while time.monotonic() < deadline:
            time.sleep(0.05)
            content = _capture()
            if content is None:
                break  # session/tmux gone — degrade to a blind paste below

            now = time.monotonic()
            stable = bool(content.strip())
            if content != prev_content:
                prev_content = content
                quiescent_since = now if stable else None
            elif stable and quiescent_since is not None:
                # A recognised input box needs less settle time to trust —
                # we've seen the actual prompt render, not just SOME static
                # content.  When the marker never shows (older CLI, unusual
                # render, or a session that died before drawing anything),
                # fall back to a longer quiescence window rather than
                # spinning for the full ``timeout`` (#865).
                quiet_for = now - quiescent_since
                threshold = (
                    _READY_QUIESCE_S
                    if INPUT_BOX_MARKER in content
                    else _READY_QUIESCE_NO_MARKER_S
                )
                if quiet_for >= threshold:
                    break  # settled — inject

        # ── Phase 2: paste, verify, retry on a miss (#865) ─────────────────
        for attempt in range(1, _INJECT_MAX_ATTEMPTS + 1):
            _paste_once()
            time.sleep(_INJECT_VERIFY_SETTLE_S)
            content = _capture()
            if content is None:
                # Can't observe the pane — nothing more to learn from a
                # retry.  The paste itself was issued; treat as best-effort
                # success (matches pre-#865 behaviour when tmux is broken).
                return True
            if fingerprint_in_text(content, fingerprint):
                return True
            if attempt < _INJECT_MAX_ATTEMPTS:
                time.sleep(_INJECT_RETRY_BACKOFF_S)

        logging.error(
            "briefing injection unverified after %d attempt(s) for tmux "
            "session %r — the briefing may not have landed in the input "
            "box; the operator should check and paste manually",
            _INJECT_MAX_ATTEMPTS,
            session_name,
        )
        return False
    except (subprocess.SubprocessError, OSError):
        return False


def _with_coord_on_path(shell_cmd: str) -> str:
    """Prefix *shell_cmd* so ``coord`` resolves inside the interactive session
    (#606 PATH-fix).

    Interactive sessions run under the machine's ``$SHELL``, which often lacks
    the coord venv bin on ``PATH`` (same class as the claude-not-on-ssh-PATH
    issue).  A human-attended REVIEW agent then can't run ``coord
    report-result`` and falls back to printing ``REVIEW_VERDICT:`` (recovered by
    the #606 transcript-floor).  Prepend the agent's coord venv bin — the
    ``~/.coord-venv`` install INVARIANT, kept as a literal ``$HOME`` so the
    session's own shell expands it on the machine the session runs on — so the
    agent's PREFERRED self-report path works.  Additive: a non-existent PATH
    entry is simply ignored, so this is harmless when coord lives elsewhere.
    """
    return f'export PATH="$HOME/.coord-venv/bin:$PATH"; {shell_cmd}'


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
        # #606: make `coord` resolvable in the session (agent self-report path).
        shell_cmd = _with_coord_on_path(shell_cmd)

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

        # Inject briefing (best-effort; a failure here is non-fatal to the
        # session itself, but MUST be visible to the operator — #865 review
        # follow-up: the previous code discarded this return value, so an
        # exhausted verify+retry only produced a ``logging.error`` call.
        # With no ``logging.basicConfig`` anywhere in the repo, that falls
        # through to Python's "handler of last resort" — one stderr line
        # that the very next statement (tmux attach-session, below) blows
        # away by switching the terminal to the tmux alt-screen before the
        # operator can read it. Print a message to the operator's OWN
        # terminal (not logging) and pause for an explicit acknowledgment
        # BEFORE attaching, so the failure can't be missed.
        if briefing.strip():
            injected = _inject_briefing_into_tmux_session(session_name, briefing, host=host)
            if not injected:
                print(
                    "\n"
                    "!!! coord: briefing injection could not be verified after "
                    "multiple attempts.\n"
                    "!!! The input box in the session below may be EMPTY — "
                    "paste the briefing yourself if so.\n",
                    file=sys.stderr,
                )
                try:
                    input("Press Enter to attach to the session... ")
                except (EOFError, KeyboardInterrupt):
                    pass

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

# #865: requiring INPUT_BOX_MARKER is a STRONGER readiness signal than bare
# quiescence (it means we've actually seen the rendered prompt, not just
# some static content — a promo banner mid-paint, say), but making it a hard
# requirement for the fast quiescence exit would regress the case where the
# render never emits a recognisable marker at all (older CLI, unusual
# terminal, or a session that exits before drawing anything — caught by the
# test suite).  So: exit fast (after _READY_QUIESCE_S) once the marker has
# been seen; fall back to this longer window when it hasn't, rather than
# spinning all the way to the outer cap/timeout.
_READY_QUIESCE_NO_MARKER_S = 1.6

# #865: paste-verify-retry constants shared by the tmux path
# (:func:`_inject_briefing_into_tmux_session`) and the PTY relay path
# (:func:`_launch_via_pty`'s prefill state machine).  A miss re-pastes after
# a short backoff rather than silently giving up — this is the core #865
# fix, since fire-and-forget (no verification at all) was the root defect.
_INJECT_MAX_ATTEMPTS = 3
_INJECT_VERIFY_SETTLE_S = 0.5
_INJECT_RETRY_BACKOFF_S = 0.4

#: Cap on the in-memory copy of recent master-fd output the PTY relay keeps
#: for readiness/verification (#865).  ~32KB comfortably holds a full TUI
#: frame plus ANSI escapes; older bytes are dropped so a long-lived session
#: doesn't grow this unbounded.
_PREFILL_SCREEN_BUF_CAP = 32768


@dataclass
class _PrefillState:
    """Pure state for the PTY relay's pre-fill-then-verify state machine (#865).

    Deliberately free of any I/O — :func:`_prefill_on_master_data` folds
    observed master-fd bytes in, :func:`_prefill_step` decides (given the
    current time) whether :func:`_launch_via_pty` should write
    ``paste_block`` to the master fd right now.  Splitting the decision out
    as pure functions means the readiness+verify+retry logic can be unit
    tested without a real pty, and keeps the relay's ``select()`` loop
    (which must stay non-blocking — it's also servicing the operator's
    keystrokes and the child's live output) free of any ``time.sleep()``.

    Attributes:
        fingerprint: The whitespace-normalized briefing snippet (see
            :func:`~coord.providers.claude_pty.briefing_fingerprint`) used to
            confirm a paste landed.
        started: ``time.monotonic()`` when the relay loop began — anchors
            the overall degraded-fallback cap.
        last_master_activity: ``time.monotonic()`` of the most recent
            master-fd read — anchors the pre-paste quiescence check.
        done: ``True`` once the state machine has either verified success or
            exhausted its retry budget — the caller stops driving it.
        seen_enable: Whether the bracketed-paste-enable DECSET has been
            observed in the master output yet.
        screen_buf: Rolling tail of recent master-fd bytes (capped at
            :data:`_PREFILL_SCREEN_BUF_CAP`), used both to look for
            :data:`~coord.providers.claude_pty.INPUT_BOX_MARKER_BYTES`
            (readiness) and the fingerprint (post-paste verification).
        paste_attempts: How many times the paste has been written so far.
        next_paste_at: ``time.monotonic()`` deadline for the next paste
            attempt when a retry is pending; ``None`` otherwise.
        verify_deadline: ``time.monotonic()`` deadline to check for the
            fingerprint after the most recent paste; ``None`` when not
            currently waiting on a verification window.
    """

    fingerprint: str
    started: float
    last_master_activity: float
    done: bool = False
    seen_enable: bool = False
    screen_buf: bytearray = field(default_factory=bytearray)
    paste_attempts: int = 0
    next_paste_at: float | None = None
    verify_deadline: float | None = None


def _prefill_on_master_data(state: _PrefillState, data: bytes, now: float) -> None:
    """Fold newly-observed master-fd *data* into *state*.

    Call this for every chunk read from the master fd, BEFORE the next
    :func:`_prefill_step` call, while ``state.done`` is still ``False``.
    """
    if state.done:
        return
    state.screen_buf.extend(data)
    overflow = len(state.screen_buf) - _PREFILL_SCREEN_BUF_CAP
    if overflow > 0:
        del state.screen_buf[:overflow]
    if not state.seen_enable and BRACKETED_PASTE_ENABLE in data:
        state.seen_enable = True
    state.last_master_activity = now


def _prefill_step(state: _PrefillState, now: float) -> bool:
    """Advance the pre-fill state machine one tick.

    Returns ``True`` exactly when the caller should write the bracketed-paste
    block to the master fd right now (once for the initial attempt, again
    for each retry).  Mutates *state* in place; check ``state.done`` after
    calling to know whether the machine has finished (verified success, or
    exhausted its retry budget — see :data:`_INJECT_MAX_ATTEMPTS`).
    """
    if state.done:
        return False

    if state.verify_deadline is not None:
        if now < state.verify_deadline:
            return False
        # Verification window elapsed — did the fingerprint land?
        if fingerprint_in_bytes(bytes(state.screen_buf), state.fingerprint):
            state.done = True
        elif state.paste_attempts >= _INJECT_MAX_ATTEMPTS:
            state.done = True
            logging.error(
                "briefing pre-fill unverified after %d attempt(s) in the "
                "PTY relay session — the briefing may not have landed in "
                "the input box; the operator should check and paste "
                "manually",
                state.paste_attempts,
            )
        else:
            state.next_paste_at = now + _INJECT_RETRY_BACKOFF_S
        state.verify_deadline = None
        return False

    if state.next_paste_at is not None:
        if now < state.next_paste_at:
            return False
        state.next_paste_at = None
        state.paste_attempts += 1
        state.verify_deadline = now + _INJECT_VERIFY_SETTLE_S
        return True

    # No attempt in flight yet — wait for bracketed-paste-enable AND render
    # quiescence.  A recognised input box (INPUT_BOX_MARKER) needs only the
    # short _READY_QUIESCE_S settle window; without it (older CLI, unusual
    # render, or a session that never draws anything — see the two-tier
    # rationale on _READY_QUIESCE_NO_MARKER_S above) fall back to the longer
    # window instead of spinning all the way to the overall cap (degraded
    # fallback — paste anyway rather than hang forever).
    quiet_for = now - state.last_master_activity
    marker_seen = INPUT_BOX_MARKER_BYTES in bytes(state.screen_buf)
    threshold = _READY_QUIESCE_S if marker_seen else _READY_QUIESCE_NO_MARKER_S
    ready_quiet = state.seen_enable and quiet_for >= threshold
    ready_cap = now - state.started >= _READY_QUIESCE_CAP_S
    if ready_quiet or ready_cap:
        state.paste_attempts += 1
        state.verify_deadline = now + _INJECT_VERIFY_SETTLE_S
        return True
    return False


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
            # #606: ensure `coord` resolves for the agent's self-report (mirrors
            # the tmux path's _with_coord_on_path; the PTY fallback execs argv
            # directly, so set PATH in the child env here).
            os.environ["PATH"] = (
                os.path.expanduser("~/.coord-venv/bin") + ":" + os.environ.get("PATH", "")
            )
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

    prefilled = not bool(paste_block)
    started = time.monotonic()
    # #865: pure paste/verify/retry state machine — see _PrefillState.  Only
    # constructed when there's actually a briefing to pre-fill; when
    # ``prefilled`` starts ``True`` (empty briefing) it's never touched.
    prefill_state = _PrefillState(
        fingerprint=briefing_fingerprint(briefing) if paste_block else "",
        started=started,
        last_master_activity=started,
    )
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
                    _prefill_on_master_data(prefill_state, data, time.monotonic())

            if not prefilled:
                # #865: readiness-anchored paste, verified and retried by
                # _prefill_step — a mistimed/lost paste is no longer
                # fire-and-forget.
                if _prefill_step(prefill_state, time.monotonic()):
                    try:
                        os.write(master_fd, paste_block)
                    except OSError:
                        pass
                prefilled = prefill_state.done

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
    # #604: populated only on the --merge-of path (verify_merge=True).  Carries
    # the merge verification (default_ahead / added / foreign) so the caller can
    # surface the offending commits even after the worktree is removed.  None
    # for every other interactive flavour.
    merge_verify: MergeVerify | None = field(default=None)


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


def _finalize_merge_blocked(
    *,
    merge_verify,  # agent.MergeVerify (not ok)
    assignment_id: str,
    machine_name: str,
    repo_name: str,
    repo_github: str,
    issue_number: int,
    base_branch: str,
    worktree_path: str | None,
    repo_path: str | None,
    started_at: float | None,
    log_path: str | None,
    branch: str | None = None,
) -> InteractiveFinalizeResult:
    """Record a botched ``--merge-of`` rebase as blocked → ``failed`` (#604).

    Routes through the same ``post_result(status=blocked)`` seam ``coord
    report-result`` uses (so a thin client still lands the write on the daemon),
    OVERRIDING any ``done`` the agent self-reported.  ``blocked`` maps to a
    ``failed`` board state — there is no literal ``blocked`` status the rest of
    the pipeline understands; the override's whole purpose is to keep the entry
    out of the merge-ready set.  The offending commits ride in the summary,
    posted on the issue, so the operator sees exactly why.  Cleans up the
    worktree afterwards, matching the normal-path discipline.
    """
    from coord.issue_store import (  # noqa: PLC0415
        STATUS_BLOCKED,
        ResultRecord,
        post_result,
    )

    wt_p = Path(worktree_path) if worktree_path else None
    branch_now = (
        _current_branch(wt_p) if wt_p is not None and wt_p.exists() else None
    )
    # Capture whether the agent had already self-reported BEFORE we override it,
    # so the caller can tell "we overrode a self-reported done" from "we were
    # the only writer".
    prior_recorded = _assignment_already_recorded(assignment_id)

    duration: float | None = None
    if started_at is not None:
        duration = max(0.0, time.time() - started_at)

    outcome = post_result(
        ResultRecord(
            assignment_id=assignment_id,
            machine_name=machine_name,
            repo_name=repo_name,
            repo_github=repo_github,
            issue_number=issue_number,
            status=STATUS_BLOCKED,
            verdict=None,
            summary=merge_verify.block_summary(base_branch),
            duration_seconds=duration,
            log_path=log_path,
            branch=branch_now or branch,
        )
    )

    worktree_removed = False
    if repo_path is not None and wt_p is not None:
        worktree_removed = _remove_worktree(Path(repo_path), wt_p)

    # #546: mark as interactive + capture tokens from the session transcript.
    _persist_interactive_tokens(assignment_id, started_at, worktree_path)

    return InteractiveFinalizeResult(
        terminal_status=outcome.status,  # "failed" (blocked → failed)
        commits_ahead=merge_verify.default_ahead,
        push_ok=True,
        push_error=None,
        already_recorded=prior_recorded,
        seam_outcome=outcome,
        worktree_removed=worktree_removed,
        merge_verify=merge_verify,
    )


def _transcript_names_issue(body: str, issue_number: int) -> bool:
    """True when a review body explicitly names *issue_number* (#617).

    Accepts BOTH the ``#<N>`` form (how reviewers refer to an issue in prose —
    the real #607 review body used ``#607``) and the ``issue-<N>`` form (the
    branch / briefing tag).  The original floor gated on a literal
    ``issue-<N>`` only, so a review whose body said ``#607`` was silently
    rejected and its findings lost — a hidden contributor to the recurring
    drop, caught by live-smoking the real transcript.  The trailing-digit
    negative lookahead stops ``#607`` from matching ``#6070``; gating on an
    explicit reference still prevents mis-attributing a concurrent, unrelated
    review's verdict.
    """
    return re.search(rf"(?:#|issue-){issue_number}(?!\d)", body) is not None


def _fetch_remote_review_findings(
    issue_number: int,
    cutoff: float,
    ssh_target: str,
    *,
    timeout: float = 30.0,
):
    """Remote twin of the transcript-floor: parse the review block from the
    Claude transcript on the SESSION'S OWN host (#617).

    When an interactive review ran on a remote machine and the operator
    reattached + exited from a DIFFERENT machine, the local
    ``~/.claude/projects`` scan is blind — the transcript lives on the remote
    host, so the findings vanished and only a verdict-less operator prompt was
    left (the #607 incident).  This lists the remote host's recent transcripts
    over ssh (newest-first, active at/after *cutoff*), streams each candidate
    back, and parses it with the same :func:`coord.review.parse_review_from_log`
    used locally.  Returns the first ``ReviewFindings`` that BOTH parses as a
    review AND names this issue (``issue-<N>``), or ``None`` (ssh failure / no
    match) so the caller falls through to the operator-prompt backstop.

    The ``cutoff`` filter already bounds the candidate set to the session
    window — no additional per-scan cap is applied (#619: the original
    ``max_candidates=6`` dropped the target transcript under concurrent-session
    load, exactly the condition when reviews run).
    """
    from coord.review import parse_review_from_log  # noqa: PLC0415

    # GNU find on the fleet (Linux): list every recent transcript with its
    # mtime, newest-first.  `$HOME` is expanded by the REMOTE shell.
    # head -200 is a safety rail against enormous project dirs; the cutoff
    # filter below is the real bound.
    list_cmd = (
        'find "$HOME/.claude/projects" -maxdepth 2 -name "*.jsonl" '
        r"-printf '%T@\t%p\n' 2>/dev/null | sort -rn | head -200"
    )
    try:
        listing = subprocess.run(
            ["ssh", *_SSH_MUX_OPTS, ssh_target, list_cmd],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if listing.returncode != 0:
        return None

    candidates: list[str] = []
    for raw in (listing.stdout or "").splitlines():
        if "\t" not in raw:
            continue
        mtime_s, path = raw.split("\t", 1)
        try:
            mtime = float(mtime_s)
        except ValueError:
            continue
        path = path.strip()
        # Collect ALL candidates within the cutoff window — no early break.
        # The cutoff is the authoritative bound; stopping at an arbitrary N
        # is what caused the #619 miss under concurrent-session load.
        if path and mtime >= cutoff:
            candidates.append(path)

    for remote_path in candidates:
        try:
            cat = subprocess.run(
                ["ssh", *_SSH_MUX_OPTS, ssh_target, f"cat {shlex.quote(remote_path)}"],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except (subprocess.SubprocessError, OSError):
            continue
        if cat.returncode != 0 or not cat.stdout:
            continue
        tmp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", suffix=".jsonl", delete=False, encoding="utf-8"
            ) as tf:
                tf.write(cat.stdout)
                tmp_path = Path(tf.name)
            findings = parse_review_from_log(tmp_path)
        finally:
            if tmp_path is not None:
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
        # Gate on the WHOLE transcript, not just the parsed review prose: a
        # reviewer describes the code, not the issue number, so the body often
        # never says "#362" (real #362 miss, 2026-06-18) — but the review
        # BRIEFING seeds the issue/branch (`issue-<N>`), so the transcript does.
        if findings is not None and _transcript_names_issue(cat.stdout, issue_number):
            return findings
    return None


def _tokens_from_transcript(
    started_at: float | None,
    *,
    worktree_path: str | None = None,
    projects_dir: Path | None = None,
) -> tuple[int, int, int, int]:
    """#546: sum token usage from Claude Code session transcripts for this session.

    Claude Code persists every session as a JSONL transcript under
    ``~/.claude/projects/<cwd>/``.  Each ``assistant`` message carries a
    ``usage`` dict with ``input_tokens``, ``output_tokens``,
    ``cache_creation_input_tokens``, and ``cache_read_input_tokens``.

    When *worktree_path* is given, we restrict the scan to the project
    directory that corresponds to that CWD (the same mapping Claude Code uses:
    replace ``/`` with ``-``).  This prevents attributing a concurrent
    unrelated session's tokens to this assignment.  Without *worktree_path*
    we fall back to scanning ALL project directories that were active since
    *started_at* — broader but still bounded by the time window.

    Returns ``(input_tokens, output_tokens, cache_creation_tokens,
    cache_read_tokens)``.  Returns ``(0, 0, 0, 0)`` when no tokens can be
    recovered — callers treat that as "no data" and skip the write rather
    than zeroing out existing columns.
    """
    if started_at is None:
        return 0, 0, 0, 0

    base = projects_dir if projects_dir is not None else (Path.home() / ".claude" / "projects")
    if not base.is_dir():
        return 0, 0, 0, 0

    # Determine which project directories to search.  When we have the CWD
    # (worktree_path) we can be targeted; otherwise we scan all dirs that were
    # touched during the session window.
    if worktree_path is not None:
        # Claude Code names project dirs by replacing every '/' in the CWD
        # with '-'.  Example: /home/john/.coord/worktrees/abc → -home-john--coord-worktrees-abc
        proj_name = str(worktree_path).replace("/", "-")
        proj_dir = base / proj_name
        search_dirs = [proj_dir] if proj_dir.is_dir() else []
    else:
        search_dirs = []

    # Fallback: scan ALL project dirs and filter by file mtime.
    if not search_dirs:
        try:
            search_dirs = [d for d in base.iterdir() if d.is_dir()]
        except OSError:
            return 0, 0, 0, 0

    cutoff = started_at - 5.0  # small clock-skew buffer
    input_tokens = output_tokens = cache_creation = cache_read = 0

    for proj_dir in search_dirs:
        try:
            jsonl_files = list(proj_dir.glob("*.jsonl"))
        except OSError:
            continue
        for p in jsonl_files:
            try:
                if p.stat().st_mtime < cutoff:
                    continue
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for line in text.splitlines():
                if not line or not line.startswith("{"):
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") != "assistant":
                    continue
                msg = obj.get("message") or {}
                usage = msg.get("usage") or {}
                input_tokens += int(usage.get("input_tokens") or 0)
                output_tokens += int(usage.get("output_tokens") or 0)
                cache_creation += int(usage.get("cache_creation_input_tokens") or 0)
                cache_read += int(usage.get("cache_read_input_tokens") or 0)

    return input_tokens, output_tokens, cache_creation, cache_read


def _review_findings_from_transcript(
    issue_number: int,
    started_at: float | None,
    *,
    projects_dir: Path | None = None,
    ssh_target: str | None = None,
):
    """Recover a human-attended review's verdict + findings from the Claude
    session transcript — the **transcript-floor** backstop (#606).

    A review session emits its verdict as ``REVIEW_VERDICT:`` / ``REVIEW_BODY:``
    in its output, but often cannot run ``coord report-result`` because ``coord``
    is not on the interactive session's PATH (same class as the ``claude``-not-
    on-ssh-PATH issue).  Reviews also have no git-floor (read-only, 0 commits),
    so without this the findings are lost the moment the operator exits Claude.

    Claude Code persists every session as a JSONL transcript under
    ``~/.claude/projects/<cwd>/``; we parse the most-recent one(s) active during
    this session with the existing :func:`coord.review.parse_review_from_log`
    (which already handles the transcript's stream-json shape).  Returns the
    parsed ``ReviewFindings`` or ``None`` — independent of the agent running any
    command.

    Robustness: only a transcript that BOTH parses as a review AND names this
    issue (``issue-<N>``, the format the review briefing produces) is trusted —
    newest-first, so the just-exited session wins.  There is deliberately NO
    "guess the only review in the window" fallback: that could mis-attribute an
    unrelated/concurrent review (and record its verdict against the wrong
    assignment id).  Self-gating — a work session's transcript carries no
    ``REVIEW_VERDICT`` block, so this returns ``None`` and the caller falls
    through to the git-floor.
    """
    from coord.review import parse_review_from_log  # noqa: PLC0415

    # No session start → no bounded window → don't scan (avoid matching a stale,
    # unrelated transcript).  Production always passes a real started_at; only a
    # caller that can't bound the session (or a test) passes None.
    if started_at is None:
        return None
    # Only sessions active at/after this review started (small clock-skew buffer).
    cutoff = started_at - 5.0
    # #617: when the session ran on a REMOTE host (operator reattached + exited
    # from a different machine), its Claude transcript lives THERE — the local
    # projects dir is blind to it (the #607 silent-drop).  Scan the session's
    # own host over ssh instead.
    if ssh_target is not None:
        result = _fetch_remote_review_findings(issue_number, cutoff, ssh_target)
        if result is None:
            # One settle-and-retry: covers a transcript-flush blip where the
            # JSONL hasn't been fully flushed to disk at the instant we read it
            # (#619).  2 s is long enough for a local flush but short enough
            # to avoid annoying the operator on the fast path.
            time.sleep(2.0)
            result = _fetch_remote_review_findings(issue_number, cutoff, ssh_target)
        return result
    base = projects_dir if projects_dir is not None else (Path.home() / ".claude" / "projects")
    if not base.is_dir():
        return None
    candidates: list[tuple[float, Path]] = []
    for p in base.glob("*/*.jsonl"):
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        if mtime >= cutoff:
            candidates.append((mtime, p))
    candidates.sort(reverse=True)  # newest first — the just-exited session

    for _mtime, p in candidates:
        findings = parse_review_from_log(p)
        if findings is None:
            continue
        # Gate on the WHOLE transcript, not just the parsed review prose (the
        # body often never names the issue — the reviewer describes the code;
        # the BRIEFING seeds `issue-<N>`/the branch, so the transcript does).
        try:
            raw = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            raw = findings.body
        if _transcript_names_issue(raw, issue_number):
            return findings
    return None


def _persist_interactive_tokens(
    assignment_id: str,
    started_at: float | None,
    worktree_path: str | None,
) -> None:
    """#546: flag as interactive + capture token counts from the session transcript.

    Called before every return in :func:`finalize_interactive_exit`.  Best-effort
    — any exception is swallowed so the caller's normal finalization path is never
    blocked.
    """
    from coord.state import mark_assignment_interactive, update_assignment_tokens  # noqa: PLC0415
    try:
        mark_assignment_interactive(assignment_id)
    except Exception:  # noqa: BLE001
        pass
    if started_at is None:
        return
    try:
        inp, out, cc, cr = _tokens_from_transcript(
            started_at, worktree_path=worktree_path
        )
        if inp + out + cc + cr > 0:
            update_assignment_tokens(
                assignment_id,
                input_tokens=inp,
                output_tokens=out,
                cache_creation_tokens=cc,
                cache_read_tokens=cr,
            )
    except Exception:  # noqa: BLE001
        pass


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
    artifact_paths: list[str] | None = None,
    verify_merge: bool = False,
    ssh_target: str | None = None,
    branch: str | None = None,
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
    _effective_patterns = list(artifact_paths or [])

    # ── Merge-prep verification gate (#604) ──────────────────────────────────
    # For the interactive MERGE agent (--merge-of), GIT TRUTH OVERRIDES the
    # agent's self-report.  A rebase that left the branch behind the target
    # branch, or force-pushed a polluted history dragging in unrelated
    # already-merged commits, must NEVER be recorded as `done` — even if the
    # agent ran `coord report-result --status done` (vimcode #494, 2026-06-15).
    #
    # This deliberately INVERTS the precedence used everywhere else in this
    # function, where `_assignment_already_recorded` wins and a 0-commit review
    # session is legitimately fine.  For merge-prep the opposite is true: a
    # clean-exit self-report is exactly what a botched rebase looks like, so we
    # re-derive the truth from git and let it override.  Do NOT "simplify" this
    # back to deferring to the report.
    merge_verify = None
    if verify_merge and worktree_path:
        wt_v = Path(worktree_path)
        if wt_v.exists():
            from coord.agent import verify_merge_branch  # noqa: PLC0415

            merge_verify = verify_merge_branch(
                wt_v, base=base_branch, issue_number=issue_number
            )
            if not merge_verify.ok:
                return _finalize_merge_blocked(
                    merge_verify=merge_verify,
                    assignment_id=assignment_id,
                    machine_name=machine_name,
                    repo_name=repo_name,
                    repo_github=repo_github,
                    issue_number=issue_number,
                    base_branch=base_branch,
                    worktree_path=worktree_path,
                    repo_path=repo_path,
                    started_at=started_at,
                    log_path=log_path,
                    branch=branch,
                )

    # Respect an explicit `coord report-result` from the agent.  Without
    # this check, every review session (which legitimately has 0 commits)
    # would have its agent-reported verdict overwritten with an advisory
    # the instant the human closed the TTY.
    if _assignment_already_recorded(assignment_id):
        worktree_removed = False
        wt_p = Path(worktree_path) if worktree_path else None
        if wt_p is not None and wt_p.exists():
            # #562: stash before removing — `coord report-result` never stashes.
            if _effective_patterns:
                from coord.agent import (  # noqa: PLC0415
                    stash_artifacts_for_branch as _stash_fn,
                )
                from coord.state import COORD_DIR as _CD  # noqa: PLC0415
                _stash_fn(
                    worktree_path=wt_p,
                    branch=_current_branch(wt_p) or "",
                    repo_name=repo_name,
                    patterns=_effective_patterns,
                    state_dir=_CD,
                    assignment_id=assignment_id,
                    log_path=log_path,
                )
        if repo_path is not None and wt_p is not None:
            worktree_removed = _remove_worktree(Path(repo_path), wt_p)
        # #546: mark as interactive + capture tokens from the session transcript.
        _persist_interactive_tokens(assignment_id, started_at, worktree_path)
        return InteractiveFinalizeResult(
            terminal_status="report-result",  # informational only
            commits_ahead=None,
            push_ok=True,
            push_error=None,
            already_recorded=True,
            seam_outcome=None,
            worktree_removed=worktree_removed,
            merge_verify=merge_verify,
        )

    # ── Transcript-floor (#606): durable review capture ──────────────────────
    # A human-attended REVIEW emits its verdict (REVIEW_VERDICT:/REVIEW_BODY:)
    # but frequently can't run `coord report-result` (coord not on the session
    # PATH), and a review has no git-floor (0 commits) — so without this the
    # findings vanish when the operator exits Claude.  Recover them from the
    # Claude session transcript and record them through the same issue_store
    # seam.  Runs BEFORE the git-floor (which would otherwise stamp a verdict-
    # less advisory).  Self-gating: a work session's transcript has no review
    # block, so this is a no-op there and the git-floor below still handles it.
    _tf = _review_findings_from_transcript(
        issue_number, started_at, ssh_target=ssh_target
    )
    if _tf is not None:
        try:
            from coord import issue_store  # noqa: PLC0415

            issue_store.post_result(
                issue_store.ResultRecord(
                    assignment_id=assignment_id,
                    machine_name=machine_name,
                    repo_name=repo_name,
                    repo_github=repo_github,
                    issue_number=issue_number,
                    status="done",
                    verdict=_tf.verdict,  # type: ignore[arg-type]  # approve|request-changes
                    summary="Review verdict recovered from the session transcript "
                    "(agent could not run `coord report-result`).",
                    findings_body=_tf.body,
                    branch=branch,
                )
            )
        except Exception:  # noqa: BLE001 — best-effort
            # Recording failed (e.g. board unreachable) — fall through to the
            # git-floor / human-prompt backstop rather than swallowing the review.
            _tf = None
        else:
            _wt = Path(worktree_path) if worktree_path else None
            _removed = (
                _remove_worktree(Path(repo_path), _wt)
                if repo_path is not None and _wt is not None and _wt.exists()
                else False
            )
            # #546: mark as interactive + capture tokens from the session transcript.
            _persist_interactive_tokens(assignment_id, started_at, worktree_path)
            return InteractiveFinalizeResult(
                terminal_status="transcript-floor",
                commits_ahead=None,
                push_ok=True,
                push_error=None,
                already_recorded=True,
                seam_outcome=None,
                worktree_removed=_removed,
                merge_verify=merge_verify,
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

    # #717: For interactive WORK sessions the session exit_code is unreliable
    # (operator Ctrl-C after the push landed, tmux kill, a deny-listed `gh`
    # command at the tail end of the session, etc.).  When commits_ahead >= 1
    # the branch IS pushed and the work IS reviewable — normalise to exit_code=0
    # so _post_completion_local takes the DONE path and the Test → Review →
    # Merge chain stays alive.
    # commits_ahead == 0 (no work produced) and None (git unreachable / no
    # worktree — covers review sessions) keep the original exit_code so:
    #   • exit_code != 0, commits == 0  → still recorded as FAILED
    #   • exit_code == 0, commits == 0  → still recorded as ADVISORY (#448)
    #   • exit_code == 0, commits is None → still recorded as DONE (unknown-commit heuristic)
    effective_exit_code = 0 if (commits is not None and commits >= 1) else exit_code

    record = CompletionRecord(
        assignment_id=assignment_id,
        machine_name=machine_name,
        repo_name=repo_name,
        repo_github=repo_github,
        issue_number=issue_number,
        exit_code=effective_exit_code,
        commits_ahead=commits,
        branch=branch_now or branch,
        duration_seconds=duration,
        log_path=log_path,
        summary="",
    )
    outcome = post_completion(record)

    # Step 3.5 — stash artifacts BEFORE removing the worktree (#562).
    # This is the missing link that caused "Artifact unavailable" in the TUI
    # for every interactive work session.  Same discipline as the agent-side
    # stash: best-effort, runs regardless of outcome.status so partially
    # built artifacts on a failed session are still captured.
    if _effective_patterns and wt_path is not None and wt_path.exists():
        from coord.agent import stash_artifacts_for_branch as _stash_fn  # noqa: PLC0415
        from coord.state import COORD_DIR as _COORD_DIR  # noqa: PLC0415
        _stash_fn(
            worktree_path=wt_path,
            branch=branch_now or "",
            repo_name=repo_name,
            patterns=_effective_patterns,
            state_dir=_COORD_DIR,
            assignment_id=assignment_id,
            log_path=log_path,
        )

    # Step 4 — remove the interactive worktree when repo_path is provided.
    # Matches _cleanup_worktree discipline: always runs, best-effort.
    worktree_removed = False
    if repo_path is not None and wt_path is not None:
        worktree_removed = _remove_worktree(Path(repo_path), wt_path)

    # #546: mark as interactive + capture tokens from the session transcript.
    _persist_interactive_tokens(assignment_id, started_at, worktree_path)

    return InteractiveFinalizeResult(
        terminal_status=outcome.status,
        commits_ahead=commits,
        push_ok=push_ok,
        push_error=push_error,
        already_recorded=False,
        seam_outcome=outcome,
        worktree_removed=worktree_removed,
        merge_verify=merge_verify,
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


def _remote_orphan_is_safe_to_prune(
    ssh_target: str,
    remote_repo_sh: str,
    holder_abs_path: str,
    branch: str,
    *,
    timeout: float = 15.0,
) -> bool:
    """Return ``True`` only when a dead holder worktree is safe to auto-prune.

    Safety gate for the auto-prune-and-retry path (#759).  Both conditions
    must hold before the coordinator removes a worktree without operator
    intervention:

    1. **Clean** — ``git status --porcelain`` inside *holder_abs_path* is
       empty (no staged or unstaged changes).
    2. **Fully pushed** — the holder's HEAD SHA matches the tip of
       ``origin/refs/heads/<branch>`` (no local commits that haven't been
       pushed to the remote).

    *holder_abs_path* must be the **absolute** path as returned by
    :func:`find_remote_branch_holder` (e.g.
    ``/home/john/.coord/worktrees/<id>``).

    *remote_repo_sh* is the ``$HOME``-form path to the repo checkout on
    the remote machine (e.g. ``$HOME/src/myrepo``); the remote shell
    expands ``$HOME`` correctly.

    Returns ``False`` on any SSH / git failure — conservative default,
    never auto-removes when the check cannot be completed.
    """
    holder_q = shlex.quote(holder_abs_path)
    # branch is always issue-<N>-<slug> (alphanumeric + hyphens) — safe to
    # interpolate directly, but quote defensively.
    branch_q = shlex.quote(branch)
    # The script runs on the remote shell.  $HOME in remote_repo_sh is
    # expanded by the remote shell (passed unquoted); holder_q is a
    # single-quoted absolute path (no $HOME expansion needed).
    remote_cmd = (
        f"cd {holder_q} 2>/dev/null || {{ echo __NOT_SAFE; exit 0; }}; "
        f"STATUS=$(git status --porcelain 2>/dev/null); "
        f"HEAD_SHA=$(git rev-parse HEAD 2>/dev/null); "
        f"ORIGIN_SHA=$(git -C {remote_repo_sh} ls-remote origin"
        f" refs/heads/{branch_q} 2>/dev/null | awk '{{print $1}}'); "
        f"if [ -z \"$STATUS\" ] && [ -n \"$HEAD_SHA\" ] && [ -n \"$ORIGIN_SHA\" ]"
        f" && [ \"$HEAD_SHA\" = \"$ORIGIN_SHA\" ]; then"
        f" echo __SAFE; else echo __NOT_SAFE; fi"
    )
    try:
        result = subprocess.run(
            ["ssh", *_SSH_MUX_OPTS, ssh_target, remote_cmd],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (subprocess.SubprocessError, OSError):
        return False  # SSH/timeout failure → conservative: do not auto-prune
    return "__SAFE" in (result.stdout or "")


def remote_worktree_exists(
    ssh_target: str,
    remote_worktree_sh: str,
    *,
    timeout: float = 10.0,
) -> bool:
    """Return ``True`` if *remote_worktree_sh* exists as a directory on *ssh_target*.

    Used by the remote interactive launch paths (#560) to distinguish a setup
    failure (git worktree add was refused — directory never created) from a
    worker failure (the directory was created but ``claude`` exited non-zero).

    ``remote_worktree_sh`` is a ``$HOME``-form path (e.g.
    ``$HOME/.coord/worktrees/<id>``); the remote shell expands ``$HOME``
    correctly because the argument is passed as a single unquoted token inside
    a double-quoted string.

    Returns ``True`` on SSH/timeout errors — safe fallback that prevents false
    "setup failed" diagnoses when connectivity is the real problem.
    """
    # We intentionally pass remote_worktree_sh un-quoted so $HOME expands.
    # coord always generates this path from a hex assignment_id, so there is
    # no injection risk.
    remote_cmd = f"test -d {remote_worktree_sh} && echo __WT_EXISTS || echo __WT_MISSING"
    try:
        result = subprocess.run(
            ["ssh", *_SSH_MUX_OPTS, ssh_target, remote_cmd],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (subprocess.SubprocessError, OSError):
        return True  # assume exists; prevents false "setup failed" on SSH error
    return "__WT_EXISTS" in (result.stdout or "")


def find_remote_branch_holder(
    ssh_target: str,
    remote_repo_sh: str,
    branch: str,
    *,
    timeout: float = 15.0,
) -> str | None:
    """Return the worktree path that has *branch* checked out on *ssh_target*.

    Runs ``git worktree prune`` then ``git worktree list --porcelain`` in
    *remote_repo_sh*; parses the output to find an entry whose ``branch``
    field matches ``refs/heads/<branch>``.  Returns the worktree path string
    (as reported by git — a fully expanded absolute path, not a
    ``$HOME``-style shell path) or ``None`` if the branch is not locked.

    Used by the interactive launch paths (#560) to produce an actionable
    "branch already checked out at <path>" error instead of the raw git
    error surfaced via the tmux session exit.

    ``remote_repo_sh`` is a ``$HOME``-form path passed un-quoted so the
    remote shell expands ``$HOME``.
    """
    remote_cmd = (
        f"cd {remote_repo_sh} 2>/dev/null || exit 0; "
        f"git worktree prune 2>/dev/null; "
        f"git worktree list --porcelain 2>/dev/null"
    )
    try:
        result = subprocess.run(
            ["ssh", *_SSH_MUX_OPTS, ssh_target, remote_cmd],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if result.returncode != 0:
        return None

    # Parse porcelain output.  Each worktree block is separated by a blank
    # line; within a block the fields appear in order: worktree, HEAD,
    # branch (or "detached" / "bare").
    current_path: str | None = None
    for raw_line in result.stdout.splitlines():
        line = raw_line.strip()
        if line.startswith("worktree "):
            current_path = line[len("worktree "):].strip()
        elif line.startswith("branch ") and current_path is not None:
            branch_ref = line[len("branch "):].strip()
            if branch_ref == f"refs/heads/{branch}":
                return current_path
        elif not line:
            current_path = None  # blank line: end of a worktree block
    return None


def _holder_is_base_checkout(holder_abs_path: str) -> bool:
    """Return ``True`` when *holder_abs_path* is the repo base checkout.

    Coord always places worker worktrees under ``~/.coord/worktrees/<id>``.
    Any holder path that does NOT contain ``.coord/worktrees`` is therefore the
    base repo checkout (e.g. ``~/src/<repo>``) — the invariant path that must
    NEVER be pruned or removed (#814 / #561).

    Used by the remote interactive launch paths to distinguish a stale orphan
    (safe to force-remove) from the base checkout (must be freed with
    ``git checkout <default_branch>`` instead).
    """
    return ".coord/worktrees" not in holder_abs_path


def _remote_base_checkout_free_branch(
    ssh_target: str,
    remote_repo_sh: str,
    default_branch: str,
    *,
    timeout: float = 15.0,
) -> bool:
    """Check out *default_branch* in the remote base checkout to free a branch.

    When ``~/src/<repo>`` is checked out on an issue branch, ``git worktree
    add`` refuses to create a new worktree for the same branch.  The correct
    remedy is to switch the base back to *default_branch* so the branch ref is
    no longer occupied by the main checkout (#814 / #561).

    *remote_repo_sh* is a ``$HOME``-form path (e.g. ``$HOME/src/myrepo``); the
    remote shell expands ``$HOME`` correctly because the path is passed unquoted
    inside the compound command.

    Returns ``True`` when the remote ``git checkout`` succeeded.
    Returns ``False`` on SSH / timeout / git failure — the caller then falls
    back to printing a manual-fix command for the operator.
    """
    default_branch_q = shlex.quote(default_branch)
    remote_cmd = (
        f"git -C {remote_repo_sh} checkout {default_branch_q} 2>/dev/null"
        f" && echo __FREE_DONE || echo __FREE_FAIL"
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
    return "__FREE_DONE" in (result.stdout or "")


def _remote_stash_artifacts(
    ssh_target: str,
    remote_worktree_sh: str,
    repo_name: str,
    branch: str,
    patterns: list[str],
    assignment_id: str,
    *,
    timeout: float = 60.0,
) -> bool:
    """Run :func:`coord.agent.stash_artifacts_for_branch` on the remote machine.

    SSHes into *ssh_target* and invokes the standalone stash function via the
    remote's coord venv Python so the artifact-filter logic is identical to the
    local worker path.  Must be called **before** :func:`_remote_worktree_remove`
    so the files are still present.

    All dynamic values (worktree path, branch, patterns JSON, assignment_id)
    are passed as ``sys.argv`` positional arguments so no shell-within-shell
    quoting is needed — the outer ssh call handles quoting of the argv list.

    Returns ``True`` when the remote command echoed ``__STASH_DONE``.
    Best-effort: SSH/import failures are silently ignored by the caller.
    """
    patterns_json = json.dumps(patterns)
    # The Python snippet reads all inputs from argv — no interpolation of
    # branch/repo/patterns inside the script string itself.  This avoids any
    # quoting-within-quoting hazards.
    #
    # ``remote_worktree_sh`` is a ``$HOME``-form path (e.g.
    # ``$HOME/.coord/worktrees/<id>``).  When passed via shlex.quote() it
    # becomes a single-quoted shell token, so the remote shell NEVER expands
    # ``$HOME``.  ``Path.expanduser()`` only handles ``~``, not ``$HOME``.
    # Use ``os.path.expandvars()`` in the snippet to resolve ``$HOME`` on the
    # remote machine before constructing the Path.
    py_snippet = (
        "import sys,json,os; from pathlib import Path; "
        "from coord.agent import stash_artifacts_for_branch; "
        "stash_artifacts_for_branch("
        "worktree_path=Path(os.path.expandvars(sys.argv[1])),"
        "branch=sys.argv[2],"
        "repo_name=sys.argv[3],"
        "patterns=json.loads(sys.argv[4]),"
        "state_dir=Path.home()/'.coord',"
        "assignment_id=sys.argv[5]"
        ")"
    )
    # Try the coord venv Python first; fall back to plain python3.
    # Both invocations pass the same argv so the snippet is identical.
    argv_tail = [
        remote_worktree_sh,
        branch,
        repo_name,
        patterns_json,
        assignment_id,
    ]
    # Build a single shell command:
    #   ( ~/.coord-venv/bin/python3 -c SNIPPET ARGS || python3 -c SNIPPET ARGS )
    #   && echo __STASH_DONE
    # The final echo is a reliable sentinel even if the snippet's own print()
    # is swallowed by 2>/dev/null.
    snippet_q = shlex.quote(py_snippet)
    args_q = " ".join(shlex.quote(a) for a in argv_tail)
    remote_cmd = (
        f"( ~/.coord-venv/bin/python3 -c {snippet_q} {args_q} 2>/dev/null"
        f" || python3 -c {snippet_q} {args_q} 2>/dev/null )"
        " && echo __STASH_DONE"
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
    return "__STASH_DONE" in (result.stdout or "")


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
    artifact_paths: list[str] | None = None,
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
    2.5 (#562) stash configured artifacts ON THE REMOTE via ssh so the built
        binaries survive worktree removal and ``coord pull-artifact`` can fetch
        them.  Stash runs before worktree removal regardless of push outcome.
    3. remove the remote worktree — but ONLY when the push succeeded, so
       unpushed commits are never silently destroyed.

    Respects an existing ``coord report-result`` (same as the local backstop):
    if the row already holds a terminal status, the verdict/result wins and
    this only attempts worktree cleanup (after a best-effort remote stash).
    """
    _effective_patterns = list(artifact_paths or [])

    if _assignment_already_recorded(assignment_id):
        # #562: stash artifacts before removing the worktree, even when the
        # agent already recorded via `coord report-result` — that path never
        # stashes on its own.
        if _effective_patterns:
            _remote_stash_artifacts(
                ssh_target, remote_worktree_sh, repo_name, branch,
                _effective_patterns, assignment_id,
            )
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

    # #717: same normalisation as the local path — a remote interactive session
    # that pushed ≥1 commit must not land as FAILED because the ssh session
    # exited non-zero (e.g. tmux kill after the push finished).
    effective_exit_code = 0 if (commits is not None and commits >= 1) else exit_code

    record = CompletionRecord(
        assignment_id=assignment_id,
        machine_name=machine_name,
        repo_name=repo_name,
        repo_github=repo_github,
        issue_number=issue_number,
        exit_code=effective_exit_code,
        commits_ahead=commits,
        branch=branch_now or branch,
        duration_seconds=duration,
        log_path=None,
        summary="",
    )
    outcome = post_completion(record)

    # #562: stash artifacts BEFORE removing the worktree so compiled outputs
    # survive cleanup — same discipline as the agent-side stash on workers.
    # Stash regardless of push_ok (the worktree files are still present even
    # when the push failed) so a failed-push session doesn't lose its build.
    if _effective_patterns:
        _remote_stash_artifacts(
            ssh_target, remote_worktree_sh, repo_name, branch,
            _effective_patterns, assignment_id,
        )

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
    * tmux is available on the local machine AND EITHER:

      - ``coord-<assignment_id>`` is NOT alive (session gone), OR
      - the session IS alive but its pane has exited (dead-pane case — claude
        finished while the operator was detached).

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
    1a. (Dead-pane only) Pushes any commits to the remote with
       ``git push -u origin HEAD`` (best-effort) so the work is not lost when
       the worktree is removed.  Failures are silently ignored.
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
    4a. (Dead-pane only) Kills the now-empty tmux session with
       ``tmux kill-session`` (best-effort) so ``coord sessions`` stops
       listing it.

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
        _dead_pane_kill_needed = False
        if tmux_session_alive(sname):
            # Session is alive — but check whether the claude pane has already
            # exited (dead pane).  This is the detach-and-abandon case: the
            # operator detached while claude was running, and claude finished in
            # the background.  The tmux session is still up but the work is
            # stranded without finalize.
            if not tmux_pane_dead(sname):
                continue  # session alive AND pane running — genuinely in progress
            # Pane exited; the session is now an empty shell.  Fall through to
            # the reap logic and kill the session at the end of this iteration.
            _dead_pane_kill_needed = True

        # ── Session is dead (or dead-pane) locally: check locality ──────────

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

        # 1a. (Dead-pane only) Push commits before removing the worktree so
        #     that any work the operator produced survives.  Best-effort: a push
        #     failure does NOT abort the reap — the worktree is still removed
        #     and the DB still updated.  Skipped for the all-dead-session case
        #     (where the session is already gone) and when there are no commits.
        if _dead_pane_kill_needed and wt_path.exists() and commits:
            try:
                subprocess.run(
                    ["git", "push", "-u", "origin", "HEAD"],
                    cwd=str(wt_path),
                    capture_output=True,
                    timeout=30.0,
                )
            except Exception:  # noqa: BLE001
                pass  # non-fatal — work may already be on the remote

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

        # 4a. (Dead-pane only) Kill the now-empty tmux session so it disappears
        #     from ``coord sessions`` immediately.  Best-effort — if kill fails
        #     the session will be gone on its own eventually (or the operator
        #     can kill it manually).
        if _dead_pane_kill_needed:
            try:
                subprocess.run(
                    ["tmux", "kill-session", "-t", sname],
                    capture_output=True,
                    timeout=5.0,
                )
            except Exception:  # noqa: BLE001
                pass  # non-fatal

        reaped.append(a.assignment_id)

    return reaped


# ── Remote stale-session reaper (#588) ───────────────────────────────────────

#: Per-session count of consecutive reconcile passes where SSH to the remote
#: host timed out or refused.  Reset to 0 on first successful SSH probe.
#: Module-level (ephemeral — not persisted to DB) so the count survives
#: multiple reconcile calls within a coordinator session.
_REMOTE_SSH_UNREACHABLE_COUNTS: dict[str, int] = {}


def _mark_stale_reap_in_db(assignment_id: str, status: str, finished_at: float) -> None:
    """Best-effort DB update when a stale remote session is reaped without a
    full ``finalize_remote_interactive_exit`` call.

    Called when either:

    * ``finalize_remote_interactive_exit`` raised an exception mid-run, OR
    * There is no branch / repo path to pass to finalize (push cannot run).

    The update is silently swallowed on DB error so the in-memory board
    update that follows always frees the machine slot regardless.
    """
    try:
        from coord.state import get_connection  # noqa: PLC0415
        conn = get_connection()
        conn.execute(
            "UPDATE assignments SET status=?, finished_at=? "
            "WHERE assignment_id=? AND status IN ('running', 'pending')",
            (status, finished_at, assignment_id),
        )
        conn.commit()
    except Exception:  # noqa: BLE001
        pass


def _probe_remote_tmux_alive(
    session_name: str,
    host: TmuxHost,
) -> tuple[bool, bool]:
    """Probe a remote tmux session and report liveness + SSH reachability.

    Returns a ``(session_alive, ssh_ok)`` tuple:

    * ``session_alive`` — ``True`` when ``tmux has-session`` exited 0.
    * ``ssh_ok`` — ``True`` when SSH connected successfully (even if the
      session was absent).  ``False`` indicates an SSH transport failure
      (connection refused / timeout / auth error) rather than a tmux
      answer of "no such session".

    SSH exit code 255 is the canonical "SSH could not connect" indicator.
    The ``BatchMode=yes`` + ``ConnectTimeout=4`` flags on the
    :class:`TmuxHost` (``batch=True``) ensure the call fails fast and
    never prompts for credentials.
    """
    try:
        result = subprocess.run(
            host.cmd(["has-session", "-t", session_name]),
            capture_output=True,
            timeout=8.0,
        )
        # SSH itself returns 255 when it cannot connect (refused, timeout,
        # auth failure).  tmux returns 0 (alive) or 1 (no session) — both
        # mean SSH succeeded.
        if result.returncode == 255:
            return False, False
        return result.returncode == 0, True
    except (subprocess.TimeoutExpired, subprocess.SubprocessError, OSError):
        return False, False


def reap_stale_remote_interactive_sessions(
    board: "Board",
    config: "Config",
) -> list[str]:
    """Sweep *board.active* for remote interactive sessions whose tmux session
    is dead, and release their machine slots via
    :func:`finalize_remote_interactive_exit`.

    This is the **remote** complement to :func:`reap_stale_interactive_sessions`,
    which deliberately skips remote assignments to avoid false-positive reaping
    of live sessions.  This function only touches assignments that are:

    * ``provider_name == "claude-pty"``
    * ``status`` is ``"running"`` or ``"pending"``
    * The assigned machine is **not** the local host
    * The assignment has been running for at least
      ``concurrency.interactive_session_timeout_hours`` (default 12 h)

    For each candidate it SSHes to the remote host (using ``BatchMode=yes``
    + ``ConnectTimeout`` for a fast, non-prompting probe) and runs
    ``tmux has-session -t coord-<id>``.  Three outcomes:

    1. **Session alive** — skip; the human is still working.
    2. **SSH ok but session dead** — the tmux session exited without going
       through the normal finalize path.  Call
       :func:`finalize_remote_interactive_exit` to push any outstanding
       commits, record the terminal status, and remove the remote worktree.
       The assignment is moved to ``board.completed`` and the machine slot
       is freed.
    3. **SSH unreachable** — the host is down or the connection timed out.
       Increment a per-session counter in :data:`_REMOTE_SSH_UNREACHABLE_COUNTS`
       and emit a ``logging.warning()`` so the operator sees it in CLI
       output.  The assignment is left ``running`` — a host that was up 22h
       ago might just be temporarily unreachable.

    Returns the assignment IDs that were reaped (outcome 2 only).  The
    caller should extend its ``changed`` list with these IDs so
    :func:`coord.state.save_board` is triggered.
    """
    timeout_hours = config.concurrency.interactive_session_timeout_hours
    if timeout_hours <= 0:
        return []  # sweep disabled by config

    timeout_secs = timeout_hours * 3600
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

        # Only probe remote machines — local ones are handled by
        # reap_stale_interactive_sessions().
        machine = machines_by_name.get(a.machine_name or "")
        if machine is None:
            continue
        _is_local = (
            machine.name.lower() == _local_hn
            or machine.host.split(".")[0].lower() == _local_hn
        )
        if _is_local:
            continue

        # Skip sessions that are too young to have plausibly gone stale.
        dispatched_at = a.dispatched_at or 0.0
        age_secs = now - dispatched_at
        if age_secs < timeout_secs:
            continue

        # ── SSH probe ──────────────────────────────────────────────────────
        sname = tmux_session_name(a.assignment_id)
        tmux_host = TmuxHost(ssh_target=machine.host, batch=True)
        alive, ssh_ok = _probe_remote_tmux_alive(sname, tmux_host)

        if alive:
            # Session is genuinely running — clear any unreachable counter
            # accumulated from prior SSH hiccups.
            _REMOTE_SSH_UNREACHABLE_COUNTS.pop(a.assignment_id, None)
            continue

        if not ssh_ok:
            # SSH transport failure — host may be rebooting, not crashed.
            # Emit a warning and leave the slot occupied rather than
            # destroying potentially good work.
            count = _REMOTE_SSH_UNREACHABLE_COUNTS.get(a.assignment_id, 0) + 1
            _REMOTE_SSH_UNREACHABLE_COUNTS[a.assignment_id] = count
            age_h = age_secs / 3600
            logging.warning(
                "⚠ %s unreachable — interactive session %s may be stale; "
                "running for %.1fh (consecutive SSH failures: %d)",
                machine.name, a.assignment_id, age_h, count,
            )
            continue

        # ── SSH ok, tmux session dead — reap it ───────────────────────────
        _REMOTE_SSH_UNREACHABLE_COUNTS.pop(a.assignment_id, None)

        repo_cfg = repos_by_name.get(a.repo_name or "")
        repo_github = repo_cfg.github if repo_cfg is not None else ""
        base_branch = (repo_cfg.default_branch or "main") if repo_cfg is not None else "main"
        artifact_paths_val = list(repo_cfg.artifact_paths or []) if repo_cfg is not None else []

        rp = machine.repo_path(a.repo_name or "")
        remote_repo_sh: str | None = None
        if rp:
            # Convert the machine's ``~/…`` repo path to a ``$HOME/…`` form
            # that the *remote* shell (not the coordinator's local shell) will
            # expand correctly.
            remote_repo_sh = (
                "$HOME/" + rp[2:]
                if rp.startswith("~/")
                else ("$HOME" if rp == "~" else rp)
            )

        remote_worktree_sh = "$HOME/.coord/worktrees/" + a.assignment_id

        # Recover the branch from the assignment record.  When the branch
        # wasn't persisted at dispatch time (pre-#557 boards) derive it from
        # the remote worktree HEAD so we don't strand commits.
        branch_val = a.branch
        if not branch_val and machine.host:
            try:
                probe = subprocess.run(
                    [
                        "ssh", *_SSH_MUX_OPTS, machine.host,
                        f"git -C {remote_worktree_sh}"
                        " rev-parse --abbrev-ref HEAD 2>/dev/null",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                if probe.returncode == 0:
                    derived = probe.stdout.strip()
                    if derived and derived != "HEAD":
                        branch_val = derived
            except Exception:  # noqa: BLE001
                pass

        if branch_val and remote_repo_sh:
            # Full finalize: push commits, record completion, clean worktree.
            try:
                fr = finalize_remote_interactive_exit(
                    assignment_id=a.assignment_id,
                    repo_name=a.repo_name or "",
                    repo_github=repo_github,
                    issue_number=a.issue_number,
                    machine_name=a.machine_name or "",
                    ssh_target=machine.host,
                    remote_worktree_sh=remote_worktree_sh,
                    remote_repo_sh=remote_repo_sh,
                    branch=branch_val,
                    base_branch=base_branch,
                    exit_code=1,  # stale / timed-out exit
                    started_at=a.dispatched_at,
                    artifact_paths=artifact_paths_val,
                )
                terminal_status = fr.terminal_status
                if terminal_status not in ("done", "advisory", "failed", "report-result"):
                    terminal_status = "failed"
            except Exception:  # noqa: BLE001
                # Finalize failed (e.g. network error mid-push). Fall back to
                # a plain DB mark so the slot is still freed.
                terminal_status = "failed"
                _mark_stale_reap_in_db(a.assignment_id, terminal_status, now)
        else:
            # branch or remote_repo_sh unavailable — can't push via finalize;
            # fall back to a bare DB mark so the machine slot is freed.
            terminal_status = "failed"
            _mark_stale_reap_in_db(a.assignment_id, terminal_status, now)

        # Update the in-memory board so the claim is released immediately.
        moved = board.mark_failed_by_id(a.assignment_id, finished_at=now)
        if moved is not None and terminal_status == "advisory":
            moved.status = "advisory"
        reaped.append(a.assignment_id)

    return reaped
