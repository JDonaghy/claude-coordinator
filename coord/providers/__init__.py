"""Provider registry: construct and resolve worker-command providers.

Public API
----------
``build_provider(name, definition, models_cfg) -> Provider``
    Instantiate the correct concrete :class:`~.base.Provider` subclass from a
    :class:`~coord.config.ProviderDef`'s ``type`` field.  Raises
    :class:`ValueError` for unknown types.

``resolve_provider_name(spec_provider, repo_provider, providers_cfg) -> str``
    Apply the precedence chain
    ``spec → repo → providers.default → "claude"``
    and return the winning provider name.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from coord.providers.base import Capabilities, Provider, WorkerSummary
from coord.providers.claude import ClaudeProvider
from coord.providers.claude_pty import ClaudePtyProvider
from coord.providers.opencode import OpenCodeProvider

if TYPE_CHECKING:
    from coord.config import ModelsConfig, ProviderDef, ProvidersConfig

__all__ = [
    "Capabilities",
    "ClaudeProvider",
    "ClaudePtyProvider",
    "OpenCodeProvider",
    "Provider",
    "WorkerSummary",
    "build_provider",
    "guard_unattended_dispatch",
    "resolve_provider_name",
]


def build_provider(
    name: str,
    definition: "ProviderDef",
    models_cfg: "ModelsConfig | None",
) -> Provider:
    """Construct a :class:`Provider` from *definition*.

    Args:
        name: The provider's logical name (key in ``providers.definitions``).
            Used only for error messages.
        definition: The parsed :class:`~coord.config.ProviderDef` for this
            provider.
        models_cfg: The coordinator's :class:`~coord.config.ModelsConfig` (may
            be ``None`` when called outside a full config context, e.g. tests).

    Returns:
        A ready-to-use :class:`Provider` instance.

    Raises:
        ValueError: When ``definition.type`` is not a known provider type.
    """
    ptype = definition.type
    if ptype == "claude":
        # TODO(#322 wiring issue): also thread definition.model,
        # definition.env, and definition.extra_args through to the spawn
        # path.  Today only `binary` is consumed because ClaudeProvider
        # is not yet wired into AgentServer.spawn() — see the next child
        # issue of #322.  Until then these fields are parsed and
        # validated but ignored when build_provider() is called.
        return ClaudeProvider(binary=definition.binary)
    if ptype == "claude-pty":
        # #425: interactive `claude` driven through a PTY — the
        # subscription-billed escape hatch from the 2026-06-15 metering
        # change.  Like the "claude" branch above, only `binary` is
        # consumed in this PR; threading `model` / `env` / `extra_args`
        # is left to a follow-up wiring issue.
        return ClaudePtyProvider(binary=definition.binary)
    if ptype == "opencode":
        # #325: OpenCode (sst/opencode) worker backend — uses the operator's
        # own API keys, runs `opencode run BRIEFING`.  Only `binary` is
        # consumed here; `model` / `env` / `extra_args` threading is left to
        # the follow-up wiring issue (#324).
        return OpenCodeProvider(binary=definition.binary)
    raise ValueError(
        f"Unknown provider type {ptype!r} (provider name: {name!r}). "
        f"Supported types: ['claude', 'claude-pty', 'opencode']"
    )


def resolve_provider_name(
    spec_provider: str | None,
    repo_provider: str | None,
    providers_cfg: "ProvidersConfig",
) -> str:
    """Return the effective provider name using the precedence chain.

    Precedence (highest to lowest):
    1. *spec_provider* — per-assignment override (``AssignmentSpec.provider``).
    2. *repo_provider* — per-repo default (``Repo.provider`` in config).
    3. ``providers_cfg.default`` — global default (defaults to ``"claude"``).

    Args:
        spec_provider: Provider name from the assignment spec, or ``None``.
        repo_provider: Provider name from the repo config, or ``None``.
        providers_cfg: The parsed :class:`~coord.config.ProvidersConfig`.

    Returns:
        The winning provider name (always a non-empty string).
    """
    if spec_provider is not None:
        return spec_provider
    if repo_provider is not None:
        return repo_provider
    return providers_cfg.default


def guard_unattended_dispatch(
    *,
    spec_provider: str | None,
    repo_provider: str | None,
    providers_cfg: "ProvidersConfig",
    models_cfg: "ModelsConfig | None" = None,
    where: str = "unattended dispatch",
) -> str:
    """STRUCTURAL TOS-COMPLIANCE GATE for unattended dispatch (#437).

    Resolves the effective provider name with :func:`resolve_provider_name`
    (precedence: spec → repo → providers.default), then instantiates the
    provider via :func:`build_provider` and inspects its
    :class:`~coord.providers.base.Capabilities`.  Raises :class:`ValueError`
    if ``capabilities().human_attended_only`` is ``True`` — that flag means
    the backend (currently :class:`~coord.providers.claude_pty.ClaudePtyProvider`,
    interactive subscription-billed Claude Code) is only licensed for
    human-attended use under Anthropic ToS §3.7 and must NEVER be selected
    for autonomous routing.

    This gate is called from every unattended dispatch path
    (``coord.dispatch.dispatch``, ``coord.review.dispatch_review``,
    ``coord.reconcile._reassign``).  The human-attended escape hatch
    (``coord assign --interactive``) deliberately skips this gate.

    Args:
        spec_provider: Per-spec/per-proposal provider override, or ``None``.
        repo_provider: Per-repo provider override (``Repo.provider``), or
            ``None``.
        providers_cfg: The coordinator's
            :class:`~coord.config.ProvidersConfig`.
        models_cfg: Optional :class:`~coord.config.ModelsConfig`, forwarded
            to :func:`build_provider`.
        where: Short description of the calling site (e.g.
            ``"coord approve / dispatch"``) — interpolated into the error
            message so the human knows which path refused.

    Returns:
        The effective provider name (also returned for callers that want to
        thread it onward to the wire payload).

    Raises:
        ValueError: When the effective provider opts out of unattended use.
            The message names the provider, explains why, and points the
            user at ``coord assign --interactive``.
    """
    name = resolve_provider_name(spec_provider, repo_provider, providers_cfg)
    definition = providers_cfg.definitions.get(name)
    if definition is None:
        # Unknown name (not in registry) → fall through; the agent's own
        # unknown-provider handling kicks in.  Don't fabricate a refusal
        # for a typo'd provider name; let the existing error path surface
        # it as a validation failure at the agent.
        return name
    try:
        provider = build_provider(name, definition, models_cfg)
    except ValueError:
        # build_provider raises on unknown TYPE; the caller will hit the
        # same error path on dispatch.  Don't shadow it here.
        return name
    caps = provider.capabilities()
    if caps.human_attended_only:
        raise ValueError(
            f"refusing {where}: provider {name!r} reports "
            f"capabilities().human_attended_only=True — this backend is "
            f"licensed only for human-attended interactive use (Anthropic "
            f"ToS §3.7) and must NEVER be selected for unattended "
            f"automation.  To launch a human-attended session, run "
            f"`coord assign --interactive <machine> <repo> <issue>` from "
            f"the operator's terminal; the human drives and closes the "
            f"session.  To dispatch unattended, configure a non-human-"
            f"attended provider (e.g. `claude`)."
        )
    return name
