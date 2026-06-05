"""ClaudePtyProvider: interactive ``claude`` driven through a pseudo-terminal.

This is the **metering escape hatch** introduced for #425.  After 2026-06-15,
``claude -p`` (the :class:`~.claude.ClaudeProvider` backend) is billed at full
API rates against a non-rolling credit pool — see #322.  Interactive
``claude`` (no ``-p``) is still covered by the Max/Pro subscription, so the
coordinator exposes it as a separate provider that **opt-in** users can
select via ``coordinator.yml`` ``providers`` config or per-spec
``provider="claude-pty"``.

Differences from :class:`~.claude.ClaudeProvider`:

* ``build_command()`` does **not** pass ``-p`` and does **not** use
  ``stream-json`` formats.  Interactive ``claude`` reads typed input from a
  TTY and renders TUI output — the agent attaches the worker to a PTY
  (:mod:`pty`) in :meth:`coord.agent.AgentServer._spawn_pty`.
* ``initial_input()`` returns the briefing as **plain text + newline** (the
  bytes typed into the TTY), not a stream-json envelope.
* ``capabilities()`` reports ``billing_mode="subscription"`` (this is the
  whole point) and conservatively reports ``enforces_deny_list=False`` —
  the interactive PTY path has **not been verified** to honour
  ``--allowedTools`` / ``--permission-mode`` in this PR, so the agent's
  safety gate refuses to run write-capable assignment types (``work``,
  ``review``, ``smoke``, ``conflict-fix``) on this provider until that
  verification lands.  Non-mutating assignment types (``plan``,
  ``refinement``, ``test-chat``, ``new-issue-chat``) may still use it.
* ``parse_log()`` delegates to :func:`coord.worker_events.parse_log` so it
  degrades gracefully when the log contains raw TTY bytes rather than
  stream-json — :func:`~coord.worker_events.parse_log` silently skips
  non-JSON lines and returns a blank :class:`WorkerSummary`.

Imports from :mod:`coord.agent` are deferred (inside method bodies) to keep
the import cycle latent, matching the pattern in
:class:`~.claude.ClaudeProvider`.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from coord.providers.base import Capabilities, Provider, WorkerSummary

if TYPE_CHECKING:
    from coord.agent import AssignmentSpec


# Sentinel string written to the log file by the PTY spawn path when the
# worker exits.  Used as :meth:`ClaudePtyProvider.result_marker` because the
# interactive ``claude`` CLI does NOT emit a stream-json ``result`` event,
# so the standard ``"type":"result"`` marker never appears.
#
# MUST stay byte-equal to ``coord.agent._PTY_RESULT_LINE_MARKER`` (which is
# the bytes form of this constant — kept separate there to dodge a
# module-level import cycle).  The sync is asserted in
# ``tests/test_agent_reap.py::test_pty_marker_bytes_sync_with_provider_string``.
PTY_RESULT_MARKER = "# pty: worker exited"

# Briefing-submission control bytes for the interactive TUI (#425).
#
# A multi-line briefing MUST be delivered inside a bracketed-paste block
# (``ESC[200~`` ... ``ESC[201~``) — otherwise the TUI treats each embedded
# newline as a soft line break and the message is never submitted.  The
# submitting Enter is a CARRIAGE RETURN (``\r``); ``\n`` does NOT submit under
# the TUI's enhanced (kitty) keyboard protocol.  The CR must be written
# *separately*, after a short settle delay, by
# :meth:`coord.agent.AgentServer._spawn_pty` — a CR glued onto ``ESC[201~`` in
# the same write is swallowed by paste-end processing.  ``initial_input``
# returns only the bracketed-paste block; the spawn path appends the CR.
# Verified live against interactive ``claude`` v2.1.165 (#425 smoke: raw
# multi-line never submits; bracketed-paste + separate CR reliably does).
BRACKETED_PASTE_START = b"\x1b[200~"
BRACKETED_PASTE_END = b"\x1b[201~"

# DECSET emitted by the TUI once it has ENABLED bracketed-paste input — the
# spawn path waits for this (then for render quiescence) before pasting, so the
# briefing is never sent before the TUI can accept it (the enable arrives
# ~0.85s after first output, while the TUI is still drawing; pasting then is
# silently dropped).
BRACKETED_PASTE_ENABLE = b"\x1b[?2004h"


# #426 — Completion sentinel.
#
# Interactive ``claude`` is a REPL: it returns to an idle prompt after each
# turn and does NOT exit, so ``proc.wait()`` would block until the
# ``_REAP_MAX_WAIT`` (2 h) safety net fires.  To detect logical turn
# completion, autonomous PTY workers (plan / work / review / smoke /
# conflict-fix) are instructed via their system prompt to emit this
# string on its own line as the LAST thing they output.  The
# :func:`coord.agent._pty_completion_watcher` thread polls the
# ANSI-stripped log for it and force-kills the process group on detection,
# which closes the PTY → ``_pump`` stamps :data:`PTY_RESULT_MARKER` →
# ``_reap`` completes and pushes the worker's branch.
#
# The sentinel is INJECTED INTO THE SYSTEM PROMPT, not the briefing — a
# briefing pasted into the TUI is echoed in the input area and would
# trigger the watcher prematurely on its own text.  System prompts arrive
# via ``--system-prompt`` and are NEVER rendered in the TTY output.  This
# distinction was verified live against interactive ``claude`` v2.1.160
# (#426 smoke).
COMPLETION_SENTINEL = "COORD_PTY_DONE"

# Spec types whose system prompt is augmented with the completion sentinel
# instruction.  Chat-style sessions (refinement / test-chat /
# new-issue-chat) are interactive with the developer and end via
# :meth:`coord.agent.AgentServer.cancel`, not via a self-emitted
# sentinel, so they are NOT augmented and the completion watcher does
# not run for them.
_AUTONOMOUS_SPEC_TYPES: frozenset[str] = frozenset({
    "plan",
    "work",
    "review",
    "smoke",
    "conflict-fix",
})


def _completion_instruction(spec_type: str) -> str:
    """Build the completion-sentinel instruction appended to the system prompt.

    For ``review`` workers, also instructs emission of a short
    ``VERDICT: approve|request-changes`` line right before the sentinel so
    :func:`coord.providers.claude_pty._parse_pty_log` can pick up the
    review verdict directly from the TTY-rendered output.
    """
    lines: list[str] = [
        "",
        "",
        f"#426 — PTY worker completion protocol (you are running interactive claude in a pseudo-terminal).",
        f"When you have COMPLETELY finished the assignment, on a NEW line emit EXACTLY this string and nothing else after it:",
        f"  {COMPLETION_SENTINEL}",
        "Do not surround it with backticks, quotes, or commentary.  After this line, end your turn.",
        "The coordinator's PTY watcher will detect this line, terminate the session, push your branch, and notify the human.",
        "If you cannot finish (you are stuck and need guidance), still emit a STUCK: line as your final progress signal and STOP — do not emit the sentinel.",
    ]
    if spec_type == "review":
        lines.extend([
            "",
            "Because this is a REVIEW assignment, immediately BEFORE the sentinel line, emit one of:",
            "  VERDICT: approve",
            "  VERDICT: request-changes",
            "Then emit the sentinel on its own line.  Reuse the same verdict in any REVIEW_VERDICT block you also produce.",
        ])
    return "\n".join(lines)


# ── #426 ANSI / TTY stripping helpers ────────────────────────────────────────
#
# Interactive ``claude`` writes raw TTY bytes to its PTY: ANSI/CSI escapes,
# kitty-keyboard sequences (``ESC[>1u``), bracketed-paste markers
# (``ESC[?2004h``), and full TUI frames.  ``_strip_ansi`` removes the four
# most common families so the log can be regex-searched for plain content
# (the completion sentinel, ``STUCK:``, ``VERDICT:``, ``Total cost: $...``).
# We deliberately do NOT bring in a full vt100 emulator — the issue's
# review explicitly forbids adding a dependency for this — and stdlib
# regex is enough for the markers we care about (verified against the
# live #426 capture).
_ANSI_CSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_ANSI_OSC_RE = re.compile(r"\x1b\][^\x07]*\x07")
_ANSI_OTHER_RE = re.compile(r"\x1b[()][0-9A-Za-z]")
_ANSI_ESC_RE = re.compile(r"\x1b[=>78]")


def _strip_ansi(text: str) -> str:
    """Remove the four common ANSI escape families from *text*.

    Strips OSC first (they may contain ``[`` characters that the CSI
    pattern would otherwise mis-anchor on), then CSI, then SS2/SS3 / charset
    selection (``ESC(``/``ESC)``), then single-character escapes (``ESC=``,
    ``ESC>``, ``ESC7``/``ESC8`` for save/restore cursor).  Returns plain
    text suitable for substring / regex search.
    """
    text = _ANSI_OSC_RE.sub("", text)
    text = _ANSI_CSI_RE.sub("", text)
    text = _ANSI_OTHER_RE.sub("", text)
    text = _ANSI_ESC_RE.sub("", text)
    return text


def _sentinel_in_worker_output(text: str, sentinel: str) -> bool:
    """Return True iff *sentinel* appears on a non-comment line in *text*.

    The agent writes a ``# agent=... argv=...`` header to the log before
    spawning the worker, and the ``argv`` contains the worker's full
    ``--system-prompt`` — which now (per #426) literally contains the
    sentinel string.  A bare ``sentinel in text`` substring search would
    therefore false-positive on the spawn header itself before the worker
    has produced any output.  Restricting matches to lines that don't
    start with ``#`` skips the agent-authored comments (header, reap
    notes, ``# pty: worker exited`` marker, ``# pty-watcher: ...``
    diagnostics) without losing legitimate worker output — the
    interactive TUI never renders a line that begins with ``#`` in
    column zero.
    """
    if sentinel not in text:
        return False
    for line in text.splitlines():
        if sentinel in line and not line.lstrip().startswith("#"):
            return True
    return False


# Cost-line patterns.  The PTY captures two forms in the wild:
#   * ``Total cost: $0.1516`` from the ``/cost`` slash-command summary
#     (verified in #426 live capture).
#   * ``Cost: $.09`` from the status line in some configurations (note the
#     missing leading zero — ``$.09`` not ``$0.09``).  Also seen alongside
#     stale ``Cost: $0.00`` placeholders earlier in the same frame, so the
#     extractor picks the LATEST non-zero figure.
_PTY_COST_RE = re.compile(
    r"(?i)(?:total\s+)?cost\s*[:=]?\s*\$(\.?\d+(?:\.\d+)?)"
)

# Status / progress line patterns reused from coord.progress so the PTY
# log surfaces the same STATUS / STUCK signals workers emit on the
# ``claude -p`` path.  ``re.MULTILINE`` lets the regex anchor on the start
# of any line in the ANSI-stripped text.
_PTY_STATUS_RE = re.compile(r"^STATUS:\s*(.+)$", re.MULTILINE)
_PTY_STUCK_RE = re.compile(r"^STUCK:\s*(.+)$", re.MULTILINE)

# Review verdict line — the simplified ``VERDICT: approve|request-changes``
# form #426 introduces (see :func:`_completion_instruction`).  The
# existing ``REVIEW_VERDICT: ... REVIEW_BODY: ... END_REVIEW`` block is
# also still respected by :func:`coord.review.parse_review_from_log`
# — this is the lightweight machine-readable signal used by the PTY
# watcher and by the WorkerSummary.stop_reason mapping.
_PTY_VERDICT_RE = re.compile(
    r"^VERDICT:\s*(approve|request-changes)\s*$",
    re.MULTILINE | re.IGNORECASE,
)


def _parse_pty_log(
    log_path: str | Path,
    *,
    sentinel: str = COMPLETION_SENTINEL,
    tail_bytes: int = 65536,
) -> WorkerSummary:
    """Parse an interactive-``claude`` PTY log into a :class:`WorkerSummary`.

    The PTY log is raw TTY bytes — not stream-json — so the rich field set
    populated by :func:`coord.worker_events.parse_log` (session_id,
    per-tool tracking, etc.) is largely unavailable.  This function fills
    the fields whose values can be reliably extracted from the
    ANSI-stripped TTY stream:

    * ``stop_reason`` — set to ``"approve"`` / ``"request-changes"`` when
      a ``VERDICT:`` line is present (reviews), ``"stuck"`` when a
      ``STUCK:`` line is present, ``"end_turn"`` when the completion
      sentinel is present, otherwise ``None``.
    * ``total_cost_usd`` — extracted from ``Total cost: $X.XX`` or
      ``Cost: $.0X`` lines.  Stays ``0.0`` (the WorkerSummary default,
      meaning "n/a") when no cost line is present; this matches
      ``capabilities().cost_reporting=False`` for the PTY provider.

    All other WorkerSummary fields stay at their dataclass defaults — the
    interactive TUI doesn't expose them as machine-readable text.
    Returning the same WorkerSummary shape as the ``claude -p`` path lets
    downstream consumers (``coord.agent.AgentServer.list_assignments``,
    ``coord.notify._capture_cost``, etc.) treat both providers uniformly.
    """
    summary = WorkerSummary()
    p = Path(log_path)
    if not p.exists():
        return summary
    try:
        # Read the tail (cheap for live polling) or the whole file when
        # tail_bytes is 0.  We use binary mode so the ANSI strip can run
        # over predictable bytes; decode to str only after stripping.
        size = p.stat().st_size
        with open(p, "rb") as fh:
            if tail_bytes and size > tail_bytes:
                fh.seek(size - tail_bytes)
            raw = fh.read()
    except OSError:
        return summary
    text = _strip_ansi(raw.decode("utf-8", errors="replace"))

    # Stop reason precedence: VERDICT (most specific for reviews) > STUCK
    # (worker explicitly bailed) > sentinel (clean finish).  We prefer
    # VERDICT over the sentinel because a review worker emits BOTH, and
    # downstream code keys off the verdict to decide approve/request-changes.
    verdict_matches = list(_PTY_VERDICT_RE.finditer(text))
    if verdict_matches:
        summary.stop_reason = verdict_matches[-1].group(1).lower()
    elif _PTY_STUCK_RE.search(text):
        summary.stop_reason = "stuck"
    elif _sentinel_in_worker_output(text, sentinel):
        # ``_sentinel_in_worker_output`` skips ``#``-comment lines so the
        # spawn header (whose argv now contains the literal sentinel) does
        # not cause a false-positive "end_turn" verdict on a worker that
        # crashed before producing any real output.
        summary.stop_reason = "end_turn"

    # Cost extraction — pick the latest non-zero figure to skip the
    # stale ``Cost: $0.00`` placeholder mentioned in the #426 issue body.
    latest_nonzero: float | None = None
    for m in _PTY_COST_RE.finditer(text):
        raw_value = m.group(1)
        # ``$.09`` form has no leading digit; ``float(".09")`` works.
        try:
            value = float(raw_value)
        except ValueError:
            continue
        if value > 0:
            latest_nonzero = value
    if latest_nonzero is not None:
        summary.total_cost_usd = latest_nonzero

    return summary


class ClaudePtyProvider(Provider):
    """Interactive ``claude`` (no ``-p``) attached to a PTY.

    This provider is **opt-in** — operators select it by adding a definition
    to ``coordinator.yml`` ``providers.definitions`` with
    ``type: claude-pty``, then either:

    * setting it as a per-repo ``provider:`` override, or
    * setting ``providers.default: <name>`` to make it the global default.

    Args:
        binary: Override the worker binary name/path.  ``None`` falls back to
            :data:`coord.agent.DEFAULT_WORKER_BINARY` (``"claude"``).
    """

    def __init__(self, binary: str | None = None) -> None:
        self._binary = binary

    # ── Capabilities ──────────────────────────────────────────────────────────

    def capabilities(self) -> Capabilities:
        """Subscription-billed interactive worker.

        ``billing_mode="subscription"``: interactive ``claude`` sessions are
        covered by the operator's Max/Pro plan and do not consume the
        ``claude -p`` metered credit pool — this is the entire reason the
        provider exists (#425).

        ``enforces_deny_list=False`` is set **conservatively**: this PR does
        not verify that the interactive PTY mode actually enforces
        ``--allowedTools`` / ``--permission-mode`` (the agent passes those
        flags as a safety hedge, but the runtime behaviour is unverified).
        The safety gate in :meth:`coord.agent.AgentServer.assign` keys off
        this flag and refuses write-capable assignment types on
        unverified-deny-list providers.  Flip to ``True`` only after the
        deny-list enforcement is demonstrably exercised in the PTY path.

        Other flags:
        * ``resume=False`` — no ``--resume`` flag wired in the PTY argv.
        * ``inject=False`` — no mid-session injection path exists for PTY.
        * ``cost_reporting=False`` — interactive ``claude`` does not emit a
          per-run cost figure in the TTY stream.
        * ``true_system_prompt=True`` — the argv still passes
          ``--system-prompt`` so the worker honours it the same way
          ``claude -p`` does.
        """
        return Capabilities(
            resume=False,
            inject=False,
            cost_reporting=False,
            true_system_prompt=True,
            enforces_deny_list=False,
            billing_mode="subscription",
        )

    # ── Core methods ──────────────────────────────────────────────────────────

    def build_command(  # noqa: PLR0912  (mirrors ClaudeProvider's spec.type branches)
        self,
        spec: "AssignmentSpec",
        *,
        resolved_model: str | None = None,
        system_prompt: str | None = None,
        allowed_tools: str | None = None,
        permission_mode: str = "acceptEdits",
    ) -> list[str]:
        """Build the interactive ``claude`` argv for *spec*.

        Unlike :meth:`ClaudeProvider.build_command` this:

        * Does **not** pass ``-p``.
        * Does **not** pass ``--input-format`` / ``--output-format`` /
          ``--verbose`` (interactive ``claude`` writes TUI output).
        * Still passes ``--system-prompt``, ``--allowedTools``,
          ``--permission-mode``, and ``--model`` so the operator's safety
          posture and the configured model are honoured even though
          enforcement is unverified.
        """
        # Deferred import — keeps the cycle latent.
        from coord.agent import (  # noqa: PLC0415
            DEFAULT_WORKER_BINARY,
            NEW_ISSUE_CHAT_DENY_COMMANDS,
            NEW_ISSUE_CHAT_SYSTEM_PROMPT,
            REFINEMENT_SYSTEM_PROMPT,
            TEST_CHAT_SYSTEM_PROMPT,
            WORKER_PLAN_PROMPT,
            WORKER_SYSTEM_PROMPT,
            build_deny_prompt,
        )

        binary = self._binary if self._binary is not None else DEFAULT_WORKER_BINARY

        # Use resolved_model when provided; fall back to spec.model so a plain
        # ClaudePtyProvider().build_command(spec) is self-consistent.
        effective_model = resolved_model if resolved_model is not None else spec.model

        # Compute system_prompt / allowed_tools from spec.type when not
        # provided — same logic as ClaudeProvider so the safety hedge flags
        # carry the same content for the same spec.
        if system_prompt is None or allowed_tools is None:
            if spec.type == "plan":
                _sp = spec.system_prompt if spec.system_prompt else WORKER_PLAN_PROMPT
                _at = "Read,Bash"
            elif spec.type == "refinement":
                _sp = spec.system_prompt if spec.system_prompt else REFINEMENT_SYSTEM_PROMPT
                _at = "Read"
            elif spec.type == "test-chat":
                _sp = spec.system_prompt if spec.system_prompt else TEST_CHAT_SYSTEM_PROMPT
                _sp += build_deny_prompt(spec.deny_commands)
                _at = "Read,Bash"
            elif spec.type == "new-issue-chat":
                _sp = spec.system_prompt if spec.system_prompt else NEW_ISSUE_CHAT_SYSTEM_PROMPT
                _sp += build_deny_prompt(NEW_ISSUE_CHAT_DENY_COMMANDS)
                if spec.new_issue_guidance:
                    _sp += (
                        "\n\nThe user's repo has the following guidance for "
                        "new-issue drafts. Follow it: ask focused questions "
                        "matched to the required sections, then produce a "
                        "finalised issue body using the same structure. Do not "
                        "invent sections that aren't there; do not omit required "
                        "sections (mark them `(TBD)` if the conversation hasn't "
                        "covered them yet).\n\n"
                        + spec.new_issue_guidance
                    )
                _at = "Read,Bash"
            else:
                _sp = spec.system_prompt if spec.system_prompt else WORKER_SYSTEM_PROMPT
                _sp += build_deny_prompt(spec.deny_commands)
                _at = "Read,Edit,Write,Bash"

            if system_prompt is None:
                system_prompt = _sp
            if allowed_tools is None:
                allowed_tools = _at

        # #426: append the completion-sentinel instruction to the system
        # prompt for AUTONOMOUS spec types.  Chat-style sessions
        # (refinement / test-chat / new-issue-chat) are interactive with
        # the developer and end via :meth:`AgentServer.cancel`, not via
        # a self-emitted sentinel — augmenting their prompt would
        # confuse them into prematurely emitting the sentinel.  Always
        # append when an explicit system_prompt override was supplied
        # AND the spec type is autonomous (the override pathway is used
        # by review.py to install the reviewer prompt — that worker
        # still needs the sentinel to flag completion).
        if spec.type in _AUTONOMOUS_SPEC_TYPES:
            system_prompt = system_prompt + _completion_instruction(spec.type)

        # NOTE: no -p, no stream-json flags.  --system-prompt /
        # --allowedTools / --permission-mode are still passed as a safety
        # hedge even though enforcement is unverified in the PTY path
        # (capabilities().enforces_deny_list == False).
        argv: list[str] = [
            binary,
            "--system-prompt", system_prompt,
            "--allowedTools", allowed_tools,
            "--permission-mode", permission_mode,
        ]
        if effective_model:
            argv.extend(["--model", effective_model])
        return argv

    def initial_input(self, spec: "AssignmentSpec") -> bytes:
        """Return the briefing wrapped in a bracketed-paste block.

        Interactive ``claude`` reads typed input from the TTY, NOT stream-json
        envelopes.  A real briefing is multi-line, and the TUI swallows
        embedded newlines unless the whole block arrives as a bracketed paste
        (:data:`BRACKETED_PASTE_START` ... :data:`BRACKETED_PASTE_END`).  This
        returns only that paste block — **no trailing newline and no submit
        key**.  The submitting carriage return (``\\r``; ``\\n`` does NOT
        submit under the TUI's enhanced keyboard protocol) is written
        *separately* by :meth:`coord.agent.AgentServer._spawn_pty` after a
        short settle delay, because a CR glued onto ``ESC[201~`` in the same
        write is consumed by paste-end processing.  Verified live against
        interactive ``claude`` (#425 smoke).
        """
        body = spec.briefing.rstrip("\n")
        return BRACKETED_PASTE_START + body.encode("utf-8") + BRACKETED_PASTE_END

    def result_marker(self) -> str:
        """Sentinel written by the PTY spawn path after the worker exits.

        Interactive ``claude`` does not emit a stream-json ``result`` event,
        so the standard marker never appears.  :mod:`coord.agent` writes
        :data:`PTY_RESULT_MARKER` to the log after the subprocess exits so
        downstream code that polls for completion (the reap thread's
        ``_log_has_result`` helper) still has something to look for in the
        PTY case.
        """
        return PTY_RESULT_MARKER

    def env(self) -> dict[str, str]:
        """No extra environment variables required for interactive ``claude``.

        The agent already forces ``TERM`` via the PTY setup; nothing else
        needs to be injected for this provider in this PR.
        """
        return {}

    def parse_log(
        self, log_path: str | Path, tail_bytes: int = 65536
    ) -> WorkerSummary:
        """Parse a PTY worker log into a :class:`WorkerSummary` (#426).

        Delegates to :func:`_parse_pty_log` which ANSI-strips the raw TTY
        bytes and extracts the fields recoverable from the interactive
        ``claude`` output stream: ``stop_reason`` (from the
        :data:`COMPLETION_SENTINEL`, ``STUCK:``, or ``VERDICT:`` lines)
        and ``total_cost_usd`` (from ``Total cost: $X.XX`` / ``Cost:
        $.0X`` lines).  Other WorkerSummary fields stay at their
        dataclass defaults — they have no plain-text representation in
        the TUI.

        The returned shape is the SAME :class:`WorkerSummary` that the
        ``claude -p`` path returns, so downstream consumers
        (:meth:`coord.agent.AgentServer.list_assignments`,
        :func:`coord.notify._capture_cost`, etc.) treat both providers
        uniformly.
        """
        return _parse_pty_log(log_path, tail_bytes=tail_bytes)
