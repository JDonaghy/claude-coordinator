"""Read-only per-issue pipeline state oracle for ``coord drive`` (#1392).

Answers the one question the coord CLI has no single command for: *"what stage
is issue N in, and what is blocking it?"*

This is the in-process port of ``scripts/coord_issue_state.py``, which was a
standalone script whose output the bash driver ``eval``-ed as ``KEY='value'``
lines.  The shell-quoting handshake is gone (that ``eval`` was one of the
bugs — a diagnostic on stdout would have executed as shell); the driver now
imports :func:`project` and branches on a typed :class:`IssueState`.

Why this is a projection over ``GET /board`` rather than an existing command:

- ``coord wait`` reads the **local** dispatched ledger (``load_dispatched()``),
  which is empty on a thin client — so it cannot be used from an operator box
  that reads the board from the daemon.  This polls the daemon instead.
- ``coord diagnose --json`` is per-*stage* and **mutates** (it performs
  best-effort recovery).  A driver loop needs a pure read.
- ``GET /board`` is ~4.4 MB, but it supports ETags.  We cache the payload and
  send ``If-None-Match``, so a steady-state poll is a 304 in ~30 ms instead of
  a multi-megabyte transfer.  This keeps a 60-second poll loop from hammering
  the daemon (the failure mode behind the #1244 / board-timeout incidents).

Everything here is a pure function over a board payload except
:func:`fetch_board`, which is the one I/O boundary.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from coord.models import WORK_LIKE_TYPES

# Assignment types that can carry the Test/Review gates for an issue.  Sourced
# from coord.models so this never drifts from the source of truth (#1141 was
# exactly a hardcoded copy of this set going stale).
WORK_LIKE: frozenset[str] = WORK_LIKE_TYPES

TERMINAL_STATUSES = frozenset({"done", "failed", "cancelled", "merged", "advisory"})


class DriveStateError(Exception):
    """The board or config could not be read well enough to drive anything."""


# ── the projection ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class IssueState:
    """Everything ``coord drive``'s state machine branches on, and nothing else.

    Field names mirror the ``KEY='value'`` variables the bash driver used, so
    the ``--dry-run`` JSON stays recognisable to anyone who ran the script
    (see :meth:`as_flat_dict`).
    """

    repo: str
    issue: int
    repo_github: str = ""
    repo_default_branch: str = "main"
    repo_test_command: str = ""
    max_review_iterations: int = 5
    auto_loop: bool = True

    plan_aid: str = ""
    plan_status: str = ""

    work_aid: str = ""
    work_type: str = ""
    work_status: str = ""
    work_branch: str = ""
    work_machine: str = ""
    work_provider: str = ""
    work_test_state: str = ""
    work_test_reason: str = ""
    work_review_state: str = ""
    work_review_iter: int = 0
    work_exit_code: int | None = None
    work_failure_reason: str = ""

    review_aid: str = ""
    review_status: str = ""
    review_verdict: str = ""

    smoke_aid: str = ""
    smoke_status: str = ""

    active_count: int = 0
    active_types: tuple[str, ...] = ()

    merge_status: str = ""
    merge_reason: str = ""
    merge_pr_url: str = ""
    merge_aid: str = ""

    picked_machine: str = ""

    # ── #1453: oracle-loop JIT slice authoring ──────────────────────────
    # `milestone_number` is the issue's own GitHub milestone (the `ms-NN`
    # Gate-A contract this issue's slice would live under); resolved from
    # the same `/board` `issues` list the TUI's `pipeline_issue_milestone`
    # reads. `milestone_tracking_issue` is the epic that owns the `##
    # Work order` block this issue is a member node of — resolved from
    # `milestone_work_orders`, mirroring the TUI's
    # `milestone_tracking_issue_for` (tui/src/app/pipeline.rs). Both are
    # ``None`` for a plain issue with no milestone, or one not (yet) a
    # member of any tracked work order — the "normal drive" case.
    milestone_number: int | None = None
    milestone_tracking_issue: int | None = None

    # The JIT slice's own `type="test-author"` assignment (#1171: keyed on
    # `for_issue_number == issue`, NOT `issue_number` — that field is the
    # milestone's TRACKING issue, so this row is invisible to `work_aid`
    # above by design). Empty until `coord acceptance author ... --issue
    # <N>` has been dispatched for this issue.
    acceptance_author_aid: str = ""
    acceptance_author_status: str = ""

    # ── derived ──────────────────────────────────────────────────────────
    @property
    def fingerprint(self) -> str:
        """Compact fingerprint of every field the state machine branches on.

        Used to tell a *stall* (no transition) apart from "still working" —
        the bash ``state_fingerprint`` function, field-for-field.
        """
        return "|".join(
            str(v)
            for v in (
                self.work_aid,
                self.work_status,
                self.work_test_state,
                self.work_review_state,
                self.work_review_iter,
                self.review_status,
                self.review_verdict,
                self.merge_status,
            )
        )

    def as_flat_dict(self) -> dict[str, Any]:
        """Upper-cased flat dict, matching the old script's variable names."""
        out: dict[str, Any] = {}
        for key, value in asdict(self).items():
            if isinstance(value, tuple):
                value = ",".join(value)
            elif isinstance(value, bool):
                value = "1" if value else "0"
            elif value is None:
                value = ""
            out[key.upper()] = value
        return out


