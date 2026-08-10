"""Cheap, independent, per-turn liveness auditor (#2048).

Fills the gap between three existing stall signals:

1. Worker self-report (``STATUS:``/``STUCK:`` lines, ``coord/worker_events.py``)
   — free, but a worker that doesn't know it's stuck never says so.
2. ``EVENT_NEEDS_ATTENTION`` (``coord/notify.py``'s ``attention_signal``) —
   independent, but it's a clock, not a judge: it can say "90 minutes
   elapsed", not "this has been circling for six turns".
3. Adversarial review / Test agent / sealed oracle — independent judgment,
   but metered and only runs once per stage boundary.

This module is tier 2.5: independent judgment, per turn, at clock prices.
A small model (default Claude Haiku) sees ONLY the assignment's objective
and the raw output of its single most recent turn — never the transcript,
never the worker's own ``STATUS:``/``STUCK:`` self-report (feeding those in
would just re-introduce the tier-1 problem this exists to route around) —
and rules ``continue`` / ``done`` / ``blocked``. The audit's context is
fixed-size (~1k tokens in, ~30 out) regardless of how long the session has
run, which is what keeps it cheap on turn 300 as well as turn 5.

**This module gates nothing.** It has no access to (and must never be
given) anything that could change board state — no ``Assignment.status``,
no ``review_state``, no ``test_state``. Its only output is a verdict and a
strike count; ``coord.notify.detect_liveness_stall`` is the only caller,
and it only ever posts a diagnostic comment + records a durable audit
trail. See that function's docstring for the "never gates" contract.

Runs the check via a one-shot ``claude -p`` subprocess (design decision
(a) in #2048 — no new API key, no new SDK dependency, matches "No API key
needed" / "No Anthropic SDK" in CLAUDE.md). The ~1-2s spawn cost is off the
critical path (the worker never waits on this) and bounded by the debounce
interval, not by turn count.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass

# ── Verdicts ─────────────────────────────────────────────────────────────

CONTINUE = "continue"
DONE = "done"
BLOCKED = "blocked"
KNOWN_VERDICTS = frozenset({CONTINUE, DONE, BLOCKED})

DEFAULT_MODEL = "claude-haiku-4-5"
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_STRIKES = 3
DEFAULT_DEBOUNCE_SECONDS = 60.0

_VERDICT_RE = re.compile(r"\b(continue|done|blocked)\b", re.IGNORECASE)

# Lines the worker wrote about its OWN state — the tier-1 self-report this
# auditor exists to be independent of. Stripped from the turn text before
# it ever reaches the model, so a worker that falsely claims "STATUS:
# making great progress" can't talk the auditor out of a stall verdict, and
# a worker's own "STUCK:" line can't be reused as second-hand evidence
# either.
_SELF_REPORT_LINE_RE = re.compile(r"^\s*(STATUS|STUCK):.*$", re.MULTILINE | re.IGNORECASE)

# The audit's whole cost model rests on a FIXED context size — see the
# module docstring's "~1,000 in / ~30 out" breakdown, and the issue's own
# "objective / briefing excerpt — ~300 tokens" line (note: *excerpt*, not
# the whole document). Nothing upstream of this module enforces that: the
# caller (``coord.notify.detect_liveness_stall``) passes an assignment's
# raw ``briefing`` straight through, and a full GitHub-issue briefing
# regularly runs 5,000-15,000+ tokens (see ``AgentAssignment.to_status_dict``'s
# docstring in ``coord/agent.py`` — "a full briefing can be tens of KB").
# Truncate defensively here, in the one place every caller funnels
# through, so the excerpt the cost model assumes is what the model
# actually sees regardless of what a caller was handed.
_OBJECTIVE_EXCERPT_CHARS = 1_200  # ~300 tokens @ ~4 chars/token
_TURN_EXCERPT_CHARS = 2_000  # ~500 tokens @ ~4 chars/token


def _excerpt(text: str, limit: int) -> str:
    """Truncate *text* to at most *limit* characters, marking truncation
    so a shortened excerpt is never silently indistinguishable from the
    full text."""
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + " …[truncated]"


_AUDIT_SYSTEM_PROMPT = (
    "You are a cheap, independent liveness auditor watching a coding agent "
    "one turn at a time. You do NOT see the conversation history and you "
    "do NOT see anything the worker says about its own status — only the "
    "objective it was given and the raw output of its single MOST RECENT "
    "turn. Judge whether that one turn shows the worker making real "
    "progress toward the objective, having already finished it, or stuck.\n\n"
    "Reply with EXACTLY ONE WORD and nothing else:\n"
    "  continue - the turn shows real, concrete progress (a file changed, "
    "a command ran and moved things forward, new information was gathered)\n"
    "  done - the turn indicates the objective is complete\n"
    "  blocked - the turn shows no real progress: repeating an earlier "
    "action, going in circles, confused, or stuck\n\n"
    "One word only: continue, done, or blocked."
)


def strip_self_report_lines(text: str) -> str:
    """Remove any ``STATUS:``/``STUCK:`` line from *text*.

    Applied to a turn's raw text before it is ever sent to the auditor —
    see the module docstring's "context isolation" note. Pure string
    transform, safe to unit test without a subprocess.
    """
    if not text:
        return text
    return _SELF_REPORT_LINE_RE.sub("", text).strip()


def build_audit_user_message(objective: str, turn_text: str) -> str:
    """Render the ``(objective, latest turn)`` pair as the auditor's one
    and only user message. No transcript, no history — this string IS the
    entire context the model sees, by construction.

    Both inputs are excerpted (see ``_OBJECTIVE_EXCERPT_CHARS``/
    ``_TURN_EXCERPT_CHARS`` above) so a caller handing this a raw,
    multi-KB assignment briefing or an unusually large turn still gets
    the fixed-size context the auditor's cost model is built on."""
    objective = (objective or "").strip() or "(no objective provided)"
    objective = _excerpt(objective, _OBJECTIVE_EXCERPT_CHARS)
    turn_text = (turn_text or "").strip() or "(no output on this turn)"
    turn_text = _excerpt(turn_text, _TURN_EXCERPT_CHARS)
    return f"OBJECTIVE:\n{objective}\n\nLATEST TURN OUTPUT:\n{turn_text}"


