"""Framework driver adapters for ``coord acceptance`` (#944,
docs/ORACLE_LOOP.md).

``coord acceptance`` is a thin, framework-agnostic orchestrator; this module
is the one seam that varies per medium — TUI (quadraui ``TuiDriver``), web
(Playwright), native, etc. Each driver knows how to *run* a repo's declared
acceptance suite and *parse* its raw stdout into a normalized list of
``{"id": str, "status": "pass"|"fail"|"skip", "message": str}`` dicts. Only
``tui-tuidriver`` is implemented in this issue; other ``kind`` values are
declared in ``coordinator.yml`` (see :class:`coord.config.AcceptanceConfig`)
but rejected here with a clear "not yet implemented" error until their issues
land (web-playwright, native).
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field

# Driver kinds this module knows how to run. Keep in sync with the adapters
# implemented below — a kind can be *declared* in coordinator.yml ahead of its
# adapter landing, but running it must fail loudly rather than silently no-op.
SUPPORTED_KINDS = ("tui-tuidriver",)

# libtest's ``--format json`` per-line test-event stream (`cargo test -- -Z
# unstable-options --format json`) event -> our normalized status.
_LIBTEST_EVENT_STATUS = {"ok": "pass", "failed": "fail", "ignored": "skip"}


class DriverError(Exception):
    """Raised when a driver can't run its suite or the ``kind`` is unknown."""


@dataclass
class DriverResult:
    """The outcome of running one driver invocation."""

    exit_code: int
    tests: list[dict] = field(default_factory=list)
    raw_output: str = ""

    @property
    def ok(self) -> bool:
        """True when the run command itself exited 0.

        This is distinct from "all tests passed" — a driver can exit 0 while
        reporting individual test failures (cargo's own exit code already
        reflects failures, but a hand-rolled ``run:`` wrapper might not), so
        callers should judge pass/fail from ``tests`` rather than this alone.
        """
        return self.exit_code == 0


def run_driver(kind: str, run_command: str, cwd: str, *, timeout: int = 900) -> DriverResult:
    """Execute *run_command* in *cwd* and parse its stdout for *kind*.

    Raises :class:`DriverError` for an unsupported *kind* or a timeout. A
    non-zero exit from the command is NOT raised — it's folded into the
    returned :class:`DriverResult` so callers can still inspect whatever
    partial JSON the suite printed before dying.
    """
    if kind not in SUPPORTED_KINDS:
        raise DriverError(
            f"acceptance driver kind {kind!r} is not implemented yet "
            f"(supported: {', '.join(SUPPORTED_KINDS)}). web-playwright / "
            "native adapters land in later oracle-loop issues — see "
            "docs/ORACLE_LOOP.md."
        )

    try:
        proc = subprocess.run(
            run_command,
            shell=True,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        raise DriverError(
            f"acceptance run command timed out after {timeout}s: {run_command!r}"
        ) from e
    except OSError as e:
        raise DriverError(f"acceptance run command failed to start: {e}") from e

    tests = parse_test_output(proc.stdout)
    return DriverResult(
        exit_code=proc.returncode,
        tests=tests,
        raw_output=(proc.stdout or "") + (proc.stderr or ""),
    )


def parse_test_output(output: str) -> list[dict]:
    """Parse a driver's stdout into normalized ``{"id", "status", "message"}``.

    Two shapes are recognized:

    1. A single JSON blob whose whole stdout is one object of the form
       ``{"tests": [{"id": ..., "status": "pass"|"fail"|"skip", "message":
       ...}, ...]}`` — the direct contract for a driver that already speaks
       it natively.
    2. libtest's JSON-lines test-event stream (``cargo test -- -Z
       unstable-options --format json``): one JSON object per line, only
       ``{"type": "test", "event": "ok"|"failed"|"ignored", "name": ...}``
       lines carry a verdict. Non-JSON lines (cargo build progress,
       warnings) and ``"type": "suite"``/``"type": "bench"`` lines are
       skipped.

    Unparsable input returns an empty list rather than raising — a failed
    parse is surfaced by the caller as "0 tests found", not a crash.
    """
    stripped = (output or "").strip()
    if stripped.startswith("{"):
        blob = _try_json(stripped)
        if isinstance(blob, dict) and isinstance(blob.get("tests"), list):
            tests: list[dict] = []
            for t in blob["tests"]:
                if not isinstance(t, dict) or "id" not in t or "status" not in t:
                    continue
                tests.append({
                    "id": str(t["id"]),
                    "status": str(t["status"]),
                    "message": str(t.get("message", "")),
                })
            return tests

    tests = []
    for line in (output or "").splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        obj = _try_json(line)
        if not isinstance(obj, dict) or obj.get("type") != "test":
            continue
        event = obj.get("event")
        name = obj.get("name")
        if not name or event not in _LIBTEST_EVENT_STATUS:
            continue
        entry = {"id": str(name), "status": _LIBTEST_EVENT_STATUS[event], "message": ""}
        stdout_msg = obj.get("stdout")
        if stdout_msg:
            entry["message"] = str(stdout_msg)
        tests.append(entry)
    return tests


def _try_json(text: str):
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
