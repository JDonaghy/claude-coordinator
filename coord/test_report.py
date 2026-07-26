"""Pure parsers for the coord-test-runner.sh (#1436) failure classifier.

`scripts/coord-test-runner.sh` decides whether a Test-gate run is a genuine
failure, a flake, a build/collection error (never flake-retried), or a run
whose output it simply could not parse (which must never be silently
recorded as a pass). Four of those decisions were `grep`/`awk` one-liners
with zero test coverage; this module extracts them into tested Python
functions. The shell script is untouched by this change — see #1392 for the
follow-up that ports the state machine and wires these in.

Each function's docstring names the shell expression it mirrors and any
behavioural difference (the ``pytest_failed_node_ids`` parametrized-id
truncation fix is the one deliberate difference — see its docstring).
"""

from __future__ import annotations

import re

# ── pytest ───────────────────────────────────────────────────────────────────

# pytest's own "short test summary info" format is:
#   FAILED <node id> - <failure message, possibly truncated with '...'>
# The " - " separator is not part of any valid node id (pytest node ids are
# `path::name[params]`), so splitting on the first occurrence of it is safe
# even when the message itself contains " - ".
_FAILED_PREFIX = "FAILED "
_FAILED_MESSAGE_SEP = " - "


def pytest_failed_node_ids(output: str) -> list[str]:
    """Extract unique pytest failure node ids from ``pytest`` output.

    Mirrors the shell's::

        grep '^FAILED ' | awk '{print $2}' | sort -u

    with one deliberate fix: ``awk '{print $2}'`` splits the line on
    whitespace, so a parametrized node id containing a space — e.g.
    ``test_x[a b]`` — is truncated to ``test_x[a``. This is a live bug in
    ``coord-test-runner.sh`` today (see #1436). This implementation instead
    strips the leading ``FAILED `` marker and takes everything up to the
    first `` - `` (the separator pytest itself puts before the failure
    summary message), so a node id with an embedded space survives intact.

    Returns node ids deduped and sorted, matching ``sort -u``.
    """
    seen: set[str] = set()
    for line in output.splitlines():
        if not line.startswith(_FAILED_PREFIX):
            continue
        rest = line[len(_FAILED_PREFIX) :]
        node_id = rest.split(_FAILED_MESSAGE_SEP, 1)[0].strip()
        if node_id:
            seen.add(node_id)
    return sorted(seen)


# A collection/import error is never a flake — the suite could not even run,
# so re-running it in isolation reproduces the identical error. Matched
# structurally against pytest's own session-level markers rather than a bare
# "line starts with ERROR" substring check: a test whose *body* prints a
# string starting with "ERROR" (e.g. `print("ERROR: retrying request")`) ends
# up verbatim in "Captured stdout call" and would trip a naive check — see
# tests/fixtures/test_report/pytest_error_string_in_body.txt for a genuine
# repro. INTERNALERROR (pytest itself crashing, as opposed to a user test
# failing) always uses the "INTERNALERROR>" marker.
_PYTEST_COLLECTION_ERROR_PATTERNS = [
    re.compile(r"^INTERNALERROR>"),
    re.compile(r"^=+\s*ERRORS\s*=+\s*$"),
    re.compile(r"^!+\s*Interrupted:.*error.*!+\s*$"),
]


def pytest_has_collection_error(output: str) -> bool:
    """True if ``output`` shows a pytest collection/import error or an INTERNALERROR.

    Mirrors the shell's intent behind::

        grep -qE "^(ERROR|INTERNALERROR)"

    but checks pytest's own structural markers (the "=== ERRORS ===" section
    header, the "Interrupted: N error during collection" summary line, and
    "INTERNALERROR>") instead of a bare line-start substring match, so a test
    body printing "ERROR ..." to stdout does not false-positive.
    """
    lines = output.splitlines()
    return any(p.match(line) for line in lines for p in _PYTEST_COLLECTION_ERROR_PATTERNS)


# ── cargo ────────────────────────────────────────────────────────────────────