def parse_verdict(text: str) -> str | None:
    """Pull the verdict word out of the model's reply, or ``None`` if it
    said something unparseable. A ``None`` verdict is treated as "the audit
    itself failed" by :func:`apply_verdict` — it never counts as evidence
    either way, so a flaky/garbled response can't accidentally contribute
    to (or reset) a stall streak."""
    if not text:
        return None
    m = _VERDICT_RE.search(text)
    return m.group(1).lower() if m else None


@dataclass
class AuditOutcome:
    """Result of one :func:`run_audit` call."""

    verdict: str | None  # one of KNOWN_VERDICTS, or None on parse/subprocess failure
    raw_output: str
    cost_usd: float | None = None
    error: str | None = None


def _build_audit_command(*, model: str, claude_bin: str | None) -> list[str]:
    from coord.providers.claude import ClaudeProvider  # noqa: PLC0415

    provider = ClaudeProvider(binary=claude_bin)
    cmd = provider.oneshot_command(system_prompt=_AUDIT_SYSTEM_PROMPT, output_format="json")
    cmd.extend(["--model", model])
    return cmd


def _parse_json_envelope(stdout: str) -> tuple[str, float | None]:
    """Pull ``(result_text, total_cost_usd)`` out of a ``claude -p
    --output-format json`` envelope. Falls back to the raw stdout (and no
    cost) for a malformed/unexpected shape — best-effort, matches
    ``coord.brain.call_claude``'s own fallback."""
    try:
        outer = json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        return stdout.strip(), None
    if not isinstance(outer, dict):
        return stdout.strip(), None
    text = outer.get("result")
    if not isinstance(text, str):
        text = stdout.strip()
    cost = outer.get("total_cost_usd")
    if not isinstance(cost, (int, float)):
        cost = None
    else:
        cost = float(cost)
    return text, cost


