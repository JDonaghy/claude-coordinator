"""Tests for coord.providers — interface, registry, and ClaudeProvider.

Covers:
* Parity: ClaudeProvider.build_command == default_worker_command for all spec
  types, with and without model, with and without resume_session_id.
* Registry: build_provider dispatches on type; unknown type raises ValueError.
* Resolution chain: spec > repo > providers.default > "claude" precedence.
* Capabilities: all-true for ClaudeProvider; supports_inject() matches
  capabilities().inject (no drift).
* parse_log: delegates correctly to worker_events.parse_log.
* initial_input: produces a valid stream-json user-message line.
* result_marker: correct sentinel string.
* env: returns empty dict.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from coord.agent import (
    AssignmentSpec,
    default_worker_command,
)
from coord.config import ModelsConfig, ProviderDef, ProvidersConfig
from coord.models import Machine
from coord.providers import (
    build_provider,
    describe_provider_choice,
    guard_provider_machine_capability,
    machine_supports_provider,
    machines_supporting_provider,
    provider_type_for,
    resolve_default_provider,
    resolve_provider_name,
)
from coord.providers.base import Capabilities, Provider, WorkerSummary
from coord.providers.claude import ClaudeProvider
from coord.providers.opencode import (
    DEFAULT_OPENCODE_BINARY,
    RESULT_MARKER,
    OpenCodeProvider,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_spec(**kwargs) -> AssignmentSpec:
    """Create an AssignmentSpec with sensible defaults for tests."""
    defaults: dict = {
        "repo_name": "myrepo",
        "repo_path": "/some/path",
        "issue_number": 42,
        "issue_title": "Add something",
        "briefing": "Please do the thing.",
    }
    defaults.update(kwargs)
    return AssignmentSpec(**defaults)


# ── Parity tests ─────────────────────────────────────────────────────────────


def _parity(spec: AssignmentSpec) -> None:
    """Assert ClaudeProvider().build_command(spec) == default_worker_command(spec)."""
    legacy = default_worker_command(spec)
    provider_result = ClaudeProvider().build_command(spec)
    assert provider_result == legacy, (
        f"Parity failure for spec.type={spec.type!r}:\n"
        f"  legacy:   {legacy}\n"
        f"  provider: {provider_result}"
    )


def test_parity_work_type() -> None:
    """Work assignment (default type) without model."""
    spec = _make_spec(type="work")
    _parity(spec)


def test_parity_work_with_model() -> None:
    """Work assignment with a model alias."""
    spec = _make_spec(type="work", model="sonnet")
    _parity(spec)


def test_parity_work_with_deny_commands() -> None:
    """Work assignment with deny_commands appended to system prompt."""
    spec = _make_spec(
        type="work",
        deny_commands=["Bash(gh *)", "Bash(git push --force *)"],
    )
    _parity(spec)


def test_parity_plan_type() -> None:
    """Plan assignment (read-only worker)."""
    spec = _make_spec(type="plan")
    _parity(spec)


def test_parity_plan_with_model() -> None:
    """Plan assignment with a model."""
    spec = _make_spec(type="plan", model="haiku")
    _parity(spec)


def test_parity_refinement_type() -> None:
    """Refinement (developer-driven scoping chat)."""
    spec = _make_spec(type="refinement")
    _parity(spec)


def test_parity_test_chat_type() -> None:
    """Test-chat assignment."""
    spec = _make_spec(type="test-chat")
    _parity(spec)


def test_parity_new_issue_chat_type() -> None:
    """New-issue-chat assignment without per-repo guidance."""
    spec = _make_spec(type="new-issue-chat")
    _parity(spec)


def test_parity_new_issue_chat_with_guidance() -> None:
    """New-issue-chat with per-repo guidance appended."""
    spec = _make_spec(
        type="new-issue-chat",
        new_issue_guidance="Required sections: Title, What, Acceptance",
    )
    _parity(spec)


def test_parity_milestone_chat_type() -> None:
    """Milestone-chat assignment (#770)."""
    spec = _make_spec(type="milestone-chat")
    _parity(spec)


def test_parity_mock_author_type() -> None:
    """Mock-author assignment (#930, Gate A)."""
    spec = _make_spec(type="mock-author")
    _parity(spec)


def test_parity_with_resume_session_id() -> None:
    """Work assignment with resume_session_id (chat-continue dispatch)."""
    spec = _make_spec(type="work", resume_session_id="abc123session")
    _parity(spec)


def test_parity_plan_with_resume_session_id() -> None:
    """Plan assignment with resume_session_id."""
    spec = _make_spec(type="plan", resume_session_id="sess999")
    _parity(spec)


def test_parity_work_with_model_and_resume() -> None:
    """Work assignment with both model and resume_session_id."""
    spec = _make_spec(type="work", model="opus", resume_session_id="s42")
    _parity(spec)


def test_parity_custom_system_prompt() -> None:
    """Custom system_prompt on spec is honoured by both paths."""
    spec = _make_spec(type="work", system_prompt="Custom prompt here.")
    _parity(spec)


def test_resolved_model_overrides_spec_model() -> None:
    """resolved_model takes precedence over spec.model."""
    spec = _make_spec(type="work", model="sonnet")
    result = ClaudeProvider().build_command(spec, resolved_model="opus")
    assert "--model" in result
    idx = result.index("--model")
    assert result[idx + 1] == "opus"


def test_resolved_model_none_falls_back_to_spec_model() -> None:
    """When resolved_model is None, spec.model is used (back-compat)."""
    spec = _make_spec(type="work", model="haiku")
    result = ClaudeProvider().build_command(spec)
    assert "--model" in result
    idx = result.index("--model")
    assert result[idx + 1] == "haiku"


def test_resolved_model_suppresses_model_when_spec_model_none() -> None:
    """No --model flag when both resolved_model and spec.model are None."""
    spec = _make_spec(type="work", model=None)
    result = ClaudeProvider().build_command(spec)
    assert "--model" not in result


def test_system_prompt_override() -> None:
    """Explicit system_prompt kwarg overrides the computed one."""
    spec = _make_spec(type="work")
    custom = "My custom system prompt"
    result = ClaudeProvider().build_command(spec, system_prompt=custom)
    idx = result.index("--system-prompt")
    assert result[idx + 1] == custom


def test_allowed_tools_override() -> None:
    """Explicit allowed_tools kwarg overrides the computed value."""
    spec = _make_spec(type="work")
    result = ClaudeProvider().build_command(spec, allowed_tools="Read,Bash")
    idx = result.index("--allowedTools")
    assert result[idx + 1] == "Read,Bash"


def test_permission_mode_override() -> None:
    """Explicit permission_mode kwarg is reflected in the argv."""
    spec = _make_spec(type="work")
    result = ClaudeProvider().build_command(spec, permission_mode="bypassPermissions")
    idx = result.index("--permission-mode")
    assert result[idx + 1] == "bypassPermissions"


def test_custom_binary() -> None:
    """ClaudeProvider(binary='my-claude') uses the custom binary."""
    spec = _make_spec(type="work")
    result = ClaudeProvider(binary="my-claude").build_command(spec)
    assert result[0] == "my-claude"


# ── #1706: definition model / env / extra_args threading ──────────────────────


def test_definition_model_used_when_no_resolved_or_spec_model() -> None:
    """Provider-definition model is the lowest-precedence fallback."""
    spec = _make_spec(type="work", model=None)
    result = ClaudeProvider(model="glm-4.6").build_command(spec)
    idx = result.index("--model")
    assert result[idx + 1] == "glm-4.6"


def test_spec_model_beats_definition_model() -> None:
    """spec.model outranks the provider-definition model."""
    spec = _make_spec(type="work", model="haiku")
    result = ClaudeProvider(model="glm-4.6").build_command(spec)
    idx = result.index("--model")
    assert result[idx + 1] == "haiku"


def test_resolved_model_beats_definition_model() -> None:
    """An explicit resolved_model outranks both spec.model and the
    provider-definition model — the full precedence chain."""
    spec = _make_spec(type="work", model="haiku")
    result = ClaudeProvider(model="glm-4.6").build_command(
        spec, resolved_model="opus"
    )
    idx = result.index("--model")
    assert result[idx + 1] == "opus"


def test_no_model_anywhere_omits_flag() -> None:
    """No --model flag when resolved_model, spec.model, and the definition
    model are all None (no-config parity)."""
    spec = _make_spec(type="work", model=None)
    result = ClaudeProvider().build_command(spec)
    assert "--model" not in result


def test_definition_env_returned_by_env() -> None:
    """env() returns a copy of the definition's env dict."""
    provider = ClaudeProvider(env={"FOO": "bar", "BAZ": "qux"})
    assert provider.env() == {"FOO": "bar", "BAZ": "qux"}


def test_definition_env_is_copied_not_aliased() -> None:
    """Mutating the dict returned by env() must not affect the provider's
    internal state (or a caller's original dict passed to __init__)."""
    source = {"FOO": "bar"}
    provider = ClaudeProvider(env=source)
    returned = provider.env()
    returned["FOO"] = "mutated"
    source["FOO"] = "also-mutated"
    assert provider.env() == {"FOO": "bar"}


def test_definition_extra_args_appended_after_own_flags() -> None:
    """extra_args land at the end of the argv, after every flag build_command
    itself constructs (including --resume when a resume_session_id is set)."""
    spec = _make_spec(type="work", resume_session_id="sess1")
    result = ClaudeProvider(extra_args=["--foo", "bar"]).build_command(spec)
    assert result[-2:] == ["--foo", "bar"]
    # And --resume (the last flag build_command constructs itself) still
    # precedes the extra_args.
    resume_idx = result.index("--resume")
    assert resume_idx < len(result) - 2


def test_no_extra_args_no_trailing_entries() -> None:
    """No definition extra_args → argv is unchanged (no-config parity)."""
    spec = _make_spec(type="work")
    legacy = default_worker_command(spec)
    result = ClaudeProvider().build_command(spec)
    assert result == legacy


# ── initial_input ─────────────────────────────────────────────────────────────


def test_initial_input_is_valid_stream_json() -> None:
    """initial_input() returns a stream-json user-message line."""
    spec = _make_spec(briefing="Hello, worker!")
    data = ClaudeProvider().initial_input(spec)
    assert isinstance(data, bytes)
    obj = json.loads(data.decode("utf-8").strip())
    assert obj["type"] == "user"
    assert obj["message"]["role"] == "user"
    assert obj["message"]["content"] == "Hello, worker!"


# ── result_marker, env ────────────────────────────────────────────────────────


def test_result_marker() -> None:
    """result_marker() returns the expected sentinel."""
    assert ClaudeProvider().result_marker() == '"type":"result"'


def test_env_empty() -> None:
    """env() returns an empty dict for ClaudeProvider."""
    assert ClaudeProvider().env() == {}


# ── Capabilities / supports_inject ────────────────────────────────────────────


def test_capabilities_all_true() -> None:
    """ClaudeProvider reports all capabilities as True and billing_mode='metered'."""
    caps = ClaudeProvider().capabilities()
    assert isinstance(caps, Capabilities)
    assert caps.resume is True
    assert caps.inject is True
    assert caps.cost_reporting is True
    assert caps.true_system_prompt is True
    assert caps.enforces_deny_list is True
    # billing_mode is the Track-3 routing signal for the June-15 metering
    # mitigation (#322) — claude -p is billed at API rates.
    assert caps.billing_mode == "metered"


def test_capabilities_billing_mode_is_string() -> None:
    """billing_mode is always a string from the documented vocabulary."""
    caps = ClaudeProvider().capabilities()
    assert isinstance(caps.billing_mode, str)
    assert caps.billing_mode in {"subscription", "metered", "byo_key", "unknown"}


def test_supports_inject_agrees_with_capabilities() -> None:
    """supports_inject() must not disagree with capabilities().inject."""
    p = ClaudeProvider()
    assert p.supports_inject() == p.capabilities().inject


def test_capabilities_frozen() -> None:
    """Capabilities is a frozen dataclass — mutation raises."""
    caps = ClaudeProvider().capabilities()
    with pytest.raises(Exception):  # FrozenInstanceError
        caps.resume = False  # type: ignore[misc]


# ── Registry: build_provider ──────────────────────────────────────────────────


def test_build_provider_claude_type() -> None:
    """build_provider with type='claude' returns a ClaudeProvider."""
    defn = ProviderDef(type="claude")
    provider = build_provider("claude", defn, None)
    assert isinstance(provider, ClaudeProvider)


def test_build_provider_claude_with_binary() -> None:
    """build_provider passes the binary override to ClaudeProvider."""
    defn = ProviderDef(type="claude", binary="my-claude")
    provider = build_provider("claude", defn, None)
    assert isinstance(provider, ClaudeProvider)
    # Verify the binary is wired in by checking the argv.
    spec = _make_spec(type="work")
    argv = provider.build_command(spec)
    assert argv[0] == "my-claude"


def test_build_provider_threads_model_env_extra_args() -> None:
    """#1706: build_provider threads ProviderDef.model / env / extra_args
    into the constructed ClaudeProvider instance."""
    defn = ProviderDef(
        type="claude",
        model="glm-4.6",
        env={"FOO": "bar"},
        extra_args=["--verbose-extra"],
    )
    provider = build_provider("claude", defn, None)
    assert isinstance(provider, ClaudeProvider)
    assert provider.env() == {"FOO": "bar"}

    spec = _make_spec(type="work", model=None)
    argv = provider.build_command(spec)
    idx = argv.index("--model")
    assert argv[idx + 1] == "glm-4.6"
    assert argv[-1] == "--verbose-extra"


def test_build_provider_no_config_parity() -> None:
    """A bare ProviderDef(type='claude') (no model/env/extra_args) produces
    a provider byte-identical to a plain ClaudeProvider() — the regression
    that matters most: deployments with no providers: block are unaffected."""
    defn = ProviderDef(type="claude")
    provider = build_provider("claude", defn, None)
    assert provider.env() == {}
    spec = _make_spec(type="work")
    assert provider.build_command(spec) == ClaudeProvider().build_command(spec)
    assert provider.build_command(spec) == default_worker_command(spec)


def test_build_provider_unknown_type_raises() -> None:
    """build_provider raises ValueError with a descriptive message for unknown types."""
    defn = ProviderDef(type="unknown-backend")
    with pytest.raises(ValueError, match="unknown-backend"):
        build_provider("x", defn, None)


def test_build_provider_unknown_type_names_the_provider() -> None:
    """The ValueError names the provider (not just the type) for debuggability."""
    defn = ProviderDef(type="some-other-backend")
    with pytest.raises(ValueError, match="my-weird-provider"):
        build_provider("my-weird-provider", defn, None)


def test_build_provider_with_models_cfg() -> None:
    """build_provider accepts a ModelsConfig without error."""
    defn = ProviderDef(type="claude")
    models = ModelsConfig()
    provider = build_provider("claude", defn, models)
    assert isinstance(provider, ClaudeProvider)


# ── Registry: resolve_provider_name ──────────────────────────────────────────


def _make_providers_cfg(default: str = "claude") -> ProvidersConfig:
    return ProvidersConfig(default=default)


def test_resolve_spec_beats_repo_and_default() -> None:
    """spec_provider has highest precedence."""
    cfg = _make_providers_cfg(default="claude")
    result = resolve_provider_name("fast-claude", "repo-provider", cfg)
    assert result == "fast-claude"


def test_resolve_repo_beats_default() -> None:
    """repo_provider beats the global default when spec has none."""
    cfg = _make_providers_cfg(default="claude")
    result = resolve_provider_name(None, "repo-provider", cfg)
    assert result == "repo-provider"


def test_resolve_default_when_no_spec_or_repo() -> None:
    """Falls back to providers.default when neither spec nor repo override."""
    cfg = _make_providers_cfg(default="my-default")
    result = resolve_provider_name(None, None, cfg)
    assert result == "my-default"


def test_resolve_default_is_claude_when_unconfigured() -> None:
    """Default ProvidersConfig has default='claude'."""
    cfg = ProvidersConfig()
    result = resolve_provider_name(None, None, cfg)
    assert result == "claude"


def test_resolve_spec_none_repo_none_uses_default() -> None:
    """Double-None falls back to configured default."""
    cfg = _make_providers_cfg(default="claude")
    assert resolve_provider_name(None, None, cfg) == "claude"


# ── provider-availability machine capability gate (#1711) ──────────────────────


def _machine(name: str, capabilities: list[str] | None = None) -> Machine:
    return Machine(
        name=name, host=f"{name}.tailnet", capabilities=capabilities or [],
        repos=["api"], repo_paths={"api": f"/home/user/src/{name}"},
    )


class TestProviderTypeFor:
    def test_resolves_registered_name_to_its_type(self) -> None:
        cfg = ProvidersConfig(
            definitions={"claude": ProviderDef(type="claude"), "oc": ProviderDef(type="opencode")},
        )
        assert provider_type_for("oc", cfg) == "opencode"
        assert provider_type_for("claude", cfg) == "claude"

    def test_unknown_name_falls_back_to_itself(self) -> None:
        """A typo'd / removed-after-dispatch provider name isn't fabricated
        into a refusal here — the existing unknown-provider error path
        handles it (mirrors guard_unattended_dispatch's posture)."""
        cfg = ProvidersConfig()
        assert provider_type_for("totally-unregistered", cfg) == "totally-unregistered"


class TestMachineSupportsProvider:
    def test_claude_is_implicit_baseline_with_no_capability_declared(self) -> None:
        cfg = ProvidersConfig()
        machine = _machine("laptop")  # capabilities=[]
        assert machine_supports_provider(machine, "claude", cfg) is True

    def test_claude_pty_is_implicit_baseline_by_type_not_name(self) -> None:
        cfg = ProvidersConfig(definitions={"interactive": ProviderDef(type="claude-pty")})
        machine = _machine("laptop")
        assert machine_supports_provider(machine, "interactive", cfg) is True

    def test_claude_typed_alias_needs_no_capability_either(self) -> None:
        """A claude-backed provider registered under a different NAME (the
        `fast-claude` pattern) is still implicit — keyed off type, not name."""
        cfg = ProvidersConfig(definitions={"fast-claude": ProviderDef(type="claude")})
        machine = _machine("laptop")
        assert machine_supports_provider(machine, "fast-claude", cfg) is True

    def test_opencode_requires_the_declared_capability(self) -> None:
        cfg = ProvidersConfig(definitions={"opencode": ProviderDef(type="opencode")})
        bare_machine = _machine("laptop")
        capable_machine = _machine("workstation", ["provider:opencode"])
        assert machine_supports_provider(bare_machine, "opencode", cfg) is False
        assert machine_supports_provider(capable_machine, "opencode", cfg) is True

    def test_opencode_alias_keyed_by_type_not_registered_name(self) -> None:
        """A definition named 'my-oc' of type opencode still needs
        `provider:opencode` (the TYPE), not `provider:my-oc`."""
        cfg = ProvidersConfig(definitions={"my-oc": ProviderDef(type="opencode")})
        machine = _machine("workstation", ["provider:opencode"])
        assert machine_supports_provider(machine, "my-oc", cfg) is True
        machine_wrong_cap = _machine("other", ["provider:my-oc"])
        assert machine_supports_provider(machine_wrong_cap, "my-oc", cfg) is False


class TestMachinesSupportingProvider:
    def test_lists_only_capable_machines_sorted(self) -> None:
        cfg = ProvidersConfig(definitions={"opencode": ProviderDef(type="opencode")})
        machines = [
            _machine("zeta", ["provider:opencode"]),
            _machine("bare"),
            _machine("alpha", ["provider:opencode"]),
        ]
        assert machines_supporting_provider(machines, "opencode", cfg) == ["alpha", "zeta"]

    def test_empty_when_nobody_declares_it(self) -> None:
        cfg = ProvidersConfig(definitions={"opencode": ProviderDef(type="opencode")})
        machines = [_machine("laptop"), _machine("desktop")]
        assert machines_supporting_provider(machines, "opencode", cfg) == []

    def test_every_machine_for_claude(self) -> None:
        cfg = ProvidersConfig()
        machines = [_machine("laptop"), _machine("desktop")]
        assert machines_supporting_provider(machines, "claude", cfg) == ["desktop", "laptop"]


class TestGuardProviderMachineCapability:
    def test_noop_when_machine_supports_it(self) -> None:
        cfg = ProvidersConfig(definitions={"opencode": ProviderDef(type="opencode")})
        machine = _machine("workstation", ["provider:opencode"])
        guard_provider_machine_capability(
            provider_name="opencode", machine=machine, all_machines=[machine],
            providers_cfg=cfg,
        )  # must not raise

    def test_noop_for_claude_on_a_bare_machine(self) -> None:
        cfg = ProvidersConfig()
        machine = _machine("laptop")
        guard_provider_machine_capability(
            provider_name="claude", machine=machine, all_machines=[machine],
            providers_cfg=cfg,
        )  # must not raise

    def test_raises_naming_machine_provider_and_type(self) -> None:
        cfg = ProvidersConfig(definitions={"opencode": ProviderDef(type="opencode")})
        machine = _machine("laptop")
        with pytest.raises(ValueError) as exc_info:
            guard_provider_machine_capability(
                provider_name="opencode", machine=machine, all_machines=[machine],
                providers_cfg=cfg, where="coord assign",
            )
        message = str(exc_info.value)
        assert "laptop" in message
        assert "opencode" in message
        assert "provider:opencode" in message
        assert "coord assign" in message

    def test_names_the_machines_that_do_support_it(self) -> None:
        cfg = ProvidersConfig(definitions={"opencode": ProviderDef(type="opencode")})
        bare = _machine("laptop")
        capable = _machine("workstation", ["provider:opencode"])
        with pytest.raises(ValueError, match="workstation"):
            guard_provider_machine_capability(
                provider_name="opencode", machine=bare, all_machines=[bare, capable],
                providers_cfg=cfg,
            )

    def test_states_plainly_when_no_machine_supports_it_yet(self) -> None:
        cfg = ProvidersConfig(definitions={"opencode": ProviderDef(type="opencode")})
        machine = _machine("laptop")
        with pytest.raises(ValueError, match="no configured machine advertises"):
            guard_provider_machine_capability(
                provider_name="opencode", machine=machine, all_machines=[machine],
                providers_cfg=cfg,
            )

    def test_unregistered_provider_name_does_not_crash(self) -> None:
        """A typo'd/unregistered name resolves to itself (provider_type_for's
        fallback) — the gate can still refuse cleanly rather than raising a
        KeyError, deferring the "is this even a real provider" question to
        the existing unknown-provider error path."""
        cfg = ProvidersConfig()
        machine = _machine("laptop")
        with pytest.raises(ValueError, match="totally-unregistered"):
            guard_provider_machine_capability(
                provider_name="totally-unregistered", machine=machine,
                all_machines=[machine], providers_cfg=cfg,
            )


# ── describe_provider_choice (#1707) ───────────────────────────────────────────


def test_describe_provider_choice_explicit_spec_wins() -> None:
    """An explicit spec override (e.g. `coord assign --provider`) is labelled
    as such, even when a repo default is also configured."""
    cfg = _make_providers_cfg(default="claude")
    reason = describe_provider_choice("fast-claude", "repo-provider", cfg)
    assert reason == "fast-claude (explicit --provider)"


def test_describe_provider_choice_repo_default() -> None:
    """No spec override: the repo's Repo.provider is named as the source."""
    cfg = _make_providers_cfg(default="claude")
    reason = describe_provider_choice(None, "repo-provider", cfg)
    assert reason == "repo-provider (repo default: Repo.provider)"


def test_describe_provider_choice_global_default() -> None:
    """Neither spec nor repo override: falls through to providers.default."""
    cfg = _make_providers_cfg(default="my-default")
    reason = describe_provider_choice(None, None, cfg)
    assert reason == "my-default (providers.default)"


def test_describe_provider_choice_matches_resolve_provider_name() -> None:
    """The resolved name embedded in the description always matches what
    resolve_provider_name itself would return for the same inputs — the
    description can never disagree with the resolution it's explaining."""
    cfg = ProvidersConfig(
        default="claude",
        definitions={"claude": ProviderDef(type="claude"), "x": ProviderDef(type="claude")},
    )
    for spec, repo in [(None, None), (None, "x"), ("x", None), ("x", "claude")]:
        resolved = resolve_provider_name(spec, repo, cfg)
        reason = describe_provider_choice(spec, repo, cfg)
        assert reason.startswith(resolved + " (")


# ── parse_log delegation ──────────────────────────────────────────────────────


def test_parse_log_empty_file(tmp_path: Path) -> None:
    """parse_log on an empty file returns a blank WorkerSummary."""
    log = tmp_path / "worker.log"
    log.write_text("")
    summary = ClaudeProvider().parse_log(log)
    assert isinstance(summary, WorkerSummary)
    assert summary.num_turns == 0
    assert summary.total_cost_usd == 0.0


def test_parse_log_delegates_to_worker_events(tmp_path: Path) -> None:
    """parse_log returns the same result as worker_events.parse_log."""
    from coord.worker_events import parse_log as we_parse_log

    log = tmp_path / "worker.log"
    # Write a minimal stream-json log with a result event.
    lines = [
        json.dumps({
            "type": "system",
            "subtype": "init",
            "session_id": "sess-abc",
            "model": "claude-sonnet",
        }),
        json.dumps({
            "type": "result",
            "num_turns": 3,
            "total_cost_usd": 0.42,
            "stop_reason": "end_turn",
        }),
    ]
    log.write_text("\n".join(lines) + "\n")

    provider_summary = ClaudeProvider().parse_log(log, tail_bytes=0)
    direct_summary = we_parse_log(log, tail_bytes=0)

    assert provider_summary.session_id == direct_summary.session_id
    assert provider_summary.num_turns == direct_summary.num_turns
    assert provider_summary.total_cost_usd == direct_summary.total_cost_usd
    assert provider_summary.stop_reason == direct_summary.stop_reason
    assert provider_summary.model_used == direct_summary.model_used


# ── Provider ABC structural check ────────────────────────────────────────────


def test_provider_is_abstract() -> None:
    """Provider cannot be instantiated directly (it is abstract)."""
    with pytest.raises(TypeError, match="abstract"):
        Provider()  # type: ignore[abstract]


# ── OpenCodeProvider ──────────────────────────────────────────────────────────
#
# All tests below exercise the provider in isolation (no subprocess
# execution) against its command structure, result marker, and NDJSON parse
# logic — see the module docstring in coord/providers/opencode.py.
#
# #1703 replaced tests/fixtures/opencode_run_sample.jsonl with a VERBATIM
# capture from a real opencode binary (see docs/OPENCODE_VERIFICATION.md for
# the machine, version, and full findings) and added
# tests/fixtures/opencode_run_failure_sample.jsonl for a real failing run.
# #1704 (this issue) corrected the provider to match that captured evidence:
# RESULT_MARKER, build_command's flag set, parse_log's event mapping, and
# the capabilities flags cost_reporting / true_system_prompt now all cite
# the verification doc directly. The tests below exercise the CORRECTED
# behaviour against the real fixtures (both success and failure).
# ─────────────────────────────────────────────────────────────────────────────


# ── capabilities ──────────────────────────────────────────────────────────────


def test_opencode_capabilities_declared_values() -> None:
    """OpenCodeProvider.capabilities() returns the #1704-corrected values."""
    caps = OpenCodeProvider().capabilities()
    assert isinstance(caps, Capabilities)
    assert caps.resume is True          # --session/--continue confirmed to resume
    assert caps.inject is False         # no mid-session injection path (still unknown)
    assert caps.cost_reporting is True  # part.cost summed across step_finish events
    assert caps.true_system_prompt is True  # --agent's prompt field is a real system prompt
    assert caps.enforces_deny_list is False  # SAFETY: coord-side enforcement still unproven
    assert caps.billing_mode == "byo_key"   # uses operator's own API keys
    assert caps.human_attended_only is False  # headless run mode is automatable


def test_opencode_capabilities_cost_reporting_is_true() -> None:
    """cost_reporting must be True — pins the #1704 flip so an accidental
    regression back to False (which silences real cost data in the TUI) is
    caught immediately."""
    assert OpenCodeProvider().capabilities().cost_reporting is True


def test_opencode_capabilities_true_system_prompt_is_true() -> None:
    """true_system_prompt must be True — pins the #1704 flip: --agent's
    'prompt' field is a confirmed real system-prompt equivalent."""
    assert OpenCodeProvider().capabilities().true_system_prompt is True


def test_opencode_capabilities_billing_mode_is_valid() -> None:
    """billing_mode is a string from the documented vocabulary."""
    caps = OpenCodeProvider().capabilities()
    assert isinstance(caps.billing_mode, str)
    assert caps.billing_mode in {"subscription", "metered", "byo_key", "unknown"}


def test_opencode_supports_inject_agrees_with_capabilities() -> None:
    """supports_inject() must not disagree with capabilities().inject."""
    p = OpenCodeProvider()
    assert p.supports_inject() == p.capabilities().inject
    assert p.supports_inject() is False


def test_opencode_capabilities_frozen() -> None:
    """Capabilities is a frozen dataclass — mutation raises."""
    caps = OpenCodeProvider().capabilities()
    with pytest.raises(Exception):  # FrozenInstanceError
        caps.resume = False  # type: ignore[misc]


def test_opencode_enforces_deny_list_is_false() -> None:
    """enforces_deny_list must be False — this is a safety gate requirement.

    #324's safety gate refuses write-capable worker types on any provider
    that reports enforces_deny_list=False.  This test pins the value so an
    accidental True flip is caught immediately.
    """
    assert OpenCodeProvider().capabilities().enforces_deny_list is False


# ── build_command ─────────────────────────────────────────────────────────────


def test_opencode_build_command_basic() -> None:
    """build_command uses 'opencode run BRIEFING' for a basic work spec."""
    spec = _make_spec(type="work", briefing="Implement the feature.")
    argv = OpenCodeProvider().build_command(spec)
    # ASSUMPTION: subcommand is 'run', briefing is the last positional arg.
    assert argv[0] == DEFAULT_OPENCODE_BINARY
    assert argv[1] == "run"
    assert argv[-1] == "Implement the feature."


def test_opencode_build_command_no_model_omits_flag() -> None:
    """--model is omitted when neither resolved_model nor spec.model is set."""
    spec = _make_spec(type="work", briefing="do stuff", model=None)
    argv = OpenCodeProvider().build_command(spec, resolved_model=None)
    assert "--model" not in argv


def test_opencode_build_command_with_spec_model() -> None:
    """--model is included when spec.model is set and resolved_model is None."""
    spec = _make_spec(type="work", briefing="do stuff", model="sonnet")
    argv = OpenCodeProvider().build_command(spec)
    assert "--model" in argv
    idx = argv.index("--model")
    assert argv[idx + 1] == "sonnet"


def test_opencode_build_command_resolved_model_overrides_spec() -> None:
    """resolved_model takes precedence over spec.model."""
    spec = _make_spec(type="work", briefing="do stuff", model="sonnet")
    argv = OpenCodeProvider().build_command(spec, resolved_model="opus")
    idx = argv.index("--model")
    assert argv[idx + 1] == "opus"


def test_opencode_definition_model_used_when_no_resolved_or_spec_model() -> None:
    """#1706: the provider-definition model is the lowest-precedence
    fallback — this is opencode's `--model provider/model` selection, the
    whole point of the opencode backend."""
    spec = _make_spec(type="work", briefing="do stuff", model=None)
    argv = OpenCodeProvider(model="zhipuai/glm-4.6").build_command(spec)
    idx = argv.index("--model")
    assert argv[idx + 1] == "zhipuai/glm-4.6"


def test_opencode_spec_model_beats_definition_model() -> None:
    """spec.model outranks the provider-definition model."""
    spec = _make_spec(type="work", briefing="do stuff", model="sonnet")
    argv = OpenCodeProvider(model="zhipuai/glm-4.6").build_command(spec)
    idx = argv.index("--model")
    assert argv[idx + 1] == "sonnet"


def test_opencode_resolved_model_beats_definition_model() -> None:
    """resolved_model outranks both spec.model and the definition model."""
    spec = _make_spec(type="work", briefing="do stuff", model="sonnet")
    argv = OpenCodeProvider(model="zhipuai/glm-4.6").build_command(
        spec, resolved_model="opus"
    )
    idx = argv.index("--model")
    assert argv[idx + 1] == "opus"


def test_opencode_definition_extra_args_before_briefing() -> None:
    """extra_args land after this method's own flags but before the
    trailing positional briefing argument."""
    spec = _make_spec(type="work", briefing="THE-BRIEFING")
    argv = OpenCodeProvider(extra_args=["--foo", "bar"]).build_command(spec)
    assert argv[-1] == "THE-BRIEFING"
    assert argv[-3:-1] == ["--foo", "bar"]


def test_opencode_build_command_with_resume_session_id() -> None:
    """--session SESSION_ID is included when spec.resume_session_id is set."""
    spec = _make_spec(type="work", briefing="continue", resume_session_id="oc-sess-xyz")
    argv = OpenCodeProvider().build_command(spec)
    assert "--session" in argv
    idx = argv.index("--session")
    assert argv[idx + 1] == "oc-sess-xyz"


def test_opencode_build_command_no_resume_omits_flag() -> None:
    """--session is omitted when resume_session_id is None."""
    spec = _make_spec(type="work", briefing="fresh start")
    argv = OpenCodeProvider().build_command(spec)
    assert "--session" not in argv


def test_opencode_build_command_custom_binary() -> None:
    """OpenCodeProvider(binary='my-opencode') uses the custom binary."""
    spec = _make_spec(type="work", briefing="do it")
    argv = OpenCodeProvider(binary="/opt/opencode").build_command(spec)
    assert argv[0] == "/opt/opencode"


def test_opencode_build_command_with_attach_url() -> None:
    """When attach_url is set, --attach <url> is inserted before the briefing."""
    spec = _make_spec(type="work", briefing="do it")
    p = OpenCodeProvider(attach_url="http://localhost:4242")
    argv = p.build_command(spec)
    assert "--attach" in argv, "--attach flag missing from argv"
    idx = argv.index("--attach")
    assert argv[idx + 1] == "http://localhost:4242", "attach URL value mismatch"
    # Briefing must still be the final argument.
    assert argv[-1] == "do it"


def test_opencode_build_command_without_attach_url_omits_flag() -> None:
    """When attach_url is None (default), --attach is absent from argv."""
    spec = _make_spec(type="work", briefing="do it")
    argv = OpenCodeProvider().build_command(spec)
    assert "--attach" not in argv


def test_opencode_build_command_briefing_is_last_arg() -> None:
    """Briefing is always the last element of the argv."""
    spec = _make_spec(type="work", briefing="my briefing text", model="haiku",
                      resume_session_id="sess-1")
    argv = OpenCodeProvider().build_command(spec)
    assert argv[-1] == "my briefing text"


def test_opencode_build_command_multiline_briefing() -> None:
    """Multi-line briefings are passed as a single argv element (no shell splitting)."""
    briefing = "Line one.\nLine two.\nLine three."
    spec = _make_spec(type="work", briefing=briefing)
    argv = OpenCodeProvider().build_command(spec)
    assert argv[-1] == briefing  # no splitting — subprocess receives it intact


def test_opencode_build_command_system_prompt_ignored() -> None:
    """system_prompt kwarg is accepted but silently ignored (no OpenCode equivalent)."""
    spec = _make_spec(type="work", briefing="do stuff")
    argv = OpenCodeProvider().build_command(spec, system_prompt="My system prompt")
    # system_prompt must not appear anywhere in the argv
    assert "My system prompt" not in argv
    assert "--system-prompt" not in argv


def test_opencode_build_command_allowed_tools_ignored() -> None:
    """allowed_tools kwarg is accepted but silently ignored."""
    spec = _make_spec(type="work", briefing="do stuff")
    argv = OpenCodeProvider().build_command(spec, allowed_tools="Read,Bash")
    assert "Read,Bash" not in argv
    assert "--allowedTools" not in argv


def test_opencode_build_command_permission_mode_ignored() -> None:
    """permission_mode kwarg is accepted but silently ignored."""
    spec = _make_spec(type="work", briefing="do stuff")
    argv = OpenCodeProvider().build_command(spec, permission_mode="bypassPermissions")
    assert "bypassPermissions" not in argv
    assert "--permission-mode" not in argv


def test_opencode_build_command_returns_list_of_strings() -> None:
    """build_command always returns a list[str] (safe for subprocess.Popen)."""
    spec = _make_spec(type="work", briefing="hello", model="haiku")
    argv = OpenCodeProvider().build_command(spec)
    assert isinstance(argv, list)
    for item in argv:
        assert isinstance(item, str)


# ── #1704: --format json / --agent / --auto ────────────────────────────────────


def test_opencode_build_command_always_includes_format_json() -> None:
    """--format json is always present — without it there is no NDJSON
    stream for result_marker()/parse_log() to work against."""
    spec = _make_spec(type="work", briefing="do stuff")
    argv = OpenCodeProvider().build_command(spec)
    assert "--format" in argv
    idx = argv.index("--format")
    assert argv[idx + 1] == "json"


@pytest.mark.parametrize(
    ("spec_type", "expected_agent"),
    [
        ("work", "coord-work"),
        ("plan", "coord-plan"),
        ("review", "coord-review"),
        ("conflict-fix", "coord-conflict-fix"),
        ("smoke", "coord-smoke"),
        ("refinement", "coord-refinement"),
        ("test-chat", "coord-test-chat"),
        ("new-issue-chat", "coord-new-issue-chat"),
        ("milestone-chat", "coord-milestone-chat"),
        ("mock-author", "coord-mock-author"),
    ],
)
def test_opencode_build_command_agent_flag_follows_spec_type(
    spec_type: str, expected_agent: str
) -> None:
    """--agent NAME is derived from spec.type via the coord-<type> naming
    contract (see _agent_name_for_type's docstring) — this is what
    replaces the ignored system_prompt/allowed_tools kwargs."""
    spec = _make_spec(type=spec_type, briefing="do stuff")
    argv = OpenCodeProvider().build_command(spec)
    assert "--agent" in argv
    idx = argv.index("--agent")
    assert argv[idx + 1] == expected_agent


def test_opencode_build_command_agent_flag_default_type_is_work() -> None:
    """AssignmentSpec.type defaults to 'work' → --agent coord-work."""
    spec = _make_spec(briefing="do stuff")
    argv = OpenCodeProvider().build_command(spec)
    idx = argv.index("--agent")
    assert argv[idx + 1] == "coord-work"


def test_opencode_build_command_agent_ignores_explicit_system_prompt_text() -> None:
    """Even when system_prompt/allowed_tools text is passed, --agent's value
    is still derived from spec.type, never from that text — --agent
    REPLACES those kwargs rather than being built from them."""
    spec = _make_spec(type="plan", briefing="do stuff")
    argv = OpenCodeProvider().build_command(
        spec, system_prompt="ignore me", allowed_tools="Read,Bash"
    )
    idx = argv.index("--agent")
    assert argv[idx + 1] == "coord-plan"


def test_opencode_build_command_always_includes_auto() -> None:
    """--auto is always passed (headless-safety; see build_command's
    docstring for the deny-list safety note)."""
    spec = _make_spec(type="work", briefing="do stuff")
    argv = OpenCodeProvider().build_command(spec)
    assert "--auto" in argv


def test_opencode_build_command_format_agent_auto_precede_extra_args_and_briefing() -> None:
    """--format/--agent/--auto are inserted before extra_args and the
    trailing briefing — never after (the briefing must stay last)."""
    spec = _make_spec(type="work", briefing="THE-BRIEFING")
    argv = OpenCodeProvider(extra_args=["--foo", "bar"]).build_command(spec)
    assert argv[-1] == "THE-BRIEFING"
    assert argv[-3:-1] == ["--foo", "bar"]
    for flag in ("--format", "--agent", "--auto"):
        assert argv.index(flag) < argv.index("--foo")


# ── initial_input ─────────────────────────────────────────────────────────────


def test_opencode_initial_input_returns_empty_bytes() -> None:
    """initial_input() returns b'' — briefing is on argv, nothing goes to stdin."""
    spec = _make_spec(briefing="Hello, worker!")
    data = OpenCodeProvider().initial_input(spec)
    assert isinstance(data, bytes)
    assert data == b""


def test_opencode_initial_input_is_falsy() -> None:
    """initial_input() must be falsy so the spawn path skips the stdin write."""
    spec = _make_spec(briefing="Anything here.")
    assert not OpenCodeProvider().initial_input(spec)


# ── result_marker ─────────────────────────────────────────────────────────────


def test_opencode_result_marker() -> None:
    """result_marker() returns the module-level RESULT_MARKER constant."""
    assert OpenCodeProvider().result_marker() == RESULT_MARKER


def test_opencode_result_marker_matches_real_success_fixture() -> None:
    """#1704 FIX PINNED: the corrected RESULT_MARKER ('"reason":"stop"')
    DOES appear in a real successful ``opencode run --format json`` capture.

    #1703 replaced ``opencode_run_sample.jsonl`` with a verbatim capture from
    a real opencode binary (see ``docs/OPENCODE_VERIFICATION.md``) and
    proved the FIRST-PASS marker ('"type":"session.complete"') never
    matched real output. #1704 corrected RESULT_MARKER to the real
    terminal signal — the last ``step_finish`` event's
    ``part.reason == "stop"`` — this test pins that the fix actually landed.
    """
    fixtures_dir = Path(__file__).parent / "fixtures"
    fixture = fixtures_dir / "opencode_run_sample.jsonl"
    assert fixture.exists(), "opencode_run_sample.jsonl fixture is missing"
    lines = [ln for ln in fixture.read_text().splitlines() if ln.strip()]
    marker = OpenCodeProvider().result_marker()
    assert any(marker in line for line in lines), (
        f"result_marker {marker!r} not found in the real success fixture — "
        "RESULT_MARKER regressed away from the verified 'reason':'stop' signal"
    )
    # The old, invented marker must never match either (regression guard).
    assert not any('"type":"session.complete"' in line for line in lines)


def test_opencode_result_marker_absent_from_real_failure_fixture() -> None:
    """A failing run has NO terminal step_finish at all (confirmed: it ends
    with a top-level error event instead), so the success marker correctly
    never matches — this is expected, not a gap. Failure is detected via
    exit code / parse_log's error-event handling, not this marker."""
    fixture = Path(__file__).parent / "fixtures" / "opencode_run_failure_sample.jsonl"
    assert fixture.exists(), "opencode_run_failure_sample.jsonl fixture is missing"
    lines = [ln for ln in fixture.read_text().splitlines() if ln.strip()]
    marker = OpenCodeProvider().result_marker()
    assert not any(marker in line for line in lines)


def test_opencode_result_marker_is_string() -> None:
    """result_marker() returns a non-empty string."""
    marker = OpenCodeProvider().result_marker()
    assert isinstance(marker, str)
    assert len(marker) > 0


# ── env ───────────────────────────────────────────────────────────────────────


def test_opencode_env_empty() -> None:
    """env() returns an empty dict for OpenCodeProvider."""
    assert OpenCodeProvider().env() == {}


def test_opencode_definition_env_returned_by_env() -> None:
    """#1706: env() returns a copy of the definition's env dict — this is
    how an operator points a named opencode provider at ANTHROPIC_BASE_URL /
    ANTHROPIC_AUTH_TOKEN / an API key without baking it into the machine."""
    provider = OpenCodeProvider(env={"ANTHROPIC_BASE_URL": "https://example.test"})
    assert provider.env() == {"ANTHROPIC_BASE_URL": "https://example.test"}


# ── parse_log ─────────────────────────────────────────────────────────────────


def test_opencode_parse_log_missing_file(tmp_path: Path) -> None:
    """parse_log on a non-existent file returns a blank WorkerSummary."""
    summary = OpenCodeProvider().parse_log(tmp_path / "nonexistent.log")
    assert isinstance(summary, WorkerSummary)
    assert summary.num_turns == 0
    assert summary.total_cost_usd == 0.0
    assert summary.session_id is None


def test_opencode_parse_log_empty_file(tmp_path: Path) -> None:
    """parse_log on an empty file returns a blank WorkerSummary."""
    log = tmp_path / "worker.log"
    log.write_text("")
    summary = OpenCodeProvider().parse_log(log)
    assert isinstance(summary, WorkerSummary)
    assert summary.num_turns == 0


def test_opencode_parse_log_never_raises_on_garbage(tmp_path: Path) -> None:
    """parse_log never raises regardless of log content."""
    log = tmp_path / "garbage.log"
    log.write_text("this is not json\n{broken\n\x00\xff\n")
    # Must not raise — any exception here is a contract violation.
    summary = OpenCodeProvider().parse_log(log)
    assert isinstance(summary, WorkerSummary)


def test_opencode_parse_log_never_raises_on_mixed_lines(tmp_path: Path) -> None:
    """parse_log silently skips non-JSON lines (e.g. '# agent=...' header)."""
    log = tmp_path / "mixed.log"
    log.write_text(
        "# agent=precision repo=myrepo issue=#42 argv=opencode run ...\n"
        '{"type":"tool_use","sessionID":"s1",'
        '"part":{"type":"tool","tool":"glob","state":{"status":"completed"}}}\n'
        "plain text output from opencode\n"
        '{"type":"step_finish","sessionID":"s1",'
        '"part":{"type":"step-finish","reason":"stop",'
        '"tokens":{"input":1,"output":1,"cache":{"write":0,"read":0}},"cost":0}}\n'
    )
    summary = OpenCodeProvider().parse_log(log, tail_bytes=0)
    assert summary.session_id == "s1"
    assert summary.num_turns == 1
    assert summary.stop_reason == "stop"


def test_opencode_parse_log_session_id_from_any_event() -> None:
    """sessionID is captured off the FIRST event seen, regardless of type —
    confirmed real opencode has no dedicated session.start/init event."""
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as f:
        f.write(json.dumps({
            "type": "step_start", "sessionID": "oc-123",
            "part": {"type": "step-start"},
        }) + "\n")
        name = f.name
    try:
        summary = OpenCodeProvider().parse_log(name, tail_bytes=0)
        assert summary.session_id == "oc-123"
        # Known, named gap: no event carries a model identifier — never
        # invent one.
        assert summary.model_used is None
    finally:
        os.unlink(name)


def test_opencode_parse_log_step_finish_sums_tokens_and_cost_across_events() -> None:
    """Confirmed (OPENCODE_VERIFICATION.md): there is no cumulative
    session-total field anywhere in the stream — cost/tokens must be SUMMED
    across every step_finish event, not read off a single one."""
    import tempfile
    lines = [
        json.dumps({
            "type": "step_finish", "sessionID": "oc-456",
            "part": {"type": "step-finish", "reason": "tool-calls",
                     "tokens": {"input": 100, "output": 20,
                                "cache": {"write": 5, "read": 50}},
                     "cost": 0.001},
        }),
        json.dumps({
            "type": "step_finish", "sessionID": "oc-456",
            "part": {"type": "step-finish", "reason": "stop",
                     "tokens": {"input": 30, "output": 10,
                                "cache": {"write": 0, "read": 15}},
                     "cost": 0.0002},
        }),
    ]
    with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as f:
        f.write("\n".join(lines) + "\n")
        name = f.name
    try:
        summary = OpenCodeProvider().parse_log(name, tail_bytes=0)
        assert summary.num_turns == 2
        assert summary.input_tokens == 130
        assert summary.output_tokens == 30
        assert summary.cache_creation_tokens == 5
        assert summary.cache_read_tokens == 65
        assert abs(summary.total_cost_usd - 0.0012) < 1e-9
        # Last step_finish's reason wins.
        assert summary.stop_reason == "stop"
    finally:
        os.unlink(name)


def test_opencode_parse_log_tool_use_bash_and_edit_tracked(tmp_path: Path) -> None:
    """tool_use events populate tools_used/last_tool, and bash/edit calls
    populate bash_commands/files_edited from their real input shape."""
    log = tmp_path / "tools.log"
    lines = [
        json.dumps({
            "type": "tool_use", "sessionID": "s1",
            "part": {"type": "tool", "tool": "bash",
                     "state": {"status": "completed",
                               "input": {"command": "git status"}}},
        }),
        json.dumps({
            "type": "tool_use", "sessionID": "s1",
            "part": {"type": "tool", "tool": "edit",
                     "state": {"status": "completed",
                               "input": {"filePath": "/repo/foo.py"}}},
        }),
    ]
    log.write_text("\n".join(lines) + "\n")
    summary = OpenCodeProvider().parse_log(log, tail_bytes=0)
    assert summary.tools_used == ["bash", "edit"]
    assert summary.last_tool == "edit"
    assert summary.bash_commands == ["git status"]
    assert summary.files_edited == ["/repo/foo.py"]


def test_opencode_parse_log_permission_denial_recorded(tmp_path: Path) -> None:
    """A tool_use with state.status=='error' whose message mentions
    'permission' (the confirmed permission-denial shape, which quotes the
    matching rules verbatim) is recorded in permission_denials."""
    log = tmp_path / "denied.log"
    denial_msg = (
        "The user has specified a rule which prevents you from using this "
        'specific tool call. Here are some of the relevant rules '
        '[{"permission":"*","action":"allow","pattern":"*"}]'
    )
    log.write_text(json.dumps({
        "type": "tool_use", "sessionID": "s1",
        "part": {"type": "tool", "tool": "bash",
                 "state": {"status": "error",
                           "input": {"command": "gh issue list"},
                           "error": denial_msg}},
    }) + "\n")
    summary = OpenCodeProvider().parse_log(log, tail_bytes=0)
    assert summary.permission_denials == [denial_msg]


def test_opencode_parse_log_text_event_result_text_last_wins(tmp_path: Path) -> None:
    """Confirmed: the assistant's final answer is the LAST text event
    before the terminal step_finish — each text event overwrites
    result_text so the final value is the true last one."""
    log = tmp_path / "text.log"
    lines = [
        json.dumps({"type": "text", "sessionID": "s1",
                    "part": {"type": "text", "text": "thinking out loud"}}),
        json.dumps({"type": "text", "sessionID": "s1",
                    "part": {"type": "text", "text": "final answer"}}),
    ]
    log.write_text("\n".join(lines) + "\n")
    summary = OpenCodeProvider().parse_log(log, tail_bytes=0)
    assert summary.result_text == "final answer"


def test_opencode_parse_log_error_event_sets_is_error(tmp_path: Path) -> None:
    """Confirmed run-level failure shape: top-level error event, no more
    events follow. Sets is_error/stop_reason/terminal_reason/result_text."""
    log = tmp_path / "error.log"
    log.write_text(json.dumps({
        "type": "error", "sessionID": "s1",
        "error": {"name": "UnknownError",
                  "data": {"message": '"Streaming response failed: [503] boom"'}},
    }) + "\n")
    summary = OpenCodeProvider().parse_log(log, tail_bytes=0)
    assert summary.is_error is True
    assert summary.stop_reason == "error"
    assert summary.terminal_reason == "UnknownError"
    # The literal captured message includes its own embedded quote chars —
    # preserved verbatim, not stripped.
    assert summary.result_text == '"Streaming response failed: [503] boom"'


def test_opencode_parse_log_unknown_events_ignored(tmp_path: Path) -> None:
    """parse_log silently ignores unrecognised event types, but still
    captures sessionID off them (sessionID capture doesn't depend on the
    event type being one this parser understands)."""
    log = tmp_path / "unknown.log"
    lines = [
        json.dumps({"type": "some.future.event", "sessionID": "s99", "data": "ignored"}),
        json.dumps({"type": "another.unknown", "x": 42}),
        json.dumps({
            "type": "step_finish", "sessionID": "s99",
            "part": {"type": "step-finish", "reason": "stop",
                     "tokens": {"input": 1, "output": 1,
                                "cache": {"write": 0, "read": 0}},
                     "cost": 0},
        }),
    ]
    log.write_text("\n".join(lines) + "\n")
    summary = OpenCodeProvider().parse_log(log, tail_bytes=0)
    # Unknown events are silently skipped for extraction purposes, but the
    # session id off the FIRST (unknown-typed) event still lands.
    assert summary.session_id == "s99"
    assert summary.num_turns == 1
    assert summary.stop_reason == "stop"


def test_opencode_parse_log_tail_bytes(tmp_path: Path) -> None:
    """parse_log with tail_bytes>0 reads only the end of the file — and,
    because a real opencode event carries sessionID on EVERY line (not just
    a synthetic first one), the session id captured is whichever event
    survives the tail cut, not necessarily the run's first line."""
    log = tmp_path / "big.log"
    lines = []
    # An event early in the file, under a DIFFERENT sessionID, that
    # tail_bytes will cut off entirely.
    lines.append(json.dumps({
        "type": "step_start", "sessionID": "early-session",
        "part": {"type": "step-start"},
    }))
    # Padding so the tail read genuinely misses the line above.
    for i in range(200):
        lines.append(json.dumps({
            "type": "text", "sessionID": "early-session",
            "part": {"type": "text", "text": "x" * 50},
        }))
    # A real step_finish near the end, in the tail, under a different
    # (later/resumed) session id.
    lines.append(json.dumps({
        "type": "step_finish", "sessionID": "tail-session",
        "part": {"type": "step-finish", "reason": "stop",
                 "tokens": {"input": 10, "output": 20,
                            "cache": {"write": 0, "read": 0}},
                 "cost": 0},
    }))
    log.write_text("\n".join(lines) + "\n")
    # 250 bytes is enough to land inside the LAST padding line (discarded as
    # the partial leading line) and still capture the full trailing
    # step_finish line — any padding line surviving intact would leak
    # "early-session" back in, since session_id capture takes the first one
    # seen in read order.
    summary = OpenCodeProvider().parse_log(log, tail_bytes=250)
    # The tail-read summary picks up the trailing step_finish.
    assert summary.num_turns == 1
    assert summary.stop_reason == "stop"
    assert summary.session_id == "tail-session"


# ── parse_log against the REAL #1703 fixtures ──────────────────────────────────


def test_opencode_parse_log_real_success_fixture() -> None:
    """#1704 FIX PINNED: parse_log() now correctly extracts real data from
    the verbatim successful capture (see docs/OPENCODE_VERIFICATION.md).
    Expected values were computed directly from the fixture's step_finish
    events (4 of them: 8083+103+99+107 input, 48+55+121+19 output,
    0+8064+8192+8320 cache-read tokens, 0 cost — a free-tier model)."""
    fixture = Path(__file__).parent / "fixtures" / "opencode_run_sample.jsonl"
    assert fixture.exists(), "opencode_run_sample.jsonl fixture is missing"
    summary = OpenCodeProvider().parse_log(fixture, tail_bytes=0)
    assert isinstance(summary, WorkerSummary)
    assert summary.session_id == "ses_036b4a104ffeIOILOMFtWVIoOb"
    # Known, named gap: no event in the real schema carries a model
    # identifier — never invent one.
    assert summary.model_used is None
    assert summary.num_turns == 4
    assert summary.stop_reason == "stop"
    assert summary.is_error is False
    assert summary.tools_used == ["glob", "read", "edit"]
    assert summary.last_tool == "edit"
    assert summary.files_edited == ["/tmp/oc-throwaway/math_utils.py"]
    assert summary.bash_commands == []
    assert summary.input_tokens == 8392
    assert summary.output_tokens == 243
    assert summary.cache_read_tokens == 24576
    assert summary.cache_creation_tokens == 0
    # Free-tier model (opencode/big-pickle) — correctly summed to 0.0, not a
    # parsing gap (see OPENCODE_VERIFICATION.md "Token usage and cost").
    assert summary.total_cost_usd == 0.0


def test_opencode_parse_log_real_failure_fixture() -> None:
    """#1704: parse_log() correctly extracts the real failing-run capture —
    one glob tool call, one intermediate step_finish, then a top-level
    error event (a real 503 from the model's request queue) with no
    terminal step_finish at all."""
    fixture = Path(__file__).parent / "fixtures" / "opencode_run_failure_sample.jsonl"
    assert fixture.exists(), "opencode_run_failure_sample.jsonl fixture is missing"
    summary = OpenCodeProvider().parse_log(fixture, tail_bytes=0)
    assert isinstance(summary, WorkerSummary)
    assert summary.session_id == "ses_036b53a0cffeKyzq3jsYZhULzj"
    assert summary.is_error is True
    assert summary.terminal_reason == "UnknownError"
    assert summary.stop_reason == "error"
    assert "Streaming response failed" in summary.result_text
    assert "503" in summary.result_text
    assert summary.num_turns == 1  # one step_finish before the error
    assert summary.tools_used == ["glob"]
    assert summary.input_tokens == 6258
    assert summary.output_tokens == 56
    assert summary.cache_read_tokens == 1792
    assert summary.total_cost_usd == 0.0
    # The success marker must never match a run that never terminated
    # cleanly.
    assert OpenCodeProvider().result_marker() not in fixture.read_text()


def test_opencode_parse_log_real_success_fixture_truncated_tail_read() -> None:
    """A small tail_bytes read against the REAL success fixture must not
    raise, must discard the leading partial line, and must still recover
    the trailing step_finish/stop event and its session id."""
    fixture = Path(__file__).parent / "fixtures" / "opencode_run_sample.jsonl"
    # 600 bytes lands inside the second-to-last line (346+1 + 424+1 = 772
    # bytes for the last two lines combined) — the leading partial line is
    # discarded, but the full final step_finish line survives intact.
    summary = OpenCodeProvider().parse_log(fixture, tail_bytes=600)
    assert isinstance(summary, WorkerSummary)
    assert summary.stop_reason == "stop"
    assert summary.session_id == "ses_036b4a104ffeIOILOMFtWVIoOb"


def test_opencode_parse_log_real_failure_fixture_truncated_tail_read() -> None:
    """Same truncated-tail-read guarantee against the REAL failure fixture:
    must not raise and must still recover the terminal error event."""
    fixture = Path(__file__).parent / "fixtures" / "opencode_run_failure_sample.jsonl"
    # 300 bytes lands inside the second-to-last line (303+1 + 204+1 = 509
    # bytes for the last two lines combined) — the partial line 4 is
    # discarded, but the full error line (line 5) survives intact.
    summary = OpenCodeProvider().parse_log(fixture, tail_bytes=300)
    assert isinstance(summary, WorkerSummary)
    assert summary.is_error is True
    assert summary.terminal_reason == "UnknownError"


# ── Registry: build_provider with opencode type ───────────────────────────────


def test_build_provider_opencode_type() -> None:
    """build_provider with type='opencode' returns an OpenCodeProvider."""
    defn = ProviderDef(type="opencode")
    provider = build_provider("myoc", defn, None)
    assert isinstance(provider, OpenCodeProvider)


def test_build_provider_opencode_with_binary() -> None:
    """build_provider passes the binary override to OpenCodeProvider."""
    defn = ProviderDef(type="opencode", binary="/usr/local/bin/opencode")
    provider = build_provider("oc", defn, None)
    assert isinstance(provider, OpenCodeProvider)
    spec = _make_spec(type="work", briefing="hi")
    argv = provider.build_command(spec)
    assert argv[0] == "/usr/local/bin/opencode"


def test_build_provider_opencode_with_attach_url() -> None:
    """build_provider threads attach_url from ProviderDef into OpenCodeProvider."""
    defn = ProviderDef(type="opencode", attach_url="http://localhost:4242")
    provider = build_provider("oc", defn, None)
    assert isinstance(provider, OpenCodeProvider)
    spec = _make_spec(type="work", briefing="hi")
    argv = provider.build_command(spec)
    assert "--attach" in argv
    idx = argv.index("--attach")
    assert argv[idx + 1] == "http://localhost:4242"


def test_build_provider_opencode_threads_model_env_extra_args() -> None:
    """#1706: build_provider threads model / env / extra_args into
    OpenCodeProvider the same way as the claude backends."""
    defn = ProviderDef(
        type="opencode",
        model="zhipuai/glm-4.6",
        env={"OPENCODE_API_KEY": "secret"},
        extra_args=["--verbose"],
    )
    provider = build_provider("oc", defn, None)
    assert isinstance(provider, OpenCodeProvider)
    assert provider.env() == {"OPENCODE_API_KEY": "secret"}

    spec = _make_spec(type="work", briefing="hi", model=None)
    argv = provider.build_command(spec)
    idx = argv.index("--model")
    assert argv[idx + 1] == "zhipuai/glm-4.6"
    assert "--verbose" in argv
    # extra_args precede the trailing positional briefing.
    assert argv[-1] == "hi"
    assert argv[argv.index("--verbose") + 1] == "hi"


def test_build_provider_unknown_type_still_raises() -> None:
    """Existing unknown-type error path is still intact after opencode addition."""
    defn = ProviderDef(type="not-a-real-backend")
    with pytest.raises(ValueError, match="not-a-real-backend"):
        build_provider("x", defn, None)


def test_build_provider_error_message_lists_opencode() -> None:
    """The ValueError message for an unknown type now lists 'opencode'."""
    defn = ProviderDef(type="mystery-backend")
    with pytest.raises(ValueError, match="opencode"):
        build_provider("x", defn, None)


# ── oneshot_command: ClaudeProvider ──────────────────────────────────────────


def test_claude_oneshot_command_default_json_format() -> None:
    """Default call returns [..., '--output-format', 'json'] for brain use."""
    cmd = ClaudeProvider().oneshot_command(system_prompt="sys")
    assert cmd[0] == "claude"
    assert "-p" in cmd
    assert "--system-prompt" in cmd
    idx = cmd.index("--system-prompt")
    assert cmd[idx + 1] == "sys"
    assert "--output-format" in cmd
    oi = cmd.index("--output-format")
    assert cmd[oi + 1] == "json"


def test_claude_oneshot_command_no_output_format() -> None:
    """output_format=None omits --output-format (dashboard streaming path)."""
    cmd = ClaudeProvider().oneshot_command(system_prompt="sys", output_format=None)
    assert "--output-format" not in cmd


def test_claude_oneshot_command_custom_output_format() -> None:
    """Custom output_format value is forwarded verbatim."""
    cmd = ClaudeProvider().oneshot_command(system_prompt="sp", output_format="text")
    assert "--output-format" in cmd
    oi = cmd.index("--output-format")
    assert cmd[oi + 1] == "text"


def test_claude_oneshot_command_no_stream_flags() -> None:
    """oneshot_command must NOT include stream-json worker flags."""
    cmd = ClaudeProvider().oneshot_command(system_prompt="sp")
    assert "--input-format" not in cmd
    assert "--verbose" not in cmd
    assert "--allowedTools" not in cmd
    assert "--permission-mode" not in cmd


def test_claude_oneshot_command_custom_binary() -> None:
    """ClaudeProvider(binary='my-claude') is reflected in oneshot_command."""
    cmd = ClaudeProvider(binary="my-claude").oneshot_command(system_prompt="sp")
    assert cmd[0] == "my-claude"


def test_claude_oneshot_command_returns_list_of_strings() -> None:
    """oneshot_command always returns list[str]."""
    cmd = ClaudeProvider().oneshot_command(system_prompt="sp")
    assert isinstance(cmd, list)
    for item in cmd:
        assert isinstance(item, str)


# ── oneshot_command: OpenCodeProvider ────────────────────────────────────────


def test_opencode_oneshot_command_returns_run_subcommand() -> None:
    """OpenCode oneshot uses 'run' subcommand (best-effort headless mode)."""
    cmd = OpenCodeProvider().oneshot_command(system_prompt="sp")
    assert cmd[0] == DEFAULT_OPENCODE_BINARY
    assert cmd[1] == "run"


def test_opencode_oneshot_command_ignores_system_prompt() -> None:
    """system_prompt is silently dropped — no CLI flag accepts ad hoc
    system-prompt text (only --agent NAME selecting a pre-configured
    agent, and oneshot_command has no spec context to name one from)."""
    cmd = OpenCodeProvider().oneshot_command(system_prompt="My system prompt")
    assert "--system-prompt" not in cmd
    assert "My system prompt" not in cmd
    assert "--agent" not in cmd


def test_opencode_oneshot_command_default_json_includes_format_flag() -> None:
    """#1704: output_format='json' (the default) now maps to --format json —
    the only usable structured-output flag confirmed to exist."""
    cmd = OpenCodeProvider().oneshot_command(system_prompt="sp")
    assert "--format" in cmd
    idx = cmd.index("--format")
    assert cmd[idx + 1] == "json"


def test_opencode_oneshot_command_none_output_format_omits_format_flag() -> None:
    """output_format=None omits --format entirely, falling back to
    opencode's human-transcript default — the dashboard-streaming case."""
    cmd = OpenCodeProvider().oneshot_command(system_prompt="sp", output_format=None)
    assert "--format" not in cmd


def test_opencode_oneshot_command_custom_output_format_forwarded_verbatim() -> None:
    """A non-'json' output_format string is forwarded as-is (mirrors
    ClaudeProvider.oneshot_command's behaviour for --output-format)."""
    cmd = OpenCodeProvider().oneshot_command(system_prompt="sp", output_format="text")
    idx = cmd.index("--format")
    assert cmd[idx + 1] == "text"


def test_opencode_oneshot_command_no_output_format_still_differs_from_claude_shape() -> None:
    """The 'no --output-format' shape is opencode's OWN flag name, never
    claude's --output-format spelling."""
    cmd_json = OpenCodeProvider().oneshot_command(system_prompt="sp", output_format="json")
    cmd_none = OpenCodeProvider().oneshot_command(system_prompt="sp", output_format=None)
    assert "--output-format" not in cmd_json
    assert "--output-format" not in cmd_none
    assert cmd_json != cmd_none  # #1704: these now differ (--format json vs. omitted)


def test_opencode_oneshot_command_always_includes_auto() -> None:
    """#1704: --auto is always appended, for the same headless-safety
    reason as build_command."""
    cmd_json = OpenCodeProvider().oneshot_command(system_prompt="sp", output_format="json")
    cmd_none = OpenCodeProvider().oneshot_command(system_prompt="sp", output_format=None)
    assert "--auto" in cmd_json
    assert "--auto" in cmd_none


def test_opencode_oneshot_command_custom_binary() -> None:
    """OpenCodeProvider(binary=...) is reflected in oneshot_command."""
    cmd = OpenCodeProvider(binary="/opt/oc").oneshot_command(system_prompt="sp")
    assert cmd[0] == "/opt/oc"


def test_opencode_oneshot_command_returns_list_of_strings() -> None:
    """oneshot_command always returns list[str]."""
    cmd = OpenCodeProvider().oneshot_command(system_prompt="sp")
    assert isinstance(cmd, list)
    for item in cmd:
        assert isinstance(item, str)


# ── resolve_default_provider ──────────────────────────────────────────────────


def test_resolve_default_provider_default_returns_claude_provider() -> None:
    """Default ProvidersConfig resolves to ClaudeProvider (no guard raised)."""
    cfg = ProvidersConfig()
    provider = resolve_default_provider(cfg)
    assert isinstance(provider, ClaudeProvider)


def test_resolve_default_provider_explicit_claude_returns_claude_provider() -> None:
    """An explicit claude definition resolves to ClaudeProvider."""
    cfg = ProvidersConfig(
        default="my-claude",
        definitions={"my-claude": ProviderDef(type="claude", binary="claude2")},
    )
    provider = resolve_default_provider(cfg)
    assert isinstance(provider, ClaudeProvider)
    # Binary override is threaded through.
    cmd = provider.oneshot_command(system_prompt="sp")
    assert cmd[0] == "claude2"


def test_resolve_default_provider_unknown_name_falls_back_to_claude() -> None:
    """When the default name is not in definitions, falls back to ClaudeProvider."""
    cfg = ProvidersConfig(default="missing", definitions={})
    provider = resolve_default_provider(cfg)
    assert isinstance(provider, ClaudeProvider)


def test_resolve_default_provider_raises_for_human_attended_only() -> None:
    """Raises ValueError when the resolved provider reports human_attended_only=True.

    This is the STRUCTURAL gate that prevents brain planning and dashboard
    assistant calls from routing through ClaudePtyProvider (subscription-billed
    interactive Claude) — Anthropic ToS §3.7 forbids unattended use.
    """
    from coord.providers.claude_pty import ClaudePtyProvider

    cfg = ProvidersConfig(
        default="my-pty",
        definitions={"my-pty": ProviderDef(type="claude-pty")},
    )
    with pytest.raises(ValueError, match="human_attended_only=True"):
        resolve_default_provider(cfg)


def test_resolve_default_provider_raises_names_the_provider() -> None:
    """The ValueError names the provider so the operator knows which one to reconfigure."""
    cfg = ProvidersConfig(
        default="named-pty",
        definitions={"named-pty": ProviderDef(type="claude-pty")},
    )
    with pytest.raises(ValueError, match="named-pty"):
        resolve_default_provider(cfg)


def test_resolve_default_provider_with_models_cfg() -> None:
    """resolve_default_provider accepts an optional models_cfg without error."""
    cfg = ProvidersConfig()
    models = ModelsConfig()
    provider = resolve_default_provider(cfg, models)
    assert isinstance(provider, ClaudeProvider)


def test_resolve_default_provider_opencode_allowed() -> None:
    """OpenCodeProvider (human_attended_only=False) does not trigger the guard."""
    cfg = ProvidersConfig(
        default="my-oc",
        definitions={"my-oc": ProviderDef(type="opencode")},
    )
    provider = resolve_default_provider(cfg)
    assert isinstance(provider, OpenCodeProvider)
