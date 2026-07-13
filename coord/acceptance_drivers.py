"""Framework driver adapters for ``coord acceptance`` (#944,
docs/ORACLE_LOOP.md).

``coord acceptance`` is a thin, framework-agnostic orchestrator; this module
is the one seam that varies per medium — TUI (quadraui ``TuiDriver``), CLI
(pytest), web (Playwright), native, etc. Each driver knows how to *run* a
repo's declared acceptance suite and *parse* its raw output into a
normalized list of ``{"id": str, "status": "pass"|"fail"|"skip", "message":
str}`` dicts (``cli-pytest`` additionally carries ``"expected"``/``"got"``
on a failing test — see :func:`parse_pytest_junit_xml`). ``tui-tuidriver``
and ``cli-pytest`` (#1125) are implemented; other ``kind`` values are
declared in ``coordinator.yml`` (see :class:`coord.config.AcceptanceConfig`)
but rejected here with a clear "not yet implemented" error until their issues
land (web-playwright, native).

``cli-pytest`` parses pytest's built-in ``--junit-xml`` report (a core
pytest flag, not a plugin — no extra dependency required in the driven
repo, unlike ``pytest-json-report``/``pytest-reportlog``) rather than
stdout, since junit-xml already carries a structured per-test
pass/fail/skip verdict plus each failure's message.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

# Driver kinds this module knows how to run. Keep in sync with the adapters
# implemented below — a kind can be *declared* in coordinator.yml ahead of its
# adapter landing, but running it must fail loudly rather than silently no-op.
SUPPORTED_KINDS = ("tui-tuidriver", "cli-pytest")

# libtest's ``--format json`` per-line test-event stream (`cargo test -- -Z
# unstable-options --format json`) event -> our normalized status.
_LIBTEST_EVENT_STATUS = {"ok": "pass", "failed": "fail", "ignored": "skip"}

# A junit-xml <failure>/<error> "message" attribute for a plain
# ``assert got == expected`` AssertionError has ``assert <got> ==
# <expected>`` on its first line (typically prefixed with the exception
# class, e.g. ``AssertionError: assert 'a' == 'b'``) — this is the common
# shape a cli-pytest test comparing actual CLI stdout to a `*.out` mock
# produces. Anything else (multi-line diffs, non-equality asserts, a raised
# exception with no ``assert``) is left unparsed rather than guessed at.
_ASSERT_EQ_RE = re.compile(r"assert\s+(.*?)\s+==\s+(.*)$")


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


def render_run_command(run_command: str, *, ms: str | None = None) -> str:
    """Substitute the ``{ms}`` template in *run_command* with *ms* (the
    ``ms-NN`` milestone dirname — see :func:`coord.acceptance.ms_dirname`),
    e.g. ``"pytest tests/acceptance/{ms}"`` -> ``"pytest
    tests/acceptance/ms-37"``.

    Left unsubstituted when *ms* is ``None`` — callers that aren't scoping to
    a milestone (or a driver's ``run:`` that never references ``{ms}`` at
    all, e.g. today's single-driver ``tui-tuidriver`` configs) pass the
    command through unchanged.
    """
    if ms is None:
        return run_command
    return run_command.replace("{ms}", ms)


def run_driver(
    kind: str, run_command: str, cwd: str, *, timeout: int = 900, ms: str | None = None,
) -> DriverResult:
    """Execute *run_command* in *cwd* and parse its output for *kind*.

    Raises :class:`DriverError` for an unsupported *kind* or a timeout. A
    non-zero exit from the command is NOT raised — it's folded into the
    returned :class:`DriverResult` so callers can still inspect whatever
    partial JSON the suite printed before dying.

    *ms*, when given, renders the ``{ms}`` template in *run_command* first
    (see :func:`render_run_command`).
    """
    if kind not in SUPPORTED_KINDS:
        raise DriverError(
            f"acceptance driver kind {kind!r} is not implemented yet "
            f"(supported: {', '.join(SUPPORTED_KINDS)}). web-playwright / "
            "native adapters land in later oracle-loop issues — see "
            "docs/ORACLE_LOOP.md."
        )

    run_command = render_run_command(run_command, ms=ms)

    if kind == "cli-pytest":
        return _run_cli_pytest(run_command, cwd, timeout=timeout)
    return _run_generic(run_command, cwd, timeout=timeout)


def _run_generic(run_command: str, cwd: str, *, timeout: int) -> DriverResult:
    """The ``tui-tuidriver`` (and any future stdout-native) shape: the
    command itself is responsible for printing structured verdicts to
    stdout — this just runs it and hands the raw stdout to
    :func:`parse_test_output`."""
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


def _run_cli_pytest(run_command: str, cwd: str, *, timeout: int) -> DriverResult:
    """The ``cli-pytest`` shape: append pytest's own built-in
    ``--junit-xml=<path>`` (a core pytest flag — no extra plugin required in
    the driven repo) so structured per-test verdicts are always produced
    regardless of what *run_command* itself prints, then parse that XML
    report with :func:`parse_pytest_junit_xml`.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        report_path = Path(tmp_dir) / "coord-acceptance-junit.xml"
        full_command = f"{run_command} --junit-xml={report_path}"
        try:
            proc = subprocess.run(
                full_command,
                shell=True,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as e:
            raise DriverError(
                f"acceptance run command timed out after {timeout}s: {full_command!r}"
            ) from e
        except OSError as e:
            raise DriverError(f"acceptance run command failed to start: {e}") from e

        report_text = report_path.read_text() if report_path.exists() else ""
        tests = parse_pytest_junit_xml(report_text)
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


def parse_pytest_junit_xml(xml_text: str) -> list[dict]:
    """Parse pytest's built-in ``--junit-xml=<path>`` report (a core pytest
    flag — no extra plugin required in the driven repo, unlike
    ``pytest-json-report``/``pytest-reportlog``) into normalized ``{"id",
    "status", "message", "expected", "got"}`` dicts — the same ``id``/
    ``status`` shape :func:`parse_test_output` returns for
    ``tui-tuidriver``, so :func:`coord.acceptance.build_verdict` /
    ``_scoped_verdict`` / :func:`coord.acceptance.load_manifest` work
    unchanged regardless of which driver kind produced the verdicts.

    Each ``<testcase classname="..." name="...">`` becomes one entry with
    ``id = "{classname}::{name}"``. A ``<failure>`` or ``<error>`` child
    means ``"fail"``; a ``<skipped>`` child means ``"skip"``; otherwise
    ``"pass"``. ``"expected"``/``"got"`` are populated only for a failing
    test, and only when the failure's ``message`` attribute's first line is
    pytest's own plain ``assert <got> == <expected>`` rendering (the shape a
    cli-pytest test comparing actual CLI stdout to a ``*.out`` mock
    produces) — anything else (a raised exception, a multi-line diff with no
    single ``==``) leaves them empty rather than guessing.

    Unparsable / empty input returns an empty list rather than raising —
    mirrors :func:`parse_test_output`'s "0 tests found, not a crash"
    contract.
    """
    text = (xml_text or "").strip()
    if not text:
        return []
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []

    tests = []
    for testcase in root.iter("testcase"):
        classname = testcase.get("classname", "")
        name = testcase.get("name", "")
        if not name:
            continue
        nodeid = f"{classname}::{name}" if classname else name

        failure = testcase.find("failure")
        if failure is None:
            failure = testcase.find("error")
        skipped = testcase.find("skipped")

        entry = {
            "id": nodeid, "status": "pass", "message": "",
            "expected": "", "got": "",
        }
        if failure is not None:
            entry["status"] = "fail"
            message = failure.get("message", "") or (failure.text or "")
            entry["message"] = message
            first_line = message.splitlines()[0] if message else ""
            m = _ASSERT_EQ_RE.search(first_line)
            if m:
                entry["got"] = m.group(1).strip()
                entry["expected"] = m.group(2).strip()
        elif skipped is not None:
            entry["status"] = "skip"
            entry["message"] = skipped.get("message", "") or (skipped.text or "")
        tests.append(entry)
    return tests


def _try_json(text: str):
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
