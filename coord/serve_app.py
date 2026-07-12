"""``coord serve`` — the portable control center daemon (#584/#589/#594).

A lean, **read-only** Starlette app that fronts the coordinator board so any
Tailscale-reachable machine can render the same live board without a local
``~/.coord/coord.db`` or ``coordinator.yml``.

It mirrors the agent server (``coord/agent_app.py``, port 7433) and the dashboard
(``coord/dashboard/server.py``, port 7434); this daemon listens on **7435**.

Endpoints:

* ``GET /healthz``  — liveness; no DB access, never auth-gated.
* ``GET /board``    — the full board projection (``CoordStore.board_projection``).
* ``GET /audit``    — paginated, newest-first read over the append-only
  ``audit_log`` (#1037); keyset cursor, not part of ``/board``.
* ``GET /config``   — the raw ``coordinator.yml`` bytes the daemon owns, so a
  client needs no local config file.
* ``POST /result``  — record an interactive-session result (#590); body is a
  serialized ``issue_store.ResultRecord``. Re-invokes the seam against the
  shared DB so a remote ``coord report-result`` lands here.
* ``POST /completion`` — record a git-floor backstop completion (#590); body is
  a serialized ``issue_store.CompletionRecord``.

The write endpoints call ``issue_store._post_*_local`` directly (never the
routing wrapper), so the daemon writes its own DB and can never recurse back out
over HTTP.

Auth: optional shared bearer token (defence-in-depth on top of Tailscale ACLs).
When no token is configured the endpoints are open (matching the agent/dashboard
servers, which have no auth). Per-user auth is #282 / team-mode territory.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import asdict, fields
from pathlib import Path

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from coord import __version__
from coord.config import Config
from coord.dao import _DROP_COLUMNS, _JSON_COLUMNS, SCHEMA_VERSION, CoordStore
from coord.db import _ensure_schema
from coord.openapi import build_spec, dataclass_schema, openapi_and_docs_routes, sqlite_table_schema

# Default port for the coordination daemon (agent=7433, dashboard=7434).
SERVE_PORT = 7435

# Server-side bearer token sources, in precedence order.  Distinct from the
# *client's* ``COORD_TOKEN`` so the two never collide on a box that runs both.
# The file source is what a systemd unit uses (an ``EnvironmentFile`` or a
# command-line ``--token`` would leak the secret into ``ps``).
SERVE_TOKEN_ENV = "COORD_SERVE_TOKEN"
SERVE_TOKEN_FILE = Path.home() / ".coord" / "serve_token"


def resolve_serve_token(flag_token: str | None = None) -> str | None:
    """Resolve the daemon's bearer token: flag > ``COORD_SERVE_TOKEN`` > file.

    Returns ``None`` when none is configured (the daemon runs open, relying on
    the Tailscale ACL — fine for dev/dogfood; the production daemon should set
    one).  A blank/whitespace token is treated as unset.
    """
    # Each source falls through to the next when blank/whitespace-only, so a
    # blank --token can't silently disable auth ahead of a configured env/file.
    for src in (flag_token, os.environ.get(SERVE_TOKEN_ENV)):
        if src and src.strip():
            return src.strip()
    if SERVE_TOKEN_FILE.exists():
        try:
            from_file = SERVE_TOKEN_FILE.read_text().strip()
        except OSError:
            from_file = ""
        if from_file:
            return from_file
    return None


class _BearerAuthMiddleware(BaseHTTPMiddleware):
    """Reject requests without ``Authorization: Bearer <token>`` (``/healthz`` exempt)."""

    def __init__(self, app, token: str) -> None:  # noqa: ANN001
        super().__init__(app)
        self._expected = f"Bearer {token}"

    async def dispatch(self, request: Request, call_next):  # noqa: ANN001, ANN201
        if request.url.path != "/healthz":
            if request.headers.get("authorization", "") != self._expected:
                return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


def _reload_config_if_stale(
    current: Config, last_mtime: float | None
) -> tuple[Config, float | None]:
    """Re-parse *current*'s backing ``coordinator.yml`` if it changed on disk (#1081).

    The daemon's in-memory ``Config`` is otherwise fixed at process startup, so
    a hand-edit to ``coordinator.yml`` on the daemon host silently diverges
    from the file until a restart — even though ``GET /config`` (which serves
    the raw bytes fresh every request) shows the new content immediately. This
    closes that gap for the daemon's *own* gating decisions (review/pipeline/
    merge-auto-drain/milestone-auto-dispatch) by tracking the file's mtime and
    swapping in a freshly-parsed ``Config`` whenever a caller notices it moved.

    Returns ``(config, mtime)`` — either *current* unchanged (no backing path,
    a ``stat()`` failure, or no on-disk change since *last_mtime*) or a
    freshly-loaded ``Config`` paired with its new mtime. A malformed hand-edit
    (invalid YAML, a validation error) is logged and swallowed rather than
    raised into a request handler or the tick loop — the daemon keeps serving
    the last-good config. *last_mtime* still advances past a bad edit so it
    isn't re-parsed (and re-logged) on every subsequent call; it will be
    retried once the file changes again (e.g. the edit is fixed).
    """
    path = current.path
    if path is None:
        return current, last_mtime
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return current, last_mtime
    if last_mtime is not None and mtime <= last_mtime:
        return current, last_mtime

    import logging  # noqa: PLC0415

    from coord.config import ConfigError  # noqa: PLC0415
    from coord.config import load as _load_coordinator_config  # noqa: PLC0415

    log = logging.getLogger("coord.serve")
    try:
        reloaded = _load_coordinator_config(path)
    except ConfigError as e:
        log.warning(
            "coord serve: %s changed on disk but failed to reload (%s); "
            "keeping last-good config until the file is fixed",
            path,
            e,
        )
        return current, mtime
    log.info("coord serve: reloaded %s (on-disk change detected)", path)
    return reloaded, mtime


def _passive_tick(config: Config) -> tuple[list[dict], list[str]]:
    """One passive daemon tick: reconcile completed assignments + enqueue approved work.

    Extracted as a module-level function so tests can call it directly without
    wiring up the async ``_tick_loop`` infrastructure.

    Steps:
    1. ``reconcile_completed_assignments`` — flip any agent-finished running
       rows to their terminal status (the #625 passive reconcile).  Loads the
       board internally so it can be fully monkeypatched in tests.
    2. ``enqueue_approved_work`` — add / re-key merge-queue entries for all
       approved + tested done work (#736 / #217 invisible limbo fix).  Also
       loads the board internally (a fresh snapshot after reconcile wrote DB
       state) so the two steps are independently testable.

    Returns ``(reconciled, enqueued)`` where *reconciled* is the list of dicts
    from :func:`~coord.reconcile.reconcile_completed_assignments` and *enqueued*
    is the list of assignment IDs newly added/re-keyed in the merge queue.

    Note: the daemon ``_tick_loop`` calls these two steps with **separate**
    ``try/except`` blocks so a failure in one does not silence the other.  This
    function combines them for convenience in tests that want both results.

    The slower-cadence merge-reconcile and issues-sync steps (``_reconcile_merges_tick``
    / ``_sync_issues_tick``, #775) run in ``_tick_loop`` on a separate timer and
    are tested via those helpers directly.
    """
    from coord.reconcile import reconcile_completed_assignments  # noqa: PLC0415
    from coord import merge_queue as mq  # noqa: PLC0415

    reconciled = reconcile_completed_assignments(config)
    _audit_reconciled(reconciled)
    enqueued = mq.enqueue_approved_work(config)  # loads its own board snapshot
    _audit_enqueued(enqueued)
    return reconciled, enqueued


# ── #1038: operational-tier audit hooks for the daemon tick ────────────────
#
# These are coarse, tick-scoped rows (``tier="operational"``, ``actor=
# "daemon"``) recorded ALONGSIDE the fine-grained business-tier rows #1036
# already emits at the state.py/issue_store.py write choke points (e.g.
# ``mark_assignment_merged`` already records a business ``merged`` row
# regardless of caller).  The operational layer exists specifically to mark
# *that the daemon tick itself* drove the action — a human running
# ``coord merge``/``coord reconcile-merges`` produces the same business rows
# without these.  Hooked here (the tick call sites) rather than inside the
# shared ``reconcile``/``merge_queue`` functions so CLI-triggered runs never
# get mislabeled ``actor="daemon"``.  ``record_audit`` never raises, so none
# of these need their own try/except.


def _audit_reconciled(reconciled: list[dict]) -> None:
    """One operational row per assignment the passive reconcile flipped
    running → terminal (#625's reconcile, #1038's audit)."""
    from coord.audit import record_audit  # noqa: PLC0415

    for r in reconciled:
        repo = r.get("repo")
        issue = r.get("issue_number")
        record_audit(
            tier="operational",
            category="reconcile",
            event_type="passive_reconcile",
            actor="daemon",
            summary=f"passive reconcile: {repo}#{issue} → {r.get('to_status')}"
            if repo is not None and issue is not None
            else f"passive reconcile: {r.get('assignment_id')} → {r.get('to_status')}",
            repo=repo,
            issue=issue,
            assignment_id=r.get("assignment_id"),
            details={"type": r.get("type"), "to_status": r.get("to_status")},
        )


def _audit_enqueued(enqueued: list[str]) -> None:
    """One operational row per assignment id the passive tick added/re-keyed
    into the merge queue (#736/#217, #1038's audit).

    ``enqueue_approved_work`` returns bare assignment ids; look the freshly
    written rows back up via ``load_queue()`` for repo/issue context.
    """
    if not enqueued:
        return
    from coord.audit import record_audit  # noqa: PLC0415
    from coord import merge_queue as mq  # noqa: PLC0415

    ids = set(enqueued)
    by_id = {item.assignment_id: item for item in mq.load_queue() if item.assignment_id in ids}
    for aid in enqueued:
        entry = by_id.get(aid)
        record_audit(
            tier="operational",
            category="merge_queue",
            event_type="enqueued",
            actor="daemon",
            summary=f"enqueued: {entry.repo_name}#{entry.issue_number}"
            if entry is not None else f"enqueued: {aid}",
            repo=entry.repo_name if entry is not None else None,
            issue=entry.issue_number if entry is not None else None,
            assignment_id=aid,
            details={"branch": entry.branch} if entry is not None else None,
        )


def _audit_housekeeping_sweep(swept: dict) -> None:
    """One operational row summarizing a housekeeping archival sweep
    (#762's ``housekeeping.sweep()``, #1038's audit).  Called only when the
    sweep actually archived something — an empty sweep is a no-op tick, not
    an event worth a row."""
    from coord.audit import record_audit  # noqa: PLC0415

    record_audit(
        tier="operational",
        category="housekeeping",
        event_type="sweep",
        actor="daemon",
        summary=(
            f"housekeeping: archived {swept.get('archived_assignments', 0)} "
            f"assignment(s), {swept.get('archived_notifications', 0)} "
            "notification(s)"
        ),
        details=swept,
    )


def _reconcile_merges_tick(config: Config) -> list[str]:
    """Load the board, run ``reconcile_board_merges``, save the result.

    Called on a slow throttled cadence by ``_tick_loop`` (#775).  Flips
    ``done`` work assignments whose PR merged on GitHub to ``status='merged'``
    and prunes the corresponding merge-queue rows, so the Pipeline:Live card
    leaves the Merge gate without a manual ``coord reconcile-merges``.

    Extracted as a module-level function so tests can call it directly without
    wiring up the async ``_tick_loop`` infrastructure.
    """
    from coord.reconcile import reconcile_board_merges  # noqa: PLC0415
    from coord.state import build_board, save_board  # noqa: PLC0415

    board = build_board()
    actions = reconcile_board_merges(board, config)
    save_board(board)
    if actions:
        # #1038: one coarse operational row per tick that did something —
        # the individual branch-backfill/mark-merged writes already get
        # their own business-tier rows (state.py), this just marks that the
        # daemon tick (not a manual `coord reconcile-merges`) drove them.
        from coord.audit import record_audit  # noqa: PLC0415

        record_audit(
            tier="operational",
            category="reconcile",
            event_type="merge_reconcile",
            actor="daemon",
            summary=f"merge reconcile: {len(actions)} action(s)",
            details={"actions": actions[:20]},
        )
    return actions


def _sync_issues_tick(config: Config) -> int:
    """Fetch open issues from GitHub and update the local issues cache.

    Called on the same slow cadence as ``_reconcile_merges_tick`` by
    ``_tick_loop`` (#775).  Keeps the board's ``is_closed`` flag current so
    issues closed by a merge appear in the Done section without a manual
    ``coord sync``.

    Returns the total number of open issues synced across all repos.
    Extracted as a module-level function so tests can call it directly.
    """
    import logging  # noqa: PLC0415

    from coord import github_ops  # noqa: PLC0415
    from coord.state import _upsert_open_issues_local  # noqa: PLC0415

    # Use the private _upsert_open_issues_local (underscore-prefixed) rather
    # than the public upsert_open_issues, because the public variant routes
    # through the daemon HTTP seam (/issues-sync) when a board-service URL is
    # configured.  Since this function IS the daemon, we must write directly
    # to the local DB to avoid a self-referential HTTP call.
    log = logging.getLogger("coord.serve")
    total = 0
    for repo in config.repos:
        try:
            issues = github_ops.get_open_issues(repo.github)
            _upsert_open_issues_local(repo.name, issues)
            total += len(issues)
        except Exception:  # noqa: BLE001
            log.warning(
                "issues-sync tick: repo %s failed", repo.name, exc_info=True
            )
    log.debug("issues-sync tick: %d open issues across %d repos", total, len(config.repos))
    return total


def _auto_drain_tick(config: Config) -> "list":
    """Drain READY merge-queue entries — the opt-in daemon auto-merge (#781).

    Called by ``_tick_loop`` when ``merge.auto_drain: true`` is set in
    ``coordinator.yml``.  Evaluates the live merge plan (review + smoke + CI
    gates) and calls :func:`coord.merge_queue.process` on exactly the entries
    the plan marks ``READY``.  ``BLOCKED``, ``MERGING``, ``MERGED``, and
    ``NEEDS_ATTENTION`` entries are never touched.

    ``merge.max_per_tick > 0`` caps how many READY entries are attempted in a
    single tick (0 = unlimited).

    Gate policy is inherited from :func:`coord.merge_queue.process`:
    no ``force_merge``, no ``skip_review``, no ``skip_smoke``.  A drain error
    must not silence the enqueue/reconcile steps — the caller wraps this in its
    own ``try/except``.

    Mutates merge-queue rows in place and persists the changes.  Returns the
    list of :class:`~coord.merge_queue.MergeEvent` objects so the caller can
    log each event.  Returns an empty list when there are no READY entries.

    Extracted as a module-level function so tests can call it directly without
    wiring up the async ``_tick_loop`` infrastructure.
    """
    import logging  # noqa: PLC0415

    from coord import github_ops  # noqa: PLC0415
    from coord import merge_queue as mq  # noqa: PLC0415
    from coord.ci_store import build_ci_store  # noqa: PLC0415
    from coord.merge_queue import PENDING, PLAN_READY  # noqa: PLC0415
    from coord.state import build_board  # noqa: PLC0415

    log = logging.getLogger("coord.serve")

    board = build_board()

    # Build the CI store; fail-open so a transient gh error doesn't disable drain.
    try:
        ci_store = build_ci_store(config.ci_store.type)
    except Exception:  # noqa: BLE001
        ci_store = None

    # Compute the gate-annotated plan — the single source of truth for READY.
    merge_plan = mq.plan(board, config, ci_store=ci_store)
    ready_aids = {pm.assignment_id for pm in merge_plan if pm.status == PLAN_READY}

    if not ready_aids:
        log.debug("auto-drain: no READY entries")
        return []

    # Load the raw queue and restrict to PENDING + READY.
    all_items = mq.load_queue()
    ready_items = [
        item for item in all_items
        if item.assignment_id in ready_aids and item.state == PENDING
    ]

    if not ready_items:
        log.debug("auto-drain: plan shows READY but no PENDING queue rows match")
        return []

    # Apply per-tick cap when configured.
    cap = config.merge.max_per_tick
    if cap > 0 and len(ready_items) > cap:
        log.debug(
            "auto-drain: capping %d READY entries to %d (max_per_tick)",
            len(ready_items), cap,
        )
        ready_items = ready_items[:cap]

    # process() mutates ready_items in place (state, pr_number, etc.).
    events = mq.process(
        ready_items,
        github_ops,
        method="rebase",
        dry_run=False,
        presorted=False,
        ci_store=ci_store,
        force_merge=False,
        config=config,
        board=board,
        skip_review=False,
        skip_smoke=False,
    )

    # Persist: merge the mutated rows back over the on-disk queue (same
    # pattern as ``coord merge`` in cli.py to avoid clobbering unrelated rows).
    fresh = mq.load_queue()
    by_id = {item.assignment_id: item for item in ready_items}
    merged = [by_id.get(item.assignment_id, item) for item in fresh]
    mq.save_queue(merged)

    # #1038: one operational row per MergeEvent this auto-drain tick produced
    # (opened/sized/merged/checks_failed/checks_pending/review_required/
    # smoke_required/conflict/...).  process() is also called by the
    # `coord merge` CLI (human-triggered, business intent) so the audit call
    # lives here — the auto-drain-exclusive call site — not inside
    # merge_queue.process() itself.
    from coord.audit import record_audit  # noqa: PLC0415

    for ev in events:
        record_audit(
            tier="operational",
            category="merge",
            event_type=f"merge_{ev.kind}",
            actor="daemon",
            summary=f"auto-drain {ev.kind}: {ev.entry.repo_name}#{ev.entry.issue_number} — {ev.message}",
            repo=ev.entry.repo_name,
            issue=ev.entry.issue_number,
            assignment_id=ev.entry.assignment_id,
            details={"kind": ev.kind, "pr_number": ev.entry.pr_number},
        )

    return events


def _milestone_drain_tick(config: Config) -> list:
    """Re-drain every actively-registered milestone — #769 Phase 1 auto-dispatch.

    Called by ``_tick_loop`` when ``milestone.auto_dispatch: true`` is set in
    ``coordinator.yml`` (default-off). For each ``(repo_name, tracking_issue)``
    registered via a non-dry-run ``coord milestone dispatch`` (``coord.state.
    register_milestone_drain``), re-fetches the tracking issue, recomputes the
    ready frontier (:func:`coord.milestone_dispatch.plan_dispatch`), and
    dispatches any newly-unblocked entries — the same mechanism a manual
    ``coord milestone dispatch`` uses, so a fix that lands and merges for one
    cohort member automatically unblocks and dispatches the next one. Once a
    milestone's whole work order reaches a terminal state it's deregistered.

    A single shared :class:`~coord.models.Board` snapshot is used across all
    registered milestones in one tick (loaded once via ``build_board()``) and
    updated in place by ``dispatch_entry`` as each dispatch lands, so two
    milestones competing for the same idle machine in one tick don't
    double-book it.

    Gate policy mirrors the manual CLI path exactly — same claim recheck,
    same ``can_work_on``/idle/paused machine filter (#688's "never route
    coord-self to a machine whose ``repos:`` list excludes it" falls out of
    that filter for free). A per-milestone fetch/dispatch error must not
    silence the other registered milestones — caught and logged per entry.

    Extracted as a module-level function so tests can call it directly
    without wiring up the async ``_tick_loop`` infrastructure (mirrors
    ``_auto_drain_tick``'s doc comment above).
    """
    import logging  # noqa: PLC0415

    from coord import milestone_dispatch as md  # noqa: PLC0415
    from coord.state import (  # noqa: PLC0415
        build_board,
        deregister_milestone_drain,
        list_milestone_drains,
    )

    log = logging.getLogger("coord.serve")

    drains = list_milestone_drains()
    if not drains:
        return []

    board = build_board()
    outcomes: list = []
    for entry in drains:
        repo_name = entry.get("repo_name")
        tracking_issue = entry.get("tracking_issue")
        repo_cfg = config.repo(repo_name) if repo_name else None
        if repo_cfg is None or tracking_issue is None:
            log.warning(
                "milestone-drain: dropping malformed/unknown-repo entry %r", entry
            )
            deregister_milestone_drain(
                repo_name=repo_name or "", tracking_issue=tracking_issue or 0
            )
            continue

        try:
            ctx = md.fetch_milestone_context(repo_cfg, tracking_issue)
        except md.MilestoneDispatchError as e:
            log.warning(
                "milestone-drain: %s #%d fetch failed: %s", repo_name, tracking_issue, e
            )
            continue

        # Gate A (#930, docs/ORACLE_LOOP.md): don't drain a milestone whose
        # black-box contract doesn't exist yet — skip this tick and retry
        # later (not deregistered) once `coord acceptance mock` lands one.
        block_reason = md.gate_a_status(repo_cfg, config, ctx.milestone_number)
        if block_reason:
            log.warning(
                "milestone-drain: %s #%d gated: %s", repo_name, tracking_issue, block_reason
            )
            continue

        plan = md.plan_dispatch(ctx.work_order, board, config, repo_cfg, ctx.terminal_issues)
        for pick in plan.to_dispatch:
            outcome = md.dispatch_entry(
                pick, repo_cfg, config, board, tracking_issue=tracking_issue
            )
            outcomes.append(outcome)
            if outcome.ok:
                log.info(
                    "milestone-drain: %s #%d → %s (assignment %s)",
                    repo_name, outcome.issue_number, outcome.machine_name,
                    outcome.assignment_id,
                )
            else:
                log.warning(
                    "milestone-drain: %s #%d dispatch failed: %s",
                    repo_name, outcome.issue_number, outcome.error,
                )

        if md.is_milestone_complete(ctx):
            log.info(
                "milestone-drain: %s #%d work order complete — deregistering",
                repo_name, tracking_issue,
            )
            deregister_milestone_drain(repo_name=repo_name, tracking_issue=tracking_issue)

    return outcomes


def _board_response_schema(components: dict) -> dict:
    """#757: the `GET /board` response schema, built straight from the live
    (migrated) SQLite DDL — not a dataclass. Per
    ``scripts/gen_board_fixture.py``: "the wire schema *is* the SQLite DDL",
    so this introspects the exact same schema + JSON/dropped-column tables
    (``coord.dao._JSON_COLUMNS`` / ``_DROP_COLUMNS``) that
    ``SqliteStore.board_projection()`` uses, rather than hand-duplicating the
    column list here where it could drift.
    """
    from coord.merge_queue import PlannedMerge, StagingItem  # noqa: PLC0415

    conn = sqlite3.connect(":memory:")
    try:
        _ensure_schema(conn)
        for table, key in (
            ("assignments", "BoardAssignment"),
            ("machines", "BoardMachine"),
            ("merge_queue", "BoardMergeQueueEntry"),
            ("proposals", "BoardProposal"),
            ("issues", "BoardIssue"),
        ):
            components[key] = sqlite_table_schema(
                conn,
                table,
                drop=frozenset(_DROP_COLUMNS.get(table, ())),
                json_columns=frozenset(_JSON_COLUMNS.get(table, ())),
            )
    finally:
        conn.close()

    planned_merge_ref = dataclass_schema(PlannedMerge, components)
    staging_item_ref = dataclass_schema(StagingItem, components)

    def _list_of(key: str) -> dict:
        return {"type": "array", "items": {"$ref": f"#/components/schemas/{key}"}}

    return {
        "type": "object",
        "properties": {
            "schema_version": {"type": "integer"},
            "round_number": {"type": "integer"},
            "assignments": _list_of("BoardAssignment"),
            "machines": _list_of("BoardMachine"),
            "merge_queue": _list_of("BoardMergeQueueEntry"),
            "proposals": _list_of("BoardProposal"),
            "issues": _list_of("BoardIssue"),
            "plans": {
                "type": "object",
                "description": "assignment_id -> parsed structured plan",
                "additionalProperties": {"type": "object"},
            },
            "notifications": {"type": "array", "items": {"type": "object"}},
            "board_meta": {"type": "object", "additionalProperties": {"type": "string"}},
            "audit_recent_count": {
                "type": "integer",
                "description": (
                    "#1037: count of audit_log rows written in the last 15 "
                    "minutes — a single forward-compatible integer so the "
                    "coord-tui activity bar can show an attention badge "
                    "without fetching the full paginated /audit log."
                ),
            },
            "merge_plan": {
                "type": "array",
                "description": "#776: server-side, gate-annotated merge plan",
                "items": planned_merge_ref,
            },
            "merge_staging": {
                "type": "array",
                "description": "#778: approved/done work not yet in the merge queue",
                "items": staging_item_ref,
            },
            "issue_stage_projection": {
                "type": "array",
                "description": (
                    "#550: server-computed per-issue stage/gate badges "
                    "(work/review/smoke/test/merge status, has_approved_review) — "
                    "generalizes the #776/#778 pattern so coord-tui's "
                    "pipeline.rs stops re-deriving this from raw rows"
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "repo_name": {"type": "string"},
                        "issue_number": {"type": "integer"},
                        "issue_title": {"type": "string"},
                        "stages": {
                            "type": "object",
                            "description": "stage name -> pending|active|done|failed|stale|skipped",
                            "additionalProperties": {"type": "string"},
                        },
                        "has_approved_review": {"type": "boolean"},
                    },
                    "required": ["repo_name", "issue_number", "stages", "has_approved_review"],
                },
            },
            "plan_roster": {
                "type": "array",
                "description": (
                    "#975: milestone plan-roster — one entry per milestone/epic "
                    "with ready / blocked / in-flight / done counts and a "
                    "`needs_you` list of attention signals. Computed server-side "
                    "by reusing coord.plans.aggregate_repo_plans over the same "
                    "board + issues snapshot; the coord-tui \"Plans\" panel "
                    "deserialises this and renders one row per plan."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "repo": {"type": "string"},
                        "title": {"type": "string"},
                        "milestone_number": {"type": "integer"},
                        "tracking_issue": {"type": ["integer", "null"]},
                        "has_work_order": {"type": "boolean"},
                        "ready_frontier": {"type": "integer"},
                        "blocked": {"type": "integer"},
                        "in_flight": {"type": "integer"},
                        "done": {"type": "integer"},
                        "total": {"type": "integer"},
                        "needs_you": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": [
                        "repo", "title", "milestone_number", "has_work_order",
                        "ready_frontier", "blocked", "in_flight", "done", "total", "needs_you",
                    ],
                },
            },
            "plan_roster_supported": {
                "type": "boolean",
                "description": (
                    "#976: capability flag — true whenever this daemon computes "
                    "plan_roster at all (even if it came back empty this tick due "
                    "to a per-repo aggregation error). Absent (defaults false on "
                    "the client) on daemons older than #975, which never emit "
                    "plan_roster. Lets the Plans panel distinguish a genuinely "
                    "empty roster from a daemon too old to compute one."
                ),
            },
            "goal_header": {
                "type": "object",
                "description": (
                    "#978: GOAL.md pinned north-star header for the coord-tui "
                    "Plans panel. Computed server-side by coord.goal.read_goal_header() "
                    "from the repo-root GOAL.md this daemon is running from — fail-open "
                    "to {\"available\": false} when GOAL.md can't be located (a "
                    "packaged/PyPI install has no repo root to read; see "
                    "pyproject.toml, which never ships GOAL.md) or read."
                ),
                "properties": {
                    "available": {"type": "boolean"},
                    "headline": {"type": "string"},
                    "last_updated": {"type": ["string", "null"], "description": "ISO YYYY-MM-DD"},
                    "days_since_update": {"type": ["integer", "null"]},
                },
                "required": ["available"],
            },
            "milestone_work_orders": {
                "type": "array",
                "description": (
                    "#795 Phase 3b: server-computed per-milestone work-order "
                    "rank + ready/blocked frontier so coord-tui can display "
                    "work-order rank, next-up, and blocked-on badges on Pipeline "
                    "milestone cards without re-implementing the DAG logic in Rust."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "repo_name": {"type": "string"},
                        "tracking_issue": {"type": "integer"},
                        "milestone_title": {"type": "string"},
                        "nodes": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "issue_number": {"type": "integer"},
                                    "rank": {"type": "integer", "description": "0-indexed position in the work order"},
                                    "ready": {"type": "boolean", "description": "true when all `after` dependencies are terminal, regardless of claim state"},
                                    "next_up": {"type": "boolean", "description": "true when on the ready frontier: ready AND not already claimed/conflict-blocked — the dispatcher's next candidate"},
                                    "blocked_on": {"type": "array", "items": {"type": "integer"}, "description": "unmet dependency issue numbers; empty when ready (including 'ready but claimed')"},
                                },
                                "required": ["issue_number", "rank", "ready", "next_up", "blocked_on"],
                            },
                        },
                    },
                    "required": ["repo_name", "tracking_issue", "nodes"],
                },
            },
        },
        "required": [
            "schema_version", "round_number", "assignments", "machines",
            "merge_queue", "proposals", "issues",
        ],
    }


