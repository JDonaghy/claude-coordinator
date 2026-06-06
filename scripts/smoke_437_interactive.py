"""Live smoke test for #437: real `claude` interactive PRE-FILL without auto-submit.

This is NOT a pytest unit — it spawns the REAL ``claude`` binary inside a
pty and validates the byte stream the launcher writes to the master fd.
Run manually with:

    .venv/bin/python scripts/smoke_437_interactive.py

The script:

1. Forks; the child first monkey-patches ``coord.interactive.os.write`` so
   every byte string the launcher injects into the PTY master fd is
   recorded to a side log file, then invokes
   ``coord.interactive.launch_human_attended_interactive`` with the real
   ``~/.local/bin/claude`` binary and a unique briefing marker.
2. The parent owns the controlling pty's master fd and copies every byte
   the TUI writes to a transcript log file.
3. After a short watchdog deadline the parent sends Ctrl-C through the
   PTY so the child exits — the HUMAN, in production, would press Enter
   to submit and ``/exit`` to close.  Using Ctrl-C here proves the
   session is human-driveable and is NOT auto-terminated by the
   coordinator on any TTY content.  We send Ctrl-C and NOT Enter
   because submitting a real prompt to claude under a test harness
   would be itself a borderline TOS violation — we only want to prove
   the pre-fill landed.
4. The parent then asserts:
   * ``ESC[200~ <briefing> ESC[201~`` appears as a single contiguous
     write in the launcher-side write log (the pre-fill was injected
     as a bracketed paste);
   * NO bare carriage return (``b"\\r"``) write follows that paste —
     the agent did NOT auto-submit;
   * The briefing marker is visible in the TUI transcript (proving the
     paste landed in the TUI's input box).
"""

from __future__ import annotations

import fcntl
import os
import pty
import re
import select
import signal
import struct
import sys
import termios
import time
from pathlib import Path

CLAUDE_BIN = str(Path("~/.local/bin/claude").expanduser())
TRUSTED_CWD = str(Path("~/src/claude-coordinator").expanduser())
TUI_LOG = Path("/tmp/smoke_437_tui.log")
WRITES_LOG = Path("/tmp/smoke_437_writes.log")
BRIEFING = "PRE_FILL_SMOKE_437_MARKER_XYZ"
# Cap the session at this many seconds before we send SIGINT to the
# child.  Real users would close the session themselves; the watchdog
# is here only so the smoke test terminates without human input.
SESSION_DEADLINE_S = 18.0


