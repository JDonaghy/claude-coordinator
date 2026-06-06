"""Provider abstraction layer: base ABC and Capabilities descriptor.

Every worker-command backend (claude -p, hypothetical alternatives) implements
:class:`Provider`.  Downstream code calls ``provider.build_command(spec)`` and
``provider.initial_input(spec)`` rather than importing concrete helpers from
``coord.agent`` directly.

Re-exports :class:`~coord.worker_events.WorkerSummary` for convenience so
callers can do ``from coord.providers.base import WorkerSummary`` without
knowing which module originates it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

# Re-export WorkerSummary so callers can import it from here.
from coord.worker_events import WorkerSummary  # noqa: F401

if TYPE_CHECKING:
    from coord.agent import AssignmentSpec

__all__ = ["Capabilities", "Provider", "WorkerSummary"]


@dataclass(frozen=True)
class Capabilities:
    """What a provider can actually do.

    Downstream code gates features on these flags instead of silently
    degrading when a provider doesn't support them.

    Attributes:
        resume: Supports session resume (``--resume``) → gates chat-continue
            (#315).
        inject: Supports mid-session stdin message injection → gates
            ``inject_message``.
        cost_reporting: Emits per-run cost in the log → TUI shows a dollar
            figure rather than "n/a".
        true_system_prompt: Honours a real ``--system-prompt`` flag rather
            than prepending the prompt text to the first user message.
        enforces_deny_list: SAFETY — honours the worker deny-list and tool
            restrictions.  Providers that ignore ``--allowedTools`` / deny
            prompts must set this to ``False`` so the coordinator can warn.
        billing_mode: How runs of this provider are billed.  One of
            ``"subscription"`` (covered by a fixed-price plan, e.g.
            interactive Claude Code on Max/Pro), ``"metered"`` (billed
            per-token at API rates, e.g. ``claude -p`` after the
            2026-06-15 metering change), ``"byo_key"`` (uses the
            operator's own API key — cost is theirs), or ``"unknown"``
            for backends whose billing the coordinator can't infer.  This
            is the Track-3 routing signal for the June-15 metering
            mitigation (#322): the coordinator and TUI prefer a
            non-metered backend when one is available.
        human_attended_only: STRUCTURAL TOS-COMPLIANCE GATE (#437).  When
            ``True``, this provider may NEVER be selected for unattended
            dispatch — the coordinator refuses to route ``coord plan`` /
            ``coord approve`` / auto-review / auto-reassign /
            reconciliation through it.  Used for subscription-billed
            interactive backends (e.g. :class:`ClaudePtyProvider`) where
            running unsupervised would violate Anthropic ToS §3.7
            (no headless / agentic use of subscription Claude Code).
            The default is ``False`` — a new provider is automatable
            unless it explicitly opts out (fail-safe: a metered backend
            is safe to gate behind nothing; an interactive subscription
            backend must opt out).  Workers run via this provider must
            be launched into a human-attended terminal by ``coord assign
            --interactive`` and HUMAN-CLOSED; no coordinator-side
            completion-sentinel watching, no auto-termination on output,
            no parsing of the TTY to advance pipeline state.
    """

    resume: bool
    inject: bool
    cost_reporting: bool
    true_system_prompt: bool
    enforces_deny_list: bool
    billing_mode: str
    human_attended_only: bool = False


class Provider(ABC):
    """Abstract base class for worker-command providers.

    A provider knows how to:
    * Build the argv for spawning the worker subprocess (``build_command``).
    * Produce the initial stdin payload (``initial_input``).
    * Report what capabilities it actually has (``capabilities``).
    * Identify a successful run in the log (``result_marker``).
    * Declare extra environment variables (``env``).
    * Parse a completed log file (``parse_log``).

    The concrete ``supports_inject()`` method is derived from
    ``capabilities().inject`` to keep them in sync — subclasses should
    *not* override it.
    """

    @abstractmethod
    def build_command(
        self,
        spec: "AssignmentSpec",
        *,
        resolved_model: str | None = None,
        system_prompt: str | None = None,
        allowed_tools: str | None = None,
        permission_mode: str = "acceptEdits",
    ) -> list[str]:
        """Build the worker subprocess argv for *spec*.

        Args:
            spec: The assignment spec being dispatched.
            resolved_model: The resolved model identifier to pass (e.g.
                ``"claude-sonnet-4-6"`` after alias expansion).  ``None``
                falls back to provider-internal defaults (``spec.model`` for
                :class:`ClaudeProvider`).
            system_prompt: Override the system prompt.  ``None`` means the
                provider computes one from ``spec.type``.
            allowed_tools: Override the ``--allowedTools`` value.  ``None``
                means the provider computes one from ``spec.type``.
            permission_mode: Override the ``--permission-mode`` value.
                Defaults to ``"acceptEdits"``.
        """
        ...

    @abstractmethod
    def initial_input(self, spec: "AssignmentSpec") -> bytes:
        """Return the first stdin payload written to the worker process.

        For stream-json workers this is the JSON-encoded user message
        containing the briefing text.
        """
        ...

    def supports_inject(self) -> bool:
        """Whether this provider supports mid-session message injection.

        Derived from ``capabilities().inject`` — never let the two disagree.
        Override ``capabilities()`` to change this; do not override
        ``supports_inject()`` directly.
        """
        return self.capabilities().inject

    @abstractmethod
    def result_marker(self) -> str:
        """Return a string whose presence in the log signals job completion.

        The coordinator reads the log in binary mode so callers are
        responsible for encoding this value when comparing against raw bytes.
        """
        ...

    @abstractmethod
    def env(self) -> dict[str, str]:
        """Extra environment variables to set for the worker subprocess.

        Merged on top of the base environment in the spawn path.  Return an
        empty dict when no extra variables are needed.
        """
        ...

    @abstractmethod
    def parse_log(
        self, log_path: str | Path, tail_bytes: int = 65536
    ) -> WorkerSummary:
        """Parse a worker log file and return a rolling summary.

        Args:
            log_path: Path to the worker's log file.
            tail_bytes: When > 0, only the last *tail_bytes* of the file is
                read (cheap live-polling).  Pass ``0`` for a full parse.
        """
        ...

    @abstractmethod
    def capabilities(self) -> Capabilities:
        """Return a descriptor of what this provider can actually do."""
        ...
