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

if TYPE_CHECKING:
    from coord.config import ModelsConfig, ProviderDef, ProvidersConfig

__all__ = [
    "Capabilities",
    "ClaudeProvider",
    "ClaudePtyProvider",
    "Provider",
    "WorkerSummary",
    "build_provider",
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
    raise ValueError(
        f"Unknown provider type {ptype!r} (provider name: {name!r}). "
        f"Supported types: ['claude', 'claude-pty']"
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
