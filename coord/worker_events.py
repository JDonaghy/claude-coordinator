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
        summary.rate_limited = True
        resets = raw.get("resets_at") or raw.get("reset_at")
        if isinstance(resets, (int, float)):
            summary.rate_limit_resets_at = float(resets)
        return

    if event.type == "result":
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
        return


# ── File-level helpers ──────────────────────────────────────────────────────


def _read_tail(path: Path, tail_bytes: int) -> str:
    size = path.stat().st_size
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        if tail_bytes and size > tail_bytes:
            f.seek(size - tail_bytes)
            f.readline()  # discard partial line
        return f.read()


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
    for line in text.splitlines():
        ev = parse_event(line)
        if ev is not None:
            yield ev


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
        resets = raw.get("resets_at") or raw.get("reset_at") or "?"
        return f"[rate_limit] resets_at={resets}"

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