def _latest(rows: list[dict]) -> dict | None:
    """The most recently dispatched row, or ``None``."""
    if not rows:
        return None
    return max(rows, key=lambda r: r.get("dispatched_at") or 0.0)


def project(payload: dict, repo: str, issue: int, config: Any) -> IssueState:
    """Reduce a whole ``/board`` payload to the facts the driver branches on.

    Raises :class:`DriveStateError` when *repo* is not in coordinator.yml —
    a configuration error the driver must report, not poll through.
    """
    repo_cfg = config.repo(repo)
    if repo_cfg is None:
        raise DriveStateError(f"repo {repo!r} is not in coordinator.yml")

    mine = [
        a
        for a in payload.get("assignments") or []
        if a.get("repo_name") == repo and a.get("issue_number") == issue
    ]

    plan = _latest([a for a in mine if a.get("type") == "plan"])
    work = _latest([a for a in mine if a.get("type") in WORK_LIKE])
    work_aid = (work or {}).get("assignment_id") or ""

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

    merge_entry = _merge_entry(payload, repo, issue)

    def g(row: dict | None, key: str, default: Any = "") -> Any:
        value = (row or {}).get(key)
        return default if value is None else value

    exit_code = (work or {}).get("exit_code")

    # #1453: oracle-loop JIT slice resolution — both reads are over data
    # already published on /board, no extra I/O (see IssueState's docstring
    # for the two source lists and their TUI-side counterparts).
    milestone_number = None
    for oi in payload.get("issues") or []:
        if oi.get("repo_name") == repo and oi.get("number") == issue:
            milestone_number = oi.get("milestone_number")
            break

    milestone_tracking_issue = None
    for mwo in payload.get("milestone_work_orders") or []:
        if mwo.get("repo_name") != repo:
            continue
        if any(n.get("issue_number") == issue for n in mwo.get("nodes") or []):
            milestone_tracking_issue = mwo.get("tracking_issue")
            break

    # The JIT slice's own assignment row: keyed on `for_issue_number`, NOT
    # `issue_number` (that field carries the milestone's TRACKING issue for
    # this dispatch shape — #1171/#1138) — so it is deliberately excluded
    # from `mine`/`work_aid` above.
    acceptance_author = _latest(
        [
            a
            for a in payload.get("assignments") or []
            if a.get("repo_name") == repo
            and a.get("type") == "test-author"
            and a.get("for_issue_number") == issue
        ]
    )

    return IssueState(
        repo=repo,
        issue=issue,
        repo_github=repo_cfg.github or "",
        repo_default_branch=repo_cfg.default_branch or "main",
        repo_test_command=repo_cfg.test_command or "",
        max_review_iterations=config.pipeline.max_review_iterations,
        auto_loop=bool(config.pipeline.auto_loop),
        plan_aid=g(plan, "assignment_id"),
        plan_status=g(plan, "status"),
        work_aid=work_aid,
        work_type=g(work, "type"),
        work_status=g(work, "status"),
        work_branch=g(work, "branch"),
        work_machine=g(work, "machine_name"),
        work_provider=g(work, "provider_name"),
        work_test_state=g(work, "test_state"),
        work_test_reason=g(work, "test_reason"),
        work_review_state=g(work, "review_state"),
        work_review_iter=int(g(work, "review_iteration", 0) or 0),
        work_exit_code=None if exit_code is None else int(exit_code),
        work_failure_reason=g(work, "failure_reason"),
        review_aid=g(review, "assignment_id"),
        review_status=g(review, "status"),
        review_verdict=g(review, "review_verdict"),
        smoke_aid=g(smoke, "assignment_id"),
        smoke_status=g(smoke, "status"),
        active_count=len(active),
        active_types=tuple(sorted({(a.get("type") or "?") for a in active})),
        merge_status=(merge_entry or {}).get("status") or "",
        merge_reason=(merge_entry or {}).get("reason") or "",
        merge_pr_url=(merge_entry or {}).get("pr_url") or "",
        merge_aid=(merge_entry or {}).get("assignment_id") or "",
        picked_machine=pick_machine(payload, repo, config),
        milestone_number=milestone_number,
        milestone_tracking_issue=milestone_tracking_issue,
        acceptance_author_aid=g(acceptance_author, "assignment_id"),
        acceptance_author_status=g(acceptance_author, "status"),
    )


def _merge_entry(payload: dict, repo: str, issue: int) -> dict | None:
    """Merge state for (*repo*, *issue*) from the plan, else the raw queue.

    Matched on (repo, issue) rather than assignment id on purpose: the
    enqueued entry may be keyed to an earlier work row in a fix chain.
    """
    for entry in payload.get("merge_plan") or []:
        if entry.get("repo_name") == repo and entry.get("issue_number") == issue:
            return entry
    for entry in payload.get("merge_queue") or []:
        if entry.get("repo_name") == repo and entry.get("issue_number") == issue:
            return {
                "status": (entry.get("state") or "").upper(),
                "reason": entry.get("error"),
                "pr_url": entry.get("pr_url"),
                "assignment_id": entry.get("assignment_id"),
            }
    return None


