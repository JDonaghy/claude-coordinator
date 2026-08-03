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

``get_provider(provider_name, cfg=None) -> Provider``
    #1710: the single coordinator-side helper that turns a bare
    ``provider_name`` string (e.g. ``Assignment.provider_name``, persisted at
    dispatch time — see ``coord/models.py``) into a ready-to-use
    :class:`~.base.Provider` instance, so log consumers (``coord.progress``,
    ``coord.usage``, ``coord.failure_class``, ...) can call
    ``provider.parse_log()`` instead of importing ``coord.worker_events``
    directly and assuming every worker log is claude-shaped.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Iterable

from coord.config import IMPLICIT_PROVIDER_TYPES, provider_capability
from coord.providers.base import Capabilities, Provider, WorkerSummary
from coord.providers.claude import ClaudeProvider
from coord.providers.claude_pty import ClaudePtyProvider
from coord.providers.opencode import OpenCodeProvider

if TYPE_CHECKING:
    from coord.config import Config, ModelsConfig, ProviderDef, ProvidersConfig
    from coord.models import Machine

__all__ = [
    "Capabilities",
    "ClaudeProvider",
    "ClaudePtyProvider",
    "OpenCodeProvider",
    "Provider",
    "WorkerSummary",
    "build_provider",
    "describe_provider_choice",
    "get_provider",
    "guard_provider_machine_capability",
    "guard_unattended_dispatch",
    "machine_supports_provider",
    "machines_supporting_provider",
    "provider_type_for",
    "resolve_default_provider",
    "resolve_provider_name",
]

_log = logging.getLogger(__name__)

# Built-in provider types constructible with no ``ProviderDef`` at all (no
# model/env/extra_args threading — see #1706 comments on ``build_provider``).
# Used by :func:`get_provider` as the fallback when no ``Config`` is passed
# (many coordinator-side log consumers only ever had a bare provider_name
# string in scope — see #1710) or when the name isn't in
# ``cfg.providers.definitions`` (predates the definition, or a config-free
# test/CLI context).
_BUILTIN_PROVIDER_TYPES: dict[str, type[Provider]] = {
    "claude": ClaudeProvider,
    "claude-pty": ClaudePtyProvider,
    "opencode": OpenCodeProvider,
}


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
        # #1706: thread model / env / extra_args from the provider
        # definition into the instance.  build_command() / env() apply
        # them with the documented precedence (explicit resolved_model >
        # spec.model > definition.model for the model fallback; env and
        # extra_args are additive).
        return ClaudeProvider(
            binary=definition.binary,
            model=definition.model,
            env=definition.env,
            extra_args=definition.extra_args,
        )
    if ptype == "claude-pty":
        # #425: interactive `claude` driven through a PTY — the
        # subscription-billed escape hatch from the 2026-06-15 metering
        # change.  #1706: same model / env / extra_args threading as the
        # "claude" branch above.
        return ClaudePtyProvider(
            binary=definition.binary,
            model=definition.model,
            env=definition.env,
            extra_args=definition.extra_args,
        )
    if ptype == "opencode":
        # #325: OpenCode (sst/opencode) worker backend — uses the operator's
        # own API keys, runs `opencode run BRIEFING`.  `attach_url` is wired
        # here because it is OpenCode-specific (not a cross-provider concern).
        # #1706: model / env / extra_args threaded the same way as the other
        # provider types.
        return OpenCodeProvider(
            binary=definition.binary,
            attach_url=definition.attach_url,
            model=definition.model,
            env=definition.env,
            extra_args=definition.extra_args,
        )
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


