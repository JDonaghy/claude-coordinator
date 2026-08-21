#!/usr/bin/env python3
"""check_gh_argv_containment.py -- CI guard for #2135 (child of epic #1902).

The epic's headline finding (#1902) is that **every `["gh", ...]` argv
construction in this repo already lives inside `coord/github_ops.py`** --
that single fact is what makes the eventual forge-portability port a
*refactor* instead of a rewrite, and it is the load-bearing assumption
under the whole #1902 cost estimate. Until this script, that invariant was
protected by nothing but convention: whoever last thought to grep for it.

Same spirit as `scripts/run_tests_without_gh.sh` (#1484, the `no-gh-on-path`
CI job) -- that script proved `gh` calls are *mockable* by making the
binary unresolvable and watching the suite still pass; this script proves
they are *contained* by refusing any `gh` argv construction that lives
outside the one file that owns them. Deliberately kept a plain regex scan,
not an AST walk -- cheap enough to run on every PR, and the invariant it
guards ("does this file spell the literal string `gh` as its own quoted
token") does not need a parser to detect.

Supersedes `tests/test_no_direct_gh_calls.py` (#1483's original AST-based
guard for this same invariant, added when only `github_ops.py`'s
call-site shape needed catching). #2135 review flagged that the two
guards were an undocumented split-brain: the AST walk only fires when a
`["gh", ...]` literal is a *direct call argument*
(`subprocess.run(["gh", ...])`), so it silently missed the
variable-indirection shape (`cmd = ["gh", ...]; subprocess.run(cmd)`) that
this regex scan does catch, since it matches the token wherever it sits on
the line. Rather than keep two independently-maintained implementations of
one invariant with two different blind spots, `test_no_direct_gh_calls.py`
was deleted and this script is now the single implementation, exercised
both by the dedicated `gh-argv-containment` CI job (`.github/workflows/
test.yml`) and by the ordinary `pytest tests/` run, via
`tests/test_check_gh_argv_containment.py::test_real_tree_is_clean` and
friends -- one scan, reachable both ways, per the repo's "one question,
one answer" rule (epic #2096). The trade-off this leaves: being a plain
token scan rather than an AST walk, it can false-positive on a `"gh"`
token that isn't argv (e.g. inside a docstring or an unrelated string
literal) -- accepted deliberately, since a false positive costs an
allowlist entry or a reword, while a false negative (the AST gap above)
costs a silent regression of the epic's #1902 load-bearing assumption.

Usage:
    python scripts/check_gh_argv_containment.py [repo-root]

Exit codes: 0 if the tree is clean, 1 if a `gh` argv construction is found
in coord/**/*.py outside coord/github_ops.py.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

SEAM = "coord/github_ops.py"

# Matches a bare `"gh"` or `'gh'` token -- the exact spelling every gh argv
# construction in this repo starts with, whether written `["gh", ...]`,
# `("gh", ...)`, or (as in github_ops.py's PR-checks call, where the list is
# opened on the previous line) as a bare leading element `"gh", "pr", ...,`.
# The closing quote must land immediately after the 'h', so this does not
# match longer identifiers that merely contain "gh", like "github" or
# "ghost".
_GH_TOKEN_RE = re.compile(r"""(["'])gh\1""")

# Explicit allowlist: (file, substring-of-the-matched-line) pairs for
# known-legitimate `gh` mentions outside the seam. Keyed on line CONTENT,
# not a line number -- numbers drift as a file changes (this very entry's
# line moved from prereqs.py:288, where #2135 was filed, to prereqs.py:290
# by the time this script was written), and a number-keyed allowlist would
# either miss a real leak that later lands on that number, or stop
# exempting the intended line the moment it shifts.
#
# Add an entry here ONLY with a comment explaining why the mention is not
# an argv construction -- a loose pattern (e.g. "any line in prereqs.py")
# would let a real leak through under cover of the exemption, which is
# exactly the failure mode this script exists to prevent.
_ALLOWLIST: tuple[tuple[str, str], ...] = (
    (
        "coord/prereqs.py",
        'tool="gh", binary="gh"',
    ),
    # ^ Prereq(tool="gh", binary="gh", version_args=("--version",), ...)
    # probes for the `gh` BINARY's presence/version via shutil.which +
    # `gh --version` -- it does not drive the forge, so it correctly lives
    # outside the seam (#2135).
)


@dataclass(frozen=True)
class Violation:
    path: str
    lineno: int
    line: str


def _is_allowlisted(rel_path: str, line: str) -> bool:
    return any(
        rel_path == allow_path and allow_substr in line
        for allow_path, allow_substr in _ALLOWLIST
    )


def find_violations(repo_root: Path) -> list[Violation]:
    """Scan coord/**/*.py for `gh` argv construction outside the seam.

    Pure function of the filesystem under ``repo_root`` -- no subprocess,
    no network -- so it is cheap to point at a synthetic fixture tree in
    tests, not just the real repo.
    """
    violations: list[Violation] = []
    coord_dir = repo_root / "coord"
    if not coord_dir.is_dir():
        return violations
    for path in sorted(coord_dir.rglob("*.py")):
        rel = path.relative_to(repo_root).as_posix()
        if rel == SEAM:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError) as exc:
            # Every file under coord/ is expected to be UTF-8 Python
            # source, so this should never actually fire -- but if it
            # ever does, skipping silently would mean a real `gh` leak in
            # that file goes undetected with no trace. Surface it on
            # stderr rather than swallow it (#2135 review).
            print(
                f"check_gh_argv_containment: skipping {rel} "
                f"(unreadable: {exc}) -- unable to scan for `gh` argv "
                f"construction in this file",
                file=sys.stderr,
            )
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _GH_TOKEN_RE.search(line) and not _is_allowlisted(rel, line):
                violations.append(Violation(rel, lineno, line.strip()))
    return violations


def format_report(violations: list[Violation]) -> str:
    lines = [
        f"check_gh_argv_containment: found `gh` argv construction outside "
        f"{SEAM}:",
        "",
    ]
    for v in violations:
        lines.append(f"  {v.path}:{v.lineno}: {v.line}")
    lines += [
        "",
        "Every `gh` invocation must go through coord/github_ops.py -- that "
        "chokepoint is the load-bearing assumption behind the #1902 "
        "forge-portability epic (it's what makes the eventual port a "
        "refactor instead of a rewrite), and it's what makes `gh` mockable "
        "for tests (the no-gh-on-path CI job, #1484, depends on it too).",
        "",
        'Fix: move this call into coord/github_ops.py. If it is a genuine '
        'non-forge use of the literal string "gh" (like the binary-version '
        "probe in coord/prereqs.py), add it to the ALLOWLIST in "
        "scripts/check_gh_argv_containment.py with a comment explaining "
        "why -- see #2135.",
    ]
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    repo_root = Path(argv[1]) if len(argv) > 1 else Path(".")
    violations = find_violations(repo_root.resolve())
    if not violations:
        return 0
    print(format_report(violations), file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
