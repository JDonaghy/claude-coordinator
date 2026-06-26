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
from dataclasses import asdict, fields
from pathlib import Path

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from coord.config import Config
from coord.dao import SCHEMA_VERSION, CoordStore

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
    return total


def build_app(store: CoordStore, config: Config, *, token: str | None = None) -> Starlette:
    """Build the read-only control-center Starlette app bound to *store* + *config*.

    *token* — when set, every endpoint except ``/healthz`` requires
    ``Authorization: Bearer <token>``.
    """

    async def healthz(request: Request) -> JSONResponse:  # noqa: ARG001
        return JSONResponse({"status": "ok", "schema_version": SCHEMA_VERSION})

    async def board(request: Request) -> Response:  # noqa: ARG001
        try:
            return JSONResponse(store.board_projection())
        except Exception as e:  # noqa: BLE001 — surface a clean 503 rather than a stack trace
            return JSONResponse(
                {"error": "board read failed", "detail": str(e)}, status_code=503
            )

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

    async def post_assignment_usage(request: Request) -> Response:
        # #665: route cost/token/is_interactive writes through the daemon.
        # Body: {assignment_id, cost_usd?, input_tokens?, output_tokens?,
        #        cache_creation_tokens?, cache_read_tokens?, is_interactive?}
        # One round-trip covers all three update helpers; the daemon calls the
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
                        order=body.get("order"),
                        repo_filter=body.get("repo_filter"),
                        method=body.get("method") or "rebase",
                        force_merge=bool(body.get("force_merge")),
                        skip_review=bool(body.get("skip_review")),
                        skip_smoke=bool(body.get("skip_smoke")),
                        drop_assignment=None,  # already handled above
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
        last_merge_reconcile = _time.monotonic()

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
                # Step 3: #762 archival sweep on a slow cadence (default hourly).
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
                # Steps 4 + 5: #775 record out-of-band merges and sync the
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
        Route("/assignment-usage", post_assignment_usage, methods=["POST"]),
        Route("/issue-labels", post_issue_labels, methods=["POST"]),
        Route("/issues-sync", post_issues_sync, methods=["POST"]),
        Route("/issue-edit", post_issue_edit, methods=["POST"]),
        Route("/issue-context", get_issue_context, methods=["GET"]),
        Route("/issue-context", post_issue_context, methods=["POST"]),
        Route("/merge", post_merge, methods=["POST"]),
        Route("/reconcile-merges", post_reconcile_merges, methods=["POST"]),
        Route("/diagnose", post_diagnose, methods=["POST"]),
        Route("/housekeeping", post_housekeeping, methods=["POST"]),
    ]
    # #762: gzip the /board projection (markdown-heavy JSON compresses ~9×), so a
    # large payload can't overrun the TUI's fetch timeout on a slow link.  Gzip is
    # outermost so it compresses every response (incl. auth rejections); ureq on
    # the client decodes Content-Encoding: gzip transparently.
    middleware = [Middleware(GZipMiddleware, minimum_size=1024)]
    if token:
        middleware.append(Middleware(_BearerAuthMiddleware, token=token))
    return Starlette(routes=routes, middleware=middleware, lifespan=_lifespan)