def describe_provider_choice(
    spec_provider: str | None,
    repo_provider: str | None,
    providers_cfg: "ProvidersConfig",
) -> str:
    """Format a one-line explanation of why the effective provider was chosen.

    #1707: mirrors ``coord.config.describe_model_choice``'s shape — state the
    winning name AND which link of the ``spec → repo → providers.default``
    precedence chain (:func:`resolve_provider_name`) supplied it, so
    ``coord assign --dry-run --provider ...`` (and any other dry-run/status
    caller) never leaves an operator guessing whether a provider came from an
    explicit ``--provider``, a repo default, or the global fallback — the
    exact ambiguity #1454 fixed for models.

    Args:
        spec_provider: Provider name from the assignment spec / CLI flag, or
            ``None``.
        repo_provider: Provider name from the repo config, or ``None``.
        providers_cfg: The parsed :class:`~coord.config.ProvidersConfig`.

    Returns:
        E.g. ``"opencode (explicit --provider)"`` or
        ``"claude (providers.default)"``.
    """
    name = resolve_provider_name(spec_provider, repo_provider, providers_cfg)
    if spec_provider is not None:
        return f"{name} (explicit --provider)"
    if repo_provider is not None:
        return f"{name} (repo default: Repo.provider)"
    return f"{name} (providers.default)"


def provider_type_for(provider_name: str, providers_cfg: "ProvidersConfig") -> str:
    """Resolve *provider_name* to its backend ``type`` (#1711).

    Falls back to *provider_name* itself when the name isn't registered in
    ``providers_cfg.definitions`` (a typo'd name, or one removed from config
    after an assignment was dispatched with it) — mirrors
    :func:`guard_unattended_dispatch`'s posture of not fabricating a refusal
    for an already-broken reference and letting the existing "unknown
    provider" error path surface it instead.
    """
    definition = providers_cfg.definitions.get(provider_name)
    return definition.type if definition is not None else provider_name


def machine_supports_provider(
    machine: "Machine", provider_name: str, providers_cfg: "ProvidersConfig",
) -> bool:
    """Whether *machine* can run *provider_name* (#1711).

    ``claude``/``claude-pty`` — by resolved TYPE, not registered name, see
    :data:`coord.config.IMPLICIT_PROVIDER_TYPES` — are the implicit
    baseline every machine is assumed to support (unchanged from before
    #1711). Any other backend type requires ``machine.capabilities`` to
    include ``coord.config.provider_capability(type)``, e.g.
    ``"provider:opencode"``.
    """
    ptype = provider_type_for(provider_name, providers_cfg)
    if ptype in IMPLICIT_PROVIDER_TYPES:
        return True
    return provider_capability(ptype) in machine.capabilities


def machines_supporting_provider(
    machines: Iterable["Machine"], provider_name: str, providers_cfg: "ProvidersConfig",
) -> list[str]:
    """Names of every machine in *machines* that can run *provider_name*,
    sorted (#1711). Used to compose the "here's where you CAN dispatch
    this" half of :func:`guard_provider_machine_capability`'s refusal.
    """
    return sorted(
        m.name for m in machines
        if machine_supports_provider(m, provider_name, providers_cfg)
    )


