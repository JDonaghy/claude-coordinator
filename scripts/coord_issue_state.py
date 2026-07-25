#!/usr/bin/env python3
"""Read-only per-issue pipeline state oracle for ``scripts/drive-issue.sh``.

Answers the one question the coord CLI has no single command for: *"what stage
is issue N in, and what is blocking it?"*  Emits a flat, shell-``eval``-able
``KEY='value'`` block so a bash driver can branch on it without parsing prose.

Why this exists as a separate file rather than inline in the driver:

- ``coord wait`` reads the **local** dispatched ledger (``load_dispatched()``),
  which is empty on a thin client — so it cannot be used from an operator box
  that reads the board from the daemon.  This polls the daemon instead.
- ``coord diagnose --json`` is per-*stage* and **mutates** (it performs
  best-effort recovery).  A driver loop needs a pure read.
- ``GET /board`` is ~4.4 MB, but it supports ETags.  We cache the payload and
  send ``If-None-Match``, so a steady-state poll is a 304 in ~30 ms instead of
  a multi-megabyte transfer.  This keeps a 60-second poll loop from hammering
  the daemon (the failure mode behind the #1244 / board-timeout incidents).

Usage::

    coord_issue_state.py <repo> <issue>          # KEY='value' lines
    coord_issue_state.py <repo> <issue> --json   # the same data as JSON

Exit codes: 0 ok, 2 configuration/transport error.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

try:
    import httpx
except ImportError:  # pragma: no cover - environment problem, not logic
    print("error: httpx not importable — activate the coord venv", file=sys.stderr)
    raise SystemExit(2)


# Assignment types that can carry the Test/Review gates for an issue.  Imported
# from coord.models when available so this never drifts from the source of
# truth (#1141 was exactly a hardcoded copy of this set going stale).
try:
    from coord.models import WORK_LIKE_TYPES as _WORK_LIKE
    WORK_LIKE: frozenset[str] = frozenset(_WORK_LIKE)
except Exception:  # noqa: BLE001 — fall back to the known-good set
    WORK_LIKE = frozenset({"work", "mock-author", "test-author"})

TERMINAL_STATUSES = frozenset({"done", "failed", "cancelled", "merged", "advisory"})


# ── config ───────────────────────────────────────────────────────────────────

def load_config() -> Any:
    """Load the coordinator config the same way the CLI does.

    Mirrors ``coord.commands._common._load_config``: a thin client (one with
    ``board_service`` in ``~/.coord/client.toml``) must fetch the config from
    the daemon and must NOT trust a local ``coordinator.yml`` that happens to
    exist (#1080).
    """
    from coord.config import load, resolve_config_path

    path = resolve_config_path()
    try:
        from coord.client import fetch_remote_config, resolve_board_service

        svc = resolve_board_service()
        if svc is not None:
            path = fetch_remote_config(svc)
    except Exception as exc:  # noqa: BLE001
        print(f"error: could not fetch config from daemon: {exc}", file=sys.stderr)
        raise SystemExit(2)
    return load(path)


# ── board fetch (ETag-cached) ────────────────────────────────────────────────

def _cache_dir() -> Path:
    base = Path(os.environ.get("TMPDIR", "/tmp")) / f"coord-drive-issue-{os.getuid()}"
    base.mkdir(parents=True, exist_ok=True)
    return base


def fetch_board() -> dict:
    """GET /board with an ETag cache, or read the local DB when standalone.

    Returns the raw board wire payload (``assignments``, ``merge_plan``,
    ``merge_queue``, ``issue_stage_projection``, …).
    """
    from coord.client import _headers, resolve_board_service

    svc = resolve_board_service()
    if svc is None:
        # Standalone (daemon host): build the payload from the local DB.
        from coord.board_service import read_board
        from coord.client import serialize_board

        return serialize_board(read_board())

    key = hashlib.sha256(svc.url.encode()).hexdigest()[:16]
    cache_path = _cache_dir() / f"board-{key}.json"

    # The cache is deliberately SHARED across concurrent drivers rather than
    # split per-issue: the /board payload is identical for every issue, so
    # sharing means one driver's fetch serves everyone else's 304.
    cached: dict | None = None
    try:
        cached = json.loads(cache_path.read_text())
    except (OSError, ValueError):
        cached = None  # absent, unreadable, or a torn write from an old version

    headers = dict(_headers(svc))
    etag = (cached or {}).get("etag")
    if etag:
        headers["if-none-match"] = etag

    resp = httpx.get(f"{svc.url}/board", headers=headers, timeout=60.0)
    if resp.status_code == 304 and cached is not None:
        return cached["payload"]
    resp.raise_for_status()
    payload = resp.json()

    # Store the ETag and the payload TOGETHER in one atomically-replaced file.
    #
    # They were two files, written body-then-etag. That is safe for a single
    # writer, but two concurrent drivers can interleave so that a reader pairs
    # process A's *newer* etag with process B's *older* body — it then sends
    # If-None-Match, gets a 304, and confidently serves the WRONG board. A
    # driver acting on a stale board is precisely the class of silent wrongness
    # this whole tool exists to avoid.
    #
    # One file makes the pair inseparable; os.replace is atomic on POSIX, and
    # the temp file is created in the same directory so the rename never
    # crosses a filesystem boundary. The pid suffix keeps two writers from
    # colliding on the temp name itself.
    tmp = cache_path.with_suffix(f".{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps({"etag": resp.headers.get("etag"), "payload": payload}))
        os.replace(tmp, cache_path)
    except OSError:
        # The cache is an optimisation, never a correctness dependency — a
        # failed write just costs the next poll a full fetch.
        try:
            tmp.unlink()
        except OSError:
            pass
    return payload


# ── projection ───────────────────────────────────────────────────────────────

def _latest(rows: list[dict]) -> dict | None:
    """The most recently dispatched row, or None."""
    if not rows:
        return None
    return max(rows, key=lambda r: r.get("dispatched_at") or 0.0)


def project(payload: dict, repo: str, issue: int, config: Any) -> dict:
    """Reduce the whole board to the handful of facts the driver branches on."""
    mine = [
        a
        for a in payload.get("assignments") or []
        if a.get("repo_name") == repo and a.get("issue_number") == issue
    ]

    plan = _latest([a for a in mine if a.get("type") == "plan"])
    work = _latest([a for a in mine if a.get("type") in WORK_LIKE])
    work_aid = (work or {}).get("assignment_id")

    # The review that reviewed *this* work row.  Fix rounds produce a new work
    # row and a new review, so keying on the work id (not just the issue) is
    # what keeps a stale earlier verdict from being read as the current one.
    review = _latest(
        [
            a
            for a in mine
            if a.get("type") == "review"
            and a.get("review_of_assignment_id") == work_aid
        ]
    )
    smoke = _latest(
        [
            a
            for a in mine
            if a.get("type") == "smoke"
            and a.get("review_of_assignment_id") == work_aid
        ]
    )

    active = [a for a in mine if (a.get("status") or "") not in TERMINAL_STATUSES]

    # Merge state: match on (repo, issue) rather than assignment id — the
    # enqueued entry may be keyed to an earlier work row in a fix chain.
    merge_entry = None
    for entry in payload.get("merge_plan") or []:
        if entry.get("repo_name") == repo and entry.get("issue_number") == issue:
            merge_entry = entry
            break
    if merge_entry is None:
        for entry in payload.get("merge_queue") or []:
            if entry.get("repo_name") == repo and entry.get("issue_number") == issue:
                merge_entry = {
                    "status": (entry.get("state") or "").upper(),
                    "reason": entry.get("error"),
                    "pr_url": entry.get("pr_url"),
                    "assignment_id": entry.get("assignment_id"),
                }
                break

    repo_cfg = config.repo(repo)
    if repo_cfg is None:
        print(f"error: repo {repo!r} is not in coordinator.yml", file=sys.stderr)
        raise SystemExit(2)

    def g(row: dict | None, field: str) -> Any:
        return (row or {}).get(field)

    return {
        "REPO": repo,
        "ISSUE": issue,
        "REPO_GITHUB": repo_cfg.github or "",
        "REPO_DEFAULT_BRANCH": repo_cfg.default_branch or "main",
        "REPO_TEST_COMMAND": repo_cfg.test_command or "",
        "MAX_REVIEW_ITERATIONS": config.pipeline.max_review_iterations,
        "AUTO_LOOP": "1" if config.pipeline.auto_loop else "0",
        "PLAN_AID": g(plan, "assignment_id") or "",
        "PLAN_STATUS": g(plan, "status") or "",
        "WORK_AID": work_aid or "",
        "WORK_TYPE": g(work, "type") or "",
        "WORK_STATUS": g(work, "status") or "",
        "WORK_BRANCH": g(work, "branch") or "",
        "WORK_MACHINE": g(work, "machine_name") or "",
        "WORK_PROVIDER": g(work, "provider_name") or "",
        "WORK_TEST_STATE": g(work, "test_state") or "",
        "WORK_TEST_REASON": g(work, "test_reason") or "",
        "WORK_REVIEW_STATE": g(work, "review_state") or "",
        "WORK_REVIEW_ITER": g(work, "review_iteration") or 0,
        "WORK_EXIT_CODE": ("" if g(work, "exit_code") is None else g(work, "exit_code")),
        "WORK_FAILURE_REASON": g(work, "failure_reason") or "",
        "REVIEW_AID": g(review, "assignment_id") or "",
        "REVIEW_STATUS": g(review, "status") or "",
        "REVIEW_VERDICT": g(review, "review_verdict") or "",
        "SMOKE_AID": g(smoke, "assignment_id") or "",
        "SMOKE_STATUS": g(smoke, "status") or "",
        "ACTIVE_COUNT": len(active),
        "ACTIVE_TYPES": ",".join(sorted({(a.get("type") or "?") for a in active})),
        "MERGE_STATUS": (merge_entry or {}).get("status") or "",
        "MERGE_REASON": (merge_entry or {}).get("reason") or "",
        "MERGE_PR_URL": (merge_entry or {}).get("pr_url") or "",
        "MERGE_AID": (merge_entry or {}).get("assignment_id") or "",
        "PICKED_MACHINE": pick_machine(payload, repo, config),
    }


def pick_machine(payload: dict, repo: str, config: Any) -> str:
    """Least-loaded unpaused machine that hosts *repo*, or "" if none.

    Deliberately simple — this is not ``coord plan``'s brain (which costs an
    LLM call).  Load is counted from the board's non-terminal rows, so a
    machine already running two workers loses to an idle peer.
    """
    try:
        from coord.machine_pause import paused_set

        paused = paused_set()
    except Exception:  # noqa: BLE001 — a missing pause file means nothing paused
        paused = set()

    load: dict[str, int] = {}
    for a in payload.get("assignments") or []:
        if (a.get("status") or "") not in TERMINAL_STATUSES:
            name = a.get("machine_name") or ""
            load[name] = load.get(name, 0) + 1

    candidates = [
        m
        for m in config.machines
        if repo in (m.repos or []) and m.name not in paused
    ]
    if not candidates:
        return ""
    candidates.sort(key=lambda m: (load.get(m.name, 0), m.name))
    return candidates[0].name


def sh_quote(value: Any) -> str:
    """Single-quote *value* for safe ``eval`` in bash."""
    text = str(value)
    return "'" + text.replace("'", "'\\''") + "'"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("repo")
    ap.add_argument("issue", type=int)
    ap.add_argument("--json", action="store_true", help="emit JSON instead of KEY='value'")
    args = ap.parse_args()

    config = load_config()
    try:
        payload = fetch_board()
    except Exception as exc:  # noqa: BLE001 — a transport blip must be a clean
        # exit code, not a traceback: the driver retries on the next poll.
        print(f"error: board read failed: {exc}", file=sys.stderr)
        return 2

    state = project(payload, args.repo, args.issue, config)
    if args.json:
        print(json.dumps(state, indent=2))
    else:
        for key, value in state.items():
            print(f"{key}={sh_quote(value)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
