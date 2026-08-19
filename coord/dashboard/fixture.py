"""Deterministic seeded-board fixture mode for the web dashboard (#1538).

``coord web --fixture tests/fixtures/board-pipeline-basic.json`` boots the
**real** dashboard app — same routes, same handlers, same
``compute_pipeline``/``dataclasses.asdict`` serialization — but sourced from a
JSON fixture instead of ``~/.coord/coord.db``.  It is the web twin of
``coord-tui``'s ``make_test_app(BoardData)``: an acceptance suite can assert
against a board that does not move under it, with no live fleet, no network
and no daemon.

Two rules shape this module:

1. **No parallel fake read path.**  The fixture only replaces the *data
   source*.  ``/api/board`` and ``/api/pipeline`` still run
   ``coord.client.board_from_payload`` → ``coord.pipeline.compute_pipeline`` →
   ``asdict``, so the tests keep testing the real contract.  The fixture's
   board block is literally the daemon's ``/board`` wire payload, reconstructed
   by the same function a thin client uses.
2. **Writes are recorded, never executed.**  ``POST /api/pipeline/action``,
   ``/api/approve``, ``/api/reject`` and ``/api/chat`` return their normal
   success shape and append to an in-memory :class:`RecordedAction` log.
   Nothing is dispatched, no subprocess is spawned, no money is ever spent.

Fixture schema (every key optional except ``board``)::

    {
      "now": 1750000000.0,          # frozen clock — keeps /api/pipeline byte-stable
      "board": {                    # the daemon GET /board payload shape
        "round_number": 7,
        "assignments": [ {...assignment row...} ],
        "plans": {},                # assignment_id -> plan object
        "notifications": []         # [{"assignment_id": ...}]
      },
      "merge_queue":     [ {...coord.merge_queue.QueuedMerge fields...} ],
      "proposals":       [ {...coord.models.Proposal fields...} ],
      "machines":        [ {...GET /api/machines entry...} ],
      "sessions":        [ {...GET /api/sessions entry...} ],
      "drive_queue":     [ {...coord.state._decode_drive_queue_row shape...} ],
      "review_findings": {"rev-1": {"verdict": "approve", "body": "..."}},
      "diffs":           {"work-1": "diff --git ..."},
      "chat_reply":      "canned /api/chat response text",
      "events": [ {"after": 0.25, "type": "board_updated", "data": {}} ],
      "autoplay_events": false,     # play the script at startup too (default: no)
      "config":  { ...coordinator.yml mapping... }
    }

The event script does **not** play on startup by default: a timeline racing the
client's connect is precisely the nondeterminism this whole mode exists to
remove.  Drive it explicitly with ``POST /api/fixture/events/replay`` once the
client is subscribed (a late subscriber can still catch up via
``Last-Event-ID``).  Set ``autoplay_events: true`` for a hands-off demo board.

``board`` may also be spelled at the top level (``assignments`` /
``round_number`` as siblings of ``merge_queue``), which is exactly what
``scripts/gen_board_fixture.py`` emits — so a golden ``/board`` capture drops
in unchanged.
"""

from __future__ import annotations

import copy
import json
from dataclasses import MISSING, asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from coord.config import Config
from coord.models import Board, Proposal

__all__ = [
    "FixtureError",
    "RecordedAction",
    "ScriptedEvent",
    "FixtureServer",
    "load_fixture",
    "parse_fixture",
]


class FixtureError(ValueError):
    """A fixture file is missing, unreadable, or structurally invalid."""


@dataclass(frozen=True)
class ScriptedEvent:
    """One entry of the fixture's SSE script.

    ``after`` is the delay in seconds *before* this event is published,
    relative to the previous scripted event (so a script reads as a timeline,
    not a set of absolute offsets to keep in sync by hand).
    """

    type: str
    data: Any = None
    after: float = 0.0


@dataclass
class RecordedAction:
    """A write the dashboard would have executed, captured instead.

    ``seq`` is a 1-based monotonic counter — the stable ordering key for
    assertions.  ``at`` is the fixture's frozen clock, so two runs against the
    same fixture produce an identical log.
    """

    seq: int
    endpoint: str
    method: str
    action: str | None
    payload: dict
    at: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _filter_kwargs(cls, raw: dict, *, what: str) -> dict:
    """Keep only *cls*'s dataclass fields — tolerant like ``row_to_assignment``.

    Unknown keys are dropped rather than raising, so a fixture captured from a
    newer/older schema still loads; missing *required* fields raise a
    :class:`FixtureError` naming them, which is the failure worth being loud
    about.
    """
    if not isinstance(raw, dict):
        raise FixtureError(f"{what} entries must be mappings, got {type(raw).__name__}")
    names = {f.name for f in fields(cls)}
    kwargs = {k: v for k, v in raw.items() if k in names}
    missing = [
        f.name
        for f in fields(cls)
        if f.default is MISSING
        and f.default_factory is MISSING
        and f.name not in kwargs
    ]
    if missing:
        raise FixtureError(f"{what} entry is missing required field(s): {', '.join(missing)}")
    return kwargs


