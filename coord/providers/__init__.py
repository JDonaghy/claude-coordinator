"""Provider registry: construct and resolve worker-command providers.

Public API
----------
``build_provider(name, definition, models_cfg) -> Provider``
    Instantiate the correct concrete :class:`~.base.Provider` subclass from a
    :class:`~coord.config.ProviderDef`'s ``type`` field.  Raises
    :class:`ValueError` for unknown types.

``resolve_provider_name(spec_provider, repo_provider, providers_cfg, issue_labels=None) -> str``
    Apply the precedence chain
    ``spec → providers.labels (issue_labels) → repo → providers.default → "claude"``
    (#1889 inserted the label link) and return the winning provider name.

``get_provider(provider_name, cfg=None) -> Provider``
    #1710: the single coordinator-side helper that turns a bare
    ``provider_name`` string (e.g. ``Assignment.provider_name``, persisted at
    dispatch time — see ``coord/models.py``) into a ready-to-use
    :class:`~.base.Provider` instance, so log consumers (``coord.progress``,
    ``coord.usage``, ``coord.failure_class``, ...) can call
    ``provider.parse_log()`` instead of importing ``coord.worker_events``
    directly and assuming every worker log is claude-shaped.

``provider_def_to_wire(definition) -> dict`` / ``build_provider_from_wire(name, wire_def) -> Provider``
    #1796: the dispatch-payload counterpart to ``build_provider``.  A
    config-free agent (no local ``coordinator.yml``, no board service —
    docs/EPHEMERAL_WORKERS.md) has no ``providers.definitions`` registry to
    resolve ``AssignmentSpec.provider`` against.  ``provider_def_to_wire``
    (coordinator-side, called from ``coord/dispatch.py``) serializes the
    resolved :class:`~coord.config.ProviderDef` into the JSON-safe dict
    carried as ``AssignmentSpec.provider_def``; ``build_provider_from_wire``
    (agent-side, called from ``coord.agent.AgentServer``) reconstructs the
    same :class:`Provider` instance from it, with no local config needed.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Iterable

from coord.config import IMPLICIT_PROVIDER_TYPES, ProviderDef, provider_capability
from coord.providers.base import Capabilities, Provider, WorkerSummary
from coord.providers.claude import ClaudeProvider
from coord.providers.claude_pty import ClaudePtyProvider
from coord.providers.opencode import OpenCodeProvider

if TYPE_CHECKING:
    from coord.config import Config, ModelsConfig, ProvidersConfig
    from coord.models import Machine

__all__ = [
    "Capabilities",
    "ClaudeProvider",
    "ClaudePtyProvider",
    "OpenCodeProvider",
    "Provider",
    "WorkerSummary",
    "build_provider",
    "build_provider_from_wire",
    "describe_provider_choice",
    "get_provider",
    "guard_provider_machine_capability",
    "guard_unattended_dispatch",
    "machine_supports_provider",
    "machines_supporting_provider",
    "provider_def_to_wire",
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


def provider_def_to_wire(definition: "ProviderDef") -> "dict[str, Any]":
    """Serialize *definition* to the JSON-safe dict carried on the wire as
    ``AssignmentSpec.provider_def`` (#1796).

    Called from ``coord/dispatch.py`` alongside the existing ``"provider"``
    name field, so a config-free agent (no local ``coordinator.yml``, no
    board service — docs/EPHEMERAL_WORKERS.md) receives everything
    :func:`build_provider` needs to construct the SAME provider instance the
    coordinator resolved, without a local ``providers.definitions`` registry
    to look the name up in.  See :func:`build_provider_from_wire` for the
    agent-side reconstruction.

    Args:
        definition: The resolved :class:`~coord.config.ProviderDef`.

    Returns:
        A plain ``dict`` with JSON-safe values only (``env``/``extra_args``
        copied, not aliased, so the caller's definition is never mutated
        through the returned dict).
    """
    return {
        "type": definition.type,
        "binary": definition.binary,
        "model": definition.model,
        "attach_url": definition.attach_url,
        "env": dict(definition.env),
        "extra_args": list(definition.extra_args),
    }


def build_provider_from_wire(name: str, wire_def: "dict[str, Any]") -> Provider:
    """Agent-side counterpart to :func:`provider_def_to_wire` (#1796).

    Reconstructs the :class:`~coord.config.ProviderDef` the coordinator
    resolved at dispatch time from the plain dict carried in
    ``AssignmentSpec.provider_def``, then builds a live :class:`Provider` via
    :func:`build_provider` — the SAME dispatch table used coordinator-side —
    so a config-free agent (no local ``providers.definitions`` registry to
    look *name* up in — docs/EPHEMERAL_WORKERS.md) gets a provider instance
    equivalent to the one the coordinator resolved, instead of being unable
    to honour an explicitly requested ``spec.provider`` at all.

    This is the fix for #1796: before it existed, a config-free agent that
    could not resolve ``spec.provider`` locally silently fell back to the
    legacy ``claude -p`` spawn path with no error — an explicitly requested
    provider (e.g. ``opencode``) never ran, and every surface (coordinator
    log, board, assignment record) still reported the requested name.  See
    ``coord.agent.AgentServer._resolve_provider``, which calls this function
    and REFUSES the assignment instead when neither the local registry nor
    ``wire_def`` can resolve the name.

    Args:
        name: The provider's logical name (``AssignmentSpec.provider``).
            Used only for error messages, mirroring :func:`build_provider`.
        wire_def: The dict from ``AssignmentSpec.provider_def`` — see
            :func:`provider_def_to_wire` for its shape.

    Raises:
        ValueError: *wire_def* is malformed in any way this function can
            detect — not a dict, missing/empty ``"type"``, an ``"env"``/
            ``"extra_args"`` whose shape can't be coerced to a dict/list
            (e.g. a string or int — raises :class:`TypeError` from the
            plain ``dict()``/``list()`` calls below, caught and re-raised
            as :class:`ValueError` here) — or *wire_def* names an unknown
            provider type — the last case mirrors :func:`build_provider`'s
            own error for that case. Every case is a :class:`ValueError` so
            :meth:`coord.agent.AgentServer._resolve_provider` (this
            function's only caller) can turn ANY malformed wire payload
            into a clean refused-assignment 400 rather than an uncaught 500
            — the whole point of #1796 is "never silently misbehave", and a
            raw 500 from a coordinator-generated (trusted) but malformed
            payload would be exactly that for the operator watching it fail.
    """
    if not isinstance(wire_def, dict) or not wire_def.get("type"):
        raise ValueError(
            f"malformed provider_def for provider {name!r}: expected a dict "
            f"with a non-empty 'type' key, got {wire_def!r}"
        )
    try:
        definition = ProviderDef(
            type=wire_def["type"],
            binary=wire_def.get("binary"),
            model=wire_def.get("model"),
            attach_url=wire_def.get("attach_url"),
            env=dict(wire_def.get("env") or {}),
            extra_args=list(wire_def.get("extra_args") or []),
        )
    except (TypeError, ValueError) as e:
        # #1796 review (non-blocking): a wire "env" that isn't dict-shaped
        # (e.g. a string or int) or an "extra_args" that isn't list-shaped
        # raises TypeError from dict()/list() above, not ValueError — catch
        # both here so every malformed shape becomes the same clean refusal
        # instead of a handful of them propagating as an uncaught 500 out of
        # AgentServer.assign() (agent_app.py only catches ValueError).
        raise ValueError(
            f"malformed provider_def for provider {name!r}: {e}"
        ) from e
    return build_provider(name, definition, models_cfg=None)


def resolve_provider_name(
    spec_provider: str | None,
    repo_provider: str | None,
    providers_cfg: "ProvidersConfig",
    issue_labels: "list[str] | None" = None,
) -> str:
    """Return the effective provider name using the precedence chain.

    Precedence (highest to lowest):
    1. *spec_provider* — per-assignment override (``AssignmentSpec.provider``).
    2. A ``providers.labels`` match against *issue_labels* (#1889) — e.g.
       ``harness:opencode`` -> ``opencode``. See
       :meth:`coord.config.ProvidersConfig.provider_for_labels` for the
       match rule. ``None``/empty *issue_labels* (the default) skips this
       link entirely, reproducing pre-#1889 behavior exactly.
    3. *repo_provider* — per-repo default (``Repo.provider`` in config).
    4. ``providers_cfg.default`` — global default (defaults to ``"claude"``).

    Args:
        spec_provider: Provider name from the assignment spec, or ``None``.
        repo_provider: Provider name from the repo config, or ``None``.
        providers_cfg: The parsed :class:`~coord.config.ProvidersConfig`.
        issue_labels: The target issue's GitHub label names, or ``None``.
            #1889: every caller gates this to ``type="work"`` proposals only
            (pass ``None`` for plan/review/smoke dispatches) — the same
            restriction ``models.labels`` uses (#1430), so a harness-eval
            label meant for the eventual work dispatch never leaks into a
            cheap/read-only stage.

    Returns:
        The winning provider name (always a non-empty string).
    """
    if spec_provider is not None:
        return spec_provider
    if issue_labels:
        label_provider = providers_cfg.provider_for_labels(issue_labels)
        if label_provider is not None:
            return label_provider
    if repo_provider is not None:
        return repo_provider
    return providers_cfg.default


def describe_provider_choice(
    spec_provider: str | None,
    repo_provider: str | None,
    providers_cfg: "ProvidersConfig",
    issue_labels: "list[str] | None" = None,
) -> str:
    """Format a one-line explanation of why the effective provider was chosen.

    #1707: mirrors ``coord.config.describe_model_choice``'s shape — state the
    winning name AND which link of the
    ``spec → label → repo → providers.default`` precedence chain
    (:func:`resolve_provider_name`) supplied it, so
    ``coord assign --dry-run --provider ...`` (and any other dry-run/status
    caller) never leaves an operator guessing whether a provider came from an
    explicit ``--provider``, a ``providers.labels`` match, a repo default, or
    the global fallback — the exact ambiguity #1454 fixed for models.

    #1889: when a ``providers.labels`` match won, names the matched label
    (and any other configured label present on the issue that it shadowed —
    mirrors :func:`coord.config.describe_model_choice`'s
    *shadowed_labels* phrasing, #1633), so a route that might look
    surprising is self-explaining at dispatch time instead of read from
    source — the exact transparency gap #1798 called out for the sibling
    model-label lever.

    Args:
        spec_provider: Provider name from the assignment spec / CLI flag, or
            ``None``.
        repo_provider: Provider name from the repo config, or ``None``.
        providers_cfg: The parsed :class:`~coord.config.ProvidersConfig`.
        issue_labels: The target issue's GitHub label names, or ``None`` —
            see :func:`resolve_provider_name`'s docstring for the
            ``type="work"``-only gating every caller applies.

    Returns:
        E.g. ``"opencode (explicit --provider)"``,
        ``"opencode (via label 'harness:opencode')"``, or
        ``"claude (providers.default)"``.
    """
    name = resolve_provider_name(spec_provider, repo_provider, providers_cfg, issue_labels)
    if spec_provider is not None:
        return f"{name} (explicit --provider)"
    if issue_labels:
        _, matched_label, shadowed_labels = providers_cfg.provider_for_labels_with_reason(
            issue_labels
        )
        if matched_label:
            if shadowed_labels:
                shadowed_str = ", ".join(repr(label) for label in shadowed_labels)
                return (
                    f"{name} (via label {matched_label!r}, "
                    f"shadowing {shadowed_str})"
                )
            return f"{name} (via label {matched_label!r})"
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
    issue_labels: "list[str] | None" = None,
) -> str:
    """STRUCTURAL TOS-COMPLIANCE GATE for unattended dispatch (#437).

    Resolves the effective provider name with :func:`resolve_provider_name`
    (precedence: spec → label → repo → providers.default), then
    instantiates the provider via :func:`build_provider` and inspects its
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
        issue_labels: The target issue's GitHub label names, or ``None``
            (#1889) — forwarded to :func:`resolve_provider_name` for
            ``providers.labels`` resolution. Callers gate this to
            ``type="work"`` proposals only, mirroring ``models.labels``
            (#1430); ``None`` reproduces pre-#1889 behavior exactly.

    Returns:
        The effective provider name (also returned for callers that want to
        thread it onward to the wire payload).

    Raises:
        ValueError: When the effective provider opts out of unattended use.
            The message names the provider, explains why, and points the
            user at ``coord assign --interactive``.
    """
    name = resolve_provider_name(spec_provider, repo_provider, providers_cfg, issue_labels)
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
