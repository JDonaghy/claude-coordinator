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
        """Best-effort parse of the worker log.

        Delegates to :func:`coord.worker_events.parse_log`, which silently
        skips non-JSON lines.  Interactive ``claude`` writes raw TTY bytes
        (ANSI escape sequences, prompts, model text) rather than
        stream-json, so the returned :class:`WorkerSummary` will usually be
        mostly blank — that is the correct minimal-tail-parser behaviour
        for this provider until the PTY format gets its own structured
        log channel.
        """
        from coord.worker_events import parse_log  # noqa: PLC0415
        return parse_log(log_path, tail_bytes=tail_bytes)