def guard_provider_machine_capability(
    *,
    provider_name: str,
    machine: "Machine",
    all_machines: Iterable["Machine"],
    providers_cfg: "ProvidersConfig",
    where: str = "dispatch",
) -> None:
    """STRUCTURAL PROVIDER-AVAILABILITY GATE (#1711).

    Refuses to route *provider_name* to *machine* when *machine* can't run
    it (see :func:`machine_supports_provider`) — e.g. an ``opencode``
    assignment routed to a machine that never declared
    ``provider:opencode`` in ``coordinator.yml``. Before this gate, that
    combination failed only at spawn time, deep inside the agent process,
    with an ENOENT-shaped subprocess error discovered minutes into a
    worker — after the assignment row was created and the worktree built.
    This turns it into a clean refusal at the same dispatch chokepoint as
    the #437 TOS gate (:func:`guard_unattended_dispatch`), before any of
    that happens.

    A machine's own repo/capabilities are matched exactly as
    :func:`machine_supports_provider` defines it — this function adds only
    the refusal message, naming *machine*, the requested provider and its
    resolved type, and every OTHER configured machine that DOES advertise
    the required capability (so an operator can immediately re-target the
    dispatch), or states plainly that no machine advertises it yet.

    This is a **declaration** check only — whether *machine* actually has
    the backing binary installed is a separate, best-effort concern
    (:mod:`coord.prereqs`, surfaced via ``coord doctor``); a machine that
    lies about a capability it doesn't actually have is caught there, not
    here (mirrors the existing ``rust``/``gtk``/``browser`` split between
    "declared" and "probed-and-met").

    Args:
        provider_name: The already-resolved effective provider name (spec →
            repo → ``providers.default``), as returned by
            :func:`resolve_provider_name` or :func:`guard_unattended_dispatch`.
        machine: The machine the dispatch is being routed to.
        all_machines: Every configured machine (``config.machines``) —
            scanned to compose the "these machines DO support it" hint.
        providers_cfg: The coordinator's :class:`~coord.config.ProvidersConfig`.
        where: Short description of the calling site, interpolated into the
            error message (mirrors :func:`guard_unattended_dispatch`).

    Raises:
        ValueError: When *machine* cannot run *provider_name*.
    """
    if machine_supports_provider(machine, provider_name, providers_cfg):
        return
    ptype = provider_type_for(provider_name, providers_cfg)
    cap = provider_capability(ptype)
    candidates = [
        name for name in machines_supporting_provider(all_machines, provider_name, providers_cfg)
        if name != machine.name
    ]
    if candidates:
        hint = f"machines that DO advertise {cap!r}: {', '.join(candidates)}"
    else:
        hint = f"no configured machine advertises {cap!r} yet"
    raise ValueError(
        f"refusing {where}: machine {machine.name!r} cannot run provider "
        f"{provider_name!r} (type {ptype!r}) — it does not advertise "
        f"{cap!r} in coordinator.yml machines[].capabilities; {hint}. Add "
        f"{cap!r} to a machine that has the {ptype} binary installed, or "
        f"dispatch to one of the machines named above."
    )


def get_provider(
    provider_name: str | None,
    cfg: "Config | None" = None,
) -> Provider:
    """Resolve a bare *provider_name* string to a :class:`Provider` instance.

    #1710: this is the seam coordinator-side log consumers (``coord.progress``,
    ``coord.usage``, ``coord.failure_class``, ...) use to turn the
    already-*resolved* provider name persisted on an assignment
    (``Assignment.provider_name`` — see ``coord/models.py``, set via
    :func:`resolve_provider_name` at dispatch time) into a live
    :class:`Provider` so they can call ``provider.parse_log()`` instead of
    reaching into :mod:`coord.worker_events` directly and assuming every log
    is claude-shaped.

    Unlike :func:`build_provider`, *cfg* is optional: many call sites (e.g.
    ``coord/notify.py``'s completion-capture helpers) only ever had a bare
    ``provider_name`` string in scope, never a loaded
    :class:`~coord.config.Config` — see the #1710 inventory. Log parsing
    itself doesn't depend on ``ProviderDef.{model,env,extra_args}`` (none of
    the concrete providers' ``parse_log()`` reads those instance attributes),
    so a config-free construction is always correct for this purpose, even
    though it can't honour a custom ``binary`` override.

    Resolution order:

    1. ``provider_name is None`` → ``"claude"`` (the documented implicit
       default for assignments dispatched before #324, or via a path that
       never set the field).
    2. *cfg* given and *provider_name* found in ``cfg.providers.definitions``
       → :func:`build_provider` (honours ``binary``/``model``/``env``/
       ``extra_args`` from the definition).
    3. *provider_name* is one of the built-in type names (``"claude"``,
       ``"claude-pty"``, ``"opencode"``) → a bare, no-``ProviderDef``
       instance of that type.
    4. Anything else (a name that is neither configured nor a built-in type —
       a typo, or a provider removed from config after dispatch) →
       :class:`ClaudeProvider`, with a **logged warning**. #1710's whole point
       is that a silent fallback here is the bug; the warning names the
       assignment's actual provider so an operator can tell "misparsed
       because we guessed wrong" apart from "genuinely claude."

    Args:
        provider_name: The *resolved* provider name, or ``None``.
        cfg: The coordinator's :class:`~coord.config.Config`, when available.

    Returns:
        A ready-to-use :class:`Provider` instance. Never raises — an unknown
        name degrades to :class:`ClaudeProvider` (loudly) rather than
        blocking whatever best-effort log parsing called this.
    """
    name = provider_name or "claude"
    if cfg is not None:
        definition = cfg.providers.definitions.get(name)
        if definition is not None:
            try:
                return build_provider(name, definition, cfg.models)
            except ValueError:
                # Unknown ProviderDef.type — fall through to the built-in /
                # warning path below rather than raising out of a best-effort
                # log-parsing caller.
                pass
    ctor = _BUILTIN_PROVIDER_TYPES.get(name)
    if ctor is not None:
        return ctor()
    _log.warning(
        "get_provider: unknown provider_name %r — no matching entry in "
        "providers.definitions and not a built-in provider type. Falling "
        "back to ClaudeProvider for log parsing; if this assignment's "
        "worker is NOT claude, its log will misparse silently downstream "
        "of this warning (#1710).",
        name,
    )
    return ClaudeProvider()


