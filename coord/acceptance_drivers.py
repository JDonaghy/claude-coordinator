"""Framework driver adapters for ``coord acceptance`` (#944,
docs/ORACLE_LOOP.md).

``coord acceptance`` is a thin, framework-agnostic orchestrator; this module
is the one seam that varies per medium — TUI (quadraui ``TuiDriver``), CLI
(pytest), web (Playwright), native, etc. Each driver knows how to *run* a
repo's declared acceptance suite and *parse* its raw output into a
normalized list of ``{"id": str, "status": "pass"|"fail"|"skip", "message":
str}`` dicts (``cli-pytest`` additionally carries ``"expected"``/``"got"``
on a failing test — see :func:`parse_pytest_junit_xml`). ``tui-tuidriver``,
``cli-pytest`` (#1125), and ``web-playwright`` (#1539) are implemented;
other ``kind`` values are declared in ``coordinator.yml`` (see
:class:`coord.config.AcceptanceConfig`) but rejected here with a clear "not
yet implemented" error until their issues land (native).

``cli-pytest`` parses pytest's built-in ``--junit-xml`` report (a core
pytest flag, not a plugin — no extra dependency required in the driven
repo, unlike ``pytest-json-report``/``pytest-reportlog``) rather than
stdout, since junit-xml already carries a structured per-test
pass/fail/skip verdict plus each failure's message.

``web-playwright`` parses Playwright Test's built-in ``--reporter=json``
report rather than its built-in ``--reporter=junit`` one — see
:func:`parse_playwright_json_report` for why (short version: junit
collapses retries into one opaque CDATA blob with no per-attempt status,
which loses the "did this flake" signal this driver exists to capture; json
keeps a ``results[]`` entry per attempt).

:func:`run_driver` also runs a driver's optional ``setup:`` provisioning
command (#1733, ``AcceptanceDriverConfig.setup``) once before its suite —
e.g. ``npm ci`` for ``web-playwright``, which otherwise fails with a bare
``exit 127`` (playwright not found) the first time it runs against ``coord
acceptance record``'s throwaway, dependency-less worktree.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

# Driver kinds this module knows how to run. Keep in sync with the adapters
# implemented below — a kind can be *declared* in coordinator.yml ahead of its
# adapter landing, but running it must fail loudly rather than silently no-op.
SUPPORTED_KINDS = ("tui-tuidriver", "cli-pytest", "web-playwright")

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

# Playwright's JSON reporter bakes ANSI color/style codes (SGR sequences)
# straight into `error.message` regardless of whether stdout is a tty or
# `NO_COLOR`/`FORCE_COLOR=0` is set (verified empirically against 1.61 — the
# assertion diff formatter colors unconditionally) — strip them so a stored
# verdict message doesn't carry raw escape bytes.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

# Playwright JSON reporter's per-test `status` (already reconciled against
# retries and `expectedStatus` — e.g. a `test.fail()`-annotated test that
# fails as expected is "expected", not "unexpected") -> our normalized
# status. "flaky" (failed at least once, then passed within `retries`) is a
# "pass" for gating purposes but callers should still care it happened — see
# :func:`parse_playwright_json_report`, which folds that into the message.
_PLAYWRIGHT_STATUS = {
    "expected": "pass",
    "flaky": "pass",
    "unexpected": "fail",
    "skipped": "skip",
}


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


# Where :func:`resolve_cargo` looks, in the order a human debugging this
# would try. Quoted verbatim in the DriverError a missing toolchain raises so
# the message names the places searched rather than just "not found".
CARGO_SEARCHED = "PATH, ${CARGO_HOME:-$HOME/.cargo}/bin/cargo, `rustup which cargo`"


def resolve_cargo(env: dict[str, str] | None = None) -> str | None:
    """Absolute path to ``cargo``, or ``None`` when it can't be found.

    The Python twin of ``scripts/coord-test-runner.sh``'s ``resolve_cargo``
    (#1814), and it exists for the same reason: ``coord serve`` / ``coord
    agent`` / a ``coord drive`` loop spawned by them are systemd **user**
    units, whose PATH is systemd's rather than a login shell's. ``~/.profile``
    and the shell rcs that put ``~/.cargo/bin`` on PATH are never sourced for
    them, so a bare ``cargo`` resolves fine over ssh and not at all inside the
    daemon.

    #1814 fixed that for the Test stage but not here, and the acceptance
    driver kept paying for it: every ``coord acceptance record`` of a
    ``tui-tuidriver`` route launched from the drive loop ran ``cargo test``
    through ``/bin/sh``, got ``cargo: not found`` (exit 127, empty stdout),
    parsed zero tests out of it, and recorded that as a perfectly ordinary
    ``acceptance_failed`` verdict with ``total=0, passed=0`` — a false red on
    a green suite, indistinguishable in the audit log from a branch that
    genuinely broke its slice.

    Search order: PATH, then rustup's default install location, then rustup's
    own answer (rustup may be off PATH for exactly the same reason cargo is).
    """
    env = dict(os.environ) if env is None else env
    path = env.get("PATH") or os.defpath

    found = shutil.which("cargo", path=path)
    if found:
        return found

    cargo_home = env.get("CARGO_HOME") or str(
        Path(env.get("HOME") or Path.home()) / ".cargo"
    )
    candidate = Path(cargo_home) / "bin" / "cargo"
    if os.access(candidate, os.X_OK):
        return str(candidate)

    rustup = shutil.which("rustup", path=path)
    if not rustup:
        rustup_candidate = Path(cargo_home) / "bin" / "rustup"
        rustup = str(rustup_candidate) if os.access(rustup_candidate, os.X_OK) else None
    if rustup:
        try:
            proc = subprocess.run(
                [rustup, "which", "cargo"],
                capture_output=True, text=True, timeout=30, env=env,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        resolved = (proc.stdout or "").strip()
        if proc.returncode == 0 and resolved and os.access(resolved, os.X_OK):
            return resolved
    return None


def driver_env(
    base_env: dict[str, str] | None = None, *, repo_name: str | None = None
) -> dict[str, str]:
    """The environment a driver's ``setup``/``run`` command executes in.

    Two overlays on top of *base_env* (``os.environ`` by default), both of
    which the drive loop's systemd-user-unit environment is missing:

    1. **The rust toolchain on PATH** (#1814). :func:`resolve_cargo`'s
       directory is *prepended* to PATH when cargo isn't already resolvable
       from it — the whole bin dir, not just ``cargo``, because cargo shells
       out to ``rustc``, which lives beside it. A machine with no rust
       toolchain at all is left untouched; the run then fails loudly in
       :func:`_run_generic` rather than being papered over here.
    2. **The shared per-repo cargo target dir** (#1402,
       :func:`coord.cargo_cache.cargo_env`), when *repo_name* is given. Every
       ``coord acceptance record`` builds in a fresh throwaway worktree, so
       without this each one is a cold ~2 GiB build racing the driver's 900s
       timeout. An operator's own ``CARGO_TARGET_DIR`` still wins — that's
       ``cargo_env``'s own rule, not a special case here.
    """
    env = dict(os.environ if base_env is None else base_env)

    cargo = resolve_cargo(env)
    if cargo:
        bin_dir = str(Path(cargo).parent)
        path = env.get("PATH") or os.defpath
        if bin_dir not in path.split(os.pathsep):
            env["PATH"] = f"{bin_dir}{os.pathsep}{path}"

    if repo_name:
        from coord.cargo_cache import cargo_env  # noqa: PLC0415  (cycle-safe)
        from coord.state import COORD_DIR  # noqa: PLC0415

        env.update(cargo_env(repo_name, COORD_DIR, env))

    return env


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
    setup_command: str = "", repo_name: str | None = None,
) -> DriverResult:
    """Execute *run_command* in *cwd* and parse its output for *kind*.

    Raises :class:`DriverError` for an unsupported *kind* or a timeout. A
    non-zero exit from the command is NOT raised — it's folded into the
    returned :class:`DriverResult` so callers can still inspect whatever
    partial JSON the suite printed before dying.

    *ms*, when given, renders the ``{ms}`` template in *run_command* first
    (see :func:`render_run_command`).

    *setup_command* (#1733, ``AcceptanceDriverConfig.setup``), when
    non-empty, runs ONCE in *cwd* before *run_command* — the provisioning
    step a driver needs a bare checkout doesn't provide (e.g. ``npm ci`` for
    ``web-playwright`` in ``coord acceptance record``'s throwaway,
    dependency-less worktree). Unlike a non-zero *run_command* exit, a
    failing *setup_command* DOES raise :class:`DriverError` — immediately,
    before *run_command* ever executes — with a message that names it as a
    provisioning failure so it isn't mistaken for a test failure or folded
    into a driver's own "wrote no report" crash message.

    *repo_name*, when given, points cargo at this machine's shared per-repo
    target dir for both commands (see :func:`driver_env`).
    """
    if kind not in SUPPORTED_KINDS:
        raise DriverError(
            f"acceptance driver kind {kind!r} is not implemented yet "
            f"(supported: {', '.join(SUPPORTED_KINDS)}). The native adapter "
            "lands in a later oracle-loop issue — see docs/ORACLE_LOOP.md."
        )

    env = driver_env(repo_name=repo_name)

    if setup_command:
        _run_setup(setup_command, cwd, timeout=timeout, env=env)

    run_command = render_run_command(run_command, ms=ms)

    if kind == "cli-pytest":
        return _run_cli_pytest(run_command, cwd, timeout=timeout, env=env)
    if kind == "web-playwright":
        return _run_web_playwright(run_command, cwd, timeout=timeout, env=env)
    return _run_generic(run_command, cwd, timeout=timeout, env=env)


def _run_setup(
    setup_command: str, cwd: str, *, timeout: int, env: dict[str, str] | None = None
) -> None:
    """Run a driver's ``setup:`` provisioning command (#1733) in *cwd*,
    before its suite ever runs.

    Raises :class:`DriverError` — distinctly worded as a provisioning
    failure, not a test failure — for a non-zero exit, a timeout, or the
    command failing to start at all. Callers must not proceed to
    ``run_command`` when this raises: a driver whose dependencies never
    installed cannot produce a meaningful verdict.
    """
    try:
        proc = subprocess.run(
            setup_command,
            shell=True,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired as e:
        raise DriverError(
            f"acceptance driver provisioning timed out after {timeout}s: "
            f"{setup_command!r}"
        ) from e
    except OSError as e:
        raise DriverError(
            f"acceptance driver provisioning failed to start: {setup_command!r}: {e}"
        ) from e

    if proc.returncode != 0:
        stderr_tail = "\n".join((proc.stderr or "").splitlines()[-20:])
        raise DriverError(
            f"acceptance driver provisioning failed (exit {proc.returncode}): "
            f"{setup_command!r}\n{stderr_tail}"
        )


def _run_generic(
    run_command: str, cwd: str, *, timeout: int, env: dict[str, str] | None = None
) -> DriverResult:
    """The ``tui-tuidriver`` (and any future stdout-native) shape: the
    command itself is responsible for printing structured verdicts to
    stdout — this just runs it and hands the raw stdout to
    :func:`parse_test_output`.

    A non-zero exit that *also* parsed **zero** tests raises
    :class:`DriverError` rather than returning an empty result — the same
    rule :func:`_run_web_playwright` has enforced since #1539 ("a crashed run
    ... must surface as a DriverError or an explicit failure — never as an
    empty pass list"), which this driver was missing.

    The asymmetry is deliberate and is the whole point: a non-zero exit *with*
    tests parsed is an ordinary red suite (cargo exits 101 whenever a test
    fails) and is still returned intact, partial JSON and all. A non-zero exit
    with *nothing* parsed means the suite never produced a verdict — cargo not
    on PATH (exit 127, the #1814 systemd-PATH bug), a compile error, a harness
    that aborted before libtest emitted its first event. ``coord acceptance
    record`` used to store that as ``total=0, passed=0`` + ``acceptance_failed``:
    a false red, worded identically to a real one, that no amount of fixing
    the branch could ever clear.
    """
    try:
        proc = subprocess.run(
            run_command,
            shell=True,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired as e:
        raise DriverError(
            f"acceptance run command timed out after {timeout}s: {run_command!r}"
        ) from e
    except OSError as e:
        raise DriverError(f"acceptance run command failed to start: {e}") from e

    tests = parse_test_output(proc.stdout)
    if not tests and proc.returncode != 0:
        stderr_tail = "\n".join((proc.stderr or "").splitlines()[-20:])
        hint = ""
        if "cargo" in run_command and not resolve_cargo(env):
            hint = (
                "\n  cargo could not be found either — searched: "
                f"{CARGO_SEARCHED}\n"
                f"  PATH={(env or os.environ).get('PATH', '')}\n"
                "  A systemd user unit's PATH is not a login shell's; "
                "~/.profile is never sourced for it (#1814)."
            )
        raise DriverError(
            f"acceptance run reported no tests (exit {proc.returncode}): "
            f"{run_command!r}\n"
            "  This is an INFRASTRUCTURE failure, not a test failure: the "
            "suite never produced a verdict, so nothing about the branch may "
            f"be inferred from it.{hint}\n{stderr_tail}"
        )
    return DriverResult(
        exit_code=proc.returncode,
        tests=tests,
        raw_output=(proc.stdout or "") + (proc.stderr or ""),
    )


def _run_cli_pytest(
    run_command: str, cwd: str, *, timeout: int, env: dict[str, str] | None = None
) -> DriverResult:
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
                env=env,
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


def _run_web_playwright(
    run_command: str, cwd: str, *, timeout: int, env: dict[str, str] | None = None
) -> DriverResult:
    """The ``web-playwright`` shape: force Playwright Test's built-in
    ``json`` reporter to a known path via ``--reporter=json`` plus the
    ``PLAYWRIGHT_JSON_OUTPUT_FILE`` env var it honors (the json-reporter
    twin of the documented ``PLAYWRIGHT_JUNIT_OUTPUT_NAME``) — so a
    structured report is always produced at a path we control regardless of
    what reporters the driven repo's own ``playwright.config.ts`` declares,
    the same trick :func:`_run_cli_pytest` plays with ``--junit-xml``.

    Unlike ``_run_cli_pytest`` (which treats "no report file" as a benign
    zero-tests result — see its own crash test), a missing or corrupt
    report here always raises :class:`DriverError`. Per #1539: "a crashed
    run ... must surface as a DriverError or an explicit failure — never as
    an empty pass list." Playwright can die before the json reporter ever
    flushes (a bad config file, a browser that never launches, `--grep`
    matching nothing without `--pass-with-no-tests`) and an empty list from
    that is indistinguishable from "the suite legitimately has zero tests
    right now" — exactly the silent-green failure mode this driver must not
    produce.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        report_path = Path(tmp_dir) / "coord-acceptance-playwright.json"
        full_command = f"{run_command} --reporter=json"
        run_env = {
            **(os.environ if env is None else env),
            "PLAYWRIGHT_JSON_OUTPUT_FILE": str(report_path),
        }
        try:
            proc = subprocess.run(
                full_command,
                shell=True,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=run_env,
            )
        except subprocess.TimeoutExpired as e:
            raise DriverError(
                f"acceptance run command timed out after {timeout}s: {full_command!r}"
            ) from e
        except OSError as e:
            raise DriverError(f"acceptance run command failed to start: {e}") from e

        if not report_path.exists():
            stderr_tail = "\n".join((proc.stderr or "").splitlines()[-20:])
            raise DriverError(
                f"web-playwright run wrote no report (exit {proc.returncode}): "
                f"{full_command!r}\n{stderr_tail}"
            )
        tests = parse_playwright_json_report(report_path.read_text())
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


def parse_playwright_json_report(json_text: str) -> list[dict]:
    """Parse Playwright Test's built-in ``--reporter=json`` report (a core
    reporter, not a plugin — no extra npm dependency required in the driven
    repo) into normalized ``{"id", "status", "message"}`` dicts.

    **Why json, not junit** (Playwright ships both as built-ins): junit
    collapses every retry attempt of one test into a single ``<testcase>``
    with no per-attempt status — a test that failed once then passed on
    retry renders as a plain, silent pass, identical to a test that never
    failed at all. That's a real gap verified against Playwright 1.61
    output, not a hypothetical: see the recorded
    ``tests/fixtures/playwright/retry_then_pass.json`` fixture and its junit
    sibling this module's tests compare it against. Since #1539 explicitly
    calls out retries as signal ("flake is signal for this program"), junit
    can't carry the contract this driver needs. json's per-test ``results``
    array keeps one entry per attempt (with its own ``status`` and
    ``errors``), and its per-test ``projectName`` disambiguates the same
    test title run under multiple ``projects:`` entries — junit only
    distinguishes projects via a ``<testsuite hostname="...">`` attribute
    shared by every testcase in that project, not the id.

    **id shape**: ``"[{project}] {file} › {describe path} › {test title}"``
    — stable across reruns (no timestamps/durations), unique across
    multiple ``projects:`` in one config (see above), and matches the
    ``[project] › file:line › describe › title`` shape Playwright's own
    ``list`` reporter prints, so it's recognizable when cross-referencing a
    human's terminal output.

    **status**: taken from Playwright's own reconciled per-test ``status``
    (``"expected"``/``"flaky"``/``"unexpected"``/``"skipped"``) rather than
    re-deriving it from the raw per-attempt results — that field already
    accounts for retries *and* ``test.fail()``-style "expected to fail"
    annotations, so re-implementing it here would just be a worse copy.
    ``"expected"``/``"flaky"`` -> ``"pass"`` (flaky is still a pass for
    gating, but the message says so — see below), ``"unexpected"`` ->
    ``"fail"``, ``"skipped"`` (covers both ``test.skip()`` and
    ``test.fixme()``) -> ``"skip"``.

    **message**: the last attempt's error text for a ``"fail"``; for a
    ``"flaky"`` pass, a summary noting the flake plus the first failed
    attempt's error (the signal #1539 asks this driver to preserve); the
    annotation ``description`` (the ``fixme`` reason, when given) for a
    ``"skip"``; empty for a clean pass. Error text has Playwright's
    baked-in ANSI color codes stripped (see :data:`_ANSI_RE` — verified
    these survive even with ``NO_COLOR``/piped-non-tty stdout, so stripping
    is mandatory, not a courtesy).

    **Never silently empty on a crash** — unlike this module's other parse_*
    functions, which return ``[]`` on unparsable input to keep "0 tests
    found" from ever raising. #1539 requires the opposite here: "a crashed
    run ... must surface as a DriverError ... never as an empty pass list."
    So this raises :class:`DriverError` for: empty/whitespace-only input (a
    truncated-to-nothing or never-written report); invalid JSON (a report
    cut off mid-write, e.g. the process was killed before flushing —
    ``tests/fixtures/playwright/truncated.json`` is a real report truncated
    this way); a JSON body missing the ``"suites"`` list (wrong shape
    entirely); and zero tests parsed *while Playwright's own top-level
    ``"errors"`` is non-empty* — verified empirically to be exactly how a
    thrown ``globalSetup`` hook or a ``--grep`` matching nothing report
    (``tests/fixtures/playwright/global_setup_crash.json``): a
    well-formed, zero-test report that must not be mistaken for "the suite
    is just empty right now". Zero tests with an empty top-level
    ``"errors"`` (Playwright's own ``--pass-with-no-tests`` opt-in) is left
    as a plain ``[]`` — callers (:func:`coord.acceptance.build_verdict`)
    already treat a zero-test list as not-green rather than a false "all
    green", which is the actual guarantee #1539 is protecting.
    """
    text = (json_text or "").strip()
    if not text:
        raise DriverError(
            "web-playwright report is empty — the run crashed before "
            "writing a report"
        )
    try:
        report = json.loads(text)
    except json.JSONDecodeError as e:
        raise DriverError(
            f"web-playwright report is not valid JSON (truncated or "
            f"corrupted run?): {e}"
        ) from e
    if not isinstance(report, dict) or not isinstance(report.get("suites"), list):
        raise DriverError(
            "web-playwright report has an unrecognized shape (missing a "
            "'suites' list) — this reporter version may be incompatible"
        )

    tests: list[dict] = []
    for suite in report["suites"]:
        if isinstance(suite, dict):
            tests.extend(_playwright_specs(suite, []))

    if not tests and report.get("errors"):
        first = report["errors"][0] if isinstance(report["errors"], list) else report["errors"]
        detail = first.get("message", "") if isinstance(first, dict) else str(first)
        raise DriverError(
            f"web-playwright run produced zero tests and reported a "
            f"top-level error (bad config, browser launch failure, or a "
            f"run: command matching no tests): {_strip_ansi(detail)}"
        )
    return tests


def _playwright_specs(suite: dict, ancestors: list[str]) -> list[dict]:
    """Recursively walk one Playwright json-report suite tree, returning one
    normalized dict per ``(spec, project)`` pair.

    A suite nests: the outermost suite per spec file (``title == file``,
    skipped from the id's describe-path since it's redundant with the
    ``file`` already in the id), then one nested suite per ``describe()``
    block, down to leaf ``specs`` (one per ``test()``/``it()``).
    """
    title = suite.get("title", "")
    is_file_suite = bool(suite.get("file")) and title == suite.get("file")
    path = ancestors if is_file_suite else [*ancestors, title]

    tests: list[dict] = []
    for spec in suite.get("specs") or []:
        if isinstance(spec, dict):
            tests.extend(_playwright_spec_entries(spec, path))
    for sub in suite.get("suites") or []:
        if isinstance(sub, dict):
            tests.extend(_playwright_specs(sub, path))
    return tests


def _playwright_spec_entries(spec: dict, ancestors: list[str]) -> list[dict]:
    """One normalized entry per project a leaf ``spec`` (a single
    ``test()``) ran under."""
    spec_title = spec.get("title", "")
    title_path = " › ".join([*ancestors, spec_title]) if spec_title else " › ".join(ancestors)
    file = spec.get("file", "")

    entries = []
    for t in spec.get("tests") or []:
        if not isinstance(t, dict):
            continue
        project = t.get("projectName", "")
        nodeid = f"[{project}] {file} › {title_path}" if project else f"{file} › {title_path}"
        raw_status = t.get("status")
        status = _PLAYWRIGHT_STATUS.get(raw_status, "fail")
        results = t.get("results") or []

        message = ""
        if status == "fail":
            message = _playwright_error_text(results[-1]) if results else ""
        elif raw_status == "flaky":
            failed = [r for r in results if r.get("status") not in ("passed", "skipped")]
            first_failure = _playwright_error_text(failed[0]) if failed else ""
            message = f"flaky: passed after {len(failed)} failed attempt(s)"
            if first_failure:
                message += f" — first failure: {first_failure}"
        elif status == "skip":
            message = _playwright_skip_reason(t.get("annotations") or [])

        entries.append({"id": nodeid, "status": status, "message": message})
    return entries


def _playwright_error_text(result: dict) -> str:
    """The (ANSI-stripped) error message(s) of one ``results[]`` attempt."""
    if not isinstance(result, dict):
        return ""
    errors = result.get("errors") or []
    parts = [str(e.get("message", "")) for e in errors if isinstance(e, dict) and e.get("message")]
    return _strip_ansi("\n".join(parts))


def _playwright_skip_reason(annotations: list) -> str:
    """The ``fixme``/``skip`` annotation's ``description`` (the reason
    string a caller passed, e.g. ``test.fixme(true, "blocked on #1541")``),
    or ``""`` when a bare ``test.skip()``/``.skip(true)`` gave no reason —
    mirrors :func:`parse_pytest_junit_xml`'s "empty message when no reason
    given" convention.
    """
    for a in annotations:
        if isinstance(a, dict) and a.get("type") in ("skip", "fixme") and a.get("description"):
            return str(a["description"])
    return ""


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text or "")


def _try_json(text: str):
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
