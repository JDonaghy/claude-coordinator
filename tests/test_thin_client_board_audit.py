"""#615 regression guard: no *new* direct ``coord.state.build_board`` /
``save_board`` / ``load_board`` call site may land in ``coord/commands/*.py``
without either (a) routing through the daemon first, or (b) being added to
the ``ALLOWLIST`` below with a one-line justification.

Background: ``coord/commands/*.py`` (the post-#747 split of the old
``cli.py``) used to have ~30 call sites that read/wrote the board via
``coord.state.build_board()`` / ``save_board()`` / ``load_board()``, which hit
the local SQLite DB directly. On a thin client (no daemon-backed local DB)
those commands silently operated on an empty board (#584/#615). Prior fixes
(#590, #609, #611, #614, #747, #749, #779, #821) migrated every *reachable*
call site to route through ``coord.board_service`` /
``daemon_reroute_target()`` / ``resolve_board_service()`` first, calling the
local functions only from the already-routed host branch (or behind an
explicit ``if svc is None:`` / ``if not is_remote():`` guard). The single
remaining unguarded, unrouted call site (``test_cmd``, dead code shadowed by
the `test` command — see ``coord/commands/test_gate.py``'s module docstring)
was deleted rather than migrated, since routing dead code would be pure
ceremony.

This test statically scans ``coord/commands/*.py`` for direct calls to
``build_board``/``save_board``/``load_board`` (following any
``from coord.state import X as Y`` alias) and asserts the exact set found
matches ``ALLOWLIST`` — so a future PR that reintroduces an unguarded call
fails CI immediately instead of silently reintroducing the #584/#615
thin-client bug.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
COMMANDS_DIR = REPO_ROOT / "coord" / "commands"

CANONICAL_NAMES = {"build_board", "save_board", "load_board"}


def _find_state_board_calls(path: Path) -> set[tuple[str, str]]:
    """Return ``{(enclosing_function_name, canonical_call_name)}`` for every
    direct call to ``coord.state.build_board``/``save_board``/``load_board``
    in *path*, resolved through any ``from coord.state import X as Y`` alias.
    """
    tree = ast.parse(path.read_text(), filename=str(path))

    # local (possibly aliased) name -> canonical coord.state function name.
    alias_map: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "coord.state":
            for alias in node.names:
                if alias.name in CANONICAL_NAMES:
                    alias_map[alias.asname or alias.name] = alias.name

    if not alias_map:
        return set()

    funcs = [
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]

    def _enclosing_function(lineno: int) -> str:
        best = None
        for f in funcs:
            if f.lineno <= lineno <= (f.end_lineno or f.lineno):
                if best is None or f.lineno > best.lineno:
                    best = f
        return best.name if best else "<module>"

    found: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            canonical = alias_map.get(node.func.id)
            if canonical is not None:
                found.add((_enclosing_function(node.lineno), canonical))
    return found


# Every direct coord.state.build_board()/save_board()/load_board() call left
# in coord/commands/*.py, and why it's safe. Keyed by filename (relative to
# coord/commands/).
ALLOWLIST: dict[str, set[tuple[str, str]]] = {
    # #584-routed: `reconcile_merges` and `merge` only reach these calls after
    # `daemon_reroute_target()` returns None (i.e. we ARE the daemon, or no
    # daemon is configured) — see the early `if _svc is not None: ...; return`
    # guard above each of these bodies.
    "merge.py": {
        ("reconcile_merges", "build_board"),
        ("reconcile_merges", "save_board"),
        ("merge", "load_board"),
        ("merge", "save_board"),
    },
    # #590-routed: local board only used in the `else` branch of
    # `if svc is not None: record_test_verdict(...) else: save_board(board)`.
    "test_gate.py": {("test", "save_board")},
    # #590-routed: local board only used in the `else` branch when
    # `resolve_board_service()` returned None (falls back to the dispatched
    # ledger + config-repo lookup instead of the daemon's board payload).
    "review.py": {("report_result", "build_board")},
    # #762-routed: `diagnose`'s body already routed via `daemon_reroute_target`
    # above; this build_board() is the deliberate host-local read for the
    # already-routed body — see the "NOTE: deliberately NO save_board here"
    # comment a few lines below the call.
    "status.py": {("diagnose", "build_board")},
    # #590/#749: informational-only local peek, gated behind
    # `if not is_remote():` — used only to print "no saved board" vs
    # "rebuilding" before the real (daemon-aware) `read_board()` call.
    "lifecycle.py": {("resume", "load_board")},
    # #590/#749: each of the five human-attended `--interactive` dispatch
    # flavors only calls the local build_board/save_board pair behind an
    # explicit `if _svc is None:` guard — `record_dispatched_assignment()`
    # already routed the assignment row to the daemon when one is configured,
    # so this is the "no daemon configured / standalone dev" path.
    "dispatch_workers.py": {
        ("_dispatch_review_of", "build_board"),
        ("_dispatch_review_of", "save_board"),
        ("_dispatch_smoke_of", "build_board"),
        ("_dispatch_smoke_of", "save_board"),
        ("_dispatch_fix_of", "build_board"),
        ("_dispatch_fix_of", "save_board"),
        ("_dispatch_rework_of", "build_board"),
        ("_dispatch_rework_of", "save_board"),
        ("_dispatch_merge_of", "build_board"),
        ("_dispatch_merge_of", "save_board"),
    },
}


def test_no_unallowlisted_direct_board_calls_in_commands() -> None:
    actual: dict[str, set[tuple[str, str]]] = {}
    for path in sorted(COMMANDS_DIR.glob("*.py")):
        calls = _find_state_board_calls(path)
        if calls:
            actual[path.name] = calls

    expected = {k: v for k, v in ALLOWLIST.items() if v}

    assert actual == expected, (
        "coord/commands/*.py's direct build_board()/save_board()/load_board() "
        "call sites changed since this test's ALLOWLIST was written.\n\n"
        "If you ADDED a new direct call site: it must be routed through the "
        "daemon first (mirror `coord merge`'s "
        "daemon_reroute_target()/resolve_board_service() pattern, #615) — do "
        "not call coord.state.build_board/save_board/load_board unconditionally "
        "from a CLI command. If it's already safely guarded (e.g. behind an "
        "`if svc is None:`/`if not is_remote():` check, or only reached after "
        "a daemon-routing early-return), add it to ALLOWLIST above with a "
        "one-line reason.\n"
        "If you REMOVED or renamed one: delete/update its ALLOWLIST entry.\n\n"
        f"expected: {expected}\n"
        f"actual:   {actual}"
    )
