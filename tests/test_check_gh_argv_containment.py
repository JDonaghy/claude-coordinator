"""#2135 (child of epic #1902): CI guard for the gh-argv containment
invariant -- "every `gh` argv construction lives inside
coord/github_ops.py".

This is build tooling with no user-visible behavior, so it's exempt from
the black-box acceptance bar (CLAUDE.md's "Testing" section) -- but per
that same section's note on this issue, a guard that cannot fail is worse
than no guard, so this file's job is proving the negative case: a
deliberately-introduced `gh` argv construction outside the seam actually
trips scripts/check_gh_argv_containment.py.
"""

from __future__ import annotations

from pathlib import Path

from scripts.check_gh_argv_containment import find_violations, format_report

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_real_tree_is_clean() -> None:
    """The acceptance bar from #2135: the current tree passes today, with
    coord/prereqs.py exempted by explicit allowlist rather than by
    accident (i.e. its exemption is on record, not just an absence of
    other violations)."""
    violations = find_violations(REPO_ROOT)
    assert violations == [], format_report(violations)


def test_prereqs_binary_probe_is_allowlisted_not_absent() -> None:
    """Guard against the allowlist silently becoming dead code: prove
    coord/prereqs.py actually contains a `gh` token that the scan visits
    and exempts, not that the scanner simply never looks at that file."""
    prereqs = REPO_ROOT / "coord" / "prereqs.py"
    text = prereqs.read_text(encoding="utf-8")
    assert 'tool="gh", binary="gh"' in text, (
        "coord/prereqs.py's Prereq(tool=\"gh\", binary=\"gh\", ...) moved or "
        "was rewritten -- update the ALLOWLIST entry (and this test) to "
        "match its new shape, per #2135."
    )


def test_deliberate_leak_outside_the_seam_is_caught(tmp_path: Path) -> None:
    """The acceptance criterion from #2135: a deliberately-added
    `subprocess.run(["gh", "pr", "view", "1"])` in any coord/*.py other
    than github_ops.py fails the check."""
    coord_dir = tmp_path / "coord"
    coord_dir.mkdir()
    (coord_dir / "github_ops.py").write_text(
        'import subprocess\n'
        'subprocess.run(["gh", "pr", "view", "1"])\n',
        encoding="utf-8",
    )
    leaky = coord_dir / "review.py"
    leaky.write_text(
        'import subprocess\n'
        '\n'
        'def peek(number):\n'
        '    subprocess.run(["gh", "pr", "view", str(number)])\n',
        encoding="utf-8",
    )

    violations = find_violations(tmp_path)

    assert len(violations) == 1
    v = violations[0]
    assert v.path == "coord/review.py"
    assert v.lineno == 4
    assert '"gh"' in v.line

    report = format_report(violations)
    assert "coord/review.py:4" in report
    assert "coord/github_ops.py" in report


def test_single_and_double_quote_forms_are_both_caught(tmp_path: Path) -> None:
    """Requirement from #2135: detect both quote styles."""
    coord_dir = tmp_path / "coord"
    coord_dir.mkdir()
    (coord_dir / "github_ops.py").write_text("# the seam\n", encoding="utf-8")
    (coord_dir / "leaky_double.py").write_text(
        'CMD = ["gh", "pr", "list"]\n', encoding="utf-8"
    )
    (coord_dir / "leaky_single.py").write_text(
        "CMD = ('gh', 'pr', 'list')\n", encoding="utf-8"
    )

    violations = find_violations(tmp_path)

    flagged = {v.path for v in violations}
    assert flagged == {"coord/leaky_double.py", "coord/leaky_single.py"}


def test_explicit_allowlist_entry_is_exempted(tmp_path: Path) -> None:
    """The allowlist is content-keyed, not a blanket per-file exemption:
    the exact allowlisted line is spared, but the file is otherwise still
    scanned."""
    coord_dir = tmp_path / "coord"
    coord_dir.mkdir()
    (coord_dir / "github_ops.py").write_text("# the seam\n", encoding="utf-8")
    (coord_dir / "prereqs.py").write_text(
        'Prereq(\n'
        '    tool="gh", binary="gh", version_args=("--version",),\n'
        ')\n',
        encoding="utf-8",
    )

    violations = find_violations(tmp_path)

    assert violations == []


def test_allowlist_does_not_blanket_exempt_other_gh_leaks_in_same_file(
    tmp_path: Path,
) -> None:
    """A loose allowlist ("any line in prereqs.py") would let a real leak
    through under cover of the exemption -- exactly the failure mode
    #2135 calls out. This pins the allowlist to line CONTENT, so an
    unrelated `gh` argv construction added to the same file is still
    caught."""
    coord_dir = tmp_path / "coord"
    coord_dir.mkdir()
    (coord_dir / "github_ops.py").write_text("# the seam\n", encoding="utf-8")
    (coord_dir / "prereqs.py").write_text(
        'Prereq(\n'
        '    tool="gh", binary="gh", version_args=("--version",),\n'
        ')\n'
        '\n'
        'import subprocess\n'
        'subprocess.run(["gh", "pr", "view", "1"])\n',
        encoding="utf-8",
    )

    violations = find_violations(tmp_path)

    assert len(violations) == 1
    assert violations[0].path == "coord/prereqs.py"
    assert violations[0].lineno == 6


def test_does_not_flag_words_that_merely_contain_gh(tmp_path: Path) -> None:
    """`"github"`, `"ghost"`, etc. must not false-positive -- the closing
    quote has to land immediately after the 'h'."""
    coord_dir = tmp_path / "coord"
    coord_dir.mkdir()
    (coord_dir / "github_ops.py").write_text("# the seam\n", encoding="utf-8")
    (coord_dir / "fine.py").write_text(
        'HOST = "github.com"\n'
        'NAME = "ghost"\n',
        encoding="utf-8",
    )

    violations = find_violations(tmp_path)

    assert violations == []