def _openapi_spec() -> dict:
    """#757: the daemon's OpenAPI 3 document.

    ``GET /board`` is fully specified (see :func:`_board_response_schema`);
    the write endpoints document their required JSON fields (mirroring each
    handler's own ``KeyError``/``TypeError`` validation) but keep the body
    loosely typed beyond that, since most bodies are hand-assembled dicts
    rather than a single dataclass round-trip.
    """
    components: dict = {}
    board_schema = _board_response_schema(components)
    result_body = {"type": "object", "description": "issue_store.ResultRecord fields"}
    completion_body = {"type": "object", "description": "issue_store.CompletionRecord fields"}
    ok_response = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
    cli_output_response = {
        "type": "object",
        "properties": {
            "output": {"type": "string"},
            "exit_code": {"type": "integer"},
            "error": {"type": "string", "nullable": True},
        },
    }
    paths = {
        "/healthz": {
            "get": {
                "summary": "Liveness probe (never auth-gated, no DB access)",
                "responses": {"200": {"description": "OK"}},
            }
        },
        "/board": {
            "get": {
                "summary": "The full board projection (CoordStore.board_projection)",
                "responses": {
                    "200": {
                        "description": "OK",
                        "content": {"application/json": {"schema": board_schema}},
                    },
                    "503": {"description": "Board read failed"},
                },
            },
            "post": {
                "summary": "#749: whole-board upsert (backs board_service.write_board)",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "assignments": {
                                        "type": "array",
                                        "items": {"$ref": "#/components/schemas/BoardAssignment"},
                                    },
                                    "round_number": {"type": "integer"},
                                },
                            }
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "OK",
                        "content": {"application/json": {"schema": ok_response}},
                    },
                    "400": {"description": "Bad board payload"},
                    "503": {"description": "Board write failed"},
                },
            },
        },
        "/config": {
            "get": {
                "summary": "Raw coordinator.yml bytes the daemon owns",
                "responses": {
                    "200": {"description": "OK (application/x-yaml)"},
                    "404": {"description": "No config file on the daemon host"},
                },
            }
        },
        "/result": {
            "post": {
                "summary": "Record an interactive-session result (#590)",
                "requestBody": {
                    "required": True,
                    "content": {"application/json": {"schema": result_body}},
                },
                "responses": {"200": {"description": "OK"}, "400": {"description": "Bad record"}},
            }
        },
        "/completion": {
            "post": {
                "summary": "Record a git-floor backstop completion (#590)",
                "requestBody": {
                    "required": True,
                    "content": {"application/json": {"schema": completion_body}},
                },
                "responses": {"200": {"description": "OK"}},
            }
        },
        "/dispatched-work": {
            "post": {
                "summary": "Record a thin client's work dispatch (#590 Phase 2)",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "assignment_id": {"type": "string"},
                                    "proposal": {"type": "object"},
                                    "repo_github": {"type": "string"},
                                    "provider_name": {"type": "string", "nullable": True},
                                },
                                "required": ["assignment_id", "repo_github"],
                            }
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "OK",
                        "content": {"application/json": {"schema": ok_response}},
                    },
                    "400": {"description": "Bad dispatch"},
                },
            }
        },
        "/milestone-drain": {
            "post": {
                "summary": (
                    "Register a thin client's `coord milestone dispatch` for "
                    "daemon auto-drain (#769 Phase 1)"
                ),
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "repo_name": {"type": "string"},
                                    "tracking_issue": {"type": "integer"},
                                },
                                "required": ["repo_name", "tracking_issue"],
                            }
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "OK",
                        "content": {"application/json": {"schema": ok_response}},
                    },
                    "400": {"description": "Bad milestone-drain"},
                },
            }
        },
        "/dispatched": {
            "post": {
                "summary": "Record a thin client's review/fix/rework/merge dispatch (#590 Phase 2)",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "assignment": {"$ref": "#/components/schemas/BoardAssignment"},
                                    "repo_github": {"type": "string"},
                                },
                                "required": ["repo_github"],
                            }
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "OK",
                        "content": {"application/json": {"schema": ok_response}},
                    },
                    "400": {"description": "Bad dispatch"},
                },
            }
        },
        "/test-verdict": {
            "post": {
                "summary": "Record a Test-gate verdict (#590 Phase 2)",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "assignment_id": {"type": "string"},
                                    "test_state": {"type": "string"},
                                    "test_reason": {"type": "string", "nullable": True},
                                    "smoke_test": {"type": "string", "nullable": True},
                                    "smoke_test_reason": {"type": "string", "nullable": True},
                                },
                                "required": ["assignment_id", "test_state"],
                            }
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "OK",
                        "content": {"application/json": {"schema": ok_response}},
                    },
                    "400": {"description": "Missing field"},
                },
            }
        },
        "/acceptance-verdict": {
            "post": {
                "summary": "Record an Acceptance-gate verdict (#944, oracle loop)",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "assignment_id": {"type": "string"},
                                    "acceptance_state": {"type": "string"},
                                    "acceptance_reason": {"type": "string", "nullable": True},
                                    "acceptance_sha": {"type": "string", "nullable": True},
                                    "acceptance_total": {"type": "integer", "nullable": True},
                                    "acceptance_passed": {"type": "integer", "nullable": True},
                                },
                                "required": ["assignment_id", "acceptance_state"],
                            }
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "OK",
                        "content": {"application/json": {"schema": ok_response}},
                    },
                    "400": {"description": "Missing field"},
                },
            }
        },
        "/acceptance-record": {
            "post": {
                "summary": (
                    "Run `coord acceptance record` on the daemon host: "
                    "re-run the sealed suite against a pushed SHA and write "
                    "the verdict to the board (#944, oracle loop trust gate)"
                ),
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "repo": {"type": "string"},
                                    "issue": {"type": "integer"},
                                    "sha": {"type": "string"},
                                },
                                "required": ["repo", "issue", "sha"],
                            }
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "OK — CLI output relayed verbatim",
                        "content": {"application/json": {"schema": ok_response}},
                    },
                },
            }
        },
        "/review-findings": {
            "post": {
                "summary": "Persist parsed review verdict+body on a review assignment (#905)",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "assignment_id": {"type": "string"},
                                    "verdict": {"type": "string"},
                                    "body": {"type": "string"},
                                },
                                "required": ["assignment_id", "verdict", "body"],
                            }
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "OK",
                        "content": {"application/json": {"schema": ok_response}},
                    },
                    "400": {"description": "Missing field"},
                },
            }
        },
        "/review-posted": {
            "post": {
                "summary": "Mark a review assignment's findings as posted (sets review_posted_at) (#905)",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "assignment_id": {"type": "string"},
                                },
                                "required": ["assignment_id"],
                            }
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "OK",
                        "content": {"application/json": {"schema": ok_response}},
                    },
                    "400": {"description": "Missing assignment_id"},
                },
            }
        },
        "/needs-attention-notified": {
            "post": {
                "summary": (
                    "Mark the one-shot #846 'needs attention' ledger entry "
                    "for an assignment (thin-client route for `coord "
                    "acceptance stall`'s self-report, #846 review)"
                ),
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "assignment_id": {"type": "string"},
                                },
                                "required": ["assignment_id"],
                            }
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "OK",
                        "content": {"application/json": {"schema": ok_response}},
                    },
                    "400": {"description": "Missing assignment_id"},
                },
            }
        },
        "/assignment-usage": {
            "post": {
                "summary": "Route cost/token/is_interactive/smoke_tests writes (#665/#749)",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "assignment_id": {"type": "string"},
                                    "cost_usd": {"type": "number", "nullable": True},
                                    "input_tokens": {"type": "integer"},
                                    "output_tokens": {"type": "integer"},
                                    "cache_creation_tokens": {"type": "integer"},
                                    "cache_read_tokens": {"type": "integer"},
                                    "is_interactive": {"type": "boolean"},
                                    "smoke_tests": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                        "nullable": True,
                                    },
                                },
                                "required": ["assignment_id"],
                            }
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "OK",
                        "content": {"application/json": {"schema": ok_response}},
                    },
                    "400": {"description": "Missing assignment_id"},
                },
            }
        },
        "/assignment-session-id": {
            "post": {
                "summary": "Persist a worker's claude session ID on the assignment row (#906)",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "assignment_id": {"type": "string"},
                                    "claude_session_id": {"type": "string"},
                                },
                                "required": ["assignment_id", "claude_session_id"],
                            }
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "OK",
                        "content": {"application/json": {"schema": ok_response}},
                    },
                    "400": {"description": "Missing field"},
                },
            }
        },
        "/assignment-failure-reason": {
            "post": {
                "summary": "Mark assignment failed with a reason string (#906)",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "assignment_id": {"type": "string"},
                                    "reason": {"type": "string"},
                                },
                                "required": ["assignment_id", "reason"],
                            }
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "OK",
                        "content": {"application/json": {"schema": ok_response}},
                    },
                    "400": {"description": "Missing field"},
                },
            }
        },
        "/assignment-test-plan": {
            "post": {
                "summary": "Read the cached smoke-test plan for an assignment (#906)",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "assignment_id": {"type": "string"},
                                },
                                "required": ["assignment_id"],
                            }
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "OK — test_plan is the JSON string or null",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "test_plan": {"nullable": True},
                                    },
                                }
                            }
                        },
                    },
                    "400": {"description": "Missing assignment_id"},
                },
            }
        },
        "/notify": {
            "post": {
                "summary": "Run `coord notify` against the canonical DB + agent fleet (#906)",
                "requestBody": {
                    "required": False,
                    "content": {"application/json": {"schema": {"type": "object"}}},
                },
                "responses": {
                    "200": {
                        "description": "OK",
                        "content": {"application/json": {"schema": cli_output_response}},
                    },
                },
            }
        },
        "/issue-test-mode": {
            "post": {
                "summary": "Read the cached test-mode label for an issue (#906)",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "repo_name": {"type": "string"},
                                    "issue_number": {"type": "integer"},
                                },
                                "required": ["repo_name", "issue_number"],
                            }
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "OK — test_mode is \"auto\", \"smoke\", or null",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "test_mode": {"nullable": True},
                                    },
                                }
                            }
                        },
                    },
                    "400": {"description": "Missing repo_name or issue_number"},
                },
            }
        },
        "/issue-labels": {
            "post": {
                "summary": "Update one issue's cached labels (#601)",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "repo_name": {"type": "string"},
                                    "issue_number": {"type": "integer"},
                                    "labels": {"type": "array", "items": {"type": "string"}},
                                },
                                "required": ["repo_name", "issue_number"],
                            }
                        }
                    },
                },
                "responses": {
                    "200": {"description": "OK"},
                    "400": {"description": "Missing field"},
                },
            }
        },
        "/issues-sync": {
            "post": {
                "summary": "Upsert a repo's open issues into the shared issue cache (#601)",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "repo_name": {"type": "string"},
                                    "issues": {"type": "array", "items": {"type": "object"}},
                                },
                                "required": ["repo_name"],
                            }
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "OK",
                        "content": {"application/json": {"schema": ok_response}},
                    },
                    "400": {"description": "Missing field"},
                },
            }
        },
        "/issue-edit": {
            "post": {
                "summary": "Edit an issue's title/body through the tracker seam",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "repo_name": {"type": "string"},
                                    "issue_number": {"type": "integer"},
                                    "title": {"type": "string", "nullable": True},
                                    "body": {"type": "string", "nullable": True},
                                    "repo_github": {"type": "string", "nullable": True},
                                },
                                "required": ["repo_name", "issue_number"],
                            }
                        }
                    },
                },
                "responses": {
                    "200": {"description": "OK"},
                    "400": {"description": "Missing field"},
                },
            }
        },
        "/issue-milestone": {
            "post": {
                "summary": "Assign a milestone to an issue through the tracker seam (#967)",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "repo_name": {"type": "string"},
                                    "issue_number": {"type": "integer"},
                                    "milestone_number": {"type": "integer"},
                                    "milestone_title": {"type": "string", "nullable": True},
                                    "repo_github": {"type": "string", "nullable": True},
                                },
                                "required": ["repo_name", "issue_number", "milestone_number"],
                            }
                        }
                    },
                },
                "responses": {
                    "200": {"description": "OK"},
                    "400": {"description": "Missing field"},
                    "503": {"description": "GitHub write failed"},
                },
            }
        },
        "/issue-milestone-remove": {
            "post": {
                "summary": "Clear an issue's milestone through the tracker seam (#1003)",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "repo_name": {"type": "string"},
                                    "issue_number": {"type": "integer"},
                                    "repo_github": {"type": "string", "nullable": True},
                                },
                                "required": ["repo_name", "issue_number"],
                            }
                        }
                    },
                },
                "responses": {
                    "200": {"description": "OK"},
                    "400": {"description": "Missing field"},
                    "503": {"description": "GitHub write failed"},
                },
            }
        },
        "/issue-close": {
            "post": {
                "summary": "Close an issue (optionally with a comment) through the tracker seam (#1003)",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "repo_name": {"type": "string"},
                                    "issue_number": {"type": "integer"},
                                    "comment": {"type": "string", "nullable": True},
                                    "repo_github": {"type": "string", "nullable": True},
                                },
                                "required": ["repo_name", "issue_number"],
                            }
                        }
                    },
                },
                "responses": {
                    "200": {"description": "OK"},
                    "400": {"description": "Missing field"},
                    "503": {"description": "GitHub write failed"},
                },
            }
        },
        "/milestone-edit": {
            "post": {
                "summary": "Create or edit a GitHub milestone through the tracker seam (#645)",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "repo_name": {"type": "string"},
                                    "number": {
                                        "type": "integer",
                                        "nullable": True,
                                        "description": "Omit/null to create a new milestone; set to edit an existing one.",
                                    },
                                    "title": {"type": "string", "nullable": True},
                                    "description": {"type": "string", "nullable": True},
                                    "due_on": {"type": "string", "nullable": True},
                                    "repo_github": {"type": "string", "nullable": True},
                                },
                                "required": ["repo_name"],
                            }
                        }
                    },
                },
                "responses": {
                    "200": {"description": "OK — the milestone's JSON dict"},
                    "400": {"description": "Missing field / invalid create (no title)"},
                },
            }
        },
        "/issue-label": {
            "post": {
                "summary": "Add/remove arbitrary labels on an issue through the tracker seam (#802)",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "repo_name": {"type": "string"},
                                    "issue_number": {"type": "integer"},
                                    "add": {"type": "array", "items": {"type": "string"}},
                                    "remove": {"type": "array", "items": {"type": "string"}},
                                    "repo_github": {"type": "string", "nullable": True},
                                },
                                "required": ["repo_name", "issue_number"],
                            }
                        }
                    },
                },
                "responses": {
                    "200": {"description": "OK"},
                    "400": {"description": "Missing field"},
                },
            }
        },
        "/issue-create": {
            "post": {
                "summary": "Create a new GitHub issue through the tracker seam (#802)",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "repo_name": {"type": "string"},
                                    "title": {"type": "string"},
                                    "body": {"type": "string", "nullable": True},
                                    "labels": {"type": "array", "items": {"type": "string"}},
                                    "repo_github": {"type": "string", "nullable": True},
                                },
                                "required": ["repo_name", "title"],
                            }
                        }
                    },
                },
                "responses": {
                    "200": {"description": "OK"},
                    "400": {"description": "Missing field"},
                },
            }
        },
        "/issue-context": {
            "get": {
                "summary": "#603: read an issue's raw context entries (oldest-first)",
                "parameters": [
                    {
                        "name": "repo_name", "in": "query", "required": True,
                        "schema": {"type": "string"},
                    },
                    {
                        "name": "issue_number", "in": "query", "required": True,
                        "schema": {"type": "integer"},
                    },
                ],
                "responses": {
                    "200": {"description": "OK"},
                    "400": {"description": "Missing repo_name/issue_number"},
                },
            },
            "post": {
                "summary": "#603: add / pin / clear / replace a per-issue context entry",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "action": {
                                        "type": "string",
                                        "enum": ["add", "pin", "clear", "replace"],
                                    },
                                    "repo_name": {"type": "string"},
                                    "issue_number": {"type": "integer"},
                                    "body": {"type": "string"},
                                    "pinned": {"type": "boolean"},
                                    "source": {"type": "string", "nullable": True},
                                    "entry_id": {"type": "integer"},
                                    "entries": {"type": "array", "items": {"type": "object"}},
                                },
                                "required": ["action", "repo_name", "issue_number"],
                            }
                        }
                    },
                },
                "responses": {
                    "200": {"description": "OK"},
                    "400": {"description": "Missing field / unknown action"},
                },
            },
        },
        "/audit": {
            "get": {
                "summary": (
                    "#1037: paginated, newest-first read over the append-only "
                    "audit_log — NOT part of /board (deliberately unbounded, "
                    "its own endpoint)"
                ),
                "parameters": [
                    {"name": "since", "in": "query", "schema": {"type": "string"}, "description": "epoch seconds or ISO-8601"},
                    {"name": "until", "in": "query", "schema": {"type": "string"}, "description": "epoch seconds or ISO-8601"},
                    {"name": "type", "in": "query", "schema": {"type": "string"}, "description": "event_type filter"},
                    {"name": "category", "in": "query", "schema": {"type": "string"}},
                    {"name": "repo", "in": "query", "schema": {"type": "string"}},
                    {"name": "issue", "in": "query", "schema": {"type": "integer"}},
                    {"name": "assignment", "in": "query", "schema": {"type": "string"}, "description": "assignment_id filter"},
                    {"name": "tier", "in": "query", "schema": {"type": "string"}, "description": "business|operational"},
                    {"name": "limit", "in": "query", "schema": {"type": "integer"}, "description": "default 200, hard-capped at 500"},
                    {"name": "cursor", "in": "query", "schema": {"type": "string"}, "description": "opaque keyset cursor from a previous response's next_cursor"},
                ],
                "responses": {
                    "200": {
                        "description": "OK",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "entries": {
                                            "type": "array",
                                            "items": {
                                                "type": "object",
                                                "properties": {
                                                    "id": {"type": "integer"},
                                                    "ts": {"type": "number"},
                                                    "tier": {"type": "string"},
                                                    "category": {"type": "string"},
                                                    "event_type": {"type": "string"},
                                                    "actor": {"type": "string"},
                                                    "repo": {"type": ["string", "null"]},
                                                    "issue": {"type": ["integer", "null"]},
                                                    "assignment_id": {"type": ["string", "null"]},
                                                    "machine": {"type": ["string", "null"]},
                                                    "summary": {"type": "string"},
                                                    "details": {"type": ["object", "null"]},
                                                },
                                            },
                                        },
                                        "next_cursor": {"type": ["string", "null"]},
                                        "has_more": {"type": "boolean"},
                                    },
                                    "required": ["entries", "has_more"],
                                }
                            }
                        },
                    },
                    "400": {"description": "Bad query parameter"},
                },
            }
        },
        "/merge": {
            "post": {
                "summary": "Run `coord merge` against the canonical DB (#584)",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "dry_run": {"type": "boolean"},
                                    "order": {"type": "array", "items": {"type": "string"}, "nullable": True},
                                    "repo_filter": {"type": "string", "nullable": True},
                                    "method": {"type": "string"},
                                    "force_merge": {"type": "boolean"},
                                    "skip_smoke": {"type": "boolean"},
                                    "drop": {"type": "string", "nullable": True},
                                    "only": {"type": "string", "nullable": True},
                                },
                            }
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "OK",
                        "content": {"application/json": {"schema": cli_output_response}},
                    },
                },
            }
        },
        "/reconcile-merges": {
            "post": {
                "summary": "Run `coord reconcile-merges` against the canonical DB (#584)",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "dry_run": {"type": "boolean"},
                                    "repo": {"type": "string", "nullable": True},
                                },
                            }
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "OK",
                        "content": {"application/json": {"schema": cli_output_response}},
                    },
                },
            }
        },
        "/diagnose": {
            "post": {
                "summary": "Run `coord diagnose` against the canonical DB + fleet (#diagnose)",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "repo": {"type": "string"},
                                    "issue": {"type": "integer"},
                                    "stage": {"type": "string", "nullable": True},
                                    "reset": {"type": "boolean"},
                                    "dry_run": {"type": "boolean"},
                                },
                                "required": ["issue"],
                            }
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "OK",
                        "content": {"application/json": {"schema": cli_output_response}},
                    },
                },
            }
        },
        "/test-plan": {
            "post": {
                "summary": "Run `coord test-plan` against the canonical DB (#851)",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "assignment_id": {"type": "string"},
                                    "refresh": {"type": "boolean"},
                                    "model": {"type": "string"},
                                },
                                "required": ["assignment_id"],
                            }
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "OK",
                        "content": {"application/json": {"schema": cli_output_response}},
                    },
                },
            }
        },
        "/housekeeping": {
            "post": {
                "summary": "Archive stale terminal board rows (#762)",
                "requestBody": {
                    "required": False,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {"dry_run": {"type": "boolean"}},
                            }
                        }
                    },
                },
                "responses": {
                    "200": {"description": "OK"},
                    "503": {"description": "Housekeeping failed"},
                },
            }
        },
    }
    return build_spec(
        title="coord serve",
        version=__version__,
        description=(
            "Portable control-center daemon: fronts the coordinator board over "
            "Tailscale so a thin client needs no local coord.db/coordinator.yml. "
            "Every endpoint except /healthz requires `Authorization: Bearer "
            "<token>` when the daemon is configured with one."
        ),
        paths=paths,
        components=components,
    )