def resolve_default_provider(
    providers_cfg: "ProvidersConfig",
    models_cfg: "ModelsConfig | None" = None,
) -> Provider:
    """Instantiate the coordinator's default provider for unattended oneshot use.

    Resolves the effective provider name from ``providers_cfg.default``,
    instantiates it via :func:`build_provider`, then checks
    ``capabilities().human_attended_only``.  Raises :class:`ValueError` if
    the resolved provider is human-attended-only (e.g.
    :class:`~coord.providers.claude_pty.ClaudePtyProvider`) — oneshot callers
    (brain planning, dashboard assistant) are unattended and must never route
    through such a provider.

    Falls back to a plain :class:`ClaudeProvider` when the resolved name is
    not found in ``providers_cfg.definitions`` (which shouldn't happen in
    practice because :class:`~coord.config.ProvidersConfig` always materialises
    the implicit ``"claude"`` entry).

    This is the **shared** implementation used by both :mod:`coord.brain` and
    :mod:`coord.dashboard.server`.  Any change to default-provider resolution
    or the human-attended guard belongs here — not duplicated in callers.

    Args:
        providers_cfg: The coordinator's
            :class:`~coord.config.ProvidersConfig`.
        models_cfg: Optional :class:`~coord.config.ModelsConfig`, forwarded
            to :func:`build_provider`.

    Returns:
        A ready-to-use :class:`Provider` instance whose
        ``capabilities().human_attended_only`` is ``False``.

    Raises:
        ValueError: When the resolved provider reports
            ``capabilities().human_attended_only=True``.  The error message
            names the provider and points the operator at
            ``coord assign --interactive``.
    """
    name = providers_cfg.default
    definition = providers_cfg.definitions.get(name)
    if definition is None:
        return ClaudeProvider()
    provider = build_provider(name, definition, models_cfg)
    caps = provider.capabilities()
    if caps.human_attended_only:
        raise ValueError(
            f"refusing unattended oneshot call: provider {name!r} reports "
            f"capabilities().human_attended_only=True — this backend is "
            f"licensed only for human-attended interactive use (Anthropic "
            f"ToS §3.7) and must NEVER be selected for unattended "
            f"automation (brain planning, dashboard assistant).  Configure "
            f"a non-human-attended provider (e.g. 'claude') as "
            f"providers.default, or launch a human-attended session with "
            f"`coord assign --interactive`."
        )
    return provider


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
