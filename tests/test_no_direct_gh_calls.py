"""Regression guard for #1483: `gh` must be invoked from exactly one place.

``coord/github_ops.py`` is the legitimate GitHub adapter -- "the single sink
that a GitLab backend would sit beside" (#1483). Several other modules used
to shell out to `gh` directly (``coord/review.py``, ``coord/drive.py``,
``coord/ci_github.py``, ``coord/test_orchestrator.py``,
``coord/commands/test_gate.py``), which:

- makes worker/daemon hosts implicitly depend on `gh` being on PATH for code
  paths that have nothing to do with choosing a forge backend (the elitebook
  incident: a worker silently lost GitHub access because `gh` lived at a
  linuxbrew path outside the systemd unit's PATH -- 98 failures burned
  diagnosing "environment", not the code);
- in ``coord/drive.py``, made behaviour silently depend on whether a `gh`
  binary happened to be on PATH (``shutil.which("gh")``), with no log line
  and no verdict;
- blocks a future GitLab / bare-DB backend from swapping in cleanly, since
  each leak is one more place that would need to learn about the new
  backend.

This test statically scans every non-adapter module under ``coord/`` for a
literal ``"gh"``/``'gh'`` used as a subprocess argv[0], or
``shutil.which("gh")``, and fails if either turns up outside
``coord/github_ops.py``. New `gh` call sites belong in ``github_ops.py``;
everything else should call through that seam.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
COORD_DIR = REPO_ROOT / "coord"

# The one legitimate `gh` sink -- new gh functionality belongs here, not
# scattered across the codebase.
ALLOWED_FILES = {COORD_DIR / "github_ops.py"}


def _iter_coord_py_files() -> list[Path]:
    return [
        path
        for path in sorted(COORD_DIR.rglob("*.py"))
        if path not in ALLOWED_FILES and "__pycache__" not in path.parts
    ]


def _is_gh_argv0(node: ast.AST) -> bool:
    """True if *node* is a list/tuple literal whose first element is the
    string literal ``"gh"`` -- the shape of ``subprocess.run(["gh", ...])`` /
    ``subprocess.Popen(["gh", ...])`` (under any import alias)."""
    if not isinstance(node, (ast.List, ast.Tuple)) or not node.elts:
        return False
    first = node.elts[0]
    return isinstance(first, ast.Constant) and first.value == "gh"


def _is_shutil_which_gh(node: ast.Call) -> bool:
    """True for ``shutil.which("gh")`` / ``which("gh")`` -- the silent,
    host-dependent-behaviour shape #1483 called out explicitly in
    ``coord/drive.py``."""
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else (
        func.id if isinstance(func, ast.Name) else None
    )
    if name != "which":
        return False
    return any(
        isinstance(arg, ast.Constant) and arg.value == "gh" for arg in node.args
    )


def _find_violations(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    violations = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if _is_shutil_which_gh(node):
            violations.append(f'{path}:{node.lineno}: shutil.which("gh")')
            continue
        for arg in node.args:
            if _is_gh_argv0(arg):
                violations.append(f'{path}:{node.lineno}: subprocess argv0 "gh" literal')
                break
    return violations


def test_no_direct_gh_invocations_outside_github_ops() -> None:
    all_violations: list[str] = []
    for path in _iter_coord_py_files():
        all_violations.extend(_find_violations(path))
    assert not all_violations, (
        "Direct `gh` invocation(s) found outside coord/github_ops.py -- "
        "route through the github_ops seam instead (#1483):\n" + "\n".join(all_violations)
    )


def test_guard_actually_detects_a_leak(tmp_path) -> None:
    """Prove the AST scan isn't a no-op -- planting a leak must trip it."""
    leak = tmp_path / "leaky.py"
    leak.write_text(
        "import subprocess\n"
        "def f():\n"
        "    subprocess.run(['gh', 'pr', 'view', '1'])\n"
    )
    assert _find_violations(leak)

    which_leak = tmp_path / "leaky_which.py"
    which_leak.write_text("import shutil\nshutil.which('gh')\n")
    assert _find_violations(which_leak)