def run_audit(
    objective: str,
    turn_text: str,
    *,
    model: str = DEFAULT_MODEL,
    claude_bin: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> AuditOutcome:
    """Run one liveness audit: a one-shot ``claude -p`` subprocess call.

    *objective* and *turn_text* are the ONLY context passed — no transcript,
    no session id, no ``--resume``. Each call is a fresh, independent
    process with no memory of any prior audit, by construction.

    Never raises: subprocess failures, timeouts, and unparseable replies
    all come back as ``AuditOutcome(verdict=None, ...)`` so a flaky audit
    can never crash (or gate) the caller.
    """
    cmd = _build_audit_command(model=model, claude_bin=claude_bin)
    user_message = build_audit_user_message(objective, turn_text)
    try:
        result = subprocess.run(  # noqa: S603 — fixed argv, no shell
            cmd,
            input=user_message,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return AuditOutcome(verdict=None, raw_output="", error=str(exc))

    if result.returncode != 0:
        return AuditOutcome(
            verdict=None,
            raw_output=result.stdout or "",
            error=f"exit {result.returncode}: {(result.stderr or '').strip()[:200]}",
        )

    text, cost = _parse_json_envelope(result.stdout)
    return AuditOutcome(verdict=parse_verdict(text), raw_output=text, cost_usd=cost)


# ── Debounce + strike tracking (pure) ───────────────────────────────────────


@dataclass
class AuditState:
    """Per-assignment liveness-audit tracking state.

    Persisted via ``coord.state.load_liveness_audit_state`` /
    ``save_liveness_audit_state`` (backed by the ``liveness_audits`` table)
    so the strike streak survives across separate ``coord notify``
    invocations — this is polled state, not held in a long-lived process.
    """

    consecutive_blocked: int = 0
    last_audit_at: float | None = None
    last_verdict: str | None = None
    # True once this assignment's streak has already reached the strike
    # threshold and the one-shot event has been raised for it. Once set,
    # detect_liveness_stall stops auditing this assignment entirely — a
    # stall that's already been reported doesn't need re-reporting every
    # poll while it stays blocked.
    raised: bool = False


def should_audit(
    *, last_audit_at: float | None, now: float, debounce_seconds: float
) -> bool:
    """Debounce gate: at most one audit per *debounce_seconds*.

    A stall is a multi-minute phenomenon — auditing every turn buys no
    extra signal and pays for a process spawn each time. ``last_audit_at
    is None`` (never audited) always returns True.
    """
    if last_audit_at is None:
        return True
    return (now - last_audit_at) >= debounce_seconds


def apply_verdict(
    state: AuditState, verdict: str | None, *, now: float, strikes: int
) -> tuple[AuditState, bool]:
    """Fold one audit verdict into *state*.

    Returns ``(new_state, just_raised)``. ``just_raised`` is True exactly
    on the call whose BLOCKED verdict brings ``consecutive_blocked`` to
    *strikes* for the FIRST time — edge-triggered, so a streak that stays
    at or above the threshold doesn't re-raise on every subsequent poll.
    A ``None`` verdict (audit failed / unparseable) leaves the streak
    untouched: an audit we can't read must never count as evidence either
    way, in either direction.
    """
    new_count = state.consecutive_blocked
    if verdict == BLOCKED:
        new_count += 1
    elif verdict in (CONTINUE, DONE):
        new_count = 0

    just_raised = (not state.raised) and new_count >= strikes
    new_state = AuditState(
        consecutive_blocked=new_count,
        last_audit_at=now,
        last_verdict=verdict if verdict is not None else state.last_verdict,
        raised=state.raised or just_raised,
    )
    return new_state, just_raised