def pick_machine(payload: dict, repo: str, config: Any) -> str:
    """Least-loaded unpaused machine that hosts *repo*, or ``""`` if none.

    Deliberately simple — this is not ``coord plan``'s brain (which costs an
    LLM call).  Load is counted from the board's non-terminal rows, so a
    machine already running two workers loses to an idle peer.
    """
    try:
        from coord.machine_pause import paused_set  # noqa: PLC0415

        paused = paused_set()
    except Exception:  # noqa: BLE001 — a missing pause file means nothing paused
        paused = set()

    load: dict[str, int] = {}
    for a in payload.get("assignments") or []:
        if (a.get("status") or "") not in TERMINAL_STATUSES:
            name = a.get("machine_name") or ""
            load[name] = load.get(name, 0) + 1

    candidates = [
        m for m in config.machines if repo in (m.repos or []) and m.name not in paused
    ]
    if not candidates:
        return ""
    candidates.sort(key=lambda m: (load.get(m.name, 0), m.name))
    return candidates[0].name


# ── board fetch (the one I/O boundary) ───────────────────────────────────────


def scratch_dir() -> Path:
    """Per-user scratch directory shared by every ``coord drive`` run.

    Holds the per-issue run lock + holder file, the run log, the fleet merge
    lock, and the shared board cache.

    The ``coord-drive-issue-`` name is deliberately the one ``drive-issue.sh``
    used, and every file inside keeps its old name too.  During the changeover
    a straggler bash driver launched from an older checkout still collides on
    the *same* ``lock-<repo>-<issue>`` file, so it cannot double-dispatch
    alongside a ``coord drive`` on the same issue.  Renaming the directory
    would have silently disabled that mutual exclusion for exactly as long as
    an old checkout existed anywhere in the fleet.
    """
    base = Path(os.environ.get("TMPDIR", "/tmp")) / f"coord-drive-issue-{os.getuid()}"
    base.mkdir(parents=True, exist_ok=True)
    return base


@dataclass
class BoardFetcher:
    """``GET /board`` with an ETag cache, or the local DB when standalone.

    The cache is deliberately SHARED across concurrent drivers rather than
    split per-issue: the ``/board`` payload is identical for every issue, so
    sharing means one driver's fetch serves everyone else's 304.
    """

    cache_dir: Path = field(default_factory=scratch_dir)
    timeout: float = 60.0

    def fetch(self) -> dict:
        from coord.client import _headers, resolve_board_service  # noqa: PLC0415

        svc = resolve_board_service()
        if svc is None:
            # Standalone (daemon host): build the payload from the local DB.
            from coord.board_service import read_board  # noqa: PLC0415
            from coord.client import serialize_board  # noqa: PLC0415

            return serialize_board(read_board())

        import httpx  # noqa: PLC0415

        cache_path = self._cache_path(svc.url)
        cached = self._read_cache(cache_path)

        headers = dict(_headers(svc))
        etag = (cached or {}).get("etag")
        if etag:
            headers["if-none-match"] = etag

        resp = httpx.get(f"{svc.url}/board", headers=headers, timeout=self.timeout)
        if resp.status_code == 304 and cached is not None:
            return cached["payload"]
        resp.raise_for_status()
        payload = resp.json()
        self._write_cache(cache_path, resp.headers.get("etag"), payload)
        return payload

    def _cache_path(self, url: str) -> Path:
        key = hashlib.sha256(url.encode()).hexdigest()[:16]
        return self.cache_dir / f"board-{key}.json"

    @staticmethod
    def _read_cache(path: Path) -> dict | None:
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return None  # absent, unreadable, or a torn write from an old version
        if not isinstance(data, dict) or "payload" not in data:
            return None
        return data

    @staticmethod
    def _write_cache(path: Path, etag: str | None, payload: dict) -> None:
        """Store the ETag and the payload TOGETHER in one atomically-replaced file.

        They were two files, written body-then-etag.  That is safe for a single
        writer, but two concurrent drivers can interleave so that a reader
        pairs process A's *newer* etag with process B's *older* body — it then
        sends ``If-None-Match``, gets a 304, and confidently serves the WRONG
        board.  A driver acting on a stale board is precisely the class of
        silent wrongness this whole tool exists to avoid.

        One file makes the pair inseparable; ``os.replace`` is atomic on POSIX,
        and the temp file is created in the same directory so the rename never
        crosses a filesystem boundary.  The pid suffix keeps two writers from
        colliding on the temp name itself.
        """
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        try:
            tmp.write_text(json.dumps({"etag": etag, "payload": payload}))
            os.replace(tmp, path)
        except OSError:
            # The cache is an optimisation, never a correctness dependency — a
            # failed write just costs the next poll a full fetch.
            try:
                tmp.unlink()
            except OSError:
                pass