def build_app(store: CoordStore, config: Config, *, token: str | None = None) -> Starlette:
    """Build the read-only control-center Starlette app bound to *store* + *config*.

    *token* — when set, every endpoint except ``/healthz`` requires
    ``Authorization: Bearer <token>``.
    """
    # #1081: track the backing coordinator.yml's mtime so the handlers below
    # can swap in a freshly-reloaded Config when it changes on disk, instead
    # of enforcing whatever was current at process startup until a restart.
    # A bare name reassignment is atomic w.r.t. cooperative asyncio scheduling
    # (no `await` inside `_refresh_config`), so concurrent in-flight requests
    # never see a half-swapped config.
    try:
        _config_mtime = config.path.stat().st_mtime if config.path is not None else None
    except OSError:
        _config_mtime = None

    def _refresh_config() -> None:
        nonlocal config, _config_mtime
        config, _config_mtime = _reload_config_if_stale(config, _config_mtime)

    async def healthz(request: Request) -> JSONResponse:  # noqa: ARG001
        return JSONResponse({"status": "ok", "schema_version": SCHEMA_VERSION})

    async def board(request: Request) -> Response:  # noqa: ARG001
        # #1081: pick up a hand-edited coordinator.yml before computing the
        # merge plan / staging / stage-projection below, all of which read
        # `config` for real gating decisions (reviews.enabled, default_gates,
        # require_plan, merge/milestone auto-* flags via config.repo(...)).
        _refresh_config()
        try:
            projection = store.board_projection()
        except Exception as e:  # noqa: BLE001 — surface a clean 503 rather than a stack trace
            return JSONResponse(
                {"error": "board read failed", "detail": str(e)}, status_code=503
            )
        # #776/#778/#550: inject server-side merge plan (ordered, gate-annotated),
        # staging section, and per-issue stage/gate projection so thin clients get
        # status + reason without re-implementing gate logic. All three are derived
        # from the same board snapshot + CI store, built once here and shared below
        # so a concurrent DB write can't split them across two snapshots and
        # `list_checks_for_pr` (a real `gh` round trip) isn't paid twice per
        # request. Computed after the projection so a plan failure never 503s the
        # board.
        _board = None
        _ci = None
        try:
            from coord import merge_queue as _mq  # noqa: PLC0415
            from coord.ci_store import build_ci_store as _build_ci_store  # noqa: PLC0415
            from coord.state import build_board as _build_board  # noqa: PLC0415
            from dataclasses import asdict as _asdict  # noqa: PLC0415
            _board = _build_board()
            # Build ci_store so "CI running" / "CI failed" reasons appear in the
            # plan.  Fail-open: a construction error returns None which disables
            # the CI gate without blanking the whole plan.
            try:
                _ci = _build_ci_store(config.ci_store.type)
            except Exception:  # noqa: BLE001
                _ci = None
            projection["merge_plan"] = [
                _asdict(pm) for pm in _mq.plan(_board, config, ci_store=_ci)
            ]
            # #778: staging section — approved/done work not yet in the queue.
            # Reuses the same _board snapshot built above.  Fail-open: any
            # error returns an empty list rather than 503ing the board.
            try:
                projection["merge_staging"] = [
                    _asdict(si) for si in _mq.staging_items(_board, config)
                ]
            except Exception:  # noqa: BLE001
                projection["merge_staging"] = []
        except Exception:  # noqa: BLE001 — plan failure must not blank the board
            projection["merge_plan"] = []
            projection["merge_staging"] = []
        # #550: server-computed per-issue stage/gate projection — generalizes
        # the #776/#778 pattern to coord-tui's `pipeline.rs` stage-status
        # functions.  Reuses the `_board`/`_ci` snapshot built above; only
        # falls back to a fresh `build_board()` if that block above failed
        # before reaching it (e.g. a DB error), so the common case never
        # double-builds the board or double-fetches CI checks.  Fail-open:
        # an error returns an empty list rather than 503ing the board.
        try:
            from coord import stage_projection as _sp  # noqa: PLC0415
            from coord.merge_queue import load_queue as _load_queue  # noqa: PLC0415

            if _board is None:
                from coord.state import build_board as _build_board2  # noqa: PLC0415
                _board = _build_board2()
            projection["issue_stage_projection"] = _sp.compute_board_stage_projection(
                issues=projection.get("issues", []),
                assignments=list(_board.active) + list(_board.completed),
                merge_queue_items=_load_queue(),
                default_gates=list(config.pipeline.default_gates),
                require_plan=bool(config.dispatch.require_plan),
                ci_store=_ci,
            )
        except Exception:  # noqa: BLE001 — projection failure must not blank the board
            projection["issue_stage_projection"] = []
        # #795 Phase 3b: per-milestone work-order rank + ready frontier.
        # Parsed from each tracking issue's (label="epic") `## Work order`
        # block using coord.milestone_order (Phase 0); the TUI renders rank,
        # next-up, and blocked-on badges on Pipeline milestone cards without
        # re-implementing the DAG logic in Rust.  Fail-open: any per-milestone
        # error produces an empty node list, not a 503.
        try:
            from coord.milestone_order import (  # noqa: PLC0415
                TRACKING_ISSUE_LABEL as _TRACKING_LABEL,
                parse_work_order as _parse_wo,
                ready_frontier as _ready_frontier,
            )

            if _board is None:
                try:
                    from coord.state import build_board as _build_board3  # noqa: PLC0415
                    _board = _build_board3()
                except Exception:  # noqa: BLE001 — e.g. thread-safety on test in-memory DB
                    from coord.models import Board as _Board  # noqa: PLC0415
                    _board = _Board()  # fallback: empty board → no claim blocking

            # Build an open-issue-number set per repo for terminal detection.
            # Issues absent from this set (missing entirely or state='closed')
            # are treated as terminal — mirrors the Rust DAG view's semantics.
            _open_by_repo: dict[str, set[int]] = {}
            for _oi in projection.get("issues", []):
                if _oi.get("state") == "open":
                    _rn = _oi.get("repo_name", "")
                    if _rn:
                        _open_by_repo.setdefault(_rn, set()).add(_oi["number"])

            _milestone_work_orders: list[dict] = []
            for _ti in projection.get("issues", []):
                # Only process tracking issues (carry the "epic" label).
                _labels = _ti.get("labels") or []
                if _TRACKING_LABEL not in _labels:
                    continue
                _repo_name = _ti.get("repo_name", "")
                if not _repo_name:
                    continue
                _body = _ti.get("body") or ""
                try:
                    _wo = _parse_wo(_body)
                except Exception:  # noqa: BLE001 — bad work order: skip this tracking issue
                    continue
                if not _wo.nodes:
                    continue

                # terminal = in work order but NOT currently open for this repo.
                _open_nums = _open_by_repo.get(_repo_name, set())
                _terminal: set[int] = {
                    n.issue_number for n in _wo.nodes
                    if n.issue_number not in _open_nums
                }

                # Resolve coord-local repo → GitHub slug from config.
                _repo_cfg = config.repo(_repo_name)
                _repo_github = _repo_cfg.github if _repo_cfg is not None else _repo_name

                # Compute frontier: board-only claim check (no remote branch
                # lookup) to keep the /board endpoint fast.
                try:
                    _frontier = _ready_frontier(
                        _wo,
                        _board,
                        repo_name=_repo_name,
                        repo_github=_repo_github,
                        terminal_issues=_terminal,
                        branch_lookup=lambda _r, _i: [],  # skip slow gh call
                    )
                except Exception:  # noqa: BLE001
                    # Fallback: mark nodes ready iff all after-deps are terminal.
                    from coord.milestone_order import FrontierEntry as _FE, Frontier as _Fr  # noqa: PLC0415
                    _ready_list = [
                        _FE(n.issue_number, n.group)
                        for n in _wo.nodes
                        if n.issue_number not in _terminal
                        and all(d in _terminal for d in n.after)
                    ]
                    _frontier = _Fr(ready=tuple(_ready_list), blocked=())

                _ready_nums = {fe.issue_number for fe in _frontier.ready}
                _blocked_by_num = {bn.issue_number: bn for bn in _frontier.blocked}

                _nodes = []
                for _rank, _node in enumerate(_wo.nodes):
                    if _node.issue_number in _terminal:
                        continue  # done — skip from projection
                    _is_next_up = _node.issue_number in _ready_nums
                    _bn = _blocked_by_num.get(_node.issue_number)
                    if _is_next_up:
                        # In frontier.ready: deps met, unclaimed, uncontested
                        # — the dispatcher's next candidate for this milestone.
                        _is_ready = True
                        _blocked_on: list[int] = []
                    elif _bn is not None and not _bn.waiting_on_deps:
                        # In frontier.blocked, but NOT for unmet deps — an
                        # active claim (assignment/branch elsewhere) or a
                        # conflict-checker hit. Deps ARE satisfied, so this
                        # is "ready" in the dependency sense, just not the
                        # next thing to dispatch (#795 review: previously
                        # this fell through to the "waiting on deps" branch
                        # below with an empty `_node.after` remainder,
                        # producing a dangling blocked_on with nothing to
                        # point at — distinguish it instead of reporting a
                        # phantom dependency).
                        _is_ready = True
                        _blocked_on = []
                    elif _bn is not None:
                        # In frontier.blocked, waiting on unmet deps.
                        _is_ready = False
                        _blocked_on = list(_bn.waiting_on_deps)
                    else:
                        # `ready_frontier` raised and we fell back to
                        # unmet-deps-only (see except-block above) — no
                        # claim/conflict info is available in the fallback.
                        _blocked_on = [d for d in _node.after if d not in _terminal]
                        _is_ready = not _blocked_on
                    _nodes.append({
                        "issue_number": _node.issue_number,
                        "rank": _rank,
                        "ready": _is_ready,
                        "next_up": _is_next_up,  # ready + unclaimed = next-up
                        "blocked_on": _blocked_on,
                    })

                if _nodes:
                    _milestone_work_orders.append({
                        "repo_name": _repo_name,
                        "tracking_issue": _ti["number"],
                        "milestone_title": _ti.get("milestone_title") or "",
                        "nodes": _nodes,
                    })

            projection["milestone_work_orders"] = _milestone_work_orders
        except Exception:  # noqa: BLE001 — work-order failure must not blank the board
            projection["milestone_work_orders"] = []
        # #975: milestone plan-roster — reuse coord.plans.aggregate_repo_plans
        # server-side so the Plans TUI panel gets one row per milestone/epic
        # (ready / blocked / in-flight / done counts, needs_you attention
        # signals) without shelling out from the client. Sourced from the same
        # projection["issues"] + build_board() snapshot as milestone_work_orders
        # above — no extra `gh` round trip. Fail-open: any error produces an
        # empty list rather than 503ing the board.
        # #976: always stamp the capability flag — even a daemon that hits the
        # per-repo `except` below (or a downstream error) still *supports*
        # plan-roster; only its computation failed this tick. Without this,
        # the TUI can't tell "genuinely zero milestones" apart from "daemon
        # predates #975/#976 and never sends `plan_roster` at all" — both
        # rendered as an identical, silent "0 plans" empty state (the #976
        # review finding). A pre-#975 daemon never runs this line, so the
        # field is simply absent from its JSON and the client's
        # `#[serde(default)]` leaves `plan_roster_supported` false.
        projection["plan_roster_supported"] = True
        try:
            from coord.plans import aggregate_repo_plans as _aggregate_repo_plans  # noqa: PLC0415

            if _board is None:
                try:
                    from coord.state import build_board as _build_board4  # noqa: PLC0415
                    _board = _build_board4()
                except Exception:  # noqa: BLE001 — e.g. thread-safety on test in-memory DB
                    from coord.models import Board as _Board2  # noqa: PLC0415
                    _board = _Board2()

            # Group issues by coord-local repo, converting the DAO wire shape
            # (labels: list[str], flat milestone_number/title) to the dict
            # shape coord.plans expects (labels: [{"name": ...}], nested
            # milestone). Only open issues participate — closed epics are
            # collected separately below so a milestone whose tracking epic
            # was closed still resolves via #974's closed_tracking_issues arg.
            _repo_open_issues: dict[str, list[dict]] = {}
            _repo_closed_epics: dict[str, list[dict]] = {}
            _repo_milestones: dict[str, dict[int, dict]] = {}
            for _oi in projection.get("issues", []):
                _rn = _oi.get("repo_name", "")
                if not _rn:
                    continue
                _label_names = _oi.get("labels") or []
                _adapted = {
                    "number": _oi.get("number"),
                    "title": _oi.get("title", ""),
                    "body": _oi.get("body") or "",
                    "state": _oi.get("state"),
                    "labels": [{"name": name} for name in _label_names],
                    "milestone": (
                        {
                            "number": _oi.get("milestone_number"),
                            "title": _oi.get("milestone_title") or "",
                        }
                        if _oi.get("milestone_number") is not None
                        else None
                    ),
                }
                if _oi.get("state") == "open":
                    _repo_open_issues.setdefault(_rn, []).append(_adapted)
                    _ms_num = _oi.get("milestone_number")
                    if _ms_num is not None:
                        _repo_milestones.setdefault(_rn, {}).setdefault(
                            _ms_num,
                            {
                                "number": _ms_num,
                                "title": _oi.get("milestone_title") or f"Milestone #{_ms_num}",
                            },
                        )
                elif "epic" in _label_names:
                    # A closed epic — feed into closed_tracking_issues so
                    # milestones whose tracking issue was tidied up still
                    # resolve (mirrors coord/plans.py's #974 fix). Also seed
                    # _repo_milestones from the epic's own milestone_number:
                    # if every issue under a milestone (epic included) is now
                    # closed, no *open* issue would otherwise register the
                    # milestone, and the outer aggregation loop below would
                    # never visit it at all — silently dropping a
                    # finished-but-still-open-on-GitHub milestone from the
                    # roster instead of surfacing it as done.
                    _repo_closed_epics.setdefault(_rn, []).append(_adapted)
                    _ms_num = _oi.get("milestone_number")
                    if _ms_num is not None:
                        _repo_milestones.setdefault(_rn, {}).setdefault(
                            _ms_num,
                            {
                                "number": _ms_num,
                                "title": _oi.get("milestone_title") or f"Milestone #{_ms_num}",
                            },
                        )

            _plan_roster: list[dict] = []
            for _repo_name, _milestones_by_num in _repo_milestones.items():
                _repo_cfg2 = config.repo(_repo_name)
                _repo_gh = _repo_cfg2.github if _repo_cfg2 is not None else _repo_name
                _milestones_list = [
                    _milestones_by_num[_k] for _k in sorted(_milestones_by_num.keys())
                ]
                try:
                    _entries = _aggregate_repo_plans(
                        repo_name=_repo_name,
                        repo_github=_repo_gh,
                        milestones=_milestones_list,
                        open_issues=_repo_open_issues.get(_repo_name, []),
                        board=_board,
                        closed_tracking_issues=_repo_closed_epics.get(_repo_name, []),
                    )
                except Exception:  # noqa: BLE001 — per-repo fail-open
                    continue
                for _entry in _entries:
                    _plan_roster.append(_entry.to_dict())
            projection["plan_roster"] = _plan_roster
        except Exception:  # noqa: BLE001 — plan-roster failure must not blank the board
            projection["plan_roster"] = []
        # #978: GOAL.md pinned north-star header for the Plans panel. Fail-open
        # to {"available": False} — a packaged/PyPI install has no repo root to
        # read GOAL.md from (see coord/goal.py's `_resolve_goal_md_path`), and a
        # parse failure must not blank the board.
        try:
            from coord.goal import read_goal_header as _read_goal_header  # noqa: PLC0415
            projection["goal_header"] = _read_goal_header()
        except Exception:  # noqa: BLE001 — goal-header failure must not blank the board
            projection["goal_header"] = {"available": False}
        return JSONResponse(projection)

    async def serve_config(request: Request) -> Response:  # noqa: ARG001
        # Serve the raw coordinator.yml text the daemon owns; the client caches
        # it and feeds it to the existing coord.config.load() parser (config.py
        # has no dict round-trip, so raw YAML is the lossless contract).
        path = config.path
        if path is None or not path.exists():
            return JSONResponse(
                {"error": "no config file on the daemon host"}, status_code=404
            )
        return PlainTextResponse(path.read_text(), media_type="application/x-yaml")

    async def post_result(request: Request) -> Response:
        # #590: record an interactive result against the shared DB. Reconstruct
        # the ResultRecord from JSON (dropping unknown keys so a newer client
        # can't break an older daemon) and run the LOCAL seam path.
        from coord import issue_store  # noqa: PLC0415

        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        known = {f.name for f in fields(issue_store.ResultRecord)}
        try:
            record = issue_store.ResultRecord(
                **{k: v for k, v in body.items() if k in known}
            )
        except TypeError as e:
            return JSONResponse({"error": f"bad record: {e}"}, status_code=400)
        try:
            outcome = issue_store._post_result_local(record)
        except ValueError as e:  # invalid status / verdict
            return JSONResponse({"error": str(e)}, status_code=400)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "result write failed", "detail": str(e)}, status_code=503
            )
        return JSONResponse(asdict(outcome))

    async def post_completion(request: Request) -> Response:
        # #590: record a git-floor backstop completion against the shared DB.
        from coord import issue_store  # noqa: PLC0415

        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        known = {f.name for f in fields(issue_store.CompletionRecord)}
        try:
            record = issue_store.CompletionRecord(
                **{k: v for k, v in body.items() if k in known}
            )
        except TypeError as e:
            return JSONResponse({"error": f"bad record: {e}"}, status_code=400)
        try:
            outcome = issue_store._post_completion_local(record)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "completion write failed", "detail": str(e)},
                status_code=503,
            )
        return JSONResponse(asdict(outcome))

    async def _read_json(request: Request) -> dict | None:
        try:
            data = await request.json()
        except Exception:  # noqa: BLE001
            return None
        return data if isinstance(data, dict) else None

    def _kwargs(cls, data: dict) -> dict:
        known = {f.name for f in fields(cls)}
        return {k: v for k, v in data.items() if k in known}

    async def post_dispatched_work(request: Request) -> Response:
        # #590 Phase 2: record a thin client's work dispatch on the shared DB.
        from coord import state  # noqa: PLC0415
        from coord.models import Proposal  # noqa: PLC0415

        body = await _read_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        try:
            proposal = Proposal(**_kwargs(Proposal, body.get("proposal") or {}))
            state._record_dispatched_local(
                assignment_id=body["assignment_id"],
                proposal=proposal,
                repo_github=body["repo_github"],
                provider_name=body.get("provider_name"),
            )
        except (TypeError, KeyError) as e:
            return JSONResponse({"error": f"bad dispatch: {e}"}, status_code=400)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "dispatch write failed", "detail": str(e)}, status_code=503
            )
        return JSONResponse({"ok": True})

    async def post_milestone_drain(request: Request) -> Response:
        # #769 Phase 1: register a thin client's `coord milestone dispatch`
        # for daemon auto-drain on the shared DB. Mirrors post_dispatched_work.
        from coord import state  # noqa: PLC0415

        body = await _read_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        try:
            state._register_milestone_drain_local(
                repo_name=body["repo_name"],
                tracking_issue=int(body["tracking_issue"]),
            )
        except (TypeError, KeyError, ValueError) as e:
            return JSONResponse({"error": f"bad milestone-drain: {e}"}, status_code=400)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "milestone-drain write failed", "detail": str(e)},
                status_code=503,
            )
        return JSONResponse({"ok": True})

    async def post_dispatched(request: Request) -> Response:
        # #590 Phase 2: record a thin client's review/fix/rework/merge dispatch.
        from coord import state  # noqa: PLC0415
        from coord.models import Assignment  # noqa: PLC0415

        body = await _read_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        try:
            assignment = Assignment(**_kwargs(Assignment, body.get("assignment") or {}))
            state._record_dispatched_assignment_local(
                assignment=assignment, repo_github=body["repo_github"]
            )
        except (TypeError, KeyError) as e:
            return JSONResponse({"error": f"bad dispatch: {e}"}, status_code=400)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "dispatch write failed", "detail": str(e)}, status_code=503
            )
        return JSONResponse({"ok": True})

    async def post_test_verdict(request: Request) -> Response:
        # #590 Phase 2: record a Test-gate verdict on the shared DB.
        from coord import state  # noqa: PLC0415

        body = await _read_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        try:
            state._record_test_verdict_local(
                assignment_id=body["assignment_id"],
                test_state=body["test_state"],
                test_reason=body.get("test_reason"),
                smoke_test=body.get("smoke_test"),
                smoke_test_reason=body.get("smoke_test_reason"),
            )
        except KeyError as e:
            return JSONResponse({"error": f"missing field: {e}"}, status_code=400)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "test-verdict write failed", "detail": str(e)},
                status_code=503,
            )
        return JSONResponse({"ok": True})

    async def post_acceptance_verdict(request: Request) -> Response:
        # #944: record an Acceptance-gate verdict (oracle loop) on the shared
        # DB. Mirrors post_test_verdict.
        from coord import state  # noqa: PLC0415

        body = await _read_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        try:
            state._record_acceptance_verdict_local(
                assignment_id=body["assignment_id"],
                acceptance_state=body["acceptance_state"],
                acceptance_reason=body.get("acceptance_reason"),
                acceptance_sha=body.get("acceptance_sha"),
                acceptance_total=body.get("acceptance_total"),
                acceptance_passed=body.get("acceptance_passed"),
            )
        except KeyError as e:
            return JSONResponse({"error": f"missing field: {e}"}, status_code=400)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "acceptance-verdict write failed", "detail": str(e)},
                status_code=503,
            )
        return JSONResponse({"ok": True})

    async def post_acceptance_record(request: Request) -> Response:
        # #944: the canonical board + the repo checkouts live on THIS host, so
        # a thin client's `coord acceptance record` (the external trust-gate
        # re-run) routes the whole command here. Run it in a threadpool (it
        # shells out to git + the driver's test command). Mirrors post_diagnose.
        from starlette.concurrency import run_in_threadpool  # noqa: PLC0415

        body = await _read_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)

        def _run() -> dict:
            import contextlib  # noqa: PLC0415
            import io  # noqa: PLC0415
            import os  # noqa: PLC0415

            from coord.commands.acceptance import acceptance_record  # noqa: PLC0415

            buf = io.StringIO()
            code = 0
            err = None
            prev = os.environ.get("COORD_ACCEPTANCE_ON_DAEMON")
            os.environ["COORD_ACCEPTANCE_ON_DAEMON"] = "1"  # guard against re-routing
            try:
                with contextlib.redirect_stdout(buf):
                    acceptance_record.callback(
                        repo=body.get("repo"),
                        issue_number=int(body.get("issue")),
                        sha=body.get("sha"),
                        config_path=config.path,
                    )
            except SystemExit as e:  # click commands sys.exit() on some paths
                code = e.code if isinstance(e.code, int) else (1 if e.code else 0)
            except Exception as e:  # noqa: BLE001
                err = str(e)
                code = 1
            finally:
                if prev is None:
                    os.environ.pop("COORD_ACCEPTANCE_ON_DAEMON", None)
                else:
                    os.environ["COORD_ACCEPTANCE_ON_DAEMON"] = prev
            return {"output": buf.getvalue(), "exit_code": code, "error": err}

        result = await run_in_threadpool(_run)
        return JSONResponse(result)

    async def post_review_findings(request: Request) -> Response:
        # #905: persist parsed review verdict+body on the daemon's DB so
        # post_orphaned_review_findings on a thin client reaches the shared DB.
        from coord import state  # noqa: PLC0415

        body = await _read_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        try:
            state._update_assignment_review_findings_local(
                body["assignment_id"],
                verdict=body["verdict"],
                body=body["body"],
            )
        except KeyError as e:
            return JSONResponse({"error": f"missing field: {e}"}, status_code=400)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "review-findings write failed", "detail": str(e)},
                status_code=503,
            )
        return JSONResponse({"ok": True})

    async def post_review_posted(request: Request) -> Response:
        # #905: mark a review assignment as posted (sets review_posted_at) on the
        # daemon's DB so thin-client notify runs correctly.
        from coord import state  # noqa: PLC0415

        body = await _read_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        try:
            state._mark_review_posted_local(body["assignment_id"])
        except KeyError as e:
            return JSONResponse({"error": f"missing field: {e}"}, status_code=400)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "review-posted write failed", "detail": str(e)},
                status_code=503,
            )
        return JSONResponse({"ok": True})

    async def post_needs_attention_notified(request: Request) -> Response:
        # #846 review: mark the one-shot "needs attention" ledger entry on
        # the daemon's DB so `coord acceptance stall`'s self-report is a
        # true one-shot from a thin client too — otherwise the wall-clock
        # backstop (coord.notify.detect_needs_attention) stays eligible to
        # flag the same assignment again later.
        from coord import state  # noqa: PLC0415

        body = await _read_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        try:
            state._mark_needs_attention_notified_local(body["assignment_id"])
        except KeyError as e:
            return JSONResponse({"error": f"missing field: {e}"}, status_code=400)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "needs-attention-notified write failed", "detail": str(e)},
                status_code=503,
            )
        return JSONResponse({"ok": True})

    async def post_board(request: Request) -> Response:
        # #749: generic whole-board upsert endpoint backing
        # coord.board_service.write_board() for the commands that still
        # read-modify-write the full board locally (assign/approve/stop/retry/
        # resume/bounce/done/pr/…, the dashboard, and auto_loop). save_board()
        # is upsert-only (never deletes rows), so applying a client's full
        # in-memory board here is a safe, non-lossy drop-in for what today
        # runs directly against the local DB.
        from coord import state  # noqa: PLC0415
        from coord.models import Assignment, Board  # noqa: PLC0415

        body = await _read_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        try:
            assignments = [
                Assignment(**_kwargs(Assignment, d))
                for d in body.get("assignments", [])
            ]
            board = Board(
                active=[],
                completed=assignments,
                round_number=int(body.get("round_number") or 0),
            )
            state.save_board(board)
        except (TypeError, KeyError) as e:
            return JSONResponse({"error": f"bad board payload: {e}"}, status_code=400)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "board write failed", "detail": str(e)}, status_code=503
            )
        return JSONResponse({"ok": True})

    async def post_assignment_usage(request: Request) -> Response:
        # #665/#749: route cost/token/is_interactive/smoke_tests writes through
        # the daemon.  Body: {assignment_id, cost_usd?, input_tokens?,
        #        output_tokens?, cache_creation_tokens?, cache_read_tokens?,
        #        is_interactive?, smoke_tests?}
        # One round-trip covers all four update helpers; the daemon calls the
        # _local forms directly so it never recurses back out over HTTP.
        from coord import state  # noqa: PLC0415

        body = await _read_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        aid = body.get("assignment_id")
        if not aid:
            return JSONResponse({"error": "missing assignment_id"}, status_code=400)
        try:
            if "cost_usd" in body and body["cost_usd"] is not None:
                state._update_assignment_cost_local(aid, body["cost_usd"])
            if any(
                k in body
                for k in ("input_tokens", "output_tokens", "cache_creation_tokens", "cache_read_tokens")
            ):
                state._update_assignment_tokens_local(
                    aid,
                    input_tokens=int(body.get("input_tokens") or 0),
                    output_tokens=int(body.get("output_tokens") or 0),
                    cache_creation_tokens=int(body.get("cache_creation_tokens") or 0),
                    cache_read_tokens=int(body.get("cache_read_tokens") or 0),
                )
            if body.get("is_interactive"):
                state._mark_assignment_interactive_local(aid)
            if "smoke_tests" in body and body["smoke_tests"] is not None:
                state._update_assignment_smoke_tests_local(aid, body["smoke_tests"])
            if "completion_summary" in body and body["completion_summary"]:
                state._update_assignment_completion_summary_local(aid, body["completion_summary"])
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "assignment-usage write failed", "detail": str(e)},
                status_code=503,
            )
        return JSONResponse({"ok": True})

    async def post_assignment_session_id(request: Request) -> Response:
        # #906: persist a worker's claude session ID on the daemon's DB so that
        # thin-client chat-continue calls can read it back.  Mirrors the
        # _local form called directly on the daemon host.
        from coord import state  # noqa: PLC0415

        body = await _read_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        try:
            state._update_assignment_claude_session_id_local(
                body["assignment_id"], body["claude_session_id"]
            )
        except KeyError as e:
            return JSONResponse({"error": f"missing field: {e}"}, status_code=400)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "assignment-session-id write failed", "detail": str(e)},
                status_code=503,
            )
        return JSONResponse({"ok": True})

    async def post_assignment_failure_reason(request: Request) -> Response:
        # #906: mark an assignment failed with a reason on the daemon's DB so a
        # thin-client interactive launch failure (e.g. worktree-add) reaches the
        # shared DB and the TUI shows the red-box reason.
        from coord import state  # noqa: PLC0415

        body = await _read_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        try:
            state._set_assignment_failure_reason_local(
                body["assignment_id"], body["reason"]
            )
        except KeyError as e:
            return JSONResponse({"error": f"missing field: {e}"}, status_code=400)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "assignment-failure-reason write failed", "detail": str(e)},
                status_code=503,
            )
        return JSONResponse({"ok": True})

    async def post_assignment_test_plan(request: Request) -> Response:
        # #906: read the cached smoke-test plan from the daemon's DB for a thin
        # client running --smoke-of against a local checkout.  Returns
        # {"test_plan": <raw JSON string or null>}.
        from coord import state  # noqa: PLC0415

        body = await _read_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        aid = body.get("assignment_id")
        if not aid:
            return JSONResponse({"error": "missing assignment_id"}, status_code=400)
        try:
            plan = state._get_test_plan_local(aid)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "assignment-test-plan read failed", "detail": str(e)},
                status_code=503,
            )
        import json as _json  # noqa: PLC0415

        return JSONResponse({"test_plan": _json.dumps(plan) if plan is not None else None})

    async def post_notify(request: Request) -> Response:
        # #906: run `coord notify` on the canonical DB + agent fleet so a thin
        # client's `coord notify` reaches the real assignments/notifications rather
        # than the empty local DB.  Mirrors post_merge/post_reconcile_merges.
        # COORD_NOTIFY_ON_DAEMON guards the daemon against re-routing to itself.
        from starlette.concurrency import run_in_threadpool  # noqa: PLC0415

        def _run() -> dict:
            import contextlib  # noqa: PLC0415
            import io  # noqa: PLC0415
            import os  # noqa: PLC0415

            from coord.cli import notify as notify_cmd  # noqa: PLC0415

            buf = io.StringIO()
            code = 0
            err = None
            prev = os.environ.get("COORD_NOTIFY_ON_DAEMON")
            os.environ["COORD_NOTIFY_ON_DAEMON"] = "1"
            try:
                with contextlib.redirect_stdout(buf):
                    notify_cmd.callback(config_path=config.path)
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else (1 if e.code else 0)
            except Exception as e:  # noqa: BLE001
                err = str(e)
                code = 1
            finally:
                if prev is None:
                    os.environ.pop("COORD_NOTIFY_ON_DAEMON", None)
                else:
                    os.environ["COORD_NOTIFY_ON_DAEMON"] = prev
            return {"output": buf.getvalue(), "exit_code": code, "error": err}

        result = await run_in_threadpool(_run)
        return JSONResponse(result)

    async def post_issue_test_mode(request: Request) -> Response:
        # #906: read the cached test-mode label (test-mode:auto/test-mode:smoke)
        # for an issue from the daemon's canonical `issues` table, so a thin
        # client's `coord resume` -> reconcile() smoke-auto-queue gate sees the
        # real per-issue policy instead of None from an empty local DB.
        from coord import state  # noqa: PLC0415

        body = await _read_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        repo_name = body.get("repo_name")
        issue_number = body.get("issue_number")
        if not repo_name or issue_number is None:
            return JSONResponse(
                {"error": "missing repo_name or issue_number"}, status_code=400
            )
        try:
            test_mode = state._get_issue_test_mode_local(repo_name, int(issue_number))
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "issue-test-mode read failed", "detail": str(e)},
                status_code=503,
            )
        return JSONResponse({"test_mode": test_mode})

    async def post_issue_labels(request: Request) -> Response:
        # #601: update one issue's cached labels (coord ready/backlog/refine/track).
        from coord import state  # noqa: PLC0415

        body = await _read_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        try:
            updated = state._update_issue_labels_local(
                body["repo_name"], body["issue_number"], body.get("labels") or []
            )
        except KeyError as e:
            return JSONResponse({"error": f"missing field: {e}"}, status_code=400)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "issue-labels write failed", "detail": str(e)}, status_code=503
            )
        return JSONResponse({"updated": bool(updated)})

    async def post_issues_sync(request: Request) -> Response:
        # #601: upsert a repo's open issues into the shared issue cache (coord sync).
        from coord import state  # noqa: PLC0415

        body = await _read_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        try:
            state._upsert_open_issues_local(body["repo_name"], body.get("issues") or [])
        except KeyError as e:
            return JSONResponse({"error": f"missing field: {e}"}, status_code=400)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "issues-sync write failed", "detail": str(e)}, status_code=503
            )
        return JSONResponse({"ok": True})

    async def post_issue_edit(request: Request) -> Response:
        # Edit an issue's title/body through the tracker seam (the backend write
        # — GitHub via gh today — runs HERE on the daemon, not the client, so the
        # tracker stays behind one seam for GitLab / bare-DB later).
        from coord import state  # noqa: PLC0415

        body = await _read_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        try:
            updated = state._edit_issue_content_local(
                body["repo_name"],
                body["issue_number"],
                title=body.get("title"),
                body=body.get("body"),
                repo_github=body.get("repo_github"),
            )
        except KeyError as e:
            return JSONResponse({"error": f"missing field: {e}"}, status_code=400)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "issue-edit write failed", "detail": str(e)}, status_code=503
            )
        return JSONResponse({"updated": bool(updated)})

    async def post_milestone_edit(request: Request) -> Response:
        # #645: create/edit a GitHub milestone through the tracker seam (the
        # backend write — GitHub via gh today — runs HERE on the daemon, not
        # the client, mirroring /issue-edit). number=None creates a new
        # milestone; number=<int> edits an existing one.
        from coord import state  # noqa: PLC0415

        body = await _read_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        try:
            result = state._write_milestone_local(
                body["repo_name"],
                number=body.get("number"),
                title=body.get("title"),
                description=body.get("description"),
                due_on=body.get("due_on"),
                repo_github=body.get("repo_github"),
            )
        except KeyError as e:
            return JSONResponse({"error": f"missing field: {e}"}, status_code=400)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "milestone-edit write failed", "detail": str(e)},
                status_code=503,
            )
        return JSONResponse(result)

    async def post_issue_milestone(request: Request) -> Response:
        # #967: assign a milestone to an issue through the tracker seam.
        # The actual gh call runs HERE on the daemon; the client sends
        # (repo_name, issue_number, milestone_number, milestone_title?,
        # repo_github?) and gets back {"updated": true}.
        from coord import state  # noqa: PLC0415

        body = await _read_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        try:
            _assign = state._assign_issue_milestone_local(
                body["repo_name"],
                body["issue_number"],
                body["milestone_number"],
                milestone_title=body.get("milestone_title"),
                repo_github=body.get("repo_github"),
            )
        except KeyError as e:
            return JSONResponse({"error": f"missing field: {e}"}, status_code=400)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "issue-milestone write failed", "detail": str(e)},
                status_code=503,
            )
        return JSONResponse({"updated": True})

    async def post_issue_milestone_remove(request: Request) -> Response:
        # #1003: clear an issue's milestone through the tracker seam — the
        # counterpart to /issue-milestone. Client sends (repo_name,
        # issue_number, repo_github?) and gets back {"updated": true}.
        from coord import state  # noqa: PLC0415

        body = await _read_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        try:
            state._unassign_issue_milestone_local(
                body["repo_name"],
                body["issue_number"],
                repo_github=body.get("repo_github"),
            )
        except KeyError as e:
            return JSONResponse({"error": f"missing field: {e}"}, status_code=400)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "issue-milestone-remove write failed", "detail": str(e)},
                status_code=503,
            )
        return JSONResponse({"updated": True})

    async def post_issue_close(request: Request) -> Response:
        # #1003: close an issue (optionally posting a comment first) through
        # the tracker seam — the "Close / archive plan" Plans-panel action's
        # backend, mirroring /issue-edit. Client sends (repo_name,
        # issue_number, comment?, repo_github?) and gets back {"updated": true}.
        from coord import state  # noqa: PLC0415

        body = await _read_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        try:
            state._close_issue_local(
                body["repo_name"],
                body["issue_number"],
                comment=body.get("comment"),
                repo_github=body.get("repo_github"),
            )
        except KeyError as e:
            return JSONResponse({"error": f"missing field: {e}"}, status_code=400)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "issue-close write failed", "detail": str(e)},
                status_code=503,
            )
        return JSONResponse({"updated": True})

    async def post_issue_label(request: Request) -> Response:
        # #802: generic add/remove of arbitrary labels through the seam.
        # The actual gh call runs HERE on the daemon so the tracker stays
        # behind one seam; the client just sends (repo_name, issue_number,
        # add[], remove[]) and gets back (labels[], changed).
        from coord import state  # noqa: PLC0415

        body = await _read_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        try:
            new_labels, changed = state._apply_issue_labels_local(
                body["repo_name"],
                body["issue_number"],
                add=set(body.get("add") or []),
                remove=set(body.get("remove") or []),
                repo_github=body.get("repo_github"),
            )
        except KeyError as e:
            return JSONResponse({"error": f"missing field: {e}"}, status_code=400)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "issue-label write failed", "detail": str(e)},
                status_code=503,
            )
        return JSONResponse({"labels": new_labels, "changed": changed})

    async def post_issue_create(request: Request) -> Response:
        # #802: create a new GitHub issue through the seam. The actual gh
        # call runs HERE on the daemon; the client sends (repo_name, title,
        # body, labels[], repo_github) and gets back {"number": N, "url": "..."}.
        from coord import state  # noqa: PLC0415

        body = await _read_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        try:
            result = state._create_issue_local(
                body["repo_name"],
                body["title"],
                body.get("body") or "",
                labels=body.get("labels") or [],
                repo_github=body.get("repo_github"),
            )
        except KeyError as e:
            return JSONResponse({"error": f"missing field: {e}"}, status_code=400)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "issue-create failed", "detail": str(e)},
                status_code=503,
            )
        return JSONResponse(result)

    async def get_issue_context(request: Request) -> Response:
        # #603: read an issue's raw context entries (oldest-first) for the
        # briefing read-path / `coord context show` on a thin client.
        from coord import state  # noqa: PLC0415

        repo_name = request.query_params.get("repo_name")
        raw_issue = request.query_params.get("issue_number")
        if not repo_name or raw_issue is None:
            return JSONResponse(
                {"error": "repo_name and issue_number are required"}, status_code=400
            )
        try:
            issue_number = int(raw_issue)
        except (TypeError, ValueError):
            return JSONResponse({"error": "issue_number must be an int"}, status_code=400)
        try:
            entries = state._list_issue_context_local(repo_name, issue_number)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "issue-context read failed", "detail": str(e)}, status_code=503
            )
        return JSONResponse({"entries": entries})

    async def post_issue_context(request: Request) -> Response:
        # #603: add / pin / clear a per-issue context entry on the shared DB.
        from coord import state  # noqa: PLC0415

        body = await _read_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        action = body.get("action")
        try:
            if action == "add":
                entry_id = state._add_issue_context_entry_local(
                    body["repo_name"],
                    body["issue_number"],
                    body["body"],
                    pinned=bool(body.get("pinned")),
                    source=body.get("source"),
                )
                return JSONResponse({"entry_id": entry_id})
            if action == "pin":
                updated = state._set_issue_context_pin_local(
                    body["repo_name"],
                    body["issue_number"],
                    body["entry_id"],
                    bool(body.get("pinned")),
                )
                return JSONResponse({"updated": bool(updated)})
            if action == "clear":
                deleted = state._clear_issue_context_local(
                    body["repo_name"], body["issue_number"]
                )
                return JSONResponse({"deleted": deleted})
            if action == "replace":
                state._replace_issue_context_local(
                    body["repo_name"], body["issue_number"], body.get("entries") or []
                )
                return JSONResponse({"ok": True})
        except KeyError as e:
            return JSONResponse({"error": f"missing field: {e}"}, status_code=400)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "issue-context write failed", "detail": str(e)}, status_code=503
            )
        return JSONResponse({"error": f"unknown action: {action!r}"}, status_code=400)

    async def get_audit(request: Request) -> Response:
        # #1037: paginated read over audit_log — deliberately its own endpoint
        # (NOT riding /board, which is a bounded current-state snapshot). Keyset
        # pagination on (ts, id) DESC via `cursor`, not OFFSET, so a growing
        # table stays fast.
        from coord import audit as _audit  # noqa: PLC0415

        qp = request.query_params

        def _int_param(name: str) -> int | None:
            raw = qp.get(name)
            if raw is None or raw == "":
                return None
            return int(raw)

        def _ts_param(name: str) -> float | None:
            """Accept either an epoch number or an ISO-8601 timestamp."""
            raw = qp.get(name)
            if raw is None or raw == "":
                return None
            try:
                return float(raw)
            except ValueError:
                from datetime import datetime  # noqa: PLC0415

                return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()

        try:
            since = _ts_param("since")
            until = _ts_param("until")
            issue = _int_param("issue")
            limit_raw = qp.get("limit")
            limit = int(limit_raw) if limit_raw else _audit.DEFAULT_LIMIT
        except ValueError as e:
            return JSONResponse({"error": f"bad query parameter: {e}"}, status_code=400)

        try:
            result = _audit.query_audit_log(
                since=since,
                until=until,
                event_type=qp.get("type") or None,
                category=qp.get("category") or None,
                repo=qp.get("repo") or None,
                issue=issue,
                assignment_id=qp.get("assignment") or None,
                tier=qp.get("tier") or None,
                limit=limit,
                cursor=qp.get("cursor") or None,
            )
        except Exception as e:  # noqa: BLE001 — surface a clean 503 rather than a stack trace
            return JSONResponse({"error": "audit read failed", "detail": str(e)}, status_code=503)
        return JSONResponse(result)

    async def post_merge(request: Request) -> Response:
        # #584: the merge queue + board live in THIS (canonical) DB, and gh is
        # authenticated here — so a thin client's `coord merge` / TUI 'Go' routes
        # the whole operation here.  Run it in a threadpool so a multi-minute
        # merge (PR creation, CI waits) doesn't block the event loop / other
        # board reads.  Returns the captured CLI output + exit code.
        from starlette.concurrency import run_in_threadpool  # noqa: PLC0415

        body = await _read_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)

        # #732: --drop is a surgical single-row delete; handle it before
        # running the full merge pipeline so it doesn't need to import or
        # invoke the CLI at all.
        drop_aid = body.get("drop")
        if drop_aid:
            from coord import merge_queue as _mq  # noqa: PLC0415

            removed = _mq.drop_entry(str(drop_aid))
            if removed:
                return JSONResponse(
                    {"output": f"merge-queue: dropped entry {drop_aid}\n", "exit_code": 0}
                )
            return JSONResponse(
                {
                    "output": f"merge-queue: no entry found for {drop_aid!r}\n",
                    "exit_code": 1,
                }
            )

        def _run() -> dict:
            import contextlib  # noqa: PLC0415
            import io  # noqa: PLC0415
            import os  # noqa: PLC0415

            from coord.cli import merge as merge_cmd  # noqa: PLC0415

            buf = io.StringIO()
            code = 0
            err = None
            prev = os.environ.get("COORD_MERGE_ON_DAEMON")
            os.environ["COORD_MERGE_ON_DAEMON"] = "1"  # guard against re-routing
            try:
                with contextlib.redirect_stdout(buf):
                    merge_cmd.callback(
                        config_path=config.path,
                        dry_run=bool(body.get("dry_run")),
                        # #684 added --plan/show_plan to the merge command and
                        # routes --plan via /board, so /merge never needs it —
                        # but the callback still *requires* the param.  Pass
                        # False explicitly or the call raises "merge() missing 1
                        # required positional argument: 'show_plan'" and every
                        # daemon-routed merge (thin client, TUI 'Go', headless
                        # drain) crashes before doing anything.
                        show_plan=False,
                        order=body.get("order"),
                        repo_filter=body.get("repo_filter"),
                        method=body.get("method") or "rebase",
                        force_merge=bool(body.get("force_merge")),
                        # #821: daemon always enforces review regardless of any
                        # skip_review flag the client sends.  The gate is
                        # safety-critical and must not be bypassable remotely.
                        skip_review=False,
                        skip_smoke=bool(body.get("skip_smoke")),
                        drop_assignment=None,  # already handled above
                        only_assignment=body.get("only"),  # #780: single-entry merge
                    )
            except SystemExit as e:  # click commands sys.exit() on some paths
                code = e.code if isinstance(e.code, int) else (1 if e.code else 0)
            except Exception as e:  # noqa: BLE001
                err = str(e)
                code = 1
            finally:
                if prev is None:
                    os.environ.pop("COORD_MERGE_ON_DAEMON", None)
                else:
                    os.environ["COORD_MERGE_ON_DAEMON"] = prev
            return {"output": buf.getvalue(), "exit_code": code, "error": err}

        result = await run_in_threadpool(_run)
        return JSONResponse(result)

    async def post_reconcile_merges(request: Request) -> Response:
        # #584: the canonical board + gh live in THIS DB — so a thin client's
        # `coord reconcile-merges` routes the whole operation here instead of
        # sweeping an empty local board.  Run it in a threadpool (the sweep
        # shells out to gh) so it doesn't block the event loop / board reads.
        # Returns the captured CLI output + exit code.
        from starlette.concurrency import run_in_threadpool  # noqa: PLC0415

        body = await _read_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)

        def _run() -> dict:
            import contextlib  # noqa: PLC0415
            import io  # noqa: PLC0415
            import os  # noqa: PLC0415

            from coord.cli import reconcile_merges as reconcile_cmd  # noqa: PLC0415

            buf = io.StringIO()
            code = 0
            err = None
            prev = os.environ.get("COORD_RECONCILE_ON_DAEMON")
            os.environ["COORD_RECONCILE_ON_DAEMON"] = "1"  # guard against re-routing
            try:
                with contextlib.redirect_stdout(buf):
                    reconcile_cmd.callback(
                        config_path=config.path,
                        dry_run=bool(body.get("dry_run")),
                        repo_name=body.get("repo"),
                    )
            except SystemExit as e:  # click commands sys.exit() on some paths
                code = e.code if isinstance(e.code, int) else (1 if e.code else 0)
            except Exception as e:  # noqa: BLE001
                err = str(e)
                code = 1
            finally:
                if prev is None:
                    os.environ.pop("COORD_RECONCILE_ON_DAEMON", None)
                else:
                    os.environ["COORD_RECONCILE_ON_DAEMON"] = prev
            return {"output": buf.getvalue(), "exit_code": code, "error": err}

        result = await run_in_threadpool(_run)
        return JSONResponse(result)

    async def post_diagnose(request: Request) -> Response:
        # #diagnose: the canonical board + gh + fleet ssh live on THIS host, so a
        # thin client's `coord diagnose` (and the TUI "Diagnose & fix stage"
        # action) routes the whole per-stage doctor here.  Run it in a threadpool
        # (it shells out to git/tmux/ssh) so it doesn't block the event loop.
        from starlette.concurrency import run_in_threadpool  # noqa: PLC0415

        body = await _read_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)

        def _run() -> dict:
            import contextlib  # noqa: PLC0415
            import io  # noqa: PLC0415
            import os  # noqa: PLC0415

            from coord.cli import diagnose as diagnose_cmd  # noqa: PLC0415

            buf = io.StringIO()
            code = 0
            err = None
            prev = os.environ.get("COORD_DIAGNOSE_ON_DAEMON")
            os.environ["COORD_DIAGNOSE_ON_DAEMON"] = "1"  # guard against re-routing
            try:
                with contextlib.redirect_stdout(buf):
                    diagnose_cmd.callback(
                        repo=body.get("repo"),
                        issue=int(body.get("issue")),
                        stage=body.get("stage"),
                        reset=bool(body.get("reset")),
                        dry_run=bool(body.get("dry_run")),
                        output_json=bool(body.get("output_json")),  # #935 Part C
                        config_path=config.path,
                        orphan_worktrees=False,  # fleet sweep is local-only
                    )
            except SystemExit as e:  # click commands sys.exit() on some paths
                code = e.code if isinstance(e.code, int) else (1 if e.code else 0)
            except Exception as e:  # noqa: BLE001
                err = str(e)
                code = 1
            finally:
                if prev is None:
                    os.environ.pop("COORD_DIAGNOSE_ON_DAEMON", None)
                else:
                    os.environ["COORD_DIAGNOSE_ON_DAEMON"] = prev
            return {"output": buf.getvalue(), "exit_code": code, "error": err}

        result = await run_in_threadpool(_run)
        return JSONResponse(result)

    async def post_test_plan(request: Request) -> Response:
        # #851: the assignment row + cached test_plan live in THIS (canonical)
        # DB, so a thin client's `coord test-plan` routes the whole command
        # here instead of reporting "not found" against an empty local board.
        # Run it in a threadpool since it shells out to git/gh and may invoke
        # `claude -p`. Mirrors post_diagnose.
        from starlette.concurrency import run_in_threadpool  # noqa: PLC0415

        body = await _read_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)

        def _run() -> dict:
            import contextlib  # noqa: PLC0415
            import io  # noqa: PLC0415
            import os  # noqa: PLC0415

            from coord.cli import test_plan_cmd  # noqa: PLC0415

            buf = io.StringIO()
            code = 0
            err = None
            prev = os.environ.get("COORD_TEST_PLAN_ON_DAEMON")
            os.environ["COORD_TEST_PLAN_ON_DAEMON"] = "1"  # guard against re-routing
            try:
                with contextlib.redirect_stdout(buf):
                    test_plan_cmd.callback(
                        assignment_id=body.get("assignment_id"),
                        refresh=bool(body.get("refresh")),
                        model=body.get("model") or "haiku",
                        config_path=config.path,
                    )
            except SystemExit as e:  # click commands sys.exit() on some paths
                code = e.code if isinstance(e.code, int) else (1 if e.code else 0)
            except Exception as e:  # noqa: BLE001
                err = str(e)
                code = 1
            finally:
                if prev is None:
                    os.environ.pop("COORD_TEST_PLAN_ON_DAEMON", None)
                else:
                    os.environ["COORD_TEST_PLAN_ON_DAEMON"] = prev
            return {"output": buf.getvalue(), "exit_code": code, "error": err}

        result = await run_in_threadpool(_run)
        return JSONResponse(result)

    async def post_housekeeping(request: Request) -> Response:
        # #762: archive stale terminal board rows on the canonical DB.  The CLI
        # (`coord housekeeping`) routes here because the DB lives on the daemon;
        # COORD_HOUSEKEEPING_ON_DAEMON guards the daemon against re-routing to
        # itself (mirrors the reconcile/diagnose pattern).
        from starlette.concurrency import run_in_threadpool  # noqa: PLC0415

        from coord import housekeeping  # noqa: PLC0415

        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        dry_run = bool(body.get("dry_run", False))
        os.environ["COORD_HOUSEKEEPING_ON_DAEMON"] = "1"
        try:
            result = await run_in_threadpool(housekeeping.sweep, dry_run=dry_run)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "housekeeping failed", "detail": str(e)}, status_code=503
            )
        return JSONResponse(result)

    def _lifespan(_app: Starlette):  # noqa: ANN202
        """#625: a dispatch-free passive reconcile tick.

        With the TUI auto-loop off, nothing polled the agents, so a finished
        headless worker (e.g. a `claude -p` plan) left the board — and the TUI
        box — stuck on ``running`` forever.  This polls the local agent(s) on an
        interval and flips agent-completed rows to their terminal status (+
        captures a plan's structured output).  It NEVER dispatches and NEVER
        posts to GitHub — reflecting a termination is passive state and must not
        be able to re-introduce the dispatch flood.

        Interval is ``COORD_RECONCILE_INTERVAL`` seconds (default 30); set it to
        0 to disable the tick entirely.
        """
        import asyncio  # noqa: PLC0415
        import contextlib  # noqa: PLC0415
        import logging  # noqa: PLC0415

        from starlette.concurrency import run_in_threadpool  # noqa: PLC0415

        log = logging.getLogger("coord.serve")
        try:
            interval = float(os.environ.get("COORD_RECONCILE_INTERVAL", "30"))
        except ValueError:
            interval = 30.0

        # #762: archive stale terminal board rows on a much slower cadence than
        # the reconcile tick (default hourly; 0 disables).  Tracked separately so
        # the heavy sweep doesn't run every reconcile interval.
        import time as _time  # noqa: PLC0415

        try:
            housekeeping_interval = float(
                os.environ.get("COORD_HOUSEKEEPING_INTERVAL", "3600")
            )
        except ValueError:
            housekeeping_interval = 3600.0
        last_housekeeping = _time.monotonic()

        # #775: merge-reconcile + issue-closure sync on a slow cadence
        # (default 5 min; 0 disables).  Both share one timer since they're
        # both "reconcile with GitHub" operations at the same frequency.
        try:
            merges_interval = float(
                os.environ.get("COORD_RECONCILE_MERGES_INTERVAL", "300")
            )
        except ValueError:
            merges_interval = 300.0
        # Start at 0 so the first auto-reconcile fires on the very first tick
        # (not after a full merges_interval delay).  On a daemon restart,
        # merged-but-grey work should resolve immediately, not after 5 minutes.
        last_merge_reconcile = 0.0

        async def _tick_loop() -> None:
            nonlocal last_housekeeping, last_merge_reconcile
            from coord.reconcile import reconcile_completed_assignments  # noqa: PLC0415
            from coord import merge_queue as _mq  # noqa: PLC0415

            while True:
                await asyncio.sleep(interval)
                # #1081: pick up a hand-edited coordinator.yml before this
                # tick's config.merge.auto_drain / config.milestone.auto_dispatch
                # checks and the config passed into the tick functions below —
                # the tick loop's own ~30s cadence makes this the fastest path
                # for a daemon-side hand-edit to take effect (faster than
                # waiting on a `/board` request).
                _refresh_config()
                # Step 1: reconcile (independent try/except so a failure here
                # does not prevent the enqueue step below).
                try:
                    reconciled = await run_in_threadpool(
                        reconcile_completed_assignments, config
                    )
                    if reconciled:
                        log.info(
                            "passive reconcile: %d assignment(s) → terminal (%s)",
                            len(reconciled),
                            ", ".join(
                                f"#{r['issue_number']}:{r['to_status']}"
                                for r in reconciled
                            ),
                        )
                        _audit_reconciled(reconciled)
                except Exception:  # noqa: BLE001 — a tick must never crash the daemon
                    log.warning("passive reconcile tick failed", exc_info=True)
                # Step 2: enqueue approved work (#736 / #217 invisible limbo fix).
                # Runs AFTER reconcile so freshly-completed work is on the board
                # when we scan for approved assignments.  Independent try/except
                # so a DB error here does not silence the reconcile step on the
                # next tick.
                try:
                    enqueued = await run_in_threadpool(
                        _mq.enqueue_approved_work, config
                    )
                    if enqueued:
                        log.info(
                            "passive enqueue: %d assignment(s) → merge queue (%s)",
                            len(enqueued),
                            ", ".join(enqueued),
                        )
                        _audit_enqueued(enqueued)
                except Exception:  # noqa: BLE001
                    log.warning("passive enqueue tick failed", exc_info=True)
                # Step 3: #781 auto-drain READY merge-queue entries.
                # Runs AFTER enqueue so freshly-approved work can be picked up
                # in the same tick.  Default-off (merge.auto_drain: false) —
                # no behaviour change for users who haven't opted in.
                # Independent try/except so a drain error never silences the
                # reconcile/enqueue steps on the next tick.
                if config.merge.auto_drain:
                    try:
                        drain_events = await run_in_threadpool(
                            _auto_drain_tick, config
                        )
                        for ev in drain_events:
                            log.info(
                                "auto-drain: %s %s #%d — %s",
                                ev.kind,
                                ev.entry.repo_name,
                                ev.entry.issue_number,
                                ev.message,
                            )
                    except Exception:  # noqa: BLE001
                        log.warning("auto-drain tick failed", exc_info=True)
                # Step 3b: #769 Phase 1 — re-drain actively-registered milestones'
                # ready frontier as declared-order dependencies complete.
                # Runs AFTER reconcile (Step 1) so a freshly-terminal dependency
                # is visible.  Default-off (milestone.auto_dispatch: false) — no
                # behaviour change for users who haven't opted in; `coord
                # milestone dispatch` still works as a one-shot manual drain
                # either way.  Independent try/except so a milestone-drain
                # failure never silences the other tick steps.
                if config.milestone.auto_dispatch:
                    try:
                        drain_outcomes = await run_in_threadpool(
                            _milestone_drain_tick, config
                        )
                        for outcome in drain_outcomes:
                            if outcome.ok:
                                log.info(
                                    "milestone-drain: #%d → %s (assignment %s)",
                                    outcome.issue_number,
                                    outcome.machine_name,
                                    outcome.assignment_id,
                                )
                            else:
                                log.warning(
                                    "milestone-drain: #%d dispatch failed: %s",
                                    outcome.issue_number,
                                    outcome.error,
                                )
                    except Exception:  # noqa: BLE001
                        log.warning("milestone-drain tick failed", exc_info=True)
                # Step 4: #762 archival sweep on a slow cadence (default hourly).
                # Independent try/except — a sweep failure must never crash the
                # daemon or silence the reconcile/enqueue steps above.
                if housekeeping_interval > 0 and (
                    _time.monotonic() - last_housekeeping >= housekeeping_interval
                ):
                    last_housekeeping = _time.monotonic()
                    try:
                        from coord import housekeeping as _hk  # noqa: PLC0415

                        os.environ["COORD_HOUSEKEEPING_ON_DAEMON"] = "1"
                        swept = await run_in_threadpool(_hk.sweep)
                        if swept.get("archived_assignments") or swept.get(
                            "archived_notifications"
                        ):
                            log.info(
                                "housekeeping: archived %d assignment(s), "
                                "%d notification(s)",
                                swept["archived_assignments"],
                                swept["archived_notifications"],
                            )
                            _audit_housekeeping_sweep(swept)
                    except Exception:  # noqa: BLE001
                        log.warning("housekeeping tick failed", exc_info=True)
                # Steps 5 + 6: #775 record out-of-band merges and sync the
                # open-issue closure cache on a slow cadence (default 5 min).
                # Both run under the same timer since they're both "reconcile
                # with GitHub" operations.  Independent try/except so a
                # failure in one does not silence the other.
                if merges_interval > 0 and (
                    _time.monotonic() - last_merge_reconcile >= merges_interval
                ):
                    last_merge_reconcile = _time.monotonic()
                    try:
                        actions = await run_in_threadpool(
                            _reconcile_merges_tick, config
                        )
                        if actions:
                            log.info(
                                "merge reconcile: %d action(s): %s",
                                len(actions),
                                "; ".join(actions),
                            )
                    except Exception:  # noqa: BLE001
                        log.warning("merge reconcile tick failed", exc_info=True)
                    try:
                        synced = await run_in_threadpool(
                            _sync_issues_tick, config
                        )
                        if synced:
                            log.info(
                                "issues sync: %d open issue(s) across all repos",
                                synced,
                            )
                    except Exception:  # noqa: BLE001
                        log.warning("issues sync tick failed", exc_info=True)

        @contextlib.asynccontextmanager
        async def _ctx(_a):  # noqa: ANN202
            task = (
                asyncio.create_task(_tick_loop()) if interval > 0 else None
            )
            try:
                yield
            finally:
                if task is not None:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task

        return _ctx(_app)

    routes = [
        Route("/healthz", healthz, methods=["GET"]),
        Route("/board", board, methods=["GET"]),
        Route("/audit", get_audit, methods=["GET"]),
        Route("/config", serve_config, methods=["GET"]),
        Route("/result", post_result, methods=["POST"]),
        Route("/completion", post_completion, methods=["POST"]),
        Route("/dispatched-work", post_dispatched_work, methods=["POST"]),
        Route("/milestone-drain", post_milestone_drain, methods=["POST"]),
        Route("/dispatched", post_dispatched, methods=["POST"]),
        Route("/test-verdict", post_test_verdict, methods=["POST"]),
        Route("/acceptance-verdict", post_acceptance_verdict, methods=["POST"]),
        Route("/acceptance-record", post_acceptance_record, methods=["POST"]),
        Route("/review-findings", post_review_findings, methods=["POST"]),
        Route("/review-posted", post_review_posted, methods=["POST"]),
        Route(
            "/needs-attention-notified",
            post_needs_attention_notified,
            methods=["POST"],
        ),
        Route("/board", post_board, methods=["POST"]),
        Route("/assignment-usage", post_assignment_usage, methods=["POST"]),
        Route("/assignment-session-id", post_assignment_session_id, methods=["POST"]),
        Route("/assignment-failure-reason", post_assignment_failure_reason, methods=["POST"]),
        Route("/assignment-test-plan", post_assignment_test_plan, methods=["POST"]),
        Route("/notify", post_notify, methods=["POST"]),
        Route("/issue-test-mode", post_issue_test_mode, methods=["POST"]),
        Route("/issue-labels", post_issue_labels, methods=["POST"]),
        Route("/issue-label", post_issue_label, methods=["POST"]),
        Route("/issue-create", post_issue_create, methods=["POST"]),
        Route("/issues-sync", post_issues_sync, methods=["POST"]),
        Route("/issue-edit", post_issue_edit, methods=["POST"]),
        Route("/issue-milestone", post_issue_milestone, methods=["POST"]),
        Route(
            "/issue-milestone-remove", post_issue_milestone_remove, methods=["POST"]
        ),
        Route("/issue-close", post_issue_close, methods=["POST"]),
        Route("/milestone-edit", post_milestone_edit, methods=["POST"]),
        Route("/issue-context", get_issue_context, methods=["GET"]),
        Route("/issue-context", post_issue_context, methods=["POST"]),
        Route("/merge", post_merge, methods=["POST"]),
        Route("/reconcile-merges", post_reconcile_merges, methods=["POST"]),
        Route("/diagnose", post_diagnose, methods=["POST"]),
        Route("/test-plan", post_test_plan, methods=["POST"]),
        Route("/housekeeping", post_housekeeping, methods=["POST"]),
    ]
    # #757: served OpenAPI 3 spec + Swagger UI docs page. Not exempted from
    # the bearer-auth middleware below (only /healthz is) — "behind the
    # daemon's bearer auth where applicable" per the issue.
    routes.extend(openapi_and_docs_routes(_openapi_spec()))
    # #762: gzip the /board projection (markdown-heavy JSON compresses ~9×), so a
    # large payload can't overrun the TUI's fetch timeout on a slow link.  Gzip is
    # outermost so it compresses every response (incl. auth rejections); ureq on
    # the client decodes Content-Encoding: gzip transparently.
    middleware = [Middleware(GZipMiddleware, minimum_size=1024)]
    if token:
        middleware.append(Middleware(_BearerAuthMiddleware, token=token))
    return Starlette(routes=routes, middleware=middleware, lifespan=_lifespan)
