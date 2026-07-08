"""#615/#906 regression guard: no *new* direct local-board call site may land
in ``coord/commands/*.py`` or the core coord modules below without either
(a) routing through the daemon first, or (b) being added to the matching
``ALLOWLIST`` below with a one-line justification.

## Background

``coord/commands/*.py`` (the post-#747 split of the old ``cli.py``) used to
have ~30 call sites that read/wrote the board via
``coord.state.build_board()`` / ``save_board()`` / ``load_board()``, which hit
the local SQLite DB directly.  On a thin client (no daemon-backed local DB)
those commands silently operated on an empty board (#584/#615).

Prior fixes (#590, #609, #611, #614, #747, #749, #779, #821, #905) migrated
every *reachable* call site to route through ``coord.board_service`` /
``daemon_reroute_target()`` / ``resolve_board_service()`` first.

**#906** widened the scan:

* ``BOARD_LOCAL_FUNCS`` now includes *every* non-routed board reader/writer in
  ``coord.state``: the original three (``build_board`` / ``save_board`` /
  ``load_board``) plus the newly-guarded helpers (``mark_notified``,
  ``save_plan``, ``load_dispatched``). ``get_issue_test_mode`` itself is now
  daemon-routed (mirrors ``get_test_plan``: routes to ``POST
  /issue-test-mode`` when ``board_service`` is configured, falls back to the
  private ``_get_issue_test_mode_local`` otherwise) after a review caught it
  being reachable from a thin client via ``coord resume`` -> ``reconcile()``
  — so it's no longer tracked as an unrouted local function here.
* The **second test** (``test_no_unallowlisted_board_calls_in_core_modules``)
  scans the wider set of core modules beyond ``coord/commands/`` for the same
  ``BOARD_LOCAL_FUNCS`` *plus* raw ``get_connection()`` calls — the one
  escape hatch that bypasses all state-layer guards.

Both tests follow the same "fail loud on NEW additions" policy: if you add a
new call site, you must either route it through the daemon or add it to the
module-specific ``ALLOWLIST`` below with a one-line reason.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
COMMANDS_DIR = REPO_ROOT / "coord" / "commands"
COORD_DIR = REPO_ROOT / "coord"

# ── #615-era board-persistence helpers ────────────────────────────────────────
BOARD_FUNCS_ORIGINAL = {"build_board", "save_board", "load_board"}

# ── #906 additions: non-routed board readers/writers added to state.py guards ─
# NOTE: get_issue_test_mode is NOT here — it's daemon-routed (like
# get_test_plan), not merely guarded, after the #906 review found it
# reachable from a thin client via `coord resume` -> reconcile().
BOARD_FUNCS_EXTENDED = {
    "mark_notified",       # local notifications + assignments write; guarded
    "save_plan",           # local plans write; guarded
    "load_dispatched",     # local assignments read; guarded
}

# All board-local function names tracked by both tests.
BOARD_LOCAL_FUNCS = BOARD_FUNCS_ORIGINAL | BOARD_FUNCS_EXTENDED

# Raw DB escape hatch — tracked in the extended-modules test.
GET_CONNECTION = {"get_connection"}


def _find_calls(
    path: Path,
    canonical_names: set[str],
    *,
    source_modules: frozenset[str] = frozenset({"coord.state"}),
) -> set[tuple[str, str]]:
    """Return ``{(enclosing_function_name, canonical_call_name)}`` for every
    direct call to any name in *canonical_names* in *path*, resolved through
    any ``from <module> import X as Y`` alias where <module> is in
    *source_modules*.
    """
    tree = ast.parse(path.read_text(), filename=str(path))

    # local (possibly aliased) name -> canonical function name.
    alias_map: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in source_modules:
            for alias in node.names:
                if alias.name in canonical_names:
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


# ══════════════════════════════════════════════════════════════════════════════
# Test 1: coord/commands/*.py — scans BOARD_LOCAL_FUNCS only
# ══════════════════════════════════════════════════════════════════════════════

# Every direct BOARD_LOCAL_FUNCS call left in coord/commands/*.py, and why
# it's safe.  Keyed by filename (relative to coord/commands/).
COMMANDS_ALLOWLIST: dict[str, set[tuple[str, str]]] = {
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
    # #590-routed: build_board + load_dispatched are both in the `else:` branch
    # of `svc = resolve_board_service(); if svc is not None: ...daemon path...
    # else: ...local path...`.  On a thin client `svc` is not None, so neither
    # call is reached; `report_result` routes to the daemon's board payload.
    "review.py": {("report_result", "build_board"), ("report_result", "load_dispatched")},
    # #762-routed: `diagnose`'s body already routed via `daemon_reroute_target`
    # above; this build_board() is the deliberate host-local read for the
    # already-routed body — see the "NOTE: deliberately NO save_board here"
    # comment a few lines below the call.
    # `status` uses load_dispatched() only inside `if notified:` where
    # `notified = {} if svc else load_notified()` — so on a thin client
    # `notified` is `{}` and load_dispatched() is never executed.
    "status.py": {("diagnose", "build_board"), ("status", "load_dispatched")},
    # `log` uses load_dispatched() as a fast-path local lookup; when the local
    # ledger is empty (thin client), the record is None and the function falls
    # through to `_resolve_log_machine_via_daemon()` (#851) — so thin clients
    # degrade gracefully to the daemon lookup, not a hard error.
    # `wait` + `watch` use load_dispatched() to find which machine the
    # assignment is on; on a thin client the local ledger is empty and these
    # commands error "not found" — a known gap, tracked as a follow-up
    # (needs a daemon-board fallback analogous to _resolve_log_machine_via_daemon).
    "sessions.py": {
        ("log", "load_dispatched"),
        ("wait", "load_dispatched"),
        ("watch", "load_dispatched"),
    },
    # #590/#749: informational-only local peek, gated behind
    # `if not is_remote():` — used only to print "no saved board" vs
    # "rebuilding" before the real (daemon-aware) `read_board()` call.
    "lifecycle.py": {("resume", "load_board")},
    # #590/#749: each of the five human-attended `--interactive` dispatch
    # flavors only calls the local build_board/save_board pair behind an
    # explicit `if svc is None:` guard — `record_dispatched_assignment()`
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
    """Guard: no new BOARD_LOCAL_FUNCS call site may land in coord/commands/*.py
    without routing through the daemon or being added to COMMANDS_ALLOWLIST."""
    actual: dict[str, set[tuple[str, str]]] = {}
    for path in sorted(COMMANDS_DIR.glob("*.py")):
        calls = _find_calls(path, BOARD_LOCAL_FUNCS)
        if calls:
            actual[path.name] = calls

    expected = {k: v for k, v in COMMANDS_ALLOWLIST.items() if v}

    assert actual == expected, (
        "coord/commands/*.py's direct BOARD_LOCAL_FUNCS call sites changed "
        "since this test's COMMANDS_ALLOWLIST was written.\n\n"
        "If you ADDED a new direct call site: it must be routed through the "
        "daemon first (mirror `coord merge`'s daemon_reroute_target() / "
        "board_service.route_write() pattern, #615/#906) — do not call "
        "coord.state.build_board/save_board/load_board/mark_notified/save_plan/"
        "load_dispatched unconditionally from a CLI command. "
        "If it's already safely guarded (e.g. behind an `if svc is None:` / "
        "`if not is_remote():` check, or only reached after a daemon-routing "
        "early-return), add it to COMMANDS_ALLOWLIST with a one-line reason.\n"
        "If you REMOVED or renamed one: delete/update its COMMANDS_ALLOWLIST entry.\n\n"
        f"expected: {expected}\n"
        f"actual:   {actual}"
    )


# ══════════════════════════════════════════════════════════════════════════════
# Test 2: extended core modules — scans BOARD_LOCAL_FUNCS + get_connection
# ══════════════════════════════════════════════════════════════════════════════

# Modules that can reach thin-client code paths and should never add new
# unguarded local-board reads/writes.
EXTENDED_MODULE_PATHS: list[Path] = [
    COORD_DIR / "notify.py",
    COORD_DIR / "reconcile.py",
    COORD_DIR / "issue_store.py",
    COORD_DIR / "auto_loop.py",
    COORD_DIR / "merge_queue.py",
    COORD_DIR / "interactive.py",
    COORD_DIR / "dispatch.py",
]

# Combined set tracked for extended modules.
EXTENDED_TRACKED = BOARD_LOCAL_FUNCS | GET_CONNECTION

# All source modules from which the tracked names can be imported.
EXTENDED_SOURCE_MODULES = frozenset({"coord.state", "coord.db"})

# Per-file allowlist for the extended module scan.
EXTENDED_ALLOWLIST: dict[str, set[tuple[str, str]]] = {
    # coord/notify.py — all board-local calls covered by the
    # COORD_NOTIFY_ON_DAEMON whole-command reroute (#906): on a thin client
    # `coord notify` POSTs to /notify and the daemon runs the whole function
    # against the canonical DB.
    "notify.py": {
        # load_dispatched: two callers; both are inside notify.run() which is
        # rerouted to the daemon on thin clients.
        ("detect_transitions", "load_dispatched"),
        ("detect_stuck", "load_dispatched"),
        # #846: detect_needs_attention mirrors detect_stuck exactly — same
        # load_dispatched() scan, same call site (inside notify.run()).
        ("detect_needs_attention", "load_dispatched"),
        # mark_notified: called from post_transition / post_stuck /
        # post_orphaned_review_findings — all inside notify.run() → daemon.
        ("post_transition", "mark_notified"),
        ("post_stuck", "mark_notified"),
        # #846: post_needs_attention mirrors post_stuck exactly — same
        # call site (inside notify.run()).
        ("post_needs_attention", "mark_notified"),
        ("post_orphaned_review_findings", "mark_notified"),
        # save_plan: called from _try_parse_and_post_plan (inside
        # post_transition) → daemon via COORD_NOTIFY_ON_DAEMON.
        ("_try_parse_and_post_plan", "save_plan"),
        # _persist_review_verdict: raw get_connection() backstop that writes
        # review_verdict directly.  Pre-dates update_assignment_review_findings
        # routing; safe because notify.run() is daemon-rerouted (#906).
        # TODO: migrate to update_assignment_review_findings (no raw DB call).
        ("_persist_review_verdict", "get_connection"),
    },
    # coord/reconcile.py — all board-local calls run from daemon-only paths:
    #   - reconcile_completed_assignments → only called from serve_app
    #     _passive_tick (daemon tick); never from a thin-client CLI command.
    #   - reconcile_board_merges → called from `coord reconcile-merges`
    #     (COORD_RECONCILE_ON_DAEMON rerouted) or daemon tick.
    "reconcile.py": {
        # build_board in reconcile_completed_assignments: daemon tick only.
        ("reconcile_completed_assignments", "build_board"),
        # save_plan in _capture_plan_best_effort: daemon tick only.
        ("_capture_plan_best_effort", "save_plan"),
        # NOTE: reconcile() calls get_issue_test_mode(), but that function is
        # now daemon-routed itself (not in BOARD_LOCAL_FUNCS) after the #906
        # review found reconcile() runs from the thin-client-reachable
        # `coord resume`, not just the daemon tick — so no entry is needed here.
    },
    # coord/merge_queue.py — board-local and DB calls; merge queue is a
    # separate concern (its own table); all callers are daemon-side or behind
    # COORD_MERGE_ON_DAEMON reroute.
    "merge_queue.py": {
        # build_board in enqueue_approved_work: called from daemon tick or from
        # `coord merge` (COORD_MERGE_ON_DAEMON rerouted).
        ("enqueue_approved_work", "build_board"),
        # Raw get_connection calls for the merge-queue table (_mq_* rows), not
        # the board/assignments tables — separate storage concern.  All callers
        # (plan/save_queue/load_queue/drop_entry) run on the daemon side.
        ("load_queue", "get_connection"),
        ("save_queue", "get_connection"),
        ("drop_entry", "get_connection"),
        ("_load_milestones_for_queue", "get_connection"),
    },
    # coord/issue_store.py — raw get_connection calls for the issue-store
    # seam; these write notifications/results into the DB and are called from
    # the agent's completion posting path, NOT from thin-client CLI commands.
    # The seam routes through /result and /completion endpoints on thin clients
    # (#590); the _local suffix functions are only reached on the daemon.
    "issue_store.py": {
        ("_update_local_state", "get_connection"),
        ("_assignment_type_local", "get_connection"),
        ("_record_notification", "get_connection"),
        # #990: the verdict write was factored out of _post_result_local into
        # these two named helpers (retry + readback-verify so a silent no-op
        # under SQLite lock contention can't masquerade as success) — same
        # local-DB-only seam, no new daemon-bypass.
        ("_read_review_verdict_local", "get_connection"),
        ("_persist_review_verdict", "get_connection"),
        # #886 Phase 2: Milestone Outcome Audit structured verdict — same
        # local-DB-only seam as the #990 pair above (retry + readback-verify
        # write, plus the read helpers that support it and the diff).
        ("get_audit_runs_for_epic", "get_connection"),
        ("_read_audit_run_local", "get_connection"),
        ("_persist_audit_result", "get_connection"),
    },
    # coord/interactive.py — raw get_connection calls for session/assignment
    # management (reading status, marking stale rows terminal).  These are
    # intrinsically local — they run against the local agent's own DB — so
    # routing them through the daemon would be wrong.
    "interactive.py": {
        ("_assignment_status", "get_connection"),
        ("reap_stale_interactive_sessions", "get_connection"),
        ("_mark_stale_reap_in_db", "get_connection"),
    },
}


def test_no_unallowlisted_board_calls_in_core_modules() -> None:
    """Guard: no new BOARD_LOCAL_FUNCS or raw get_connection() call site may
    land in the core coord modules without routing or an EXTENDED_ALLOWLIST
    entry with a justification."""
    actual: dict[str, set[tuple[str, str]]] = {}
    for path in EXTENDED_MODULE_PATHS:
        if not path.exists():
            continue
        calls = _find_calls(path, EXTENDED_TRACKED, source_modules=EXTENDED_SOURCE_MODULES)
        if calls:
            actual[path.name] = calls

    expected = {k: v for k, v in EXTENDED_ALLOWLIST.items() if v}

    assert actual == expected, (
        "Core coord modules' direct BOARD_LOCAL_FUNCS / get_connection() "
        "call sites changed since EXTENDED_ALLOWLIST was written.\n\n"
        "If you ADDED a new direct call: it must be routed through the daemon "
        "first (via board_service.route_write() or a whole-command reroute like "
        "COORD_NOTIFY_ON_DAEMON), OR added to EXTENDED_ALLOWLIST with a one-line "
        "justification explaining why it's safe.\n"
        "If you REMOVED or renamed one: delete/update its EXTENDED_ALLOWLIST entry.\n\n"
        f"expected: {expected}\n"
        f"actual:   {actual}"
    )
