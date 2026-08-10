"""Parse stream-json worker logs into typed events and summaries.

The worker (claude -p) is invoked with `--output-format stream-json --verbose`,
which emits one JSON object per line to stdout. The agent writes that stream
verbatim to ``~/.coord/logs/<assignment_id>.log``.

This module knows how to:

* Detect whether a log is stream-json (vs. plain text from older workers).
* Parse a single line into a :class:`WorkerEvent`.
* Walk the log and build a rolling :class:`WorkerSummary` (turns, cost,
  tools used, files edited, bash commands, rate-limit state, etc.).
* Spot anomaly patterns (repeated bash, rate-limit hits, permission denials).
* Render events as a concise one-line-per-event human-readable form.

The implementation is intentionally permissive — the stream-json shape has
changed over time and varies between claude versions. We accept a handful of
plausible field paths for each thing we care about, and ignore anything we
don't recognise.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


# ── Data classes ────────────────────────────────────────────────────────────


@dataclass
class WorkerEvent:
    """One JSON object from the stream-json log."""

    type: str
    subtype: str | None = None
    raw: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"type": self.type, "subtype": self.subtype, "raw": self.raw}


@dataclass
class WorkerSummary:
    """Rolling summary built from a stream of WorkerEvents."""

    session_id: str | None = None
    model_used: str | None = None
    num_turns: int = 0
    total_cost_usd: float = 0.0
    stop_reason: str | None = None
    permission_denials: list[str] = field(default_factory=list)
    rate_limited: bool = False
    rate_limit_resets_at: float | None = None
    tools_used: list[str] = field(default_factory=list)
    last_tool: str | None = None
    files_edited: list[str] = field(default_factory=list)
    bash_commands: list[str] = field(default_factory=list)
    duration_ms: int | None = None
    # Token counts from the result event (may be zero if the log predates
    # token reporting or the worker didn't emit usage data).
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    # #1584: `is_error` off the LAST `result` event seen (update_summary
    # overwrites these on every `result` line it processes, in log order —
    # never OR'd together), so a worker that hit a transient API error,
    # retried internally, and finished cleanly ends with `is_error=False`
    # here, exactly like any other successful run. `terminal_reason` and
    # `api_error_status` are the same event's diagnostic fields (e.g.
    # `"api_error"` / `529`); `result_text` is its raw `result` string, kept
    # so :func:`format_api_error_reason` can pull a human phrase (e.g.
    # "Overloaded") out of it. All four are blank/False for a log with no
    # `result` event at all, or whose last one wasn't an error.
    is_error: bool = False
    terminal_reason: str | None = None
    api_error_status: int | None = None
    result_text: str | None = None

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "model_used": self.model_used,
            "num_turns": self.num_turns,
            "total_cost_usd": self.total_cost_usd,
            "stop_reason": self.stop_reason,
            "permission_denials": list(self.permission_denials),
            "rate_limited": self.rate_limited,
            "rate_limit_resets_at": self.rate_limit_resets_at,
            "tools_used": list(self.tools_used),
            "last_tool": self.last_tool,
            "files_edited": list(self.files_edited),
            "bash_commands": list(self.bash_commands),
            "duration_ms": self.duration_ms,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_creation_tokens": self.cache_creation_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "is_error": self.is_error,
            "terminal_reason": self.terminal_reason,
            "api_error_status": self.api_error_status,
            "result_text": self.result_text,
        }


# ── Line-level parsing ──────────────────────────────────────────────────────


def parse_event(line: str) -> WorkerEvent | None:
    """Parse a single NDJSON line into a :class:`WorkerEvent`.

    Returns ``None`` for blank lines, lines that aren't valid JSON, or lines
    that don't decode to a JSON object (e.g. arrays, scalars). The log file
    legitimately contains a leading ``# argv=…`` comment line written by the
    agent itself; we just skip past those.
    """
    if not line or not line.strip():
        return None
    try:
        data = json.loads(line)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return WorkerEvent(
        type=data.get("type", "unknown"),
        subtype=data.get("subtype"),
        raw=data,
    )


def is_stream_json(log_path: str | Path) -> bool:
    """Heuristic: is *log_path* a stream-json log?

    The agent prepends a ``# agent=… argv=…`` comment line before spawning the
    worker, so we skip past comment lines and check whether the first
    non-comment line starts with ``{``. Returns ``False`` for missing or
    empty files.
    """
    p = Path(log_path)
    if not p.exists():
        return False
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            for _ in range(50):  # Bound the scan.
                line = f.readline()
                if not line:
                    return False
                stripped = line.lstrip()
                if not stripped:
                    continue
                if stripped.startswith("#"):
                    continue
                return stripped.startswith("{")
    except OSError:
        return False
    return False


# ── Usage-limit kill detection (#1461) ──────────────────────────────────────
#
# A worker (claude -p) that hits the account's Max/Pro *session* usage limit
# mid-flight prints a terminal line like:
#
#   "You've hit your session limit · resets 8:30pm (America/Chicago)"
#
# and exits — with no structured event marking what happened, so the reap
# path records a bare FAILED (or, if the CLI ends the turn gracefully before
# any commit, ADVISORY) indistinguishable from a real defect. This is a
# different signal from the *API* `rate_limit_event` handled above: that one
# is a structured stream-json event describing 429 throttling; this is a
# plain-text kill message from the CLI's own subscription-limit handling, and
# it never arrives as a `rate_limit_event`.


@dataclass
class UsageLimitKill:
    """Diagnostic: the transcript shows the worker was killed by hitting the
    account's session usage limit — not an API rate limit, and not a genuine
    defect. A worker in this state is safe to re-dispatch unchanged once the
    limit resets; it must never be diagnosed as a bug or escalated (e.g. via
    `coord fix`'s model bump).
    """

    reset_at_raw: str
    excerpt: str


# A stable, greppable prefix stamped onto `Assignment.failure_reason` (and
# recognised by `coord/drive.py`'s state machine) whenever a kill is detected
# — see `format_usage_limit_reason`/`is_usage_limit_reason` below.
USAGE_LIMIT_REASON_PREFIX = "usage limit — resets "

# Matches the CLI's own message regardless of whether the apostrophe/middle
# dot appear as literal unicode glyphs (a plain trailing line) or as \uXXXX
# escapes (embedded in a JSON string field) — only the stable ASCII words
# around them are required, so this matches either encoding without first
# decoding the line as JSON. Tolerant of "usage limit" phrasing too, in case
# the CLI's wording changes.
_USAGE_LIMIT_RE = re.compile(
    r"(?:session|usage) limit[^\r\n]{0,40}?resets?\s+([^\r\n\"\\]+)",
    re.IGNORECASE,
)

# How many of the transcript's final non-blank, non-comment lines to search.
# Bounded to exactly the literal last one — the issue's own evidence was
# "the literal last line of the raw transcript", and a bare substring/regex
# search over the whole log would false-positive on a worker that merely
# *discusses* usage limits mid-conversation (this very issue's own worker
# transcript, for instance). Blank lines and coordinator-appended
# `# reap: ...` comments (written to the SAME log file after the worker
# exits, e.g. by the push-attempt bookkeeping in `_reap`) are skipped so this
# still reaches the worker's own real last line despite them.
_USAGE_LIMIT_TAIL_LINES = 1


def format_usage_limit_reason(kill: UsageLimitKill) -> str:
    """Render *kill* as the one-liner stamped onto ``failure_reason``."""
    return f"{USAGE_LIMIT_REASON_PREFIX}{kill.reset_at_raw}"


def is_usage_limit_reason(reason: str | None) -> bool:
    """True iff *reason* is a `failure_reason` stamped by this detector."""
    return bool(reason) and reason.startswith(USAGE_LIMIT_REASON_PREFIX)


def detect_usage_limit_kill(text: str) -> UsageLimitKill | None:
    """Scan *text* (a transcript, or any tail slice of one) for the "hit your
    session limit" kill message.

    Only the last few meaningful (non-blank, non ``#``-comment) lines are
    considered — see ``_USAGE_LIMIT_TAIL_LINES`` — so an incidental mention
    of "session limit" earlier in a normal, successfully-completed
    conversation is never mistaken for a kill. Returns ``None`` when no match
    is found.
    """
    lines = [
        ln for ln in text.splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")
    ]
    for line in reversed(lines[-_USAGE_LIMIT_TAIL_LINES:]):
        m = _USAGE_LIMIT_RE.search(line)
        if not m:
            continue
        reset = m.group(1).strip().strip("\"'.,;: \t")
        if reset:
            return UsageLimitKill(reset_at_raw=reset, excerpt=line[:500])
    return None


def detect_usage_limit_kill_in_log(
    log_path: str | Path, tail_bytes: int = 65536
) -> UsageLimitKill | None:
    """:func:`detect_usage_limit_kill` over the tail of *log_path*.

    Reads at most *tail_bytes* from the end of the file — a kill message is
    always the transcript's last line, so there is never a need to read the
    whole (potentially multi-MB) log. Returns ``None`` for a missing file or
    any read error (best-effort, mirrors ``is_stream_json``).
    """
    p = Path(log_path)
    try:
        size = p.stat().st_size
        with open(p, "rb") as f:
            if size > tail_bytes:
                f.seek(-tail_bytes, 2)
            data = f.read()
    except OSError:
        return None
    return detect_usage_limit_kill(data.decode("utf-8", errors="replace"))


# ── Terminal API-error classification (#1584) ───────────────────────────────
#
# `is_error: true` on a worker's TERMINAL `result` event (`WorkerSummary
# .is_error`, populated above) means the session ended on a real failure —
# most often a transient upstream problem (529 Overloaded, 500, a network
# drop) that killed the worker before it did anything:
#
#   {"is_error": true, "num_turns": 1, "stop_reason": "stop_sequence",
#    "terminal_reason": "api_error", "api_error_status": 529,
#    "result": "API Error: 529 Overloaded. This is a server-side issue,
#    usually temporary...", "total_cost_usd": 0.026247}
#
# Before this, nothing mapped `is_error` to assignment status (it was read
# only for `coord watch`'s display string — see `format_important_event`
# below) so this recorded a clean `done`, indistinguishable from a real
# success. `format_api_error_reason` renders the three diagnostic fields
# into the one-line reason `AgentServer._reap` stamps onto
# `AgentAssignment.api_error_reason` when it flips the assignment to FAILED.

# Pulls the short phrase (e.g. "Overloaded") out of the raw `result` text
# that follows an "API Error: <status>" prefix, when present.
_API_ERROR_PHRASE_RE = re.compile(r"API Error:\s*\d+\s+([^.\r\n]+)")


def format_api_error_reason(
    *,
    terminal_reason: str | None,
    api_error_status: int | None,
    result_text: str | None = None,
) -> str:
    """Render a terminal API-error `result` event as a one-line failure reason.

    Prefers ``"<status> <phrase>"`` (e.g. ``"529 Overloaded"``) when both the
    structured *api_error_status* and a matching phrase in *result_text* are
    available — the shape from #1584's own worked example. Falls back to
    whatever subset of the three fields is present, so a future
    ``terminal_reason``/status combination this doesn't specifically
    recognise still renders something greppable rather than raising or
    going silent.
    """
    phrase: str | None = None
    if result_text:
        m = _API_ERROR_PHRASE_RE.search(result_text)
        if m:
            phrase = m.group(1).strip().rstrip(".") or None
    if api_error_status is not None and phrase:
        return f"{api_error_status} {phrase}"
    if api_error_status is not None:
        # Only append the parenthetical when `terminal_reason` says something
        # `"api_error"` alone doesn't already — the common case (this exact
        # field is almost always the literal string `"api_error"`) would
        # otherwise render the redundant `"api_error 500 (api_error)"`.
        if terminal_reason and terminal_reason != "api_error":
            return f"api_error {api_error_status} ({terminal_reason})"
        return f"api_error {api_error_status}"
    if terminal_reason:
        return f"api_error: {terminal_reason}"
    return "api_error"


# ── Field extraction helpers ────────────────────────────────────────────────


def _is_bash_tool_use(event: WorkerEvent) -> bool:
    """True iff this event represents a Bash tool invocation."""
    if event.type not in ("tool_use", "assistant"):
        return False
    tool_name = _tool_name_from_event(event)
    return tool_name == "Bash"


def _tool_name_from_event(event: WorkerEvent) -> str | None:
    """Try a few plausible field paths for the tool name."""
    raw = event.raw
    if event.type == "tool_use":
        return raw.get("name") or raw.get("tool") or raw.get("tool_name")
    # Assistant events may embed a tool_use block in `message.content[*]`.
    if event.type == "assistant":
        message = raw.get("message") or {}
        for block in _iter_content_blocks(message):
            if block.get("type") == "tool_use":
                return block.get("name")
    return None


def _iter_content_blocks(message: dict) -> Iterable[dict]:
    """Yield content blocks from an Anthropic-style message payload."""
    content = message.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                yield block
    elif isinstance(content, dict):
        yield content


def _bash_command_from_event(event: WorkerEvent) -> str | None:
    raw = event.raw
    # Direct tool_use form: {"type":"tool_use","name":"Bash","input":{"command":"..."}}
    if event.type == "tool_use" and raw.get("name") == "Bash":
        return _command_from_input(raw.get("input"))
    if event.type == "assistant":
        message = raw.get("message") or {}
        for block in _iter_content_blocks(message):
            if block.get("type") == "tool_use" and block.get("name") == "Bash":
                return _command_from_input(block.get("input"))
    return None


def _command_from_input(input_obj: object) -> str | None:
    if not isinstance(input_obj, dict):
        return None
    cmd = input_obj.get("command")
    if isinstance(cmd, str):
        return cmd
    return None


def _file_path_from_event(event: WorkerEvent) -> str | None:
    """Pull file_path out of an Edit/Write tool_use, if present."""
    raw = event.raw
    name = _tool_name_from_event(event)
    if name not in ("Edit", "Write", "NotebookEdit"):
        return None
    if event.type == "tool_use":
        return _file_from_input(raw.get("input"))
    if event.type == "assistant":
        message = raw.get("message") or {}
        for block in _iter_content_blocks(message):
            if block.get("type") == "tool_use" and block.get("name") in (
                "Edit",
                "Write",
                "NotebookEdit",
            ):
                return _file_from_input(block.get("input"))
    return None


def _file_from_input(input_obj: object) -> str | None:
    if not isinstance(input_obj, dict):
        return None
    for key in ("file_path", "path", "notebook_path"):
        v = input_obj.get(key)
        if isinstance(v, str):
            return v
    return None


# ── rate_limit_event wire shape (#1466) ─────────────────────────────────────
#
# Claude Code v2.1.220 emits a `rate_limit_event` on essentially every run —
# it is the *healthy* case, not a throttle signal:
#
#   {"type": "rate_limit_event",
#    "rate_limit_info": {"status": "allowed", "resetsAt": 1785133800,
#      "rateLimitType": "five_hour", "overageStatus": "rejected",
#      "overageDisabledReason": "org_level_disabled", "isUsingOverage": false},
#    "uuid": "...", "session_id": "..."}
#
# Everything lives nested under `rate_limit_info`, camelCase. There is no
# top-level `resets_at`/`reset_at` — that shape was invented (pre-#1466) and
# the real CLI never emits it, which is why `rate_limit_resets_at` was always
# ``None`` and `render_event` always printed `resets_at=?`. Only
# `allowed_warning` and `rejected` mean the account is actually throttled;
# `allowed` is the normal, common case and must never set `rate_limited`.
#
# `format_important_event` already read this shape correctly (nested,
# camelCase, status-gated) — this helper is the single place both it and
# `update_summary`/`render_event` now go through, so the two paths can't
# disagree about the wire format again.
_RATE_LIMIT_THROTTLED_STATUSES = frozenset({"allowed_warning", "rejected"})


def _rate_limit_info(raw: dict) -> tuple[str | None, float | None]:
    """Pull ``(status, resets_at)`` out of a ``rate_limit_event``'s payload.

    Returns ``(None, None)`` when ``rate_limit_info`` is missing or not a
    dict — a shape we don't recognise, not something to guess at.
    """
    info = raw.get("rate_limit_info")
    if not isinstance(info, dict):
        return None, None
    status = info.get("status")
    if not isinstance(status, str):
        status = None
    resets = info.get("resetsAt")
    if not isinstance(resets, (int, float)):
        resets = None
    else:
        resets = float(resets)
    return status, resets


def _is_rate_limit_throttled(status: str | None) -> bool:
    """True iff *status* means the account is actually being throttled.

    ``allowed`` (no status, or any other value) is the healthy/normal case.
    """
    return status in _RATE_LIMIT_THROTTLED_STATUSES


def _assistant_text(event: WorkerEvent) -> str:
    """First text block from an assistant message, truncated for display."""
    raw = event.raw
    message = raw.get("message") or {}
    for block in _iter_content_blocks(message):
        if block.get("type") == "text":
            txt = block.get("text") or ""
            if isinstance(txt, str):
                return txt.strip()
    # Some shapes carry top-level text on the event itself.
    direct = raw.get("text")
    if isinstance(direct, str):
        return direct.strip()
    return ""


# ── Streaming summary update ───────────────────────────────────────────────


def update_summary(summary: WorkerSummary, event: WorkerEvent) -> None:
    """Fold *event* into *summary* in-place."""
    raw = event.raw

    if event.type == "system" and event.subtype == "init":
        sid = raw.get("session_id") or raw.get("id")
        if isinstance(sid, str):
            summary.session_id = sid
        model = raw.get("model") or (raw.get("config") or {}).get("model")
        if isinstance(model, str) and not summary.model_used:
            summary.model_used = model
        return

    if event.type == "assistant":
        summary.num_turns += 1
        message = raw.get("message") or {}
        model = message.get("model") or raw.get("model")
        if isinstance(model, str):
            summary.model_used = model
        # Tool uses can be nested in the assistant message content.
        for block in _iter_content_blocks(message):
            if block.get("type") == "tool_use":
                name = block.get("name")
                if isinstance(name, str):
                    summary.tools_used.append(name)
                    summary.last_tool = name
                if name == "Bash":
                    cmd = _command_from_input(block.get("input"))
                    if cmd:
                        summary.bash_commands.append(cmd)
                elif name in ("Edit", "Write", "NotebookEdit"):
                    fp = _file_from_input(block.get("input"))
                    if fp:
                        summary.files_edited.append(fp)
        return

    if event.type == "tool_use":
        name = _tool_name_from_event(event)
        if name:
            summary.tools_used.append(name)
            summary.last_tool = name
        if name == "Bash":
            cmd = _bash_command_from_event(event)
            if cmd:
                summary.bash_commands.append(cmd)
        elif name in ("Edit", "Write", "NotebookEdit"):
            fp = _file_path_from_event(event)
            if fp:
                summary.files_edited.append(fp)
        return

    if event.type == "rate_limit_event":
        status, resets = _rate_limit_info(raw)
        if _is_rate_limit_throttled(status):
            summary.rate_limited = True
            if resets is not None:
                summary.rate_limit_resets_at = resets
        return

    if event.type == "result":
        # #1584: overwrite (never OR/append) on every `result` event so the
        # final state after a full parse reflects only the LAST one — a
        # worker that hit a transient API error, retried internally, and
        # finished cleanly has an earlier `result` line with `is_error: true`
        # followed by a final one without it, and only the latter must
        # survive.
        summary.is_error = bool(raw.get("is_error"))
        tr = raw.get("terminal_reason")
        summary.terminal_reason = tr if isinstance(tr, str) else None
        aes = raw.get("api_error_status")
        summary.api_error_status = aes if isinstance(aes, int) else None
        rtext = raw.get("result")
        summary.result_text = rtext if isinstance(rtext, str) else None
        cost = raw.get("total_cost_usd") or raw.get("cost_usd")
        if isinstance(cost, (int, float)):
            summary.total_cost_usd = float(cost)
        stop = raw.get("stop_reason") or raw.get("subtype")
        if isinstance(stop, str):
            summary.stop_reason = stop
        turns = raw.get("num_turns")
        if isinstance(turns, int) and turns >= summary.num_turns:
            # Prefer the explicit count from claude when available.
            summary.num_turns = turns
        dur = raw.get("duration_ms") or raw.get("duration")
        if isinstance(dur, (int, float)):
            summary.duration_ms = int(dur)
        denials = raw.get("permission_denials") or []
        if isinstance(denials, list):
            for d in denials:
                if isinstance(d, str):
                    summary.permission_denials.append(d)
                elif isinstance(d, dict):
                    label = (
                        d.get("tool_name")
                        or d.get("tool")
                        or d.get("name")
                        or json.dumps(d, sort_keys=True)
                    )
                    summary.permission_denials.append(str(label))
        # Extract token counts. Claude may report them under a nested
        # ``usage`` object or at the top level — try both forms.
        usage_obj = raw.get("usage") or {}
        if not isinstance(usage_obj, dict):
            usage_obj = {}

        def _tok(key: str, *alt_keys: str) -> int:
            """Return first non-zero int found across key variants."""
            for k in (key, *alt_keys):
                v = usage_obj.get(k) or raw.get(k)
                if isinstance(v, int) and v > 0:
                    return v
            return 0

        summary.input_tokens = _tok("input_tokens")
        summary.output_tokens = _tok("output_tokens")
        summary.cache_creation_tokens = _tok(
            "cache_creation_input_tokens", "cache_creation_tokens"
        )
        summary.cache_read_tokens = _tok(
            "cache_read_input_tokens", "cache_read_tokens"
        )
        return


# ── File-level helpers ──────────────────────────────────────────────────────


def _read_tail(path: Path, tail_bytes: int) -> str:
    size = path.stat().st_size
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        if tail_bytes and size > tail_bytes:
            f.seek(size - tail_bytes)
            f.readline()  # discard partial line
        return f.read()


def iter_events_from_text(text: str) -> Iterable[WorkerEvent]:
    """Yield :class:`WorkerEvent` for each parseable line in an in-memory
    string. Shared by :func:`iter_events` (file-backed) and callers that
    already have the log text in hand (e.g. fetched over HTTP from a
    remote agent) and want to avoid a redundant write-then-read."""
    for line in text.splitlines():
        ev = parse_event(line)
        if ev is not None:
            yield ev


def iter_events(log_path: str | Path, *, tail_bytes: int = 0) -> Iterable[WorkerEvent]:
    """Yield :class:`WorkerEvent` for each parseable line in *log_path*.

    With ``tail_bytes`` > 0, only the last *tail_bytes* of the file is read
    (after skipping a partial leading line). Use this for cheap polling of
    live, long-running assignments.
    """
    p = Path(log_path)
    if not p.exists():
        return
    try:
        text = _read_tail(p, tail_bytes)
    except OSError:
        return
    yield from iter_events_from_text(text)


# ── Single-latest-turn extraction (#2048) ───────────────────────────────────
#
# The liveness auditor (coord/liveness_auditor.py) must see ONLY the single
# most recent assistant turn — never the transcript. These helpers pick that
# one turn's text (or, for a tool-only turn with no text block, a compact
# summary of which tools it called) out of a stream-json log.


def _turn_text_or_tool_summary(event: WorkerEvent) -> str:
    text = _assistant_text(event)
    if text:
        return text
    message = event.raw.get("message") or {}
    tool_names = [
        block.get("name")
        for block in _iter_content_blocks(message)
        if block.get("type") == "tool_use"
    ]
    tool_names = [t for t in tool_names if t]
    return f"[tool_use: {', '.join(tool_names)}]" if tool_names else ""


def latest_assistant_turn_text_from_text(text: str) -> str | None:
    """Return the most recent assistant turn's text (or tool-use summary)
    found in *text*, or ``None`` if the text contains no assistant turn at
    all. An empty string is a real, meaningful result (the last turn
    produced neither text nor a recognised tool call) and is distinct from
    ``None`` (no turn found to look at)."""
    found = False
    last_text = ""
    for event in iter_events_from_text(text):
        if event.type != "assistant":
            continue
        found = True
        last_text = _turn_text_or_tool_summary(event)
    return last_text if found else None


def latest_assistant_turn_text(
    log_path: str | Path, *, tail_bytes: int = 65536
) -> str | None:
    """File-backed counterpart to
    :func:`latest_assistant_turn_text_from_text` — reads only the tail of
    *log_path* (a full stream-json transcript can be multi-MB; the auditor
    only ever needs the last turn) and returns ``None`` for a missing file,
    read error, or a tail slice with no assistant turn in it."""
    p = Path(log_path)
    if not p.exists():
        return None
    try:
        text = _read_tail(p, tail_bytes)
    except OSError:
        return None
    return latest_assistant_turn_text_from_text(text)


def parse_log(log_path: str | Path, tail_bytes: int = 65536) -> WorkerSummary:
    """Parse a stream-json log file into a :class:`WorkerSummary`.

    For active assignments we only read the tail to stay cheap. The fields
    that come from the ``init`` event (session_id, model) and per-turn
    accumulations (cost, turns) are still useful even from a tail read,
    though session_id may be missing if the head of the log has rolled off.
    Callers that need a fully reliable summary should pass ``tail_bytes=0``.
    """
    summary = WorkerSummary()
    for event in iter_events(log_path, tail_bytes=tail_bytes):
        update_summary(summary, event)
    return summary


# ── Human-readable rendering ────────────────────────────────────────────────


def _truncate(text: str, n: int = 80) -> str:
    text = text.replace("\n", " ").strip()
    if len(text) <= n:
        return text
    return text[: n - 1] + "…"


def _format_duration(ms: int | None) -> str:
    if ms is None:
        return "?"
    seconds = ms / 1000.0
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, secs = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m {secs}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


def render_event(event: WorkerEvent, *, turn_counter: list[int] | None = None) -> str | None:
    """Render an event as a single human-readable line. Returns None to skip."""
    raw = event.raw

    if event.type == "system" and event.subtype == "init":
        model = raw.get("model") or (raw.get("config") or {}).get("model") or "?"
        sid = raw.get("session_id") or raw.get("id") or "?"
        return f"[init] model={model} session={sid}"

    if event.type == "assistant":
        if turn_counter is not None:
            turn_counter[0] += 1
            n = turn_counter[0]
        else:
            n = 0
        text = _assistant_text(event)
        # If this assistant turn is purely a tool call, the text block may
        # be empty — render a placeholder so the timeline still ticks.
        if text:
            return f"[assistant] Turn {n}: {_truncate(text, 100)!r}"
        # Try to summarise the tool calls.
        message = raw.get("message") or {}
        tool_names = [
            block.get("name")
            for block in _iter_content_blocks(message)
            if block.get("type") == "tool_use"
        ]
        tool_names = [t for t in tool_names if t]
        if tool_names:
            return f"[assistant] Turn {n}: tool_use={','.join(tool_names)}"
        return f"[assistant] Turn {n}"

    if event.type == "tool_use":
        name = _tool_name_from_event(event) or "?"
        if name == "Bash":
            cmd = _bash_command_from_event(event) or ""
            return f"[tool] Bash: {_truncate(cmd, 100)}"
        if name in ("Edit", "Write", "NotebookEdit"):
            fp = _file_path_from_event(event)
            return f"[tool] {name}: {fp or '?'}"
        return f"[tool] {name}"

    if event.type == "tool_result":
        # Tool results are usually noisy — keep a compact form.
        tool_use_id = raw.get("tool_use_id") or "?"
        is_error = raw.get("is_error")
        tag = " error" if is_error else ""
        return f"[tool_result{tag}] {tool_use_id}"

    if event.type == "rate_limit_event":
        status, resets = _rate_limit_info(raw)
        return f"[rate_limit] status={status or '?'} resets_at={resets if resets is not None else '?'}"

    if event.type == "result":
        cost = raw.get("total_cost_usd") or raw.get("cost_usd") or 0.0
        stop = raw.get("stop_reason") or raw.get("subtype") or "?"
        turns = raw.get("num_turns") or "?"
        dur = _format_duration(raw.get("duration_ms"))
        return (
            f"[result] completed in {dur}, {turns} turns, "
            f"${float(cost):.2f}, stop={stop}"
        )

    # Anything else: render type/subtype only — keep one line.
    if event.subtype:
        return f"[{event.type}] {event.subtype}"
    return f"[{event.type}]"


def render_log(log_path: str | Path) -> Iterable[str]:
    """Yield rendered lines for every event in *log_path*."""
    turn_counter = [0]
    for event in iter_events(log_path):
        line = render_event(event, turn_counter=turn_counter)
        if line is not None:
            yield line


def format_important_event(event: WorkerEvent) -> str | None:
    """Format an event for ``coord watch`` output.

    Returns a human-readable string if the event is *important* (i.e. worth
    showing in filtered live output), or ``None`` to skip it.
    """
    raw = event.raw

    if event.type == "system" and event.subtype == "init":
        model = raw.get("model") or (raw.get("config") or {}).get("model") or "unknown"
        session = str(raw.get("session_id") or raw.get("id") or "?")[:8]
        return f"[init] {model} session {session}"

    if event.type == "rate_limit_event":
        status, resets = _rate_limit_info(raw)
        # Only surface throttled events — `allowed` is the healthy, common
        # case (fires on essentially every run) and must stay silent.
        if _is_rate_limit_throttled(status):
            return f"[rate_limit] {status}, resets at {resets if resets is not None else '?'}"
        return None

    if event.type == "result":
        dur = (raw.get("duration_ms") or 0) / 1000
        turns = raw.get("num_turns") or 0
        cost = raw.get("total_cost_usd") or raw.get("cost_usd") or 0
        stop = raw.get("stop_reason") or raw.get("subtype") or "?"
        is_err = raw.get("is_error", False)
        mins, secs = divmod(int(dur), 60)
        result_status = "failed" if is_err else "completed"
        base = f"[result] {result_status} in {mins}m {secs}s, {turns} turns, ${float(cost):.2f}, stop={stop}"
        # Surface permission denials attached to the result event
        denials = raw.get("permission_denials") or []
        denial_lines: list[str] = []
        if isinstance(denials, list):
            for d in denials:
                if isinstance(d, str):
                    denial_lines.append(f"[denied] {d}")
                elif isinstance(d, dict):
                    label = (
                        d.get("tool_name")
                        or d.get("tool")
                        or d.get("name")
                        or d.get("reason")
                        or str(d)
                    )
                    reason = d.get("reason") or d.get("message") or ""
                    if reason:
                        denial_lines.append(f"[denied] {label}: {reason}")
                    else:
                        denial_lines.append(f"[denied] {label}")
        if denial_lines:
            return base + "\n" + "\n".join(denial_lines)
        return base

    if event.type == "assistant":
        # Scan text blocks for STUCK: signal
        message = raw.get("message") or {}
        content = message.get("content") or []
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text") or ""
                    if "STUCK:" in text:
                        stuck_line = next(
                            (ln for ln in text.split("\n") if "STUCK:" in ln),
                            text[:200],
                        )
                        return f"[stuck] {stuck_line.strip()}"
        return None

    return None


# ── Anomaly detection ──────────────────────────────────────────────────────


def detect_anomalies(log_path: str | Path, *, tail_bytes: int = 65536) -> list[str]:
    """Scan a stream-json log for anomaly patterns. Returns warning strings."""
    warnings: list[str] = []
    summary = WorkerSummary()
    bash_cmds: list[str] = []
    saw_commit = False

    for event in iter_events(log_path, tail_bytes=tail_bytes):
        update_summary(summary, event)
        cmd = _bash_command_from_event(event)
        if cmd:
            bash_cmds.append(cmd)
            # A `git commit` command (with or without flags) breaks the
            # "many turns, no commit" pattern.
            if cmd.lstrip().startswith("git commit"):
                saw_commit = True

    # Repeated identical bash invocations.
    if bash_cmds:
        counts = Counter(bash_cmds)
        for cmd, n in counts.items():
            if n >= 3:
                warnings.append(
                    f"bash command repeated {n}x: {_truncate(cmd, 60)}"
                )

    # Rate-limit hit anywhere in the log.
    if summary.rate_limited:
        resets = summary.rate_limit_resets_at
        warnings.append(
            f"rate limited (resets at {resets})" if resets else "rate limited"
        )

    # Permission denials in the final result.
    if summary.permission_denials:
        joined = ", ".join(summary.permission_denials[:5])
        warnings.append(f"permission denials: {joined}")

    # Many turns without a commit — possible runaway / lost worker.
    if summary.num_turns >= 15 and not saw_commit:
        warnings.append(
            f"{summary.num_turns} turns without a git commit"
        )

    return warnings
