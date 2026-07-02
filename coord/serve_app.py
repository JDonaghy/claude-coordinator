"""``coord serve`` — the portable control center daemon (#584/#589/#594).

A lean, **read-only** Starlette app that fronts the coordinator board so any
Tailscale-reachable machine can render the same live board without a local
``~/.coord/coord.db`` or ``coordinator.yml``.

It mirrors the agent server (``coord/agent_app.py``, port 7433) and the dashboard
(``coord/dashboard/server.py``, port 7434); this daemon listens on **7435**.

Endpoints:

* ``GET /healthz``  — liveness; no DB access, never auth-gated.
* ``GET /board``    — the full board projection (``CoordStore.board_projection``).
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
    enqueued = mq.enqueue_approved_work(config)  # loads its own board snapshot
    return reconciled, enqueued


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

    return events


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

    async def healthz(request: Request) -> JSONResponse:  # noqa: ARG001
        return JSONResponse({"status": "ok", "schema_version": SCHEMA_VERSION})

    async def board(request: Request) -> Response:  # noqa: ARG001
        try:
            projection = store.board_projection()
        except Exception as e:  # noqa: BLE001 — surface a clean 503 rather than a stack trace
            return JSONResponse(
                {"error": "board read failed", "detail": str(e)}, status_code=503
            )
        # #776: inject server-side merge plan (ordered, gate-annotated) so thin
        # clients get status + reason without re-implementing gate logic.
        # Computed after the projection so a plan failure never 503 the board.
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
        # functions.  Fail-open: an error returns an empty list rather than
        # 503ing the board.
        try:
            from coord import stage_projection as _sp  # noqa: PLC0415
            from coord.ci_store import build_ci_store as _build_ci_store2  # noqa: PLC0415
            from coord.merge_queue import load_queue as _load_queue  # noqa: PLC0415
            from coord.state import build_board as _build_board2  # noqa: PLC0415

            _sp_board = _build_board2()
            try:
                _sp_ci = _build_ci_store2(config.ci_store.type)
            except Exception:  # noqa: BLE001
                _sp_ci = None
            projection["issue_stage_projection"] = _sp.compute_board_stage_projection(
                issues=projection.get("issues", []),
                assignments=list(_sp_board.active) + list(_sp_board.completed),
                merge_queue_items=_load_queue(),
                default_gates=list(config.pipeline.default_gates),
                require_plan=bool(config.dispatch.require_plan),
                ci_store=_sp_ci,
            )
        except Exception:  # noqa: BLE001 — projection failure must not blank the board
            projection["issue_stage_projection"] = []
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
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "assignment-usage write failed", "detail": str(e)},
                status_code=503,
            )
        return JSONResponse({"ok": True})

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
        Route("/config", serve_config, methods=["GET"]),
        Route("/result", post_result, methods=["POST"]),
        Route("/completion", post_completion, methods=["POST"]),
        Route("/dispatched-work", post_dispatched_work, methods=["POST"]),
        Route("/dispatched", post_dispatched, methods=["POST"]),
        Route("/test-verdict", post_test_verdict, methods=["POST"]),
        Route("/board", post_board, methods=["POST"]),
        Route("/assignment-usage", post_assignment_usage, methods=["POST"]),
        Route("/issue-labels", post_issue_labels, methods=["POST"]),
        Route("/issues-sync", post_issues_sync, methods=["POST"]),
        Route("/issue-edit", post_issue_edit, methods=["POST"]),
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
