"""ClaudeProvider: the ``claude -p`` concrete provider.

Parity requirement: ``ClaudeProvider().build_command(spec)`` produces the
**same argv** as ``coord.agent.default_worker_command(spec)`` for the same
inputs.  The logic is a direct transcription of that function's body with
the ``resolved_model`` / ``system_prompt`` / ``allowed_tools`` /
``permission_mode`` kwargs spliced in; the parity tests in
``tests/test_providers.py`` enforce this mechanically.

Imports from ``coord.agent`` are **deferred** (inside method bodies) to keep
the import cycle latent until the wiring issue lands.  At that point
``AgentServer`` will consume ``ClaudeProvider``, creating a two-way link;
until then the one-way ``claude → agent`` direction is safe because
``coord.agent`` does not import from ``coord.providers``.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from coord.providers.base import Capabilities, Provider, WorkerSummary

if TYPE_CHECKING:
    from coord.agent import AssignmentSpec


class ClaudeProvider(Provider):
    """Concrete provider for ``claude -p`` (Anthropic Claude Code workers).

    This is the **reference backend** — all :class:`~.base.Capabilities`
    flags are ``True`` because ``claude -p`` supports every feature the
    coordinator relies on.

    Args:
        binary: Override the worker binary name/path.  ``None`` falls back to
            :data:`coord.agent.DEFAULT_WORKER_BINARY` (``"claude"``).
    """

    def __init__(self, binary: str | None = None) -> None:
        self._binary = binary

    # ── Capabilities ──────────────────────────────────────────────────────────

    def capabilities(self) -> Capabilities:
        """All capabilities enabled — claude -p is the reference backend.

        ``billing_mode="metered"`` reflects the 2026-06-15 Anthropic
        change: ``claude -p`` (and Agent SDK) sessions are billed at full
        API rates against a small non-rolling credit pool, not against
        the Max/Pro subscription.  Downstream code uses this flag to
        prefer a non-metered backend when one is available (#322).
        """
        return Capabilities(
            resume=True,
            inject=True,
            cost_reporting=True,
            true_system_prompt=True,
            enforces_deny_list=True,
            billing_mode="metered",
        )

    # ── Core methods ──────────────────────────────────────────────────────────

    def build_command(  # noqa: PLR0912  (many spec.type branches, matches legacy)
        self,
        spec: "AssignmentSpec",
        *,
        resolved_model: str | None = None,
        system_prompt: str | None = None,
        allowed_tools: str | None = None,
        permission_mode: str = "acceptEdits",
    ) -> list[str]:
        """Build the ``claude -p`` argv for *spec*.

        Produces the **same argv** as ``default_worker_command(spec)`` when
        called with ``resolved_model=spec.model`` and without overriding
        ``system_prompt`` / ``allowed_tools`` / ``permission_mode``.
        """
        # Deferred import — keeps the cycle latent until wiring is done.
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
        # ClaudeProvider().build_command(spec) matches default_worker_command.
        effective_model = resolved_model if resolved_model is not None else spec.model

        # Compute system_prompt / allowed_tools from spec.type when not
        # provided — direct transcription of default_worker_command's logic.
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

        argv: list[str] = [
            binary, "-p",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--verbose",
            "--system-prompt", system_prompt,
            "--allowedTools", allowed_tools,
            "--permission-mode", permission_mode,
        ]
        if effective_model:
            argv.extend(["--model", effective_model])
        if spec.resume_session_id:
            argv.extend(["--resume", spec.resume_session_id])
        return argv

    def initial_input(self, spec: "AssignmentSpec") -> bytes:
        """Return the briefing encoded as a stream-json user message."""
        from coord.agent import _user_message_line  # noqa: PLC0415
        return _user_message_line(spec.briefing)

    def result_marker(self) -> str:
        """String whose presence in the log signals logical completion."""
        return '"type":"result"'

    def env(self) -> dict[str, str]:
        """No extra environment variables for claude -p."""
        return {}

    def parse_log(
        self, log_path: str | Path, tail_bytes: int = 65536
    ) -> WorkerSummary:
        """Delegate to :func:`coord.worker_events.parse_log`."""
        from coord.worker_events import parse_log  # noqa: PLC0415
        return parse_log(log_path, tail_bytes=tail_bytes)