@dataclass
class FixtureServer:
    """The seeded board + the recorded-write log behind ``coord web --fixture``.

    Every accessor rebuilds its objects from the raw payload, so a handler that
    mutates the board it was handed (``unstick`` calls
    ``board.mark_failed_by_id``) cannot leak that mutation into the next
    request.  That is what makes "two runs produce byte-identical output" hold
    *within* a process as well as across processes.
    """

    board_payload: dict = field(default_factory=lambda: {"assignments": [], "round_number": 0})
    merge_queue_raw: list = field(default_factory=list)
    proposals_raw: list = field(default_factory=list)
    machines_raw: list = field(default_factory=list)
    sessions_raw: list = field(default_factory=list)
    drive_queue_raw: list = field(default_factory=list)
    review_findings_raw: dict = field(default_factory=dict)
    diffs: dict = field(default_factory=dict)
    chat_reply: str = "fixture mode: the coordinator assistant is not wired up."
    events: list[ScriptedEvent] = field(default_factory=list)
    autoplay_events: bool = False
    now: float | None = None
    config_raw: dict | None = None
    path: Path | None = None

    _actions: list[RecordedAction] = field(default_factory=list, repr=False)

    # ── Read side (the real handlers pull from here) ────────────────────────

    def board(self) -> Board:
        """Reconstruct a :class:`Board` through the real ``/board`` deserializer."""
        from coord.client import board_from_payload  # noqa: PLC0415

        return board_from_payload(copy.deepcopy(self.board_payload))

    def merge_queue(self) -> list:
        """The seeded merge queue as real ``QueuedMerge`` objects."""
        from coord.merge_queue import QueuedMerge  # noqa: PLC0415

        return [
            QueuedMerge(**_filter_kwargs(QueuedMerge, copy.deepcopy(raw), what="merge_queue"))
            for raw in self.merge_queue_raw
        ]

    def proposals(self) -> list[Proposal]:
        return [
            Proposal(**_filter_kwargs(Proposal, copy.deepcopy(raw), what="proposals"))
            for raw in self.proposals_raw
        ]

    def machines(self) -> list[dict]:
        return copy.deepcopy(self.machines_raw)

    def sessions(self) -> list[dict]:
        return copy.deepcopy(self.sessions_raw)

    def drive_queue(self, repo_name: str | None = None) -> list[dict]:
        """The seeded `coord drive` queue rows (#2428 DQW-1).

        Same raw-row shape ``GET /api/drive-queue`` serves off the real DB
        (``coord.state._decode_drive_queue_row``) — no reshaping, so a
        fixture captured from a live queue (or hand-written to that shape)
        drops in unchanged. ``repo_name`` filters exactly like
        ``coord.state._list_drive_queue_local``'s ``WHERE repo_name = ?``.
        """
        rows = copy.deepcopy(self.drive_queue_raw)
        if repo_name:
            rows = [r for r in rows if r.get("repo_name") == repo_name]
        return rows

    def review_findings(self, assignment_id: str) -> tuple[str, str] | None:
        """Stand-in for ``coord.state.load_assignment_review_findings``.

        Same ``(verdict, body) | None`` contract, keyed by the **review**
        assignment id.
        """
        entry = self.review_findings_raw.get(assignment_id)
        if not entry:
            return None
        return str(entry.get("verdict") or ""), str(entry.get("body") or "")

    def diff(self, assignment_id: str) -> str:
        return self.diffs.get(assignment_id, "")

    def config(self, fallback: Config | None = None) -> Config:
        """Resolve the Config the seeded server runs with.

        An inline ``config`` block in the fixture wins (parsed by the real
        :func:`coord.config.parse_mapping`, so it is validated exactly like a
        coordinator.yml); otherwise *fallback*; otherwise an empty Config whose
        defaults are enough for ``compute_pipeline``.
        """
        if self.config_raw is not None:
            from coord.config import parse_mapping  # noqa: PLC0415

            return parse_mapping(copy.deepcopy(self.config_raw))
        if fallback is not None:
            return fallback
        return Config(repos=[], machines=[])

    # ── Write side (recorded, never executed) ──────────────────────────────

    def record(
        self,
        endpoint: str,
        payload: dict | None = None,
        *,
        method: str = "POST",
        action: str | None = None,
    ) -> RecordedAction:
        entry = RecordedAction(
            seq=len(self._actions) + 1,
            endpoint=endpoint,
            method=method,
            action=action,
            payload=copy.deepcopy(payload) if payload else {},
            at=self.now,
        )
        self._actions.append(entry)
        return entry

    @property
    def actions(self) -> list[RecordedAction]:
        """The recorded-write log, oldest first."""
        return list(self._actions)

    def clear_actions(self) -> int:
        n = len(self._actions)
        self._actions.clear()
        return n


