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


# ── OpenCodeProvider ──────────────────────────────────────────────────────────
#
# IMPORTANT: opencode is not installed on the build machine.  All tests below
# exercise the provider in isolation (no subprocess execution).  The assumed
# command structure, result marker, and NDJSON parse logic are tested against
# the documented schema in tests/fixtures/opencode_run_sample.jsonl.
# ─────────────────────────────────────────────────────────────────────────────


# ── capabilities ──────────────────────────────────────────────────────────────


def test_opencode_capabilities_declared_values() -> None:
    """OpenCodeProvider.capabilities() returns the documented conservative values."""
    caps = OpenCodeProvider().capabilities()
    assert isinstance(caps, Capabilities)
    assert caps.resume is True          # --session flag enables resume (assumed)
    assert caps.inject is False         # no mid-session injection path
    assert caps.cost_reporting is False # format unverified — conservative False
    assert caps.true_system_prompt is False  # no --system-prompt equivalent yet
    assert caps.enforces_deny_list is False  # SAFETY: coord deny-list not enforced
    assert caps.billing_mode == "byo_key"   # uses operator's own API keys
    assert caps.human_attended_only is False  # headless run mode is automatable


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


def test_opencode_result_marker_in_fixture() -> None:
    """result_marker() appears in the last line of the sample fixture."""
    fixtures_dir = Path(__file__).parent / "fixtures"
    fixture = fixtures_dir / "opencode_run_sample.jsonl"
    assert fixture.exists(), "opencode_run_sample.jsonl fixture is missing"
    lines = [ln for ln in fixture.read_text().splitlines() if ln.strip()]
    # The result marker should appear in the fixture (completion event present).
    marker = OpenCodeProvider().result_marker()
    assert any(marker in line for line in lines), (
        f"result_marker {marker!r} not found in fixture — "
        f"update the fixture or result_marker() to agree"
    )


def test_opencode_result_marker_is_string() -> None:
    """result_marker() returns a non-empty string."""
    marker = OpenCodeProvider().result_marker()
    assert isinstance(marker, str)
    assert len(marker) > 0


# ── env ───────────────────────────────────────────────────────────────────────


def test_opencode_env_empty() -> None:
    """env() returns an empty dict for OpenCodeProvider."""
    assert OpenCodeProvider().env() == {}


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
        '{"type":"session.start","session_id":"s1","model":"claude-sonnet"}\n'
        "plain text output from opencode\n"
        '{"type":"session.complete","session_id":"s1","num_turns":2}\n'
    )
    summary = OpenCodeProvider().parse_log(log, tail_bytes=0)
    assert summary.session_id == "s1"
    assert summary.num_turns == 2


def test_opencode_parse_log_session_start() -> None:
    """parse_log extracts session_id and model from session.start event."""
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as f:
        f.write('{"type":"session.start","session_id":"oc-123","model":"claude-haiku"}\n')
        name = f.name
    try:
        summary = OpenCodeProvider().parse_log(name, tail_bytes=0)
        assert summary.session_id == "oc-123"
        assert summary.model_used == "claude-haiku"
    finally:
        os.unlink(name)


def test_opencode_parse_log_session_complete_with_usage() -> None:
    """parse_log extracts num_turns and cost from session.complete event."""
    import tempfile
    line = json.dumps({
        "type": "session.complete",
        "session_id": "oc-456",
        "num_turns": 5,
        "usage": {
            "input_tokens": 1000,
            "output_tokens": 500,
            "cost_usd": 0.0072,
        },
    })
    with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as f:
        f.write(line + "\n")
        name = f.name
    try:
        summary = OpenCodeProvider().parse_log(name, tail_bytes=0)
        assert summary.num_turns == 5
        assert summary.input_tokens == 1000
        assert summary.output_tokens == 500
        assert abs(summary.total_cost_usd - 0.0072) < 1e-9
    finally:
        os.unlink(name)


def test_opencode_parse_log_fixture(tmp_path: Path) -> None:
    """parse_log correctly parses the sample fixture."""
    fixture = Path(__file__).parent / "fixtures" / "opencode_run_sample.jsonl"
    assert fixture.exists(), "opencode_run_sample.jsonl fixture is missing"
    summary = OpenCodeProvider().parse_log(fixture, tail_bytes=0)
    assert isinstance(summary, WorkerSummary)
    # Fixture has a session.start event with session_id and model.
    assert summary.session_id == "oc-sess-abc123"
    assert summary.model_used == "claude-sonnet-4-5"
    # Fixture has a session.complete event with num_turns=3.
    assert summary.num_turns == 3
    # Fixture has cost_usd=0.0092 in the usage block.
    assert abs(summary.total_cost_usd - 0.0092) < 1e-9


def test_opencode_parse_log_unknown_events_ignored(tmp_path: Path) -> None:
    """parse_log silently ignores unknown event types."""
    log = tmp_path / "unknown.log"
    lines = [
        json.dumps({"type": "some.future.event", "data": "ignored"}),
        json.dumps({"type": "another.unknown", "x": 42}),
        json.dumps({"type": "session.start", "session_id": "s99", "model": "gpt-4o"}),
    ]
    log.write_text("\n".join(lines) + "\n")
    summary = OpenCodeProvider().parse_log(log, tail_bytes=0)
    # Unknown events are silently skipped; known events still parse.
    assert summary.session_id == "s99"
    assert summary.model_used == "gpt-4o"


def test_opencode_parse_log_tail_bytes(tmp_path: Path) -> None:
    """parse_log with tail_bytes>0 reads only the end of the file."""
    log = tmp_path / "big.log"
    lines = []
    # Write a 'session.start' early in the file that tail would miss.
    lines.append(json.dumps({"type": "session.start", "session_id": "early", "model": "m1"}))
    # Pad with enough lines that the tail won't reach the start.
    for i in range(200):
        lines.append(json.dumps({"type": "message.partial", "i": i, "text": "x" * 50}))
    # Write a session.complete near the end (will be in the tail).
    lines.append(json.dumps({"type": "session.complete", "session_id": "tail-id", "num_turns": 7}))
    log.write_text("\n".join(lines) + "\n")
    summary = OpenCodeProvider().parse_log(log, tail_bytes=512)
    # The tail-read summary picks up the session.complete event.
    assert summary.num_turns == 7


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
    """system_prompt is silently dropped — OpenCode has no --system-prompt."""
    cmd = OpenCodeProvider().oneshot_command(system_prompt="My system prompt")
    assert "--system-prompt" not in cmd
    assert "My system prompt" not in cmd


def test_opencode_oneshot_command_ignores_output_format() -> None:
    """output_format is silently ignored — OpenCode has no --output-format."""
    cmd_json = OpenCodeProvider().oneshot_command(system_prompt="sp", output_format="json")
    cmd_none = OpenCodeProvider().oneshot_command(system_prompt="sp", output_format=None)
    assert "--output-format" not in cmd_json
    assert cmd_json == cmd_none


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
