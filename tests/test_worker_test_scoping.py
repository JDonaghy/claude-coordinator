"""Regression tests for #2169 — workers were told to run the project's
*whole* test command before declaring done. On this repo (and others with a
slow suite) that routinely exceeds Claude Code's 600-second hard Bash
ceiling: the call is killed with no output, and workers improvised
increasingly baroque workarounds (chunking the suite, backgrounding it and
blocking on a poll loop that hits the identical 600s wall) that burned the
majority of a work leg's wall clock for zero signal the Test stage and CI
don't already produce independently.

Covers:
* the "run the project's test command" phrasing must NOT reappear verbatim
  (the exact bug being fixed) — a regression guard;
* the prompt instead tells the worker to scope its run to the files/suites
  covering its diff;
* the oracle loop's acceptance carve-out survives — `coord acceptance run
  --issue N` must still be run repeatedly, not scoped down or skipped, or
  the oracle loop's whole convergence design (docs/ORACLE_LOOP.md) breaks;
* the sanctioned bounded-poll pattern (`Monitor` / `TaskOutput`) is named in
  the prompt, and `Monitor` is actually reachable — added to the worker's
  `--allowedTools` so the pattern isn't dead advice (#2158's fix leg had it
  denied and fell back to a blocking `until` loop that hit the same wall).
"""

from __future__ import annotations

import pytest

from coord.agent import (
    WORKER_SYSTEM_PROMPT,
    AssignmentSpec,
    default_worker_command,
)
from coord.providers.claude import ClaudeProvider


def _work_spec(**overrides) -> AssignmentSpec:
    base = dict(
        repo_name="api", repo_path="/tmp/api", issue_number=2169,
        issue_title="scope worker tests", briefing="b", branch="main",
        type="work",
    )
    base.update(overrides)
    return AssignmentSpec(**base)


# ── regression guard: the exact bug must not reappear ────────────────────────


def test_worker_system_prompt_does_not_tell_workers_to_run_whole_suite() -> None:
    """The load-bearing bug: this literal instruction sent workers spelunking
    through 600s timeouts trying to run a full suite that was never their
    job — the Test stage and CI both re-run it against the pushed SHA."""
    assert (
        "Run the project's test command" not in WORKER_SYSTEM_PROMPT
    ), "the pre-#2169 whole-suite instruction has reappeared in WORKER_SYSTEM_PROMPT"


# ── the worker is told to scope its run to the diff ──────────────────────────


def test_worker_system_prompt_instructs_scoping_to_the_diff() -> None:
    p = WORKER_SYSTEM_PROMPT
    assert "Before declaring done" in p
    flat = " ".join(p.split())
    assert "Run only the tests that cover your diff" in flat
    assert "never the project's whole test suite" in flat
    # Names the actual mechanism: the Test stage + CI already do the full run.
    assert "Test stage and CI both re-run the FULL suite" in flat
    assert "#2169" in flat


def test_worker_system_prompt_names_the_600s_ceiling() -> None:
    """Explaining *why* (not just *what*) is what stops a worker from
    reinventing the same workaround under a different name."""
    assert "600-second" in WORKER_SYSTEM_PROMPT or "600s" in WORKER_SYSTEM_PROMPT


def test_worker_system_prompt_forbids_the_observed_workarounds() -> None:
    """#2158: the worker split `ls tests/*.py` into thirds and separately
    tried backgrounding + a blocking `until ! pgrep` poll. Both must be
    named as the anti-pattern, not left for the next worker to reinvent."""
    flat = " ".join(WORKER_SYSTEM_PROMPT.split())
    assert "chunk-and-loop workaround" in flat or "splitting it into chunks" in flat


# ── oracle loop carve-out must survive ────────────────────────────────────────


def test_worker_system_prompt_carves_out_oracle_acceptance_runs() -> None:
    """docs/ORACLE_LOOP.md: the sealed acceptance slice run repeatedly, warm,
    in-session IS the convergence design — a scoping instruction that
    silently swept this up would disable the oracle loop's inner loop."""
    flat = " ".join(WORKER_SYSTEM_PROMPT.split())
    assert "coord acceptance run --issue N" in flat
    assert "oracle-loop acceptance round" in flat
    assert "keep running" in flat
    assert "does not apply to it" in flat


# ── the bounded-poll pattern is named, and Monitor is actually usable ────────


def test_worker_system_prompt_names_bounded_poll_pattern_not_blocking_loop() -> None:
    p = WORKER_SYSTEM_PROMPT
    flat = " ".join(p.split())
    assert "Monitor" in flat
    assert "TaskOutput" in flat
    # The exact failure mode from #2158's fix leg — must be named as wrong.
    assert "until ! pgrep" in flat or "blocks past the ceiling" in flat
    # #1394 invariant must still hold: never end the turn to wait passively.
    assert "ONE-SHOT" in p
    assert "run_in_background" in p
    assert "no next turn" in p.lower()


def test_default_worker_command_grants_monitor_for_work_type() -> None:
    """Advice to use Monitor is dead advice if the tool is denied — #2158's
    fix leg reached for it via ToolSearch and got `permission_denied`."""
    argv = default_worker_command(_work_spec())
    allowed = argv[argv.index("--allowedTools") + 1]
    assert "Monitor" in allowed.split(",")
    # Existing capabilities must not have been dropped in the process.
    for tool in ("Read", "Edit", "Write", "Bash"):
        assert tool in allowed.split(",")


@pytest.mark.parametrize("spec_type", ["review", "fix", "smoke", "conflict-fix"])
def test_default_worker_command_other_write_types_also_get_monitor(spec_type: str) -> None:
    """`review`/`fix`/`smoke`/`conflict-fix` all fall through the same `else`
    branch as `work` — confirm the grant isn't accidentally work-only."""
    argv = default_worker_command(_work_spec(type=spec_type))
    allowed = argv[argv.index("--allowedTools") + 1]
    assert "Monitor" in allowed.split(",")


def test_mock_author_type_does_not_gain_monitor() -> None:
    """Scope check: the Monitor grant is deliberately added to the generic
    worker branch only, not copy-pasted onto every Edit-capable type."""
    argv = default_worker_command(_work_spec(type="mock-author"))
    allowed = argv[argv.index("--allowedTools") + 1]
    assert allowed == "Read,Edit,Write,Bash"


def test_plan_type_unaffected_by_monitor_grant() -> None:
    argv = default_worker_command(_work_spec(type="plan"))
    allowed = argv[argv.index("--allowedTools") + 1]
    assert allowed == "Read,Bash"


# ── ClaudeProvider parity (coord/providers/claude.py is a documented direct
#    transcription of default_worker_command; #2169's Monitor grant must be
#    mirrored there or the two providers silently diverge) ──────────────────


def test_claude_provider_parity_includes_monitor_grant() -> None:
    spec = _work_spec()
    legacy = default_worker_command(spec)
    provider_result = ClaudeProvider().build_command(spec)
    legacy_allowed = legacy[legacy.index("--allowedTools") + 1]
    provider_allowed = provider_result[provider_result.index("--allowedTools") + 1]
    assert provider_allowed == legacy_allowed
    assert "Monitor" in provider_allowed.split(",")