# cargo's `failures:` summary block lists bare test names, four-space
# indented, one per line — e.g. "    tests::module::test_name". The block
# appears twice per test *target* (once interleaved with per-test stdout
# dumps, once as the final list) and cargo prints one such pair PER TARGET
# when a run spans multiple test binaries (lib + integration tests, etc).
# The block is closed by the next "test result:" line, which resets
# collection so a second target's block cannot bleed into or concatenate
# with the first — see tests/fixtures/test_report/cargo_multi_target_failures.txt
# for a genuine two-target repro.
#
# Deliberate fix vs. the shell's `/^    [a-zA-Z_]+::/` pattern: that pattern
# REQUIRES a "::" after the leading identifier, matching module-qualified
# names like "tests::lib_another_failure" but silently dropping top-level
# test functions with no enclosing `mod` — which is exactly the shape of
# tui/tests/acceptance.rs's `#[test]` functions. A run where such a test
# fails would report an empty failure list (misclassified as "no parseable
# failure list", skipping flake-retry entirely) even though cargo named the
# test plainly. Matched here in cargo_multi_target_failures.txt by the bare
# "integration_genuine_failure" entry (no "::"), captured from a genuine
# `cargo test --no-fail-fast` run against a two-target project.
_CARGO_FAILURE_LINE_RE = re.compile(r"^    ([A-Za-z_][A-Za-z0-9_:]*)\s*$")


def cargo_failed_test_names(output: str) -> list[str]:
    """Extract unique failing test names from ``cargo test`` output.

    Mirrors the intent of the shell's::

        awk '/^failures:$/{f=1;next} /^test result:/{f=0}
             f && /^    [a-zA-Z_]+::/{print $1}' | sort -u

    with one deliberate fix: the awk pattern requires "::" in the name, so a
    top-level test with no enclosing module (see comment above) is silently
    dropped. This implementation matches any four-space-indented line that
    is *entirely* a test-name token (letters/digits/underscore/colon), with
    or without "::".

    Returns test names deduped and sorted, matching ``sort -u``.
    """
    seen: set[str] = set()
    collecting = False
    for line in output.splitlines():
        if line == "failures:":
            collecting = True
            continue
        if line.startswith("test result:"):
            collecting = False
            continue
        if collecting:
            m = _CARGO_FAILURE_LINE_RE.match(line)
            if m:
                seen.add(m.group(1))
    return sorted(seen)


# A compile error is never a flake. Deliberate fix vs. the shell's
# `^error(\[E[0-9]+\])?:|could not compile`: the bare `^error:` (no `[E..]`
# code) branch also matches cargo's own generic test-failure wrapper lines —
# "error: test failed, to rerun pass `--lib`" and "error: 2 targets failed:"
# — which cargo prints on ANY nonzero-exit test run, compile error or not.
# Genuinely captured: cargo_multi_target_failures.txt and
# cargo_no_parseable_failures.txt both contain "error: test failed, to rerun
# pass ..." despite having no compile error at all; the literal shell regex
# would misclassify every such run as a compile error, never reaching (or in
# the future, never flake-retrying) the actual failure list. "error[E####]:"
# (rustc's own coded diagnostics) and "could not compile" (cargo's own
# compile-failure summary line) are unambiguous; the bare "error:" prefix is
# not. Matched per line, matching grep's per-line semantics (the "^" anchor
# is start-of-line, not start-of-output).
_CARGO_COMPILE_ERROR_RE = re.compile(r"^error\[E\d+\]:|could not compile")


def cargo_has_compile_error(output: str) -> bool:
    """True if ``output`` shows a genuine rustc/cargo compile error.

    Mirrors the intent of the shell's::

        grep -qE "^error(\\[E[0-9]+\\])?:|could not compile"

    with one deliberate fix: the bare `^error:` alternative (no error code)
    is dropped because it also matches cargo's generic "error: test failed"
    / "error: N targets failed" wrapper lines, which appear on any test
    failure — not just compile errors. See the comment above for a genuine
    repro of the misclassification this caused.
    """
    return any(_CARGO_COMPILE_ERROR_RE.search(line) for line in output.splitlines())