def main() -> int:
    if not Path(CLAUDE_BIN).resolve().exists():
        print(f"FAIL: claude binary not found at {CLAUDE_BIN}", file=sys.stderr)
        return 2

    for p in (TUI_LOG, WRITES_LOG):
        if p.exists():
            p.unlink()

    pid, master_fd = pty.fork()
    if pid == 0:
        # ── child ─────────────────────────────────────────────────────
        os.chdir(TRUSTED_CWD)
        # Patch os.write inside coord.interactive so every byte the
        # launcher injects into the PTY master fd is recorded BEFORE
        # the bytes go anywhere.  We open the writes log in 'wb' mode
        # so it overwrites on each run; we flush after every write.
        import coord.interactive as ci  # noqa: PLC0415
        real_write = ci.os.write
        sink = open(WRITES_LOG, "wb")  # noqa: SIM115 — held for process lifetime

        def recording_write(fd: int, data: bytes) -> int:
            # We only care about the launcher's writes to the PTY master.
            # Other os.write calls (e.g. writes to fd_out for TUI relay)
            # are skipped to keep the log focused.  The launcher's
            # pre-fill write targets a fresh master_fd whose number is
            # >= 3; relays to stdout (fd 1) we ignore.
            if fd not in (0, 1, 2):
                sink.write(b"WRITE(" + str(fd).encode() + b"): ")
                sink.write(data)
                sink.write(b"\n--END--\n")
                sink.flush()
            return real_write(fd, data)

        ci.os.write = recording_write  # type: ignore[assignment]

        from coord.agent import AssignmentSpec  # noqa: PLC0415
        from coord.providers import ClaudePtyProvider  # noqa: PLC0415

        provider = ClaudePtyProvider(binary=CLAUDE_BIN)
        spec = AssignmentSpec(
            repo_name="claude-coordinator",
            repo_path=TRUSTED_CWD,
            issue_number=437,
            issue_title="smoke test",
            briefing=BRIEFING,
            model=None,
            type="plan",
            provider="claude-pty",
        )
        argv = provider.build_command(spec)
        # The launcher will fork claude as a child of OUR forked
        # process; pty.fork above already made our stdin/stdout the
        # slave pty, so the relay loop in the launcher reads from /
        # writes to the parent transcript fd via THIS slave.
        from coord.interactive import (  # noqa: PLC0415
            launch_human_attended_interactive,
        )
        code = launch_human_attended_interactive(argv, BRIEFING, cwd=TRUSTED_CWD)
        sink.close()
        os._exit(code)

    # ── parent ────────────────────────────────────────────────────────
    try:
        fcntl.ioctl(
            master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0)
        )
    except OSError:
        pass

    started = time.monotonic()
    sent_sigint = False
    captured = bytearray()
    try:
        while True:
            r, _, _ = select.select([master_fd], [], [], 0.1)
            if master_fd in r:
                try:
                    chunk = os.read(master_fd, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                captured.extend(chunk)
                TUI_LOG.write_bytes(bytes(captured))

            now = time.monotonic()
            if not sent_sigint and now - started >= SESSION_DEADLINE_S - 5.0:
                # Send Ctrl-C through the PTY — the human-driveable
                # signal that ends the session.
                try:
                    os.write(master_fd, b"\x03\x03")
                except OSError:
                    pass
                sent_sigint = True

            if now - started >= SESSION_DEADLINE_S:
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass

            try:
                done_pid, _status = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                break
            if done_pid != 0:
                while True:
                    try:
                        chunk = os.read(master_fd, 4096)
                    except OSError:
                        break
                    if not chunk:
                        break
                    captured.extend(chunk)
                break
    finally:
        try:
            os.close(master_fd)
        except OSError:
            pass

    TUI_LOG.write_bytes(bytes(captured))
    print(f"=== captured {len(captured)} bytes of TUI output to {TUI_LOG} ===")
    print(f"=== launcher writes recorded to {WRITES_LOG} ===")

    # ── Assertions ──────────────────────────────────────────────────────
    BRACKETED_PASTE_START = b"\x1b[200~"
    BRACKETED_PASTE_END = b"\x1b[201~"

    if not WRITES_LOG.exists():
        print("FAIL: writes log missing — child did not record any os.write calls")
        return 1

    writes = WRITES_LOG.read_bytes()
    # Find each recorded write.  Format: WRITE(<fd>): <bytes>\n--END--\n
    write_entries = []
    for m in re.finditer(rb"WRITE\((\d+)\):\s(.*?)\n--END--\n", writes, re.DOTALL):
        fd = int(m.group(1).decode())
        data = m.group(2)
        write_entries.append((fd, data))

    print(f"recorded {len(write_entries)} os.write calls from the launcher")

    # (A) The bracketed paste block must appear as ONE contiguous write.
    paste_block = (
        BRACKETED_PASTE_START + BRIEFING.encode("utf-8") + BRACKETED_PASTE_END
    )
    paste_writes = [
        (fd, data) for fd, data in write_entries if paste_block in data
    ]
    if not paste_writes:
        print("FAIL: the bracketed-paste pre-fill block was never written")
        for fd, data in write_entries:
            print(f"  fd={fd}: {data!r}")
        return 1
    print(f"PASS: bracketed-paste block written ({len(paste_writes)} time(s))")

    # (B) NO bare carriage return write follows the paste.  The agent
    # MUST NOT auto-submit.  We tolerate \r appearing inside relay writes
    # of bytes coming back FROM the TUI (those are echoes, not the
    # launcher's own injects).  The launcher's PTY-master write of bare
    # b"\r" is the regression we removed in #437.
    bare_cr_writes = [
        (fd, data) for fd, data in write_entries if data == b"\r"
    ]
    if bare_cr_writes:
        print("FAIL: the launcher wrote a bare \\r byte to the PTY master — auto-submit regression")
        for fd, data in bare_cr_writes:
            print(f"  fd={fd}: {data!r}")
        return 1
    print("PASS: no bare \\r write — pre-fill is NOT auto-submitted")

    # (C) The briefing marker text must be visible in the TUI transcript
    # (proving the paste landed in the input box and the TUI rendered it).
    if BRIEFING.encode("utf-8") not in captured:
        print("FAIL: briefing marker missing from TUI output — TUI did not render the paste")
        _print_tui_excerpt(captured)
        return 1
    print("PASS: briefing marker visible in TUI output — paste landed")

    print()
    print("=== KEY TTY LINES (for completion summary) ===")
    _print_tui_excerpt(captured)
    print()
    print("=== KEY LAUNCHER WRITES (for completion summary) ===")
    for fd, data in write_entries:
        if BRACKETED_PASTE_START in data or BRACKETED_PASTE_END in data:
            print(f"  fd={fd}: {data!r}")
    print()
    print("SUCCESS: all live-smoke assertions passed")
    return 0


def _print_tui_excerpt(captured: bytes | bytearray) -> None:
    text = bytes(captured).decode("utf-8", errors="replace")
    cleaned = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", text)
    cleaned = re.sub(r"\x1b\][^\x07]*\x07", "", cleaned)
    print("─── TUI transcript (ANSI stripped, last 30 lines) ───")
    for line in cleaned.splitlines()[-30:]:
        print(repr(line))
    print("──────────────────────────────────────────────────────")


if __name__ == "__main__":
    sys.exit(main())