# ── Loading ─────────────────────────────────────────────────────────────────

def _as_list(raw: Any, key: str) -> list:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise FixtureError(f"fixture '{key}' must be a list, got {type(raw).__name__}")
    return raw


def _as_dict(raw: Any, key: str) -> dict:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise FixtureError(f"fixture '{key}' must be a mapping, got {type(raw).__name__}")
    return raw


def _parse_events(raw: Any) -> list[ScriptedEvent]:
    events: list[ScriptedEvent] = []
    for i, entry in enumerate(_as_list(raw, "events")):
        if not isinstance(entry, dict):
            raise FixtureError(f"events[{i}] must be a mapping, got {type(entry).__name__}")
        etype = entry.get("type")
        if not etype or not isinstance(etype, str):
            raise FixtureError(f"events[{i}].type is required (string)")
        try:
            after = float(entry.get("after", 0.0) or 0.0)
        except (TypeError, ValueError) as exc:
            raise FixtureError(f"events[{i}].after must be a number") from exc
        if after < 0:
            raise FixtureError(f"events[{i}].after must be >= 0")
        events.append(ScriptedEvent(type=etype, data=entry.get("data"), after=after))
    return events


def parse_fixture(raw: Any, *, path: Path | None = None) -> FixtureServer:
    """Validate a decoded fixture mapping into a :class:`FixtureServer`."""
    if not isinstance(raw, dict):
        raise FixtureError(f"fixture must be a JSON object, got {type(raw).__name__}")

    board_raw = raw.get("board")
    if board_raw is None:
        # Top-level /board payload shape (what scripts/gen_board_fixture.py
        # emits) — accept it verbatim so a golden capture drops straight in.
        if "assignments" in raw:
            board_raw = raw
        else:
            raise FixtureError(
                "fixture must define a 'board' object (or top-level 'assignments')"
            )
    board_raw = _as_dict(board_raw, "board")
    if not isinstance(board_raw.get("assignments", []), list):
        raise FixtureError("fixture board.assignments must be a list")

    board_payload = {
        "assignments": list(board_raw.get("assignments") or []),
        "round_number": board_raw.get("round_number") or 0,
        "plans": _as_dict(board_raw.get("plans"), "board.plans"),
        "notifications": _as_list(board_raw.get("notifications"), "board.notifications"),
    }

    now = raw.get("now")
    if now is not None:
        try:
            now = float(now)
        except (TypeError, ValueError) as exc:
            raise FixtureError("fixture 'now' must be a number (epoch seconds)") from exc

    config_raw = raw.get("config")
    if config_raw is not None and not isinstance(config_raw, dict):
        raise FixtureError("fixture 'config' must be a mapping (coordinator.yml shape)")

    chat_reply = raw.get("chat_reply")
    if chat_reply is not None and not isinstance(chat_reply, str):
        raise FixtureError("fixture 'chat_reply' must be a string")

    diffs = _as_dict(raw.get("diffs"), "diffs")
    if not all(isinstance(v, str) for v in diffs.values()):
        raise FixtureError("fixture 'diffs' values must be strings")

    server = FixtureServer(
        board_payload=board_payload,
        merge_queue_raw=_as_list(raw.get("merge_queue"), "merge_queue"),
        proposals_raw=_as_list(raw.get("proposals"), "proposals"),
        machines_raw=_as_list(raw.get("machines"), "machines"),
        sessions_raw=_as_list(raw.get("sessions"), "sessions"),
        drive_queue_raw=_as_list(raw.get("drive_queue"), "drive_queue"),
        review_findings_raw=_as_dict(raw.get("review_findings"), "review_findings"),
        diffs=diffs,
        events=_parse_events(raw.get("events")),
        autoplay_events=bool(raw.get("autoplay_events", False)),
        now=now,
        config_raw=config_raw,
        path=path,
    )
    if chat_reply is not None:
        server.chat_reply = chat_reply

    # Fail at load time, not on the first request: a malformed merge_queue /
    # proposals entry should stop `coord web --fixture` from starting rather
    # than 500 halfway through an acceptance run.
    server.merge_queue()
    server.proposals()
    return server


def load_fixture(path: str | Path) -> FixtureServer:
    """Read and validate a fixture JSON file."""
    p = Path(path).expanduser()
    try:
        text = p.read_text()
    except OSError as exc:
        raise FixtureError(f"could not read fixture {p}: {exc}") from exc
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise FixtureError(f"invalid JSON in fixture {p}: {exc}") from exc
    return parse_fixture(raw, path=p)
