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
from coord.providers import build_provider, resolve_provider_name
from coord.providers.base import Capabilities, Provider, WorkerSummary
from coord.providers.claude import ClaudeProvider


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
