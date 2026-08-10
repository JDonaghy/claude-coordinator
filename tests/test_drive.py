"""Tests for coord/drive.py — the `coord drive` state machine (#1392).

Every bug ``scripts/drive-issue.sh`` shipped was in decision logic, not in
subprocess orchestration:

- an ``advisory`` work row fell through to a bare ``sleep; continue`` — a silent
  240-minute spin (PR #1386)
- merge verification used ``merge-base --is-ancestor``, which is **always**
  wrong under ``coord merge --method rebase``
- unbounded merge retries until the deadline
- interactive work parked on a review that was never coming (#555)

In bash those were untestable. Here they are :func:`coord.drive.decide` /
:func:`coord.drive.preflight` calls, and this file is the "behaviour that must
survive the port" list from #1392, one test per item.

The other invariant under test is the CLI boundary: every board mutation must
go out as a ``coord`` subcommand argv, never a direct internal call. Calling
``record_test_verdict()`` instead of ``coord test --passed`` silently
reintroduces #1384 (the CLI mirrors ``test_state`` → the legacy ``smoke_test``
field; the function alone does not), which makes ``coord fix`` refuse to
dispatch. So the assertions below are on ``action.command``.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from coord.config import Config, ProviderDef, ProvidersConfig, UsageGateConfig
from coord.drive import (
    EXIT_DEAD_END,
    EXIT_DEADLINE,
    EXIT_DISPATCH_REFUSED,
    EXIT_ESCALATED,
    EXIT_OK,
    EXIT_TERMINAL_FAILURE,
    EXIT_USAGE,
    RUN,
    WAIT,
    Action,
    DriveCounters,
    DriveError,
    DriveOptions,
    Driver,
    FileLock,
    GitHubAcceptanceGateChecker,
    GitMergeVerifier,
    LockBusy,
    OracleDecision,
    coord_argv,
    decide,
    preflight,
    resolve_oracle_decision,
)
from coord.drive_state import IssueState
from coord.models import Machine, Repo
from coord.usage_limits import PlanLimits


REPO = "claude-coordinator"
ISSUE = 1392


def make_config() -> Config:
    return Config(
        repos=[Repo(name=REPO, github="john/claude-coordinator", test_command="pytest -q")],
        machines=[Machine(name="precision", host="precision", repos=[REPO])],
    )


def state(**kw) -> IssueState:
    base = dict(repo=REPO, issue=ISSUE, repo_github="john/claude-coordinator")
    base.update(kw)
    return IssueState(**base)


class FakeVerifier:
    """Stands in for git/gh so decision tests never touch the network."""

    def __init__(self, *, has_commits: bool = True, merged: bool = True) -> None:
        self._has_commits = has_commits
        self._merged = merged
        self.commits_calls = 0
        self.merged_calls = 0

    def branch_has_commits(self, s: IssueState) -> bool:
        self.commits_calls += 1
        return self._has_commits

    def verify_merged(self, s: IssueState) -> bool:
        self.merged_calls += 1
        return self._merged


def step(s: IssueState, opts: DriveOptions | None = None, **kw) -> Action:
    """One decide() call with sensible defaults."""
    verifier = kw.pop("verifier", None) or FakeVerifier()
    counters = kw.pop("counters", None) or DriveCounters()
    # A default gate_checker whose resolve_for_path() is a no-op (None, "no
    # --for-path needed") — every pre-#1453-review test drives an unrouted
    # (or no) acceptance config, so this preserves their assertions
    # byte-for-byte; oracle tests that care override it explicitly.
    gate_checker = kw.pop("gate_checker", None) or FakeGateChecker()
    return decide(
        s,
        opts or DriveOptions(machine="precision"),
        counters,
        verifier,
        machine=kw.pop("machine", "precision"),
        oracle=kw.pop("oracle", None),
        gate_checker=gate_checker,
    )


# ═══════════════════════════════════════════════════════════════════════════
# preflight
# ═══════════════════════════════════════════════════════════════════════════


def test_preflight_resolves_the_least_loaded_machine_when_none_given():
    pre = preflight(state(picked_machine="dellserver"), DriveOptions())
    assert pre.machine == "dellserver"


def test_preflight_prefers_an_explicit_machine():
    pre = preflight(
        state(picked_machine="dellserver"), DriveOptions(machine="precision")
    )
    assert pre.machine == "precision"


def test_preflight_refuses_when_no_machine_hosts_the_repo():
    with pytest.raises(DriveError) as exc:
        preflight(state(), DriveOptions())
    assert "no unpaused machine hosts" in str(exc.value)
    assert exc.value.exit_code == EXIT_USAGE


def test_preflight_refuses_distinctly_when_hosts_exist_but_none_are_capable():
    """#1906: a fleet that DOES host the repo but has no machine advertising
    the resolved provider must not collapse into the generic 'no unpaused
    machine hosts' message — the two are different problems (add a machine
    vs. add a capability) with different fixes."""
    with pytest.raises(DriveError) as exc:
        preflight(
            state(picked_machine_no_capable=True, picked_machine_provider="opencode"),
            DriveOptions(),
        )
    message = str(exc.value)
    assert "no unpaused machine advertises" in message
    assert "opencode" in message
    assert "no unpaused machine hosts" not in message
    assert exc.value.exit_code == EXIT_USAGE


def test_preflight_explicit_machine_wins_even_when_selection_found_no_capable_host():
    """Explicit beats inferred (#1906 design point): an operator naming a
    machine is never silently re-routed or refused by THIS gate — #1711's
    dispatch-time guard is the one that gets to refuse an explicit but
    incapable machine, not the picker."""
    pre = preflight(
        state(picked_machine_no_capable=True, picked_machine_provider="opencode"),
        DriveOptions(machine="precision"),
    )
    assert pre.machine == "precision"


def test_preflight_warns_when_the_auto_loop_is_off():
    pre = preflight(state(picked_machine="m", auto_loop=False), DriveOptions())
    assert any("auto_loop is OFF" in w for w in pre.warnings)


# ── #555: interactive work is refused at PREFLIGHT, not at the review gate ──


def test_interactive_work_is_refused_at_preflight_not_after_the_test_suite():
    """#555 + the #1357 drive: a run must not burn ~6min of tests then park.

    `dispatch_pending_reviews` carries `provider_name != "claude-pty"`, so for
    interactive work the review is not late — it is never coming.
    """
    s = state(
        picked_machine="m",
        work_aid="w1",
        work_provider="claude-pty",
        work_status="done",
        work_branch="b",
    )
    with pytest.raises(DriveError) as exc:
        preflight(s, DriveOptions())
    assert "INTERACTIVELY" in str(exc.value)
    assert "--force-review" in str(exc.value)
    assert exc.value.exit_code == EXIT_USAGE


def test_force_review_turns_the_preflight_refusal_into_a_warning():
    s = state(
        picked_machine="m",
        work_aid="w1",
        work_provider="claude-pty",
        work_status="done",
    )
    pre = preflight(s, DriveOptions(force_review=True))
    assert any("--force-review set" in w for w in pre.warnings)


def test_preflight_allows_interactive_work_that_already_has_a_review():
    s = state(
        picked_machine="m",
        work_aid="w1",
        work_provider="claude-pty",
        review_aid="r1",
    )
    assert preflight(s, DriveOptions()).machine == "m"


def test_preflight_allows_headless_work():
    s = state(picked_machine="m", work_aid="w1", work_provider="claude-code")
    assert preflight(s, DriveOptions()).machine == "m"


# ── #1466: the Max-plan 5h/weekly usage gate ────────────────────────────────
#
# preflight() stays pure — it never probes itself. A black-box test drives
# it with a stubbed PlanLimits exactly like MergeVerifier/AcceptanceGate-
# Checker are stubbed elsewhere in this file.


def _config_with_gate(**gate_kw) -> Config:
    cfg = make_config()
    cfg.usage_gate = UsageGateConfig(**gate_kw)
    return cfg


def test_preflight_with_no_config_skips_the_gate_entirely():
    """Every pre-#1466 call site (and most of this file's own tests) passes
    no config at all — must behave exactly as before, gate or no gate."""
    pre = preflight(
        state(picked_machine="m"), DriveOptions(),
        usage_limits=PlanLimits(status="ok", session_pct=99.0, week_pct=99.0),
    )
    assert pre.machine == "m"
    assert pre.warnings == ()


def test_preflight_gate_disabled_mode_ignores_a_maxed_out_probe():
    cfg = _config_with_gate(mode="disabled", session_threshold_pct=1.0)
    pre = preflight(
        state(picked_machine="m"), DriveOptions(), cfg,
        usage_limits=PlanLimits(status="ok", session_pct=99.0),
    )
    assert pre.warnings == ()


def test_preflight_gate_below_threshold_proceeds_with_no_warning():
    cfg = _config_with_gate(mode="warn", session_threshold_pct=85.0, week_threshold_pct=90.0)
    pre = preflight(
        state(picked_machine="m"), DriveOptions(), cfg,
        usage_limits=PlanLimits(status="ok", session_pct=10.0, week_pct=10.0),
    )
    assert pre.warnings == ()


def test_preflight_gate_above_threshold_warns_by_default_and_still_proceeds():
    cfg = _config_with_gate(mode="warn", session_threshold_pct=85.0)
    pre = preflight(
        state(picked_machine="m"), DriveOptions(), cfg,
        usage_limits=PlanLimits(status="ok", session_pct=90.0, session_resets_at="8pm (UTC)"),
    )
    assert pre.machine == "m"
    assert any("90" in w and "8pm (UTC)" in w for w in pre.warnings)


def test_preflight_gate_block_mode_refuses_above_threshold():
    cfg = _config_with_gate(mode="block", week_threshold_pct=90.0)
    with pytest.raises(DriveError) as exc:
        preflight(
            state(picked_machine="m"), DriveOptions(), cfg,
            usage_limits=PlanLimits(status="ok", week_pct=95.0, week_resets_at="Aug 1"),
        )
    assert "week" in str(exc.value)
    assert "Aug 1" in str(exc.value)
    assert exc.value.exit_code == EXIT_USAGE


def test_preflight_gate_unavailable_probe_proceeds_even_in_block_mode():
    """A probe we can't trust must never block (or warn) a dispatch — see
    coord.usage_limits.evaluate_usage_gate's docstring."""
    cfg = _config_with_gate(mode="block", session_threshold_pct=1.0, week_threshold_pct=1.0)
    pre = preflight(
        state(picked_machine="m"), DriveOptions(), cfg,
        usage_limits=PlanLimits(status="unknown", error="claude -p /usage timed out"),
    )
    assert pre.machine == "m"
    assert pre.warnings == ()


def test_preflight_gate_no_usage_limits_passed_is_treated_as_unavailable():
    """config given but usage_limits omitted (e.g. a caller that skipped the
    probe) — never fabricate an "ok" reading."""
    cfg = _config_with_gate(mode="block", session_threshold_pct=1.0)
    pre = preflight(state(picked_machine="m"), DriveOptions(), cfg)
    assert pre.machine == "m"
    assert pre.warnings == ()


# ── Driver._loop wiring: the probe is consulted end-to-end ──────────────────


def test_driver_loop_surfaces_a_usage_gate_warning(driver_factory, capsys):
    cfg = _config_with_gate(mode="warn", session_threshold_pct=50.0)
    driver = driver_factory(
        [board(status="merged")],
        config=cfg,
        usage_prober=lambda: PlanLimits(status="ok", session_pct=95.0, session_resets_at="8pm"),
    )
    assert driver.run() == EXIT_OK
    assert "Max-plan usage near limit" in capsys.readouterr().err


def test_driver_loop_block_mode_refuses_before_dispatching(driver_factory, capsys):
    cfg = _config_with_gate(mode="block", session_threshold_pct=50.0)
    driver = driver_factory(
        [board(status="merged")],
        config=cfg,
        usage_prober=lambda: PlanLimits(status="ok", session_pct=95.0),
    )
    # DriveError propagates out of run() unhandled (the CLI boundary in
    # coord/commands/drive.py converts it to an exit code) — same contract
    # every other preflight refusal in this file already uses.
    with pytest.raises(DriveError) as exc:
        driver.run()
    assert "Max-plan usage near limit" in str(exc.value)
    assert exc.value.exit_code == EXIT_USAGE
    assert not driver.recorded  # never got as far as running a `coord` subcommand


def test_driver_loop_disabled_gate_never_calls_the_prober(driver_factory):
    cfg = _config_with_gate(mode="disabled")
    calls = []

    def prober():
        calls.append(1)
        return PlanLimits(status="ok", session_pct=99.0)

    driver = driver_factory([board(status="merged")], config=cfg, usage_prober=prober)
    assert driver.run() == EXIT_OK
    assert calls == []


# ═══════════════════════════════════════════════════════════════════════════
# no work yet: plan / dispatch
# ═══════════════════════════════════════════════════════════════════════════


def test_no_work_row_dispatches_work_through_the_cli():
    action = step(state())
    assert action.kind == RUN
    assert action.command == (
        "assign", "precision", REPO, "1392",
        "--driven-by", f"drive:{REPO}#1392",
    )


def test_dispatch_work_passes_model_and_briefing_file():
    opts = DriveOptions(machine="precision", model="opus", briefing_file="/tmp/b.md")
    action = step(state(), opts)
    assert action.command == (
        "assign", "precision", REPO, "1392",
        "--driven-by", f"drive:{REPO}#1392",
        "--model", "opus",
        "--briefing-file", "/tmp/b.md",
    )


def test_plan_flag_dispatches_a_plan_only_assignment_first():
    action = step(state(), DriveOptions(machine="precision", do_plan=True))
    assert action.command == (
        "assign", "--plan-only", "precision", REPO, "1392",
        "--driven-by", f"drive:{REPO}#1392",
    )


def test_a_done_plan_is_auto_approved():
    action = step(
        state(plan_aid="p1", plan_status="done"),
        DriveOptions(machine="precision", do_plan=True),
    )
    assert action.command == ("approve-plan", "p1")


def test_a_failed_plan_is_terminal():
    action = step(
        state(plan_aid="p1", plan_status="failed"),
        DriveOptions(machine="precision", do_plan=True),
    )
    assert action.is_exit
    assert action.exit_code == EXIT_TERMINAL_FAILURE
    assert "plan assignment p1 failed" in action.message


def test_a_running_plan_just_waits():
    action = step(
        state(plan_aid="p1", plan_status="running"),
        DriveOptions(machine="precision", do_plan=True),
    )
    assert action.kind == WAIT


def test_anything_active_just_waits():
    action = step(state(active_count=1, active_types=("smoke",)))
    assert action.kind == WAIT


# ═══════════════════════════════════════════════════════════════════════════
# #1453: the oracle-loop JIT slice gate
# ═══════════════════════════════════════════════════════════════════════════


def make_config_with_acceptance_driver() -> Config:
    from coord.config import AcceptanceConfig, AcceptanceDriverConfig

    return Config(
        repos=[Repo(name=REPO, github="john/claude-coordinator", test_command="pytest -q")],
        machines=[Machine(name="precision", host="precision", repos=[REPO])],
        acceptance=AcceptanceConfig(
            drivers={REPO: AcceptanceDriverConfig(kind="cli-pytest", run="pytest")}
        ),
    )


class FakeGateChecker:
    def __init__(
        self,
        *,
        exists: bool = True,
        for_path: str | None = None,
        for_path_error: Exception | None = None,
    ) -> None:
        self._exists = exists
        self._for_path = for_path
        self._for_path_error = for_path_error
        self.calls: list[tuple[str, int]] = []
        self.for_path_calls: list[tuple[str, int]] = []

    def contract_exists(self, repo_name: str, milestone_number: int) -> bool:
        self.calls.append((repo_name, milestone_number))
        return self._exists

    def resolve_for_path(self, repo_name: str, milestone_number: int) -> str | None:
        self.for_path_calls.append((repo_name, milestone_number))
        if self._for_path_error is not None:
            raise self._for_path_error
        return self._for_path


def oracle_state(**kw) -> IssueState:
    base = dict(milestone_number=38, milestone_tracking_issue=1120)
    base.update(kw)
    return state(**base)


# ── resolve_oracle_decision ──────────────────────────────────────────────────


def test_resolve_oracle_decision_is_inactive_without_no_acceptance_flag_by_default():
    """No acceptance driver configured at all -> normal drive, no GitHub call."""
    checker = FakeGateChecker()
    decision = resolve_oracle_decision(
        oracle_state(), DriveOptions(), make_config(), checker
    )
    assert decision.active is False
    assert "no acceptance.drivers entry" in decision.reason
    assert checker.calls == []


def test_resolve_oracle_decision_respects_no_acceptance_opt_out():
    checker = FakeGateChecker()
    decision = resolve_oracle_decision(
        oracle_state(),
        DriveOptions(no_acceptance=True),
        make_config_with_acceptance_driver(),
        checker,
    )
    assert decision.active is False
    assert "--no-acceptance" in decision.reason
    assert checker.calls == []


def test_resolve_oracle_decision_is_inactive_with_no_milestone():
    decision = resolve_oracle_decision(
        oracle_state(milestone_number=None),
        DriveOptions(),
        make_config_with_acceptance_driver(),
        FakeGateChecker(),
    )
    assert decision.active is False
    assert "no GitHub milestone" in decision.reason


def test_resolve_oracle_decision_is_inactive_with_no_tracking_issue():
    decision = resolve_oracle_decision(
        oracle_state(milestone_tracking_issue=None),
        DriveOptions(),
        make_config_with_acceptance_driver(),
        FakeGateChecker(),
    )
    assert decision.active is False
    assert "tracked milestone work order" in decision.reason


def test_resolve_oracle_decision_is_inactive_when_the_contract_is_not_merged_yet():
    checker = FakeGateChecker(exists=False)
    decision = resolve_oracle_decision(
        oracle_state(), DriveOptions(), make_config_with_acceptance_driver(), checker
    )
    assert decision.active is False
    assert "ms-38/contract.md" in decision.reason
    assert checker.calls == [(REPO, 38)]


def test_resolve_oracle_decision_is_active_when_everything_lines_up():
    checker = FakeGateChecker(exists=True)
    decision = resolve_oracle_decision(
        oracle_state(), DriveOptions(), make_config_with_acceptance_driver(), checker
    )
    assert decision.active is True
    assert decision.tracking_issue == 1120
    assert "ms-38" in decision.reason


def test_the_default_gate_checker_reuses_gate_a_status_not_a_reimplementation():
    """#1453: must not drift from tui's gate_a_contract_exists_for /
    coord.milestone_dispatch.gate_a_status — both keyed on
    coord.acceptance.gate_a_contract_path.
    """
    import inspect

    src = inspect.getsource(GitHubAcceptanceGateChecker.contract_exists)
    assert "gate_a_status" in src


def test_the_default_gate_checker_delegates_for_path_to_the_shared_helper():
    """#1453 review finding 1: GitHubAcceptanceGateChecker.resolve_for_path
    must call coord.acceptance.resolve_for_path (the ONE shared derivation)
    rather than re-deriving --for-path itself."""
    import inspect

    src = inspect.getsource(GitHubAcceptanceGateChecker.resolve_for_path)
    assert "resolve_for_path(" in src


def test_the_default_gate_checker_resolve_for_path_is_wired_end_to_end(monkeypatch):
    """Exercises the real resolve_for_path() call through the checker with a
    stubbed mock-lister, rather than trusting the source-scan above alone."""
    calls = []

    def fake_list_repo_dir(repo: str, path: str, branch: str = "develop") -> list[str]:
        calls.append((repo, path, branch))
        return ["plans-base.screen"]

    monkeypatch.setattr("coord.github_ops.list_repo_dir", fake_list_repo_dir)

    from coord.config import AcceptanceConfig, AcceptanceDriverConfig

    cfg = Config(
        repos=[Repo(name=REPO, github="john/claude-coordinator")],
        machines=[],
        acceptance=AcceptanceConfig(
            drivers={
                REPO: AcceptanceDriverConfig(routes=[
                    AcceptanceDriverConfig(match="coord/**", kind="cli-pytest", run="pytest"),
                    AcceptanceDriverConfig(match="tui/**", kind="tui-tuidriver", run="cargo test"),
                ])
            }
        ),
    )
    checker = GitHubAcceptanceGateChecker(config=cfg)
    assert checker.resolve_for_path(REPO, 38) == "tui/**"
    assert calls == [
        ("john/claude-coordinator", "tests/acceptance/ms-38/mocks", "main"),
    ]


def test_the_default_gate_checker_resolve_for_path_returns_none_for_unknown_repo():
    checker = GitHubAcceptanceGateChecker(config=make_config())
    assert checker.resolve_for_path("no-such-repo", 38) is None


REPO_ROOT = Path(__file__).resolve().parent.parent
TUI_PIPELINE_RS = REPO_ROOT / "tui" / "src" / "app" / "pipeline.rs"


def test_gate_a_contract_path_agrees_across_tui_python_dispatch_and_drive():
    """#1453's acceptance bar: "the gate matches the TUI's and Python's,
    with a test asserting the three implementations agree."

    Three independent call sites decide whether a milestone's Gate-A
    contract exists:

    - ``tui/src/app/pipeline.rs::gate_a_contract_exists_for`` — a local-fs
      check for the interactive JIT-author menu item (#1060).
    - ``coord.milestone_dispatch.gate_a_status`` — the #930 milestone-
      dispatch gate (GitHub-fetch based); also what #1453's
      ``GitHubAcceptanceGateChecker`` reuses (previous test).
    - ``coord.drive.resolve_oracle_decision`` (#1453, this issue) — the
      unattended driver's pre-work JIT-author gate.

    All three MUST derive the path from the ``tests/acceptance/ms-NN/
    contract.md`` convention — ``coord.acceptance.gate_a_contract_path`` on
    the Python side — rather than re-deriving their own. A drifted format is
    silent: the driver would wait forever for a contract that actually
    exists at a slightly different path.
    """
    from coord.acceptance import gate_a_contract_path

    path = gate_a_contract_path(42)
    assert path == "tests/acceptance/ms-42/contract.md"

    # Rust: gate_a_contract_exists_for builds the same path via four
    # `.join()` calls — extract the literal segments and rebuild the
    # equivalent path to prove no drift.
    rust_src = TUI_PIPELINE_RS.read_text()
    fn_match = re.search(
        r"fn gate_a_contract_exists_for.*?\n    \}\n", rust_src, re.S
    )
    assert fn_match is not None, (
        f"gate_a_contract_exists_for not found in {TUI_PIPELINE_RS} — update "
        "this test's regex (it may have been renamed/moved), and re-verify "
        "it still agrees with gate_a_contract_path rather than silently "
        "leaving this test unable to catch a real drift"
    )
    fn_src = fn_match.group(0)
    join_calls = re.findall(r'\.join\(\s*(?:"([^"]+)"|format!\("([^"]+)", \w+\))\s*\)', fn_src)
    segments = [a or b for a, b in join_calls]
    assert segments == ["tests", "acceptance", "ms-{}", "contract.md"], (
        f"gate_a_contract_exists_for's path segments changed to {segments!r} "
        "— update coord.acceptance.gate_a_contract_path (and this test) to "
        "match, in the SAME change"
    )
    rust_path = "/".join(segments).replace("ms-{}", "ms-42")
    assert rust_path == path

    # Python: coord.milestone_dispatch.gate_a_status must call
    # gate_a_contract_path too, not a private re-derivation.
    import inspect

    from coord import milestone_dispatch

    assert "gate_a_contract_path(milestone_number)" in inspect.getsource(
        milestone_dispatch.gate_a_status
    )

    # coord.drive: resolve_oracle_decision must do the same.
    from coord import drive

    assert "gate_a_contract_path(state.milestone_number)" in inspect.getsource(
        drive.resolve_oracle_decision
    )


# ── decide()/_dispatch_work_stage with an active oracle decision ────────────


def test_oracle_inactive_dispatches_work_directly_as_before():
    """oracle=None (the default) is byte-for-byte the pre-#1453 behaviour."""
    action = step(state())
    assert action.command == (
        "assign", "precision", REPO, "1392",
        "--driven-by", f"drive:{REPO}#1392",
    )


def test_oracle_active_authors_the_slice_before_dispatching_work():
    oracle = OracleDecision(True, "ORACLE DRIVE", tracking_issue=1120)
    action = step(state(), oracle=oracle)
    assert action.kind == RUN
    assert action.command == (
        "acceptance", "author", REPO, "1120", "--issue", "1392",
    )


def test_oracle_active_waits_while_the_slice_is_still_authoring():
    oracle = OracleDecision(True, "ORACLE DRIVE", tracking_issue=1120)
    action = step(
        state(acceptance_author_aid="ta1", acceptance_author_status="running"),
        oracle=oracle,
    )
    assert action.kind == WAIT


def test_oracle_active_dispatches_work_once_the_slice_has_merged():
    oracle = OracleDecision(True, "ORACLE DRIVE", tracking_issue=1120)
    action = step(
        state(acceptance_author_aid="ta1", acceptance_author_status="merged"),
        oracle=oracle,
    )
    assert action.kind == RUN
    assert action.command == (
        "assign", "precision", REPO, "1392",
        "--driven-by", f"drive:{REPO}#1392",
    )


def test_oracle_active_is_terminal_when_the_slice_authoring_fails():
    oracle = OracleDecision(True, "ORACLE DRIVE", tracking_issue=1120)
    action = step(
        state(acceptance_author_aid="ta1", acceptance_author_status="failed"),
        oracle=oracle,
    )
    assert action.is_exit
    assert action.exit_code == EXIT_TERMINAL_FAILURE
    assert "acceptance author ta1 failed" in action.message
    assert "--no-acceptance" in action.message


def test_oracle_active_is_terminal_when_the_slice_authoring_is_cancelled():
    oracle = OracleDecision(True, "ORACLE DRIVE", tracking_issue=1120)
    action = step(
        state(acceptance_author_aid="ta1", acceptance_author_status="cancelled"),
        oracle=oracle,
    )
    assert action.is_exit
    assert action.exit_code == EXIT_TERMINAL_FAILURE
    assert "cancelled" in action.message


def test_oracle_active_still_honours_do_plan_after_the_slice_has_landed():
    oracle = OracleDecision(True, "ORACLE DRIVE", tracking_issue=1120)
    action = step(
        state(acceptance_author_aid="ta1", acceptance_author_status="merged"),
        DriveOptions(machine="precision", do_plan=True),
        oracle=oracle,
    )
    assert action.command == (
        "assign", "--plan-only", "precision", REPO, "1392",
        "--driven-by", f"drive:{REPO}#1392",
    )


# ── #1453 review finding 1: --for-path resolution for a routed repo ─────────


def test_oracle_active_appends_for_path_when_the_gate_checker_resolves_one():
    """A ROUTED repo's `coord acceptance author` hard-refuses with no
    --for-path (coord.test_author.dispatch_test_author) — the driver must
    resolve and pass it, not dispatch blind."""
    oracle = OracleDecision(True, "ORACLE DRIVE", tracking_issue=1120)
    checker = FakeGateChecker(for_path="tui/**")
    action = step(oracle_state(), oracle=oracle, gate_checker=checker)
    assert action.kind == RUN
    assert action.command == (
        "acceptance", "author", REPO, "1120", "--issue", "1392",
        "--for-path", "tui/**",
    )
    assert checker.for_path_calls == [(REPO, 38)]


def test_oracle_active_omits_for_path_for_an_unrouted_repo():
    """resolve_for_path() returning None means "no --for-path needed" (flat,
    unrouted driver config, or none at all) — command is unchanged from
    before #1453's review fix."""
    oracle = OracleDecision(True, "ORACLE DRIVE", tracking_issue=1120)
    action = step(oracle_state(), oracle=oracle, gate_checker=FakeGateChecker())
    assert action.command == (
        "acceptance", "author", REPO, "1120", "--issue", "1392",
    )


def test_oracle_active_dies_when_for_path_cannot_be_resolved():
    """An ambiguous/unresolvable routed config must report and stop — not
    dispatch a `coord acceptance author` that the CLI will reject anyway
    (coord.acceptance.ForPathResolutionError)."""
    from coord.acceptance import ForPathResolutionError

    oracle = OracleDecision(True, "ORACLE DRIVE", tracking_issue=1120)
    checker = FakeGateChecker(
        for_path_error=ForPathResolutionError("no route matched")
    )
    action = step(oracle_state(), oracle=oracle, gate_checker=checker)
    assert action.is_exit
    assert action.exit_code == EXIT_TERMINAL_FAILURE
    assert "no route matched" in action.message


# ── #1453 review finding 2: an ADVISORY JIT slice must not spin forever ─────


def test_oracle_active_advisory_with_no_commits_is_terminal():
    oracle = OracleDecision(True, "ORACLE DRIVE", tracking_issue=1120)
    verifier = FakeVerifier(has_commits=False)
    action = step(
        oracle_state(
            acceptance_author_aid="ta1",
            acceptance_author_status="advisory",
            acceptance_author_branch="",
        ),
        oracle=oracle,
        verifier=verifier,
    )
    assert action.is_exit
    assert action.exit_code == EXIT_TERMINAL_FAILURE
    assert "no commits" in action.message


def test_oracle_active_advisory_with_commits_requires_accept_advisory():
    """The #1357 false-positive shape — real commits, downgraded to
    advisory anyway — must not be treated as "still landing"; it needs the
    same --accept-advisory opt-in the main work row uses."""
    oracle = OracleDecision(True, "ORACLE DRIVE", tracking_issue=1120)
    verifier = FakeVerifier(has_commits=True)
    action = step(
        oracle_state(
            acceptance_author_aid="ta1",
            acceptance_author_status="advisory",
            acceptance_author_branch="issue-1453-slice",
        ),
        oracle=oracle,
        verifier=verifier,
    )
    assert action.is_exit
    assert action.exit_code == EXIT_TERMINAL_FAILURE
    assert "--accept-advisory" in action.message


def test_oracle_active_advisory_with_commits_and_accept_advisory_waits():
    oracle = OracleDecision(True, "ORACLE DRIVE", tracking_issue=1120)
    verifier = FakeVerifier(has_commits=True)
    action = step(
        oracle_state(
            acceptance_author_aid="ta1",
            acceptance_author_status="advisory",
            acceptance_author_branch="issue-1453-slice",
        ),
        DriveOptions(machine="precision", accept_advisory=True),
        oracle=oracle,
        verifier=verifier,
    )
    assert action.kind == WAIT
    assert any("--accept-advisory" in w for w in action.warnings)


def test_oracle_active_advisory_never_falls_through_to_a_bare_wait_label():
    """Regression guard for the #1453-review bug itself: an advisory JIT
    slice must never produce the generic "authoring/merging in progress"
    wait label — that label is the silent-spin signature (#1386's class)."""
    oracle = OracleDecision(True, "ORACLE DRIVE", tracking_issue=1120)
    action = step(
        oracle_state(
            acceptance_author_aid="ta1",
            acceptance_author_status="advisory",
            acceptance_author_branch="",
        ),
        oracle=oracle,
        verifier=FakeVerifier(has_commits=False),
    )
    assert "authoring/merging in progress" not in (action.label or "")


# ── #1535: a DONE JIT slice must not spin to --deadline on zero commits ─────


def test_oracle_active_done_with_no_commits_is_terminal():
    """The advisory-path guard, applied to `done`: a terminal status whose
    branch has zero commits can never reach `merged` on its own — waiting
    burns the deadline with no diagnosis (#1526's defect, reborn here)."""
    oracle = OracleDecision(True, "ORACLE DRIVE", tracking_issue=1120)
    verifier = FakeVerifier(has_commits=False)
    action = step(
        oracle_state(
            acceptance_author_aid="ta1",
            acceptance_author_status="done",
            acceptance_author_branch="test-author-ms-38-slice-1124",
        ),
        oracle=oracle,
        verifier=verifier,
    )
    assert action.is_exit
    assert action.exit_code == EXIT_TERMINAL_FAILURE
    assert "test-author-ms-38-slice-1124" in action.message
    assert "no commits" in action.message
    assert "ta1" in action.message


def test_oracle_active_done_with_no_branch_is_terminal():
    oracle = OracleDecision(True, "ORACLE DRIVE", tracking_issue=1120)
    verifier = FakeVerifier(has_commits=False)
    action = step(
        oracle_state(
            acceptance_author_aid="ta1",
            acceptance_author_status="done",
            acceptance_author_branch="",
        ),
        oracle=oracle,
        verifier=verifier,
    )
    assert action.is_exit
    assert action.exit_code == EXIT_TERMINAL_FAILURE
    assert "no commits" in action.message


def test_oracle_active_done_with_commits_waits():
    """The common case is unaffected: a DONE slice whose branch actually
    carries commits keeps waiting for coord's own Test/Review/Merge loop to
    land it."""
    oracle = OracleDecision(True, "ORACLE DRIVE", tracking_issue=1120)
    verifier = FakeVerifier(has_commits=True)
    action = step(
        oracle_state(
            acceptance_author_aid="ta1",
            acceptance_author_status="done",
            acceptance_author_branch="test-author-ms-38-slice-1124",
        ),
        oracle=oracle,
        verifier=verifier,
    )
    assert action.kind == WAIT
    assert not action.is_exit
    assert verifier.commits_calls == 1


def test_oracle_active_advisory_behaviour_is_unchanged_by_the_done_fix():
    """Regression guard: the `done` probe must not leak into the `advisory`
    branch's own handling."""
    oracle = OracleDecision(True, "ORACLE DRIVE", tracking_issue=1120)
    verifier = FakeVerifier(has_commits=False)
    action = step(
        oracle_state(
            acceptance_author_aid="ta1",
            acceptance_author_status="advisory",
            acceptance_author_branch="",
        ),
        oracle=oracle,
        verifier=verifier,
    )
    assert action.is_exit
    assert action.exit_code == EXIT_TERMINAL_FAILURE
    assert "ADVISORY" in action.message


# ═══════════════════════════════════════════════════════════════════════════
# work-stage terminal states
# ═══════════════════════════════════════════════════════════════════════════


def test_failed_work_retries_through_the_cli_then_stops_at_the_cap():
    counters = DriveCounters()
    opts = DriveOptions(machine="precision", max_work_retries=1)
    s = state(work_aid="w1", work_status="failed", work_failure_reason="boom")

    first = step(s, opts, counters=counters)
    assert first.command == ("retry", "w1")
    assert counters.work_retries == 1

    second = step(s, opts, counters=counters)
    assert second.is_exit
    assert second.exit_code == EXIT_TERMINAL_FAILURE
    assert "boom" in second.message


def test_usage_limit_kill_waits_instead_of_retrying_or_dying():
    """#1461: a usage-limit kill must WAIT (not retry, not die/escalate) —
    retrying before the reset just burns the same exhausted budget and fails
    again for no diagnostic reason. Must not consume the retry budget."""
    counters = DriveCounters()
    opts = DriveOptions(machine="precision", max_work_retries=1)
    s = state(
        work_aid="w1", work_status="failed",
        work_failure_reason="usage limit — resets 8:30pm (America/Chicago)",
    )
    action = step(s, opts, counters=counters)
    assert action.kind == WAIT
    assert counters.work_retries == 0
    assert any("usage-limit" in w for w in action.warnings)
    assert "8:30pm (America/Chicago)" in action.warnings[0]

    # And it keeps waiting — never escalates into the retry-cap die either,
    # even across repeated polls.
    action2 = step(s, opts, counters=counters)
    assert action2.kind == WAIT
    assert counters.work_retries == 0


def test_usage_limit_kill_on_advisory_also_waits():
    """A kill has been observed landing ADVISORY (clean exit, 0 commits) just
    as often as FAILED — must be recognised regardless of which terminal
    status the agent's own reap chose."""
    action = step(
        state(
            work_aid="w1", work_status="advisory",
            work_failure_reason="usage limit — resets 8:30pm (America/Chicago)",
        ),
        verifier=FakeVerifier(has_commits=False),
    )
    assert action.kind == WAIT


def test_usage_limit_wait_surfaces_the_earliest_resume_time():
    """#1590 part 3/6: the `reset_at_raw` the detector has always parsed is now
    turned into an absolute earliest-resume instant and surfaced, so the
    operator knows when the node comes back rather than just that it's parked."""
    action = step(
        state(
            work_aid="w1", work_status="failed",
            work_failure_reason="usage limit — resets 8:30pm (America/Chicago)",
        ),
    )
    assert action.kind == WAIT
    joined = "\n".join(action.warnings)
    assert "environmental (usage limit)" in joined
    assert "earliest resume 20" in joined  # ISO-8601, 20:30 local to Chicago


def test_retry_cap_death_names_the_failure_class():
    """#1590 part 6: 'drive died 3x' used to send the next person looking at
    the work. The exhausted-retry message now states which class it was."""
    counters = DriveCounters()
    opts = DriveOptions(machine="precision", max_work_retries=0)

    work = step(
        state(work_aid="w1", work_status="failed", work_failure_reason="tests failed"),
        opts, counters=counters,
    )
    assert work.is_exit
    assert "cause: work failure" in work.message

    env = step(
        state(
            work_aid="w1", work_status="failed",
            work_failure_reason='API Error: 529 {"type":"overloaded_error"}',
        ),
        opts, counters=DriveCounters(),
    )
    assert env.is_exit
    assert "cause: environmental" in env.message
    assert "529" in env.message


def test_normal_advisory_failure_reason_is_not_mistaken_for_a_usage_limit():
    """A failure_reason that doesn't carry the exact stamped prefix must fall
    through to the ordinary retry-or-die path unchanged."""
    counters = DriveCounters()
    opts = DriveOptions(machine="precision", max_work_retries=1)
    s = state(
        work_aid="w1", work_status="failed",
        work_failure_reason="usage limit exceeded on some unrelated API call",
    )
    action = step(s, opts, counters=counters)
    assert action.command == ("retry", "w1")
    assert counters.work_retries == 1


def test_cancelled_work_is_terminal_and_says_how_to_re_dispatch():
    action = step(state(work_aid="w1", work_status="cancelled"))
    assert action.is_exit
    assert "--force" in action.message


def test_an_unknown_terminal_status_refuses_to_guess():
    """No terminal status may fall through to a bare wait (PR #1386)."""
    action = step(state(work_aid="w1", work_status="wat"))
    assert action.is_exit
    assert action.exit_code == EXIT_TERMINAL_FAILURE
    assert "refusing to guess" in action.message


def test_done_work_with_no_branch_is_terminal():
    action = step(state(work_aid="w1", work_status="done", work_branch=""))
    assert action.is_exit
    assert "no branch" in action.message


# ── advisory: distinguished by whether the branch actually carries commits ───


def test_advisory_with_no_commits_on_the_branch_is_terminal():
    verifier = FakeVerifier(has_commits=False)
    action = step(
        state(work_aid="w1", work_status="advisory", work_branch="b"),
        verifier=verifier,
    )
    assert action.is_exit
    assert "no commits on its branch" in action.message
    assert verifier.commits_calls == 1


def test_advisory_with_no_commits_is_terminal_even_with_accept_advisory():
    """#1606: `--accept-advisory` exists to unblock the #1357 false-positive
    (real commits, downgraded status) — it must NOT adopt a genuine
    zero-commit advisory as though it were completed work. The zero-commit
    check runs before the accept_advisory branch is ever consulted."""
    verifier = FakeVerifier(has_commits=False)
    action = step(
        state(work_aid="w1", work_status="advisory", work_branch="b"),
        DriveOptions(machine="precision", accept_advisory=True),
        verifier=verifier,
    )
    assert action.is_exit
    assert "no commits on its branch" in action.message
    assert verifier.commits_calls == 1


def test_advisory_with_commits_stops_and_names_1357_without_accept_advisory():
    action = step(
        state(work_aid="w1", work_status="advisory", work_branch="b"),
        verifier=FakeVerifier(has_commits=True),
    )
    assert action.is_exit
    assert "#1357" in action.message
    assert "--accept-advisory" in action.message


def test_advisory_with_commits_proceeds_under_accept_advisory():
    """PR #1386: this arm used to fall through to a bare sleep — a silent
    240-minute spin. It must reach the Test gate and warn while doing so."""
    action = step(
        state(work_aid="w1", work_status="advisory", work_branch="b", work_test_state=""),
        DriveOptions(machine="precision", accept_advisory=True),
        verifier=FakeVerifier(has_commits=True),
    )
    assert action.kind == WAIT  # waiting on coord to dispatch the Test stage
    assert any("--accept-advisory" in w for w in action.warnings)


def test_advisory_with_commits_and_a_passed_test_reaches_the_merge_stage():
    action = step(
        state(
            work_aid="w1",
            work_status="advisory",
            work_branch="b",
            work_test_state="passed",
            review_verdict="approve",
        ),
        DriveOptions(machine="precision", accept_advisory=True),
        verifier=FakeVerifier(has_commits=True),
    )
    assert action.command[:2] == ("merge", "--only")
    assert any("--accept-advisory" in w for w in action.warnings)


def test_advisory_with_no_branch_at_all_is_terminal():
    action = step(state(work_aid="w1", work_status="advisory", work_branch=""))
    assert action.is_exit
    assert "no commits on its branch" in action.message


# ═══════════════════════════════════════════════════════════════════════════
# the TEST gate
# ═══════════════════════════════════════════════════════════════════════════


def done_work(**kw) -> IssueState:
    base = dict(work_aid="w1", work_status="done", work_branch="issue-1392-x")
    base.update(kw)
    return state(**base)


def test_no_test_verdict_yet_waits_for_coord_to_dispatch_the_stage():
    """#1426: coord's own dispatch_pending_smoke runs the Test stage. Two
    drivers racing to dispatch the same thing is the #476/#477 incident."""
    action = step(done_work(work_test_state=""))
    assert action.kind == WAIT
    assert action.command == ()


def test_skip_test_records_the_verdict_through_the_test_cli():
    """The CLI, never record_test_verdict() — see #1384."""
    action = step(done_work(), DriveOptions(machine="precision", skip_test=True))
    assert action.command == (
        "test", "--skipped", "--reason", "coord drive --skip-test", "w1",
    )
    assert action.sleep_after == 5.0


def test_a_running_test_stage_just_waits():
    action = step(done_work(work_test_state="running"))
    assert action.kind == WAIT


def test_a_running_test_stage_with_no_smoke_child_still_just_waits():
    """A plain in-flight Test stage (smoke child still running, or none
    dispatched yet) is not the #1605 contradiction — must not be confused
    with a stranded one."""
    action = step(done_work(work_test_state="running", smoke_aid="s1", smoke_status="running"))
    assert action.kind == WAIT


def test_stuck_test_state_with_a_terminal_smoke_child_is_actionable_not_a_loop():
    """#1605: the Test-stage CHILD assignment (`type="smoke"`) already
    finished FAILED (a dead agent, a killed process group, a terminal API
    error — the #1598 incident's exact shape) but `test_state` was never
    resolved off it — stuck at `"running"` forever. Before this, `_decide_test`
    only ever looked at `work_test_state` and returned an unbounded `_wait()`
    here, which is exactly how #1598 polled a phantom Test stage for 2.5
    hours against three idle machines. This must terminate the drive loop
    with an actionable message instead."""
    action = step(done_work(
        work_test_state="running",
        smoke_aid="smoke-1605",
        smoke_status="failed",
        smoke_failure_reason="api_error: aborted_streaming",
    ))
    assert action.is_exit
    assert action.exit_code == EXIT_TERMINAL_FAILURE
    assert "smoke-1605" in action.message
    assert "api_error: aborted_streaming" in action.message
    assert "coord diagnose" in action.message


def test_stuck_test_state_with_a_cancelled_smoke_child_is_also_actionable():
    action = step(done_work(
        work_test_state="running", smoke_aid="smoke-2", smoke_status="cancelled",
    ))
    assert action.is_exit
    assert action.exit_code == EXIT_TERMINAL_FAILURE


def test_a_done_smoke_child_with_lagging_test_state_still_just_waits():
    """A fresh `done` smoke completion has an expected, bounded propagation
    lag before `coord notify` records its verdict on the parent — that is
    NOT the #1605 bug and must not trip the contradiction check."""
    action = step(done_work(
        work_test_state="running", smoke_aid="smoke-3", smoke_status="done",
    ))
    assert action.kind == WAIT


def test_a_failed_test_loops_through_coord_fix_on_the_same_branch():
    """`coord fix` gates on the legacy smoke_test field and dispatches with
    inherit_branch=True — the same branch, model escalated (#1445)."""
    counters = DriveCounters()
    action = step(done_work(work_test_state="failed"), counters=counters)
    assert action.command == ("fix", "w1")
    assert counters.fix_rounds == 1
    assert "smoke_test" in action.error_message  # the diagnosis if it won't dispatch
    assert action.on_error == "die"


def test_the_test_fix_loop_is_bounded_by_max_fix_rounds():
    counters = DriveCounters()
    opts = DriveOptions(machine="precision", max_fix_rounds=2)
    s = done_work(work_test_state="failed", work_test_reason="3 failed")

    assert step(s, opts, counters=counters).command == ("fix", "w1")
    assert step(s, opts, counters=counters).command == ("fix", "w1")
    exhausted = step(s, opts, counters=counters)
    assert exhausted.is_exit
    assert exhausted.exit_code == EXIT_TERMINAL_FAILURE
    assert "after 2 fix round(s)" in exhausted.message
    assert "3 failed" in exhausted.message


def test_max_fix_rounds_zero_never_dispatches_a_fix():
    action = step(
        done_work(work_test_state="failed"),
        DriveOptions(machine="precision", max_fix_rounds=0),
    )
    assert action.is_exit


def test_an_unexpected_test_state_warns_and_waits_rather_than_guessing():
    action = step(done_work(work_test_state="weird"))
    assert action.kind == WAIT
    assert any("weird" in w for w in action.warnings)


@pytest.mark.parametrize("verdict", ["passed", "skipped"])
def test_a_passed_or_skipped_test_falls_through_to_the_review_gate(verdict):
    action = step(done_work(work_test_state=verdict))
    assert action.kind == WAIT  # no review row yet
    assert action.command == ()


# ═══════════════════════════════════════════════════════════════════════════
# the REVIEW gate
# ═══════════════════════════════════════════════════════════════════════════


def work_tested(**kw) -> IssueState:
    return done_work(work_test_state="passed", **kw)


def test_no_review_row_yet_waits_for_coords_auto_dispatch():
    action = step(work_tested())
    assert action.kind == WAIT
    assert action.command == ()


def test_a_review_that_finished_with_no_verdict_is_terminal():
    action = step(work_tested(review_aid="r1", work_review_state="done"))
    assert action.is_exit
    assert "NO verdict" in action.message


# ═══════════════════════════════════════════════════════════════════════════
# the dead-end predicate (#2019)
#
# `coord drive` could not tell "still working" from "finished in a state I
# cannot act on". Both rendered as `no state change`, and the second looped
# forever. claude-coordinator#1956, 2026-08-08: 140 minutes of a live drive
# session, a held queue slot and a held per-repo capacity slot (#1972),
# producing nothing, with `active=0` printed on every single line.
#
# WHY the pre-existing "review finished with no verdict" die above did not
# catch it: it keys on `work_review_state` — the WORK row's projected
# `review_state` — while the incident's board line read `review=done/-`,
# which is `review_status`, the REVIEW row's own status. Advancing the work
# row's `review_state` is exactly what recording a verdict does, so on the
# one board shape where the verdict is missing, the field the die reads is
# guaranteed to be stale. Two readings of "the review is done"; only one was
# ever checked.
# ═══════════════════════════════════════════════════════════════════════════


def test_a_terminal_review_with_no_verdict_escalates_instead_of_looping():
    """#2019 acceptance: "a board state of work=done test=passed
    review=done/verdict=None drives to escalation, not to a `no state change`
    loop", and "exits non-zero within one poll rather than looping".

    This is the #1956 incident state, verbatim, in ONE decide() call.
    """
    action = step(
        work_tested(review_aid="c9b489b2333e", review_status="done", review_verdict="")
    )
    assert action.is_exit
    assert action.exit_code == EXIT_DEAD_END
    # Distinguishable from a crash (1) and from a #1844 guard refusal (5) by
    # the exit code alone — that code is the only thing `drive_queue`'s tick
    # can read once the process is gone.
    assert action.exit_code not in (EXIT_TERMINAL_FAILURE, EXIT_DISPATCH_REFUSED)


def test_the_dead_end_message_names_the_dead_end_and_the_recovery_command():
    """#2019 ask 3. `no state change in 140.558m` is not actionable; the
    documented `coord report-result` relay is. And the reason must not send an
    operator to CLOSED #812 for a headless review that ran to completion."""
    action = step(
        work_tested(review_aid="c9b489b2333e", review_status="done", review_verdict="")
    )
    assert "review_terminal_no_verdict" in action.message
    assert "coord report-result --assignment c9b489b2333e" in action.message
    assert "--verdict-source recovered" in action.message
    assert "#812" not in action.message
    assert "no state change" not in action.message


def test_the_dead_end_records_a_board_visible_escalation_through_the_cli():
    """Same contract every other board mutation in this module honours: the
    write goes out as a `coord` subcommand argv (run by `_loop`'s exit
    handling), never a direct internal call. Without it the reason dies with
    the tmux pane — which is exactly how the 2026-07-27/28 run produced three
    unexplained deaths (#1526)."""
    action = step(
        work_tested(review_aid="c9b489b2333e", review_status="done", review_verdict="")
    )
    assert action.command[:4] == ("escalate", "record", REPO, str(ISSUE))
    assert "--stage" in action.command
    assert action.command[action.command.index("--stage") + 1] == "review"
    proposed = action.command[action.command.index("--command") + 1]
    assert proposed.startswith("coord report-result --assignment c9b489b2333e")
    assert action.command[action.command.index("--assignment") + 1] == "c9b489b2333e"


def test_a_long_running_stage_never_dead_ends_however_long_it_runs():
    """#2019 acceptance: "a genuinely long-running work stage (active=1) does
    NOT escalate, however long it runs."

    "However long" is enforced structurally rather than by a threshold: the
    predicate takes no clock at all (ask 4 — elapsed time must NOT be the
    trigger), so this is byte-for-byte the same call on poll 1 and poll
    10,000. The state is otherwise the full #1956 dead-end shape, so
    `active_count` is carrying the whole decision.
    """
    s = work_tested(
        review_aid="c9b489b2333e",
        review_status="done",
        review_verdict="",
        active_count=1,
        active_types=("work",),
    )
    for _ in range(3):  # identical result, poll after poll after poll
        action = step(s)
        assert action.kind == WAIT
        assert action.exit_code == 0


def test_a_failed_review_worker_is_still_retried_not_dead_ended():
    """Blast-radius bar. #1584's bounded `coord review` re-dispatch is a move
    that genuinely can succeed; a dead end must never steal it."""
    action = step(
        work_tested(
            review_aid="r1", review_status="failed",
            review_failure_reason="529 Overloaded",
        ),
        DriveOptions(machine="precision", max_work_retries=1),
    )
    assert action.kind == RUN
    assert action.command == ("review", "w1")


def test_a_blocked_test_stage_escalates_instead_of_warning_every_poll():
    """#1672 stamps `test_state="blocked"` when no capability-matched machine
    could run the suite, then deliberately never re-probes. Before #2019 this
    fell through `_decide_test` to a bare WAIT carrying an "unexpected
    test_state" warning — a spin with a note attached."""
    action = step(
        done_work(
            work_test_state="blocked",
            work_test_reason="no machine advertises capability 'gtk'",
        )
    )
    assert action.is_exit
    assert action.exit_code == EXIT_DEAD_END
    assert "test_stage_blocked" in action.message
    assert f"coord diagnose {REPO} {ISSUE} --stage test --reset" in action.message


def test_a_dead_end_exit_code_reaches_the_drive_exited_audit_row(
    driver_factory, monkeypatch, coord_db,
):
    """The end-to-end seam, through `Driver.run()`'s audit boundary (#1499).

    `details.exit_code == EXIT_DEAD_END` is the ONE fact
    `coord/commands/drive_queue.py`'s `_fetch_exit_reasons` reads to block the
    queue entry without spending an attempt — everything else about the run
    is gone by the time the tick looks.
    """
    monkeypatch.setattr(
        "coord.drive.Driver._post_escalation_comment", lambda *a, **kw: None
    )
    payload = board(status="done", test_state="passed")
    payload["assignments"].append({
        "repo_name": REPO,
        "issue_number": ISSUE,
        "type": "review",
        "assignment_id": "c9b489b2333e",
        "review_of_assignment_id": "w1",
        "dispatched_at": 2.0,
        "status": "done",
        "review_verdict": None,
    })
    driver = driver_factory([payload])
    assert driver.run() == EXIT_DEAD_END

    rows = _drive_audit_rows(coord_db)
    assert [r["event_type"] for r in rows] == ["drive_started", "drive_exited"]
    details = json.loads(rows[1]["details_json"])
    assert details["exit_code"] == EXIT_DEAD_END
    assert "review_terminal_no_verdict" in rows[1]["summary"]
    # ...and the board-visible escalation went out as a `coord` argv, once.
    escalations = [c for c in driver.recorded if c[1:3] == ["escalate", "record"]]
    assert len(escalations) == 1


def test_a_review_worker_that_died_retries_through_the_cli_then_stops_at_the_cap():
    """#1584: the review WORKER itself failed (transient API error, network
    drop, ...) before ever producing a verdict — `review_status="failed"`
    with `review_verdict=""`. Before #1584's reconcile-side fix, this could
    not be told apart from "no review dispatched yet" and silently waited
    out the full 240-minute deadline. Mirrors the WORK failed-retry bounded
    loop, but re-dispatches via `coord review <work_aid>` (NOT `coord retry
    <review_aid>` — that command's `_reassign` hardcodes `type="work"` on
    every re-dispatch and would silently create a bogus work assignment
    instead of a review) up to `max_work_retries`, then dies with the reason.
    """
    counters = DriveCounters()
    opts = DriveOptions(machine="precision", max_work_retries=1)
    s = work_tested(
        review_aid="r1", review_status="failed",
        review_failure_reason="529 Overloaded",
    )

    first = step(s, opts, counters=counters)
    assert first.command == ("review", "w1")
    assert counters.review_retries == 1

    second = step(s, opts, counters=counters)
    assert second.is_exit
    assert second.exit_code == EXIT_TERMINAL_FAILURE
    assert "529 Overloaded" in second.message


def test_a_usage_limit_killed_review_waits_instead_of_retrying_or_dying():
    """#1461/#1584: a review worker killed by the account's usage limit must
    WAIT like the work-side case — retrying before the reset just burns the
    same exhausted budget again."""
    counters = DriveCounters()
    opts = DriveOptions(machine="precision", max_work_retries=1)
    s = work_tested(
        review_aid="r1", review_status="failed",
        review_failure_reason="usage limit — resets 8:30pm (America/Chicago)",
    )
    action = step(s, opts, counters=counters)
    assert action.kind == WAIT
    assert counters.review_retries == 0
    assert any("usage-limit" in w for w in action.warnings)


def requested_changes(**kw) -> IssueState:
    base = dict(
        review_aid="r1",
        review_verdict="request-changes",
        work_review_iter=1,
        max_review_iterations=5,
    )
    base.update(kw)
    return work_tested(**base)


def test_request_changes_dispatches_coord_fix_against_the_REVIEW_id():
    """#1692: this arm used to `_wait()` on a comment reading "the auto-loop
    dispatches the fix". That stopped being true when #1616 replaced the
    `coord notify` timer with the daemon drain — the drain deliberately
    excludes fix dispatch (#476/#477), `run_for_review_transition` never sees
    a transition the drain already consumed, and the #1478 stalled sweeper is
    off by default. The observed cost was a 50-minute park to the deadline
    with nothing dispatched (drive-batch 2026-08-02, #1630).

    The REVIEW id is the whole point: `coord fix <work_aid>` gates on the
    legacy `smoke_test == "fail"` field and would be refused here; `coord fix
    <review_aid>` is the #1622 door, built for exactly this and never wired up.
    """
    counters = DriveCounters()
    action = step(requested_changes(), counters=counters)
    assert action.kind == RUN
    assert action.command == ("fix", "r1")  # the REVIEW id, never work_aid
    assert counters.fix_rounds == 1
    assert action.on_error == "die"
    assert "--fix-of" in action.error_message  # the manual door, named


def test_the_review_fix_arm_shares_one_fix_budget_with_the_test_arm():
    """Not a parallel counter (#1692): a failing test and a request-changes
    review are two shapes of one loop, so a drive that bounces between them
    spends ONE budget. Two rounds of mixed kinds exhaust --max-fix-rounds 2."""
    counters = DriveCounters()
    opts = DriveOptions(machine="precision", max_fix_rounds=2)

    first = step(done_work(work_test_state="failed"), opts, counters=counters)
    assert first.command == ("fix", "w1")  # test arm, round 1

    second = step(requested_changes(), opts, counters=counters)
    assert second.command == ("fix", "r1")  # review arm, round 2
    assert counters.fix_rounds == 2

    # A THIRD round of either kind is over budget.
    exhausted = step(requested_changes(review_aid="r2"), opts, counters=counters)
    assert exhausted.is_exit
    assert exhausted.exit_code == EXIT_TERMINAL_FAILURE
    assert "after 2 fix round(s)" in exhausted.message
    assert "NOT exhausted" in exhausted.message  # says WHICH cap was hit

    still_exhausted = step(
        done_work(work_test_state="failed"), opts, counters=counters
    )
    assert still_exhausted.is_exit


def test_a_second_decide_on_an_unchanged_board_does_not_dispatch_a_second_fix():
    """THE guard against re-opening #476/#477 in a new dispatcher.

    `coord fix` returns as soon as the fix worker is dispatched, but the board
    this driver polls needs a beat to show the new row. Until it does, the
    state is byte-for-byte the one that triggered the dispatch — and a driver
    that re-fires on it puts a SECOND fix worker on the SAME branch. That is
    the #476/#477 incident shape (two uncoordinated dispatchers, conflicting
    branches, real money), and `max_fix_rounds` alone does not prevent it: it
    only decides how many duplicates get spawned before the drive gives up.

    Delete `counters.review_fix_dispatched_for` and this test must fail.
    """
    counters = DriveCounters()
    opts = DriveOptions(machine="precision", max_fix_rounds=3)
    s = requested_changes()  # one board snapshot, reused verbatim

    assert step(s, opts, counters=counters).command == ("fix", "r1")
    assert counters.fix_rounds == 1

    for _ in range(3):
        again = step(s, opts, counters=counters)
        assert again.kind == WAIT, "re-dispatched a duplicate fix worker"
        assert again.command == ()
        assert counters.fix_rounds == 1, "burned a fix round on a no-op"
        assert "already dispatched" in again.label


def test_the_next_review_round_is_a_new_row_so_the_latch_does_not_wedge():
    """The de-dup latch keys on the review's assignment id, and a fix round
    produces a new work row and therefore a new review row (`drive_state.
    project` keys the review on the current work id). So the latch clears
    itself — it must not turn the second genuine round into a permanent wait.
    """
    counters = DriveCounters()
    opts = DriveOptions(machine="precision", max_fix_rounds=3)

    assert step(requested_changes(), opts, counters=counters).command == ("fix", "r1")
    second_round = step(
        requested_changes(review_aid="r2", work_review_iter=2),
        opts,
        counters=counters,
    )
    assert second_round.command == ("fix", "r2")
    assert counters.fix_rounds == 2


def test_an_in_flight_fix_row_parks_on_the_active_guard_not_a_second_dispatch():
    """Once the dispatched fix row DOES appear on the board, `decide()`'s
    `active_count > 0` guard takes over before the review gate is reached."""
    counters = DriveCounters()
    action = step(
        requested_changes(active_count=1, active_types=("work",)), counters=counters
    )
    assert action.kind == WAIT
    assert counters.fix_rounds == 0


def test_request_changes_with_no_review_id_refuses_rather_than_guessing():
    """`review_verdict` and `review_aid` come off the same board row, so this
    is impossible today — assert it anyway. Everything past this point spends
    money keyed on that id, and `coord fix ""` is not a refusal this arm
    should have to interpret."""
    counters = DriveCounters()
    action = step(requested_changes(review_aid=""), counters=counters)
    assert action.is_exit
    assert action.command == ()
    assert counters.fix_rounds == 0


def test_request_changes_with_the_auto_loop_off_reports_and_stops():
    """`coord fix` routes through `auto_loop.process_review_completion`, whose
    first line refuses when `pipeline.auto_loop` is off. Dispatching a
    subprocess that can only fail would report a subprocess error; the
    preflight warning already promises "report the verdict and stop"."""
    counters = DriveCounters()
    action = step(requested_changes(auto_loop=False), counters=counters)
    assert action.is_exit
    assert action.exit_code == EXIT_TERMINAL_FAILURE
    assert "auto_loop is OFF" in action.message
    assert counters.fix_rounds == 0


def test_request_changes_stops_when_the_review_fix_loop_is_exhausted():
    action = step(
        work_tested(
            review_aid="r1",
            review_verdict="request-changes",
            work_review_iter=5,
            max_review_iterations=5,
        )
    )
    assert action.is_exit
    assert "fix loop is exhausted" in action.message


def test_max_review_iterations_dies_BEFORE_any_fix_is_dispatched():
    """#1692: the outer cap stays first. With the whole fix budget untouched
    the driver must still refuse — `max_review_iterations` bounds the ISSUE's
    review loop across every drive that ever touches it, and
    `_dispatch_fix_for_review` would refuse this dispatch anyway
    (`next_iteration > max_iter`), turning a clear cap message into an opaque
    subprocess failure. Nothing is spawned and no round is spent."""
    counters = DriveCounters()
    opts = DriveOptions(machine="precision", max_fix_rounds=99)
    action = step(
        requested_changes(work_review_iter=5, max_review_iterations=5),
        opts,
        counters=counters,
    )
    assert action.is_exit
    assert action.command == ()
    assert counters.fix_rounds == 0
    assert counters.review_fix_dispatched_for == ""
    assert "fix loop is exhausted" in action.message


def test_max_fix_rounds_zero_never_dispatches_a_review_fix():
    """The test arm's `max_fix_rounds=0` guard, mirrored: a drive told to
    spend nothing must spend nothing on the review arm either."""
    counters = DriveCounters()
    action = step(
        requested_changes(),
        DriveOptions(machine="precision", max_fix_rounds=0),
        counters=counters,
    )
    assert action.is_exit
    assert action.command == ()
    assert counters.fix_rounds == 0


def test_interactive_work_requests_its_review_exactly_once_under_force_review():
    counters = DriveCounters()
    opts = DriveOptions(machine="precision", force_review=True)
    s = work_tested(work_provider="claude-pty")

    first = step(s, opts, counters=counters)
    assert first.command == ("review", "w1")
    assert counters.review_dispatches == 1

    second = step(s, opts, counters=counters)
    assert second.is_exit
    assert "none appeared on the board" in second.message


def test_interactive_work_without_force_review_is_terminal_at_the_review_gate():
    action = step(work_tested(work_provider="claude-pty"))
    assert action.is_exit
    assert "#555" in action.message


def test_an_unexpected_review_verdict_warns_and_waits():
    action = step(work_tested(review_aid="r1", review_verdict="maybe"))
    assert action.kind == WAIT
    assert any("maybe" in w for w in action.warnings)


# ═══════════════════════════════════════════════════════════════════════════
# the MERGE stage
# ═══════════════════════════════════════════════════════════════════════════


def approved_work(**kw) -> IssueState:
    return work_tested(review_aid="r1", review_verdict="approve", **kw)


def test_no_merge_stops_after_the_review_approves():
    action = step(approved_work(), DriveOptions(machine="precision", do_merge=False))
    assert action.is_exit
    assert action.exit_code == EXIT_OK
    assert "coord merge --only w1" in action.message


def test_an_approved_review_merges_through_the_cli_under_the_merge_lock():
    action = step(approved_work())
    assert action.command == ("merge", "--only", "w1", "--method", "rebase")
    assert action.serialize_merge is True
    # Tolerant: the first attempt often precedes enqueue_approved_work.
    assert action.on_error == "warn"


def test_the_merge_method_is_honoured():
    action = step(
        approved_work(), DriveOptions(machine="precision", merge_method="squash")
    )
    assert action.command[-1] == "squash"


def test_merge_uses_the_queue_entrys_assignment_id_when_it_differs():
    """A fix chain enqueues under an earlier work row; --only must match it."""
    action = step(approved_work(merge_aid="w0", merge_status=""))
    assert action.command == ("merge", "--only", "w0", "--method", "rebase")


def test_merge_retries_are_bounded():
    """Unbounded merge retries until the deadline was a real bug."""
    counters = DriveCounters()
    opts = DriveOptions(machine="precision", max_merge_attempts=2)
    s = approved_work()

    assert step(s, opts, counters=counters).kind == RUN
    assert step(s, opts, counters=counters).kind == RUN
    exhausted = step(s, opts, counters=counters)
    assert exhausted.is_exit
    assert "merge attempted 2 times without landing" in exhausted.message


def test_human_required_merge_is_terminal_with_the_override_recipe():
    action = step(approved_work(merge_status="HUMAN_REQUIRED", merge_reason="semantic"))
    assert action.is_exit
    assert "--override-human-required" in action.message
    assert "semantic" in action.message


@pytest.mark.parametrize("status", ["HUMAN_REQUIRED", "human_required"])
def test_human_required_is_matched_case_insensitively(status):
    assert step(approved_work(merge_status=status)).is_exit


def test_a_conflict_runs_coord_merge_rather_than_waiting_forever():
    """#1474: `dispatch_conflict_fix` has exactly two sanctioned callers — an
    actual `coord merge` run, and the semantic-escalation variant reachable
    only from `coord resume` (human-invoked). A bare `_wait()` here means
    NOTHING ever dispatches the fix worker — the exact deadlock that stalled
    #1453/#1461 for ~14 hours. The regression test that would have caught
    it: CONFLICT must yield a RUN action (the `coord merge --only <aid>`
    that actually runs `classify_conflict` + `dispatch_conflict_fix`), not a
    WAIT with nothing behind it.
    """
    action = step(approved_work(merge_status="CONFLICT"))
    assert action.kind == RUN
    assert action.command == ("merge", "--only", "w1", "--method", "rebase")


def test_conflict_retries_are_bounded_by_the_same_merge_attempt_cap():
    """CONFLICT falls through to the same bounded retry as every other
    non-terminal merge status — a `coord merge --only` that keeps landing
    back on CONFLICT must still terminate, not spin until the deadline."""
    counters = DriveCounters()
    opts = DriveOptions(machine="precision", max_merge_attempts=2)
    s = approved_work(merge_status="CONFLICT")

    assert step(s, opts, counters=counters).kind == RUN
    assert step(s, opts, counters=counters).kind == RUN
    exhausted = step(s, opts, counters=counters)
    assert exhausted.is_exit
    assert "merge attempted 2 times without landing" in exhausted.message


def test_a_conflict_with_an_active_conflict_fix_waits_instead_of_re_dispatching():
    """Once a conflict-fix worker is actually dispatched, it is a
    `type="conflict-fix"` row scoped to this same issue — `decide()`'s own
    ``active_count`` gate (checked before the merge stage is ever reached)
    must park the run there, never re-attempt `coord merge --only` while one
    is already in flight. This is what makes the #1474 fix safe: RUN once
    to dispatch, then the board itself — not a flag `_decide_merge` has to
    track — is what prevents a duplicate dispatch on the next poll.
    """
    action = step(
        approved_work(
            merge_status="CONFLICT",
            active_count=1,
            active_types=("conflict-fix",),
        )
    )
    assert action.kind == WAIT


def test_a_blocked_merge_waits_and_reports_the_gate():
    action = step(approved_work(merge_status="BLOCKED", merge_reason="CI running"))
    assert action.kind == WAIT
    assert "CI running" in action.label


# ═══════════════════════════════════════════════════════════════════════════
# #1891: a CI verdict that has not arrived must not consume merge budget —
# checked off `merge_reason` (which falls back to the raw queue row's own
# persisted `error` when the board's live re-evaluation comes back empty),
# NOT off `merge_status` — so this fires regardless of whatever `merge_status`
# happens to read: "", "PENDING", "READY", or "BLOCKED" are all real values
# the board can show for the exact same still-pending checks, depending on
# whether `_gate_refresher`'s periodic snapshot caught up with a live `coord
# merge` attempt's own fresher read. See `coord.merge_queue.CI_PENDING_PREFIX`.
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("status", ["", "PENDING", "READY", "BLOCKED"])
def test_checks_pending_waits_regardless_of_which_status_the_board_shows(status):
    """The board can legitimately show any of these for the SAME still-pending
    checks (see the module comment above) — every one of them must wait, not
    retry, as long as `merge_reason` names the pending checks."""
    action = step(
        approved_work(
            merge_status=status,
            merge_reason="CI running: build, lint",
        )
    )
    assert action.kind == WAIT
    assert "CI running: build, lint" in action.label
    assert "not retrying" in action.label


def test_checks_pending_never_spends_an_attempt_across_several_polls():
    """Acceptance (#1891): `merge_attempts` does not increase across several
    polls while checks remain pending — the exact accounting bug the GitHub
    Actions outage of 2026-08-06 hit: three attempts, then `_die()`, for an
    entry whose checks were merely starved of runners, not failing."""
    counters = DriveCounters()
    opts = DriveOptions(machine="precision", max_merge_attempts=2)
    s = approved_work(merge_status="", merge_reason="CI running: build")

    for _ in range(5):
        action = step(s, opts, counters=counters)
        assert action.kind == WAIT
        assert counters.merge_attempts == 0


def test_checks_pending_reason_is_recognised_via_the_shared_predicate_not_ad_hoc_text():
    """Guards against a future edit accidentally narrowing the match to the
    board-render wording only — `process()`'s live `entry.error` uses the
    identical `CI_PENDING_PREFIX`, and both must keep working."""
    from coord.merge_queue import CI_PENDING_PREFIX, is_ci_pending_reason

    assert is_ci_pending_reason(f"{CI_PENDING_PREFIX} build")
    assert not is_ci_pending_reason("checks failed: build (failure)")
    assert not is_ci_pending_reason("")
    assert not is_ci_pending_reason(None)


def test_a_genuinely_failed_check_still_walks_the_bounded_retry_path():
    """Regression guard: this fix must not be readable as "ignore CI
    failures". A `checks_failed` reason — even reaching the drive through the
    SAME empty/PENDING/READY `merge_status` gap `checks_pending` can — is not
    an `is_ci_pending_reason` match, so it falls through unchanged to the
    existing bounded retry (RUN, capped, then exhausted)."""
    counters = DriveCounters()
    opts = DriveOptions(machine="precision", max_merge_attempts=2)
    s = approved_work(merge_status="", merge_reason="checks failed: build (failure)")

    assert step(s, opts, counters=counters).kind == RUN
    assert counters.merge_attempts == 1
    assert step(s, opts, counters=counters).kind == RUN
    assert counters.merge_attempts == 2
    exhausted = step(s, opts, counters=counters)
    assert exhausted.is_exit
    assert "merge attempted 2 times without landing" in exhausted.message


def test_a_genuinely_failed_check_reported_as_blocked_still_just_waits_like_before():
    """When the board DOES correctly render `BLOCKED` for a failed check (the
    common case), behaviour is byte-for-byte unchanged from before #1891 —
    same as `test_a_blocked_merge_waits_and_reports_the_gate`, just with the
    CI-failed wording instead of CI-running."""
    action = step(approved_work(merge_status="BLOCKED", merge_reason="CI failed: build (failure)"))
    assert action.kind == WAIT
    assert "CI failed" in action.label


# ═══════════════════════════════════════════════════════════════════════════
# #1892: the sibling case — a CI verdict DID arrive, but every failing check
# carried no verdict about the code (never assigned a runner, or died before
# checkout). `coord merge`'s own live attempt auto-reruns CI for this case
# instead; the drive must wait for that rerun, not retry `coord merge`
# itself or burn its merge-attempt budget on a no-op observation.
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("status", ["", "PENDING", "READY", "BLOCKED"])
def test_ci_infra_failure_waits_regardless_of_which_status_the_board_shows(status):
    action = step(
        approved_work(
            merge_status=status,
            merge_reason=(
                "CI infra: no-gh-on-path (cancelled) — no verdict about the "
                "code (never assigned a runner, or died before checkout)"
            ),
        )
    )
    assert action.kind == WAIT
    assert "auto-rerunning" in action.label
    assert "not retrying" in action.label


def test_ci_infra_failure_never_spends_an_attempt_across_several_polls():
    """Acceptance (#1892): a PR whose failures are ALL verdictless does not
    consume a drive merge attempt, mirroring
    test_checks_pending_never_spends_an_attempt_across_several_polls."""
    counters = DriveCounters()
    opts = DriveOptions(machine="precision", max_merge_attempts=2)
    s = approved_work(
        merge_status="",
        merge_reason="CI infra: e2e (failure) — no verdict about the code",
    )

    for _ in range(5):
        action = step(s, opts, counters=counters)
        assert action.kind == WAIT
        assert counters.merge_attempts == 0


def test_ci_infra_reason_is_recognised_via_the_shared_predicate_not_ad_hoc_text():
    from coord.merge_queue import CI_INFRA_PREFIX, is_ci_infra_reason

    assert is_ci_infra_reason(f"{CI_INFRA_PREFIX} e2e (failure)")
    assert not is_ci_infra_reason("checks failed: build (failure)")
    assert not is_ci_infra_reason("CI running: build")
    assert not is_ci_infra_reason("")
    assert not is_ci_infra_reason(None)


def test_a_genuinely_failed_check_is_not_read_as_ci_infra():
    """Regression guard (acceptance criterion): a PR with ANY genuinely
    failed check must behave exactly as today — a plain 'checks failed: ...'
    reason (no CI_INFRA_PREFIX) still walks the bounded retry path, exactly
    like test_a_genuinely_failed_check_still_walks_the_bounded_retry_path."""
    counters = DriveCounters()
    opts = DriveOptions(machine="precision", max_merge_attempts=2)
    s = approved_work(
        merge_status="",
        merge_reason=(
            "checks failed: build (failure) — auto-rerun budget exhausted "
            "(2/2); needs a human"
        ),
    )

    assert step(s, opts, counters=counters).kind == RUN
    assert counters.merge_attempts == 1


# ── #1505: escalate on a status retrying can't fix ──────────────────────────


def test_needs_attention_escalates_on_the_first_encounter_instead_of_retrying():
    """The #1477 bug: NEEDS_ATTENTION used to fall through to the same
    bounded retry as PENDING/CONFLICT, burning the whole merge-attempt
    budget on a status no retry could ever change. It must escalate
    immediately — `counters.merge_attempts` never even increments."""
    counters = DriveCounters()
    action = step(approved_work(merge_status="NEEDS_ATTENTION"), counters=counters)
    assert action.is_exit
    assert action.exit_code == EXIT_ESCALATED
    assert counters.merge_attempts == 0
    assert action.command[:2] == ("escalate", "record")
    assert "NEEDS_ATTENTION" in action.message


def test_an_unrecognised_merge_status_also_escalates_rather_than_spinning():
    """Acceptance: "a driver reaching NEEDS_ATTENTION (or an unrecognised
    merge status) escalates" — not just the one named value."""
    action = step(approved_work(merge_status="SOME_FUTURE_STATUS"))
    assert action.is_exit
    assert action.exit_code == EXIT_ESCALATED


def test_escalation_command_proposes_the_gh_pr_merge_recipe_when_a_pr_is_known():
    """Mirrors the #1477 resolution this issue was opened over: `gh pr merge
    --rebase` + `coord reconcile-merges` when a PR number is on the board."""
    action = step(
        approved_work(
            merge_status="NEEDS_ATTENTION",
            merge_pr_url="https://github.com/john/claude-coordinator/pull/1496",
        )
    )
    command_str = " ".join(action.command)
    assert "--command" in action.command
    idx = action.command.index("--command")
    assert action.command[idx + 1] == "gh pr merge 1496 --rebase && coord reconcile-merges"
    assert "--gate" in action.command
    assert "pr_url=https://github.com/john/claude-coordinator/pull/1496" in command_str


def test_escalation_command_falls_back_to_the_plan_view_with_no_known_pr():
    action = step(approved_work(merge_status="NEEDS_ATTENTION", merge_pr_url=""))
    idx = action.command.index("--command")
    assert "coord merge --plan --repo" in action.command[idx + 1]


def test_escalation_carries_the_assignment_id_and_gate_readings():
    action = step(
        approved_work(
            merge_status="NEEDS_ATTENTION",
            merge_reason="review not approved",
        )
    )
    assert "--assignment" in action.command
    idx = action.command.index("--assignment")
    assert action.command[idx + 1] == "w1"
    command_str = " ".join(action.command)
    assert "merge_reason=review not approved" in command_str
    assert "review_verdict=approve" in command_str


def test_a_conflict_status_still_retries_rather_than_escalating():
    """CONFLICT keeps its own #1474 dispatch path — it must NOT be swept
    into the new escalate branch alongside NEEDS_ATTENTION."""
    action = step(approved_work(merge_status="CONFLICT"))
    assert action.kind == RUN


def test_the_escalate_branch_runs_before_the_attempt_cap_is_checked():
    """Even with the cap already exhausted, NEEDS_ATTENTION escalates
    (distinct message/exit code) rather than reporting a generic
    'merge attempted N times' exhaustion."""
    counters = DriveCounters(merge_attempts=5)
    opts = DriveOptions(machine="precision", max_merge_attempts=1)
    action = step(approved_work(merge_status="NEEDS_ATTENTION"), opts, counters=counters)
    assert action.exit_code == EXIT_ESCALATED
    assert "attempted" not in action.message


# ═══════════════════════════════════════════════════════════════════════════
# #1526: driver/gate divergence — `coord merge`'s own reason overrides a
# stale-green `work_test_state`/`review_verdict` reading instead of being
# retried against blind.
# ═══════════════════════════════════════════════════════════════════════════


def test_smoke_required_with_a_passed_test_state_escalates_instead_of_retrying():
    """#1526 instance 1 (#1412): board shows test=passed, but `coord merge`
    left 'smoke test required but no verdict recorded' as the merge_reason
    (`merge_queue.process()`'s wording when `has_smoke_verdict` fails closed
    on a fresher check than `work_test_state` reflects). Retrying `coord
    merge --only` unchanged reproduces the identical refusal every time — the
    driver must name the gate and stop, not spend the retry budget on it.
    """
    counters = DriveCounters()
    action = step(
        approved_work(
            merge_status="READY",
            merge_reason="smoke test required but no verdict recorded",
        ),
        counters=counters,
    )
    assert action.is_exit
    assert action.exit_code == EXIT_ESCALATED
    assert counters.merge_attempts == 0  # never even tried the doomed merge
    assert "smoke" in action.message.lower()
    assert "coord test w1 --passed" in " ".join(action.command)


def test_smoke_gate_agreeing_with_a_missing_verdict_still_retries():
    """Sanity check for the divergence gate: when `work_test_state` is
    genuinely blank (no verdict at all — the OTHER, non-divergent way to see
    a smoke_required reason), `_decide_test` — called earlier in `decide()`
    — already parks the run on a wait; `_decide_merge` is never even
    reached, so this never becomes an escalate-vs-retry question at all."""
    action = step(done_work(work_test_state=""))
    assert action.kind == WAIT


def test_stale_smoke_divergence_redispatches_the_test_stage_instead_of_escalating():
    """#1738: unlike the "missing verdict" divergence above, a STALE verdict
    (recorded, but against a base/branch that has since moved) has a safe,
    bounded self-service fix — re-run the Test stage — so the very first
    encounter must NOT escalate; it must clear the verdict via `coord
    diagnose --stage test --reset` (which `dispatch_pending_smoke` then
    re-dispatches on its own next tick)."""
    counters = DriveCounters()
    action = step(
        approved_work(
            merge_status="READY",
            merge_reason=(
                "smoke test verdict is stale: recorded against base "
                "23acfbb, base is now b263929 — re-verify against the "
                "current base, then `coord test w1 --passed`"
            ),
        ),
        counters=counters,
    )
    assert action.kind == RUN
    assert not action.is_exit
    assert action.command == (
        "diagnose", REPO, str(ISSUE), "--stage", "test", "--reset",
    )
    assert counters.fix_rounds == 1
    assert counters.merge_attempts == 0  # never even tried the doomed merge


def test_stale_smoke_divergence_also_matches_the_plan_wording():
    """`merge_queue.plan()`'s board-render wording ("test verdict stale
    (...)") names the identical stale-verdict case in different words — same
    remedy."""
    counters = DriveCounters()
    action = step(
        approved_work(
            merge_status="BLOCKED",
            merge_reason="test verdict stale (base moved b263929)",
        ),
        counters=counters,
    )
    assert action.kind == RUN
    assert action.command == (
        "diagnose", REPO, str(ISSUE), "--stage", "test", "--reset",
    )


def test_stale_smoke_redispatch_is_bounded_by_max_fix_rounds():
    """The re-test arm shares the SAME `fix_rounds` budget as the test-failed
    and review-fix arms (#1738) — it must converge to an escalation, not spin
    forever, if the verdict keeps going stale."""
    counters = DriveCounters()
    opts = DriveOptions(machine="precision", max_fix_rounds=2)
    s = approved_work(
        merge_status="READY",
        merge_reason="smoke test verdict is stale: recorded against base X, base is now Y",
    )

    first = step(s, opts, counters=counters)
    assert first.kind == RUN
    second = step(s, opts, counters=counters)
    assert second.kind == RUN
    exhausted = step(s, opts, counters=counters)
    assert exhausted.is_exit
    assert exhausted.exit_code == EXIT_ESCALATED
    assert "smoke" in exhausted.message.lower()


def test_stale_smoke_redispatch_respects_max_fix_rounds_zero():
    """A drive told to spend zero fix rounds escalates on the very first
    stale-smoke encounter rather than dispatching a re-test it isn't allowed
    to spend budget on."""
    action = step(
        approved_work(
            merge_status="READY",
            merge_reason="smoke test verdict is stale: recorded against base X, base is now Y",
        ),
        DriveOptions(machine="precision", max_fix_rounds=0),
    )
    assert action.is_exit
    assert action.exit_code == EXIT_ESCALATED


def test_missing_smoke_verdict_divergence_still_escalates_immediately():
    """Restates the #1526 "missing verdict" case side by side with the new
    #1738 "stale verdict" arm above so the two can't silently drift onto the
    same (wrong) behaviour — only staleness gets the automated re-test."""
    counters = DriveCounters()
    action = step(
        approved_work(
            merge_status="READY",
            merge_reason="smoke test required but no verdict recorded",
        ),
        counters=counters,
    )
    assert action.is_exit
    assert action.exit_code == EXIT_ESCALATED
    assert counters.fix_rounds == 0


def test_review_required_with_an_approved_verdict_escalates_instead_of_retrying():
    """#1526 instance 2 (#1483): board shows review=approve, but a rebase
    onto a moved `main` correctly voided the approval (#1475's patch-id
    gate) — `coord merge` leaves 'review required but not approved' as the
    merge_reason. Retrying cannot reconcile the two readings; the driver
    must name the gate and propose the safe corrective action (a scoped
    reaffirm or a full re-review) instead of burning the merge-attempt
    budget three times over.
    """
    counters = DriveCounters()
    action = step(
        approved_work(
            merge_status="READY",
            merge_reason="review required but not approved",
        ),
        counters=counters,
    )
    assert action.is_exit
    assert action.exit_code == EXIT_ESCALATED
    assert counters.merge_attempts == 0
    assert "review" in action.message.lower()
    command_str = " ".join(action.command)
    assert "review-reaffirm w1" in command_str
    assert "coord review w1" in command_str


def test_divergence_is_named_even_when_plans_own_gate_check_already_blocked():
    """The divergence can hide behind BLOCKED too — when `merge_queue.
    plan()`'s OWN render-time gate check caught the same disagreement (see
    `_entry_gate_status`) — not only behind a nominally-retryable status
    like READY. Either way this must escalate, never fall into the passive
    `_wait()` the plain BLOCKED branch uses for a merge-unrelated reason
    like 'CI running' (see `test_a_blocked_merge_waits_and_reports_the_gate`).
    """
    action = step(
        approved_work(
            merge_status="BLOCKED",
            merge_reason="test verdict missing",
        )
    )
    assert action.is_exit
    assert action.exit_code == EXIT_ESCALATED


def test_two_identical_merge_refusals_in_a_row_escalate_without_a_third_attempt():
    """#1526 black-box scenario (c): simulates the actual polling sequence —
    attempt 1 runs (merge_reason is still empty going in, so the divergence
    can't be seen yet), then `coord merge` leaves its refusal on the board.
    The SECOND `decide()` call, reading that refusal back against an
    unchanged 'passed' test_state, must escalate rather than spend a second
    (of three) attempts retrying the identical command."""
    counters = DriveCounters()
    opts = DriveOptions(machine="precision", max_merge_attempts=3)

    # Poll 1: nothing has run yet — merge_reason is empty, so there is
    # nothing to diverge from. A real attempt is still the right call.
    first = step(
        approved_work(merge_status="READY", merge_reason=""),
        opts,
        counters=counters,
    )
    assert first.kind == RUN
    assert counters.merge_attempts == 1

    # Poll 2: that attempt's own refusal is now on the board, and it
    # contradicts this same state's work_test_state="passed" — escalate
    # instead of burning attempt 2 (or, worse, all the way to 3).
    second = step(
        approved_work(
            merge_status="READY",
            merge_reason="smoke test required but no verdict recorded",
        ),
        opts,
        counters=counters,
    )
    assert second.is_exit
    assert second.exit_code == EXIT_ESCALATED
    assert counters.merge_attempts == 1  # unchanged — no second attempt spent


def test_merge_gate_kind_recognises_both_process_and_plan_wordings():
    from coord.drive import _merge_gate_kind

    assert _merge_gate_kind("smoke test required but no verdict recorded") == "smoke"
    assert _merge_gate_kind("test verdict missing") == "smoke"
    assert _merge_gate_kind("review required but not approved") == "review"
    assert _merge_gate_kind("review not approved") == "review"
    assert _merge_gate_kind("checks failed: build (failure)") is None
    assert _merge_gate_kind("") is None


def test_is_stale_smoke_reason_distinguishes_stale_from_missing():
    """#1738: the narrower predicate the re-test arm gates on — only the two
    STALE wordings qualify; "no verdict at all" wordings (still `_merge_gate_
    kind`'s "smoke") must not."""
    from coord.drive import _is_stale_smoke_reason

    assert _is_stale_smoke_reason(
        "smoke test verdict is stale: recorded against base X, base is now Y"
    )
    assert _is_stale_smoke_reason("test verdict stale (base moved)")
    assert not _is_stale_smoke_reason("smoke test required but no verdict recorded")
    assert not _is_stale_smoke_reason("test verdict missing")
    assert not _is_stale_smoke_reason("review required but not approved")
    assert not _is_stale_smoke_reason("")
    assert not _is_stale_smoke_reason(None)


# ── terminal: merged, verified ───────────────────────────────────────────────


def test_a_merged_board_row_is_verified_before_reporting_success():
    verifier = FakeVerifier(merged=True)
    action = step(approved_work(work_status="merged"), verifier=verifier)
    assert action.is_exit
    assert action.exit_code == EXIT_OK
    assert "MERGED" in action.message
    assert verifier.merged_calls == 1


def test_a_merged_board_row_that_did_not_actually_land_fails_loudly():
    action = step(
        approved_work(work_status="merged"), verifier=FakeVerifier(merged=False)
    )
    assert action.is_exit
    assert action.exit_code == EXIT_TERMINAL_FAILURE
    assert "has NOT landed" in action.message


def test_merge_status_merged_is_also_a_terminal_success():
    action = step(approved_work(merge_status="MERGED"), verifier=FakeVerifier(merged=True))
    assert action.exit_code == EXIT_OK


def test_a_merged_row_with_no_branch_cannot_be_verified():
    action = step(approved_work(work_status="merged", work_branch=""))
    assert action.exit_code == EXIT_TERMINAL_FAILURE


# ═══════════════════════════════════════════════════════════════════════════
# GitMergeVerifier — never `merge-base --is-ancestor`
# ═══════════════════════════════════════════════════════════════════════════


@pytest.fixture
def recorded_git(monkeypatch):
    """Capture every subprocess argv and script the return values.

    #1483 moved the ``gh pr view`` call in ``verify_merged`` behind the
    ``github_ops`` seam. Both ``coord.drive`` and ``coord.github_ops`` do a
    plain ``import subprocess``, so they share the same module-level
    ``subprocess.run`` attribute — patching it once via ``coord.drive.
    subprocess.run`` patches it for both call sites, and a single
    ``scripted`` dict drives both the git and gh sides of a scenario.
    """
    calls: list[list[str]] = []
    scripted: dict[tuple[str, ...], tuple[int, str]] = {}

    def fake_run(argv, **kw):
        calls.append(list(argv))
        for needle, (rc, out) in scripted.items():
            if all(token in argv for token in needle):
                return subprocess.CompletedProcess(argv, rc, out, "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr("coord.drive.subprocess.run", fake_run)
    return calls, scripted


def test_verify_merged_never_uses_merge_base_is_ancestor(recorded_git, tmp_path):
    """`--is-ancestor` is ALWAYS wrong under --method rebase/squash: both
    rewrite the commits, so a landed branch's tip is never an ancestor of the
    target. Verified against #1344 (merged via PR #1355, still says no)."""
    calls, scripted = recorded_git
    (tmp_path / ".git").mkdir()
    scripted[("cherry",)] = (0, "- abc123\n- def456\n")

    verifier = GitMergeVerifier(repo_path=str(tmp_path))
    s = state(work_branch="issue-1392-x", repo_github="", repo_default_branch="main")
    assert verifier.verify_merged(s) is True

    flat = [" ".join(c) for c in calls]
    assert not any("--is-ancestor" in c for c in flat), flat
    assert any("cherry" in c for c in flat), flat


def test_verify_merged_reports_unmerged_when_cherry_shows_a_plus(recorded_git, tmp_path):
    _calls, scripted = recorded_git
    (tmp_path / ".git").mkdir()
    scripted[("cherry",)] = (0, "- abc123\n+ def456\n")
    s = state(work_branch="b", repo_github="", repo_default_branch="main")
    assert GitMergeVerifier(repo_path=str(tmp_path)).verify_merged(s) is False


def test_verify_merged_prefers_the_github_pr_state(recorded_git, tmp_path):
    calls, scripted = recorded_git
    (tmp_path / ".git").mkdir()
    scripted[("pr", "view")] = (0, "MERGED\n")

    s = state(work_branch="b", repo_github="john/x")
    assert GitMergeVerifier(repo_path=str(tmp_path)).verify_merged(s) is True
    # Authoritative: no git fallback needed.
    assert not any("cherry" in " ".join(c) for c in calls)


def test_verify_merged_rejects_a_closed_pr(recorded_git, tmp_path):
    _calls, scripted = recorded_git
    (tmp_path / ".git").mkdir()
    scripted[("pr", "view")] = (0, "CLOSED\n")

    warned: list[str] = []
    verifier = GitMergeVerifier(repo_path=str(tmp_path), warn=warned.append)
    assert verifier.verify_merged(state(work_branch="b", repo_github="john/x")) is False
    assert any("CLOSED" in w for w in warned)


def test_verify_merged_falls_back_to_cherry_when_gh_finds_no_pr(
    recorded_git, tmp_path
):
    calls, scripted = recorded_git
    (tmp_path / ".git").mkdir()
    scripted[("pr", "view")] = (1, "")
    scripted[("cherry",)] = (0, "- abc\n")
    s = state(work_branch="b", repo_github="john/x")
    assert GitMergeVerifier(repo_path=str(tmp_path)).verify_merged(s) is True
    assert any("cherry" in " ".join(c) for c in calls)


def test_verify_merged_cleans_up_its_verify_ref(recorded_git, tmp_path):
    calls, scripted = recorded_git
    (tmp_path / ".git").mkdir()
    scripted[("cherry",)] = (0, "- abc\n")
    s = state(work_branch="b", repo_github="", repo_default_branch="main")
    GitMergeVerifier(repo_path=str(tmp_path)).verify_merged(s)
    assert any("update-ref -d" in " ".join(c) for c in calls)


def test_branch_has_commits_counts_commits_the_default_branch_lacks(
    recorded_git, tmp_path
):
    calls, scripted = recorded_git
    (tmp_path / ".git").mkdir()
    scripted[("rev-list",)] = (0, "3\n")
    s = state(work_branch="b", repo_default_branch="main")
    assert GitMergeVerifier(repo_path=str(tmp_path)).branch_has_commits(s) is True
    assert any("origin/main..FETCH_HEAD" in " ".join(c) for c in calls)


def test_branch_has_commits_is_false_for_zero(recorded_git, tmp_path):
    _calls, scripted = recorded_git
    (tmp_path / ".git").mkdir()
    scripted[("rev-list",)] = (0, "0\n")
    s = state(work_branch="b")
    assert GitMergeVerifier(repo_path=str(tmp_path)).branch_has_commits(s) is False


def test_branch_has_commits_is_false_when_the_remote_branch_is_missing(
    recorded_git, tmp_path
):
    _calls, scripted = recorded_git
    (tmp_path / ".git").mkdir()
    scripted[("fetch", "b")] = (128, "")
    s = state(work_branch="b")
    assert GitMergeVerifier(repo_path=str(tmp_path)).branch_has_commits(s) is False


def test_the_verifiers_are_inert_without_a_local_checkout(tmp_path):
    verifier = GitMergeVerifier(repo_path=str(tmp_path / "nope"))
    s = state(work_branch="b", repo_github="")
    assert verifier.branch_has_commits(s) is False
    assert verifier.verify_merged(s) is False


def test_branch_has_commits_is_false_with_no_branch(tmp_path):
    assert GitMergeVerifier(repo_path=str(tmp_path)).branch_has_commits(state()) is False


# ═══════════════════════════════════════════════════════════════════════════
# locking
# ═══════════════════════════════════════════════════════════════════════════


_needs_real_flock = pytest.mark.skipif(
    sys.platform == "win32",
    reason="FileLock is backed by fcntl.flock() (coord/filelock.py) — POSIX-only "
    "advisory locking, no Windows lock backend implemented yet",
)


@pytest.mark.posix_only
@_needs_real_flock
def test_a_second_lock_holder_is_refused_immediately(tmp_path):
    first = FileLock(tmp_path / "l")
    first.acquire(timeout=0.0)
    try:
        with pytest.raises(LockBusy):
            FileLock(tmp_path / "l").acquire(timeout=0.0)
    finally:
        first.release()
    # Released: it can be taken again.
    second = FileLock(tmp_path / "l")
    second.acquire(timeout=0.0)
    second.release()


@pytest.mark.posix_only
@_needs_real_flock
def test_lock_is_released_on_context_exit(tmp_path):
    with FileLock(tmp_path / "l"):
        pass
    FileLock(tmp_path / "l").acquire(timeout=0.0)


# ═══════════════════════════════════════════════════════════════════════════
# the Driver loop (I/O shell)
# ═══════════════════════════════════════════════════════════════════════════


class FakeFetcher:
    """Serves a scripted sequence of board payloads, repeating the last."""

    def __init__(
        self,
        payloads: list[dict] | None = None,
        error: Exception | None = None,
        error_on_call: int = 1,
    ):
        self.payloads = payloads or []
        self.error = error
        self.error_on_call = error_on_call
        self.calls = 0

    def fetch(self) -> dict:
        self.calls += 1
        if self.error is not None and self.calls == self.error_on_call:
            raise self.error
        if not self.payloads:
            return {"assignments": []}
        idx = min(self.calls - 1, len(self.payloads) - 1)
        return self.payloads[idx]


def board(**kw) -> dict:
    a = {
        "repo_name": REPO,
        "issue_number": ISSUE,
        "type": "work",
        "assignment_id": "w1",
        "dispatched_at": 1.0,
        "status": "done",
        "branch": "issue-1392-x",
        "machine_name": "precision",
    }
    a.update(kw)
    return {"assignments": [a]}


@pytest.fixture
def driver_factory(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr("coord.drive_state.scratch_dir", lambda: tmp_path)
    monkeypatch.setattr("coord.drive.scratch_dir", lambda: tmp_path)

    def make(
        payloads, *, opts=None, verifier=None, config=None, oracle_gate=None,
        usage_prober=None, ticks=200,
    ):
        clock = {"t": 0.0}
        recorded: list[list[str]] = []

        def fake_run(argv, **kw):
            recorded.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, "ok\n", "")

        monkeypatch.setattr("coord.drive.subprocess.run", fake_run)

        driver = Driver(
            repo=REPO,
            issue=ISSUE,
            opts=opts or DriveOptions(machine="precision", poll=1.0),
            config=config or make_config(),
            fetcher=FakeFetcher(payloads),
            verifier=verifier or FakeVerifier(),
            oracle_gate=oracle_gate,
            # #1466: never let a Driver test shell out to a real `claude -p
            # "/usage"` — default to a stub reporting "unknown" (same as no
            # probe at all), which the gate always treats as "proceed,
            # silently". Tests exercising the gate itself pass their own.
            usage_prober=usage_prober or (lambda: PlanLimits(status="unknown")),
            sleeper=lambda secs: clock.__setitem__("t", clock["t"] + secs),
            clock=lambda: clock["t"],
        )
        driver.recorded = recorded  # type: ignore[attr-defined]
        return driver

    return make


def test_driver_exits_zero_on_a_verified_merge(driver_factory, capsys):
    driver = driver_factory([board(status="merged")])
    assert driver.run() == EXIT_OK
    assert "MERGED" in capsys.readouterr().out


def _mixed_fleet_drive_config() -> Config:
    """#1906 acceptance fixture: one claude-only machine, one that also
    advertises `provider:opencode`, wired through `providers.labels` so a
    `harness:opencode` label resolves the effective provider."""
    return Config(
        repos=[Repo(name=REPO, github="john/claude-coordinator", test_command="pytest -q")],
        machines=[
            Machine(name="claude-only", host="claude-only", repos=[REPO]),
            Machine(
                name="opencode-box", host="opencode-box", repos=[REPO],
                capabilities=["provider:opencode"],
            ),
        ],
        providers=ProvidersConfig(
            definitions={"opencode": ProviderDef(type="opencode")},
            labels={"harness:opencode": "opencode"},
        ),
    )


def test_driver_auto_picks_the_capable_machine_for_an_opencode_labelled_issue(
    driver_factory, capsys,
):
    """#1906 end-to-end acceptance: no `--machine`, the issue's cached label
    resolves to `opencode`, and the dispatched `coord assign` argv — the
    actual dispatch target, not just the absence of a #1711 exception —
    names the capable machine, never the incapable one."""
    payload = {
        "assignments": [],
        "issues": [{"repo_name": REPO, "number": ISSUE, "labels": ["harness:opencode"]}],
    }
    driver = driver_factory(
        [payload],
        opts=DriveOptions(machine="", poll=1.0, deadline_mins=0.5 / 60.0),
        config=_mixed_fleet_drive_config(),
    )
    assert driver.run() == EXIT_DEADLINE
    assert driver.recorded, "expected at least one dispatched coord command"
    dispatched = driver.recorded[0]
    assert "assign" in dispatched
    assert "opencode-box" in dispatched
    assert "claude-only" not in dispatched
    assert "opencode" in capsys.readouterr().out  # the provider provenance log line


def test_driver_reports_the_distinct_no_capable_machine_error(driver_factory):
    """The fleet hosts the repo but nobody advertises `opencode` — must not
    read as the generic 'no unpaused machine hosts' message (#1906)."""
    config = _mixed_fleet_drive_config()
    config.machines = [config.machines[0]]  # claude-only survives; opencode-box doesn't
    payload = {
        "assignments": [],
        "issues": [{"repo_name": REPO, "number": ISSUE, "labels": ["harness:opencode"]}],
    }
    driver = driver_factory(
        [payload], opts=DriveOptions(machine=""), config=config,
    )
    with pytest.raises(DriveError) as exc:
        driver.run()
    message = str(exc.value)
    assert "no unpaused machine advertises" in message
    assert "opencode" in message
    assert exc.value.exit_code == EXIT_USAGE


# ═══════════════════════════════════════════════════════════════════════════
# #1499: audit events at the driver's own boundaries
# ═══════════════════════════════════════════════════════════════════════════


def _drive_audit_rows(coord_db):
    rows = coord_db.execute(
        "SELECT event_type, actor, category, repo, issue, summary, details_json "
        "FROM audit_log WHERE category = 'drive' ORDER BY id"
    ).fetchall()
    return [dict(r) for r in rows]


def test_run_records_drive_started_and_drive_exited_on_a_clean_finish(
    driver_factory, coord_db, capsys
):
    driver = driver_factory([board(status="merged")])
    assert driver.run() == EXIT_OK
    capsys.readouterr()

    rows = _drive_audit_rows(coord_db)
    assert [r["event_type"] for r in rows] == ["drive_started", "drive_exited"]
    for r in rows:
        assert r["actor"] == "drive"
        assert r["repo"] == REPO
        assert r["issue"] == ISSUE
    exit_details = json.loads(rows[1]["details_json"])
    assert exit_details["exit_code"] == EXIT_OK


def test_run_records_drive_exited_with_the_terminal_failure_reason(
    driver_factory, coord_db, capsys
):
    # A work assignment that failed outright is a terminal DriveError exit —
    # the exact "why did it stop?" the audit trail needs to answer after the
    # driver process is long gone.
    driver = driver_factory([board(status="failed")])
    code = driver.run()
    capsys.readouterr()
    assert code == EXIT_TERMINAL_FAILURE

    rows = _drive_audit_rows(coord_db)
    assert [r["event_type"] for r in rows] == ["drive_started", "drive_exited"]
    exit_details = json.loads(rows[1]["details_json"])
    assert exit_details["exit_code"] == EXIT_TERMINAL_FAILURE
    assert "failed" in rows[1]["summary"]


# ── #1453: the preflight banner never leaves oracle mode unstated ───────────


def test_driver_banner_reports_normal_drive_when_no_acceptance_driver_is_configured(
    driver_factory, capsys,
):
    driver = driver_factory([board(status="merged")])
    assert driver.run() == EXIT_OK
    out = capsys.readouterr().out
    assert "acceptance" in out
    assert "no acceptance.drivers entry" in out


def test_driver_banner_reports_oracle_drive_when_the_gate_is_satisfied(
    driver_factory, capsys,
):
    payload = board(status="merged")
    payload["issues"] = [{"repo_name": REPO, "number": ISSUE, "milestone_number": 38}]
    payload["milestone_work_orders"] = [
        {"repo_name": REPO, "tracking_issue": 1120, "nodes": [{"issue_number": ISSUE}]}
    ]
    driver = driver_factory(
        [payload],
        config=make_config_with_acceptance_driver(),
        oracle_gate=FakeGateChecker(exists=True),
    )
    assert driver.run() == EXIT_OK
    out = capsys.readouterr().out
    assert "ORACLE DRIVE" in out
    assert "ms-38" in out


def test_driver_banner_reports_normal_drive_under_no_acceptance(driver_factory, capsys):
    payload = board(status="merged")
    payload["issues"] = [{"repo_name": REPO, "number": ISSUE, "milestone_number": 38}]
    payload["milestone_work_orders"] = [
        {"repo_name": REPO, "tracking_issue": 1120, "nodes": [{"issue_number": ISSUE}]}
    ]
    driver = driver_factory(
        [payload],
        opts=DriveOptions(machine="precision", poll=1.0, no_acceptance=True),
        config=make_config_with_acceptance_driver(),
        oracle_gate=FakeGateChecker(exists=True),
    )
    assert driver.run() == EXIT_OK
    out = capsys.readouterr().out
    assert "--no-acceptance set" in out


def test_driver_shells_out_to_coord_and_never_calls_internals(driver_factory):
    """The CLI boundary, end to end: the merge really is a `coord` argv."""
    payload = board(
        status="done", test_state="passed", review_state="done", review_iteration=0
    )
    payload["assignments"].append(
        {
            "repo_name": REPO,
            "issue_number": ISSUE,
            "type": "review",
            "assignment_id": "r1",
            "dispatched_at": 2.0,
            "status": "done",
            "review_of_assignment_id": "w1",
            "review_verdict": "approve",
        }
    )
    driver = driver_factory(
        [payload],
        opts=DriveOptions(
            machine="precision", poll=1.0, max_merge_attempts=1, deadline_mins=1.0
        ),
    )
    assert driver.run() == EXIT_TERMINAL_FAILURE  # cap reached, never landed
    argvs = [" ".join(a) for a in driver.recorded]  # type: ignore[attr-defined]
    assert any("merge --only w1 --method rebase" in a for a in argvs), argvs


def test_driver_escalates_and_writes_the_record_via_the_cli(driver_factory):
    """#1505 end to end: a NEEDS_ATTENTION merge status escalates instead of
    burning `max_merge_attempts` on `coord merge --only`, and the write goes
    out as a `coord escalate record` argv — the CLI-is-the-contract rule,
    executed by the I/O shell (`Driver.run`), never `decide()` directly."""
    payload = board(
        status="done", test_state="passed", review_state="done", review_iteration=0
    )
    payload["assignments"].append(
        {
            "repo_name": REPO,
            "issue_number": ISSUE,
            "type": "review",
            "assignment_id": "r1",
            "dispatched_at": 2.0,
            "status": "done",
            "review_of_assignment_id": "w1",
            "review_verdict": "approve",
        }
    )
    payload["merge_plan"] = [
        {
            "repo_name": REPO,
            "issue_number": ISSUE,
            "status": "NEEDS_ATTENTION",
            "assignment_id": "w1",
            "pr_url": "https://github.com/john/claude-coordinator/pull/1496",
        }
    ]
    driver = driver_factory(
        [payload],
        opts=DriveOptions(
            machine="precision", poll=1.0, max_merge_attempts=3, deadline_mins=1.0
        ),
    )
    assert driver.run() == EXIT_ESCALATED
    argvs = [" ".join(a) for a in driver.recorded]  # type: ignore[attr-defined]
    assert any("escalate record" in a for a in argvs), argvs
    assert any("gh pr merge 1496 --rebase" in a for a in argvs), argvs
    assert not any(" merge --only" in a for a in argvs), argvs


def test_driver_escalates_a_gate_divergence_without_ever_attempting_the_merge(
    driver_factory,
):
    """#1526 end to end: `/board`'s `merge_plan` reads READY (a normal
    daemon-backed board build — `merge_queue.plan()`'s own render-time gate
    check didn't have the live SHA data to catch the staleness a REAL `coord
    merge` attempt would), but its `reason` already carries a smoke refusal
    left over from state persisted on the raw queue row. `work_test_state`
    reads 'passed'. `coord merge --only` must never even run — the
    divergence escalates on the very first poll.
    """
    payload = board(
        status="done", test_state="passed", review_state="done", review_iteration=0
    )
    payload["assignments"].append(
        {
            "repo_name": REPO,
            "issue_number": ISSUE,
            "type": "review",
            "assignment_id": "r1",
            "dispatched_at": 2.0,
            "status": "done",
            "review_of_assignment_id": "w1",
            "review_verdict": "approve",
        }
    )
    payload["merge_plan"] = [
        {
            "repo_name": REPO,
            "issue_number": ISSUE,
            "status": "READY",
            "reason": "smoke test required but no verdict recorded",
            "assignment_id": "w1",
        }
    ]
    driver = driver_factory(
        [payload],
        opts=DriveOptions(
            machine="precision", poll=1.0, max_merge_attempts=3, deadline_mins=1.0
        ),
    )
    assert driver.run() == EXIT_ESCALATED
    argvs = [" ".join(a) for a in driver.recorded]  # type: ignore[attr-defined]
    assert any("escalate record" in a for a in argvs), argvs
    assert any("coord test w1 --passed" in a for a in argvs), argvs
    assert not any(" merge --only" in a for a in argvs), argvs


def test_driver_posts_a_durable_comment_when_a_gate_divergence_escalates(
    driver_factory, monkeypatch,
):
    """#1526: the tmux pane and the `coord escalate` board row are not
    enough — both disappear the moment the drive session ends unless an
    operator already knows to look. The escalation must also reach the
    issue itself. Stubs `github_ops.post_issue_comment` so this never
    shells out to a real `gh`.
    """
    posted: list[tuple[str, int, str]] = []
    monkeypatch.setattr(
        "coord.github_ops.post_issue_comment",
        lambda repo, issue, body: posted.append((repo, issue, body)),
    )
    payload = board(
        status="done", test_state="passed", review_state="done", review_iteration=0
    )
    payload["assignments"].append(
        {
            "repo_name": REPO,
            "issue_number": ISSUE,
            "type": "review",
            "assignment_id": "r1",
            "dispatched_at": 2.0,
            "status": "done",
            "review_of_assignment_id": "w1",
            "review_verdict": "approve",
        }
    )
    payload["merge_plan"] = [
        {
            "repo_name": REPO,
            "issue_number": ISSUE,
            "status": "READY",
            "reason": "smoke test required but no verdict recorded",
            "assignment_id": "w1",
        }
    ]
    driver = driver_factory(
        [payload],
        opts=DriveOptions(
            machine="precision", poll=1.0, max_merge_attempts=3, deadline_mins=1.0
        ),
    )
    assert driver.run() == EXIT_ESCALATED
    assert len(posted) == 1
    repo_github, issue_number, body = posted[0]
    assert repo_github == "john/claude-coordinator"
    assert issue_number == ISSUE
    assert "smoke" in body.lower()


def test_driver_does_not_post_a_comment_on_a_normal_merge(driver_factory, monkeypatch):
    """The new #1526 comment channel is scoped to escalations only — a
    normal verified merge must not grow a GitHub side-effect it never had
    before."""
    posted: list[tuple[str, int, str]] = []
    monkeypatch.setattr(
        "coord.github_ops.post_issue_comment",
        lambda repo, issue, body: posted.append((repo, issue, body)),
    )
    driver = driver_factory([board(status="merged")])
    assert driver.run() == EXIT_OK
    assert posted == []


def test_driver_retries_a_conflict_originated_needs_attention_instead_of_escalating(
    driver_factory,
):
    """#1505 review fix, end to end: a normal daemon-backed board populates
    `merge_plan` on nearly every `/board` build, and `merge_queue.plan()`
    collapses a fresh CONFLICT into "NEEDS_ATTENTION" for display — that is
    the value `_decide_merge` actually receives for a still-auto-fixable
    conflict, NOT the literal string "CONFLICT". If the raw `merge_queue`
    row isn't cross-checked (`drive_state._merge_entry`), this escalates
    immediately and never gives `coord merge --only` (and the
    `classify_conflict`/`dispatch_conflict_fix` machinery it runs, #1474)
    another poll to clear the conflict — the same failure shape #1505 was
    opened to fix, just moved from HUMAN_REQUIRED onto every ordinary
    conflict. The board here never actually changes state (no fake merge
    lands), so the run must exhaust its bounded attempt cap and die with
    the generic exhaustion message — never the escalate branch.
    """
    payload = board(
        status="done", test_state="passed", review_state="done", review_iteration=0
    )
    payload["assignments"].append(
        {
            "repo_name": REPO,
            "issue_number": ISSUE,
            "type": "review",
            "assignment_id": "r1",
            "dispatched_at": 2.0,
            "status": "done",
            "review_of_assignment_id": "w1",
            "review_verdict": "approve",
        }
    )
    payload["merge_plan"] = [
        {
            "repo_name": REPO,
            "issue_number": ISSUE,
            "status": "NEEDS_ATTENTION",
            "assignment_id": "w1",
        }
    ]
    payload["merge_queue"] = [
        {
            "repo_name": REPO,
            "issue_number": ISSUE,
            "state": "conflict",
            "error": "rebase failed",
            "assignment_id": "w1",
        }
    ]
    driver = driver_factory(
        [payload],
        opts=DriveOptions(
            machine="precision", poll=1.0, max_merge_attempts=2, deadline_mins=1.0
        ),
    )
    assert driver.run() == EXIT_TERMINAL_FAILURE  # cap reached, never escalated
    argvs = [" ".join(a) for a in driver.recorded]  # type: ignore[attr-defined]
    assert any("merge --only w1 --method rebase" in a for a in argvs), argvs
    assert not any("escalate record" in a for a in argvs), argvs


def test_driver_returns_the_deadline_code_when_time_runs_out(driver_factory, capsys):
    driver = driver_factory(
        [board(status="done", test_state="")],
        opts=DriveOptions(machine="precision", poll=30.0, deadline_mins=1.0),
    )
    assert driver.run() == EXIT_DEADLINE
    assert "deadline" in capsys.readouterr().err


def test_driver_tolerates_a_transport_blip_mid_loop_and_retries(driver_factory, capsys):
    """A daemon blip must be a retry next poll, never a traceback."""
    driver = driver_factory([board(status="merged")])
    driver.fetcher = FakeFetcher(
        [board(status="merged")],
        error=RuntimeError("connection reset"),
        error_on_call=2,  # call 1 is the preflight read
    )
    assert driver.run() == EXIT_OK
    assert "state read failed" in capsys.readouterr().err


def test_a_blip_on_the_preflight_read_is_a_usage_error(driver_factory):
    """Preflight has nothing to resume from — mirrors the bash `die ... 2`."""
    driver = driver_factory([board(status="merged")])
    driver.fetcher = FakeFetcher([board()], error=RuntimeError("no route to host"))
    with pytest.raises(DriveError) as exc:
        driver.run()
    assert exc.value.exit_code == EXIT_USAGE


@pytest.mark.posix_only
@_needs_real_flock
def test_driver_refuses_a_second_run_on_the_same_issue(driver_factory, tmp_path):
    """A per-issue lock: two drivers on the same issue would double-dispatch."""
    held = FileLock(tmp_path / f"lock-{REPO}-{ISSUE}")
    held.acquire(timeout=0.0)
    (tmp_path / f"holder-{REPO}-{ISSUE}").write_text("someone else (pid 1)\n")
    try:
        driver = driver_factory([board(status="merged")])
        with pytest.raises(DriveError) as exc:
            driver.run()
        assert "already driving" in str(exc.value)
        assert "someone else" in str(exc.value)
        assert exc.value.exit_code == EXIT_USAGE
    finally:
        held.release()


@pytest.mark.posix_only
@_needs_real_flock
def test_driver_allows_a_concurrent_run_on_a_different_issue(driver_factory, tmp_path):
    held = FileLock(tmp_path / f"lock-{REPO}-999")
    held.acquire(timeout=0.0)
    try:
        driver = driver_factory([board(status="merged")])
        assert driver.run() == EXIT_OK
    finally:
        held.release()


@pytest.mark.posix_only
@_needs_real_flock
def test_driver_releases_the_lock_and_removes_the_holder_file(driver_factory, tmp_path):
    driver = driver_factory([board(status="merged")])
    assert driver.run() == EXIT_OK
    assert not (tmp_path / f"holder-{REPO}-{ISSUE}").exists()
    FileLock(tmp_path / f"lock-{REPO}-{ISSUE}").acquire(timeout=0.0)


def test_dry_run_prints_the_state_and_exits_without_dispatching(driver_factory, capsys):
    driver = driver_factory(
        [board(status="done", test_state="")],
        opts=DriveOptions(machine="precision", dry_run=True),
    )
    assert driver.run() == EXIT_OK
    out = capsys.readouterr().out
    assert "WORK_AID" in out
    body = out[out.index("{") :]
    parsed = json.loads(body[: body.rindex("}") + 1])
    assert parsed["WORK_AID"] == "w1"
    assert driver.recorded == []  # type: ignore[attr-defined]


def test_driver_reports_an_unconfigured_repo_as_a_usage_error(driver_factory):
    driver = driver_factory([board()])
    driver.repo = "not-a-repo"
    with pytest.raises(DriveError) as exc:
        driver.run()
    assert exc.value.exit_code == EXIT_USAGE


def test_driver_writes_the_per_issue_run_log(driver_factory, tmp_path):
    payload = board(status="done", test_state="")
    driver = driver_factory(
        [payload], opts=DriveOptions(machine="precision", skip_test=True, poll=1.0,
                                     deadline_mins=0.05)
    )
    driver.run()
    log = tmp_path / f"{REPO}-{ISSUE}.log"
    assert log.exists() and "ok" in log.read_text()


def test_driver_writes_a_start_marker_even_when_the_loop_never_spawns_anything(
    driver_factory, tmp_path
):
    """#1606: `decide()`'s very first branch after "merged" is a pure `WAIT`
    with no command whenever `state.active_count > 0` — the ordinary,
    majority-case shape of attaching to an issue that already has another
    assignment active (a review or merge dispatched via the TUI's
    interactive-agent flow, or a drive re-attached after a previous tmux
    session died mid-run). That loop never calls `_spawn` (the only other
    writer of the run log), so it must sit alive-but-log-silent for the
    whole `--poll` interval — UNLESS `Driver.run()` itself stamps a start
    marker the instant its per-issue lock is acquired, which is what
    `launch_drive_in_tmux`'s post-launch verification (~8s window) actually
    relies on to avoid killing this exact healthy session."""
    payload = board(status="dispatched")  # non-terminal → counted in `active`
    driver = driver_factory(
        [payload],
        opts=DriveOptions(machine="precision", poll=1.0, deadline_mins=0.001),
    )
    exit_code = driver.run()
    assert exit_code == EXIT_DEADLINE
    assert driver.recorded == []  # never spawned a `coord` subcommand
    log = tmp_path / f"{REPO}-{ISSUE}.log"
    assert log.exists()
    assert "drive loop started" in log.read_text()


def test_a_die_on_error_action_raises_a_drive_error(driver_factory, monkeypatch):
    driver = driver_factory([board(status="failed", failure_reason="boom")])

    def failing_run(argv, **kw):
        return subprocess.CompletedProcess(argv, 1, "", "nope")

    monkeypatch.setattr("coord.drive.subprocess.run", failing_run)
    with pytest.raises(DriveError) as exc:
        driver.run()
    assert exc.value.exit_code == EXIT_TERMINAL_FAILURE


# ═══════════════════════════════════════════════════════════════════════════
# #1844: a permanent pre-dispatch refusal is NOT a generic RUN-action
# failure. `coord assign`/`coord approve-plan`/`coord fix` exit
# EXIT_DISPATCH_REFUSED (not the generic 1) when `enforce_oracle_readiness`/
# `enforce_epic_dispatch_guard` refuse deterministically; `_loop`'s RUN
# handling must re-raise with THAT exit code, carrying the child's own
# captured output (the guard's remedy, verbatim) rather than a synthesised
# "coord ... exited 5" — that captured text is what `_drive_exit_summary`
# folds into the `drive_exited` audit row, which is the only thing
# `coord/drive_queue.py`'s tick can read once this process is gone.
# ═══════════════════════════════════════════════════════════════════════════


def test_a_dispatch_refusal_raises_a_drive_error_with_the_distinct_exit_code(
    driver_factory, monkeypatch,
):
    driver = driver_factory([board(status="failed", failure_reason="boom")])
    refusal_text = (
        "  dispatch failed: Issue #1817 is part of oracle-opted-in milestone "
        "ms-51 (Gate A satisfied) but has no acceptance slice yet — run "
        "`coord acceptance author claude-coordinator <tracking_issue> "
        "--issue 1817` first."
    )

    def refused_run(argv, **kw):
        return subprocess.CompletedProcess(argv, EXIT_DISPATCH_REFUSED, "", refusal_text)

    monkeypatch.setattr("coord.drive.subprocess.run", refused_run)
    with pytest.raises(DriveError) as exc:
        driver.run()
    # THE two things #1844 exists for: the exit code is distinguishable from
    # a crash (EXIT_TERMINAL_FAILURE), and the message is the guard's OWN
    # text — including its remedy — not a generic "exited 5".
    assert exc.value.exit_code == EXIT_DISPATCH_REFUSED
    assert exc.value.exit_code != EXIT_TERMINAL_FAILURE
    assert "acceptance author" in str(exc.value)
    assert refusal_text.strip() in str(exc.value)


def test_a_dispatch_refusals_reason_reaches_the_drive_exited_audit_row(
    driver_factory, monkeypatch, coord_db,
):
    """End-to-end through `Driver.run()`'s audit boundary (#1499): the
    `drive_exited` row's `details.exit_code` must be EXIT_DISPATCH_REFUSED —
    the ONE fact `coord/commands/drive_queue.py`'s `_fetch_exit_reasons`
    reads to tell a refusal apart from a genuine death — and its `summary`
    must carry the refusal's own text.
    """
    driver = driver_factory([board(status="failed", failure_reason="boom")])
    refusal_text = "refusing: no acceptance slice yet — run `coord acceptance author ...`"

    def refused_run(argv, **kw):
        return subprocess.CompletedProcess(argv, EXIT_DISPATCH_REFUSED, "", refusal_text)

    monkeypatch.setattr("coord.drive.subprocess.run", refused_run)
    with pytest.raises(DriveError):
        driver.run()

    rows = _drive_audit_rows(coord_db)
    assert [r["event_type"] for r in rows] == ["drive_started", "drive_exited"]
    details = json.loads(rows[1]["details_json"])
    assert details["exit_code"] == EXIT_DISPATCH_REFUSED
    assert refusal_text in rows[1]["summary"]


def test_a_warn_on_error_action_keeps_looping(driver_factory, monkeypatch, capsys):
    """A failing `coord merge` is "try again next poll", not an abort."""
    payload = board(status="done", test_state="passed")
    payload["assignments"].append(
        {
            "repo_name": REPO,
            "issue_number": ISSUE,
            "type": "review",
            "assignment_id": "r1",
            "dispatched_at": 2.0,
            "status": "done",
            "review_of_assignment_id": "w1",
            "review_verdict": "approve",
        }
    )
    driver = driver_factory(
        [payload],
        opts=DriveOptions(
            machine="precision", poll=1.0, max_merge_attempts=2, deadline_mins=1.0
        ),
    )
    monkeypatch.setattr(
        "coord.drive.subprocess.run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "queue empty"),
    )
    assert driver.run() == EXIT_TERMINAL_FAILURE
    assert "returned non-zero" in capsys.readouterr().err


def test_notify_is_off_by_default(driver_factory, monkeypatch):
    """Two drivers racing to dispatch is #476/#477 — --notify is opt-in."""
    driver = driver_factory([board(status="merged")])
    called: list[str] = []
    monkeypatch.setattr(driver, "run_coord", lambda *a, **k: called.append("x") or 0)
    driver.run_notify()
    assert called == []


def test_notify_nudges_coord_notify_under_the_shared_lock(driver_factory, tmp_path):
    driver = driver_factory(
        [board(status="merged")], opts=DriveOptions(machine="precision", notify=True)
    )
    seen: list[tuple] = []
    driver.run_coord = lambda args, **kw: seen.append(args) or 0  # type: ignore[assignment]
    driver.run_notify()
    assert seen == [("notify",)]


def test_stalled_stage_gets_re_nudged_on_every_stall_window(driver_factory, capsys):
    """#1593: a stage stuck at the same fingerprint (the worker is still
    running) must be nudged repeatedly, not just once. The old one-shot latch
    (``nudged = False``/``True``, cleared only on a fingerprint change) fired
    a single nudge near the start of the stall and then went silent for the
    rest of it — `coord notify` correctly finds nothing to settle while the
    worker is mid-run, and nothing else ever re-checks. Observed live as
    30-40 minute dead-air gaps at every stage boundary."""
    driver = driver_factory(
        [board(status="running")],
        opts=DriveOptions(
            machine="precision",
            poll=1.0,
            stall_mins=1.0 / 60.0,  # 1 "second" in the fake clock's units
            deadline_mins=8.0 / 60.0,  # 8 units — spans several stall windows
            notify=True,
        ),
    )
    notify_calls: list[tuple] = []
    driver.run_coord = lambda args, **kw: notify_calls.append(args) or 0  # type: ignore[assignment]

    assert driver.run() == EXIT_DEADLINE
    assert notify_calls.count(("notify",)) > 1, notify_calls

    err = capsys.readouterr().err
    assert err.count("no state change in") > 1, err


def test_smaller_stall_never_produces_fewer_nudges(driver_factory):
    """Regression pin for the #1593 inversion: a SMALLER ``--stall`` must
    never yield fewer nudges than a larger one over the same run. Under the
    one-shot latch, lowering ``--stall`` made things actively worse — the
    single available nudge fired earlier, while the worker was reliably still
    busy, guaranteeing no follow-up ever recorded the completion."""

    def nudge_count(stall_mins: float) -> int:
        driver = driver_factory(
            [board(status="running")],
            opts=DriveOptions(
                machine="precision",
                poll=1.0,
                stall_mins=stall_mins,
                deadline_mins=12.0 / 60.0,
                notify=True,
            ),
        )
        calls: list[tuple] = []
        driver.run_coord = lambda args, **kw: calls.append(args) or 0  # type: ignore[assignment]
        driver.run()
        return calls.count(("notify",))

    small = nudge_count(1.0 / 60.0)
    large = nudge_count(5.0 / 60.0)
    assert small >= large > 0, (small, large)


def test_the_config_path_is_threaded_onto_every_coord_subprocess(driver_factory):
    """A `coord drive --config X` run must not dispatch against a different
    config than it reads. The bash driver ran a bare `coord` and had this gap."""
    driver = driver_factory(
        [board(status="failed", failure_reason="boom")],
        opts=DriveOptions(
            machine="precision",
            poll=1.0,
            deadline_mins=0.05,
            config_path="/tmp/custom.yml",
        ),
    )
    driver.run()
    argvs = driver.recorded  # type: ignore[attr-defined]
    assert argvs, "expected at least one coord subprocess"
    for argv in argvs:
        assert argv[-2:] == ["--config", "/tmp/custom.yml"], argv


def test_no_config_flag_is_added_when_none_was_given(driver_factory):
    driver = driver_factory(
        [board(status="failed", failure_reason="boom")],
        opts=DriveOptions(machine="precision", poll=1.0, deadline_mins=0.05),
    )
    driver.run()
    for argv in driver.recorded:  # type: ignore[attr-defined]
        assert "--config" not in argv


def test_coord_argv_is_overridable_for_tests(monkeypatch):
    monkeypatch.setenv("COORD_DRIVE_COORD_BIN", "/x/coord --config /y")
    assert coord_argv() == ["/x/coord", "--config", "/y"]


def test_coord_argv_falls_back_to_the_module_when_not_on_path(monkeypatch):
    monkeypatch.delenv("COORD_DRIVE_COORD_BIN", raising=False)
    monkeypatch.setattr("coord.drive.shutil.which", lambda name: None)
    assert coord_argv()[-2:] == ["-m", "coord.cli"]


# ── #1809: the fallback must actually run, not just be shaped right ─────────
#
# The two tests above assert on coord_argv()'s RETURN VALUE only. Nothing
# ever executed the argv it returns — so `coord/cli.py` shipped with no
# `if __name__ == "__main__":` guard, meaning `python -m coord.cli <args>`
# silently imported the module (building every click.group/add_command) and
# exited 0 having run nothing and printed nothing. That import-only exit is
# exactly the path `coord_argv()` falls back to whenever `coord` isn't on
# PATH — a venv whose bin isn't exported, a systemd user unit, a
# non-interactive ssh session (#402) — so on those hosts every `coord`
# subprocess the driver or the drive queue spawned (`coord assign`, `coord
# drive --tmux`, ...) was a silent no-op that reported success. Both tests
# below run a real subprocess and assert on its OUTPUT, not just its exit
# code, because the broken path also exits 0 — a bare `returncode == 0`
# assertion would pass against the very bug this guards against.

_REPO_ROOT = Path(__file__).resolve().parent.parent
_VERSION_RE = re.compile(r"\d+\.\d+\.\d+")


def test_python_dash_m_coord_cli_prints_version_and_exits_0():
    """The direct acceptance check: ``python -m coord.cli --version`` must
    actually run ``main()``, not just import the module and exit."""
    result = subprocess.run(
        [sys.executable, "-m", "coord.cli", "--version"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert _VERSION_RE.search(result.stdout), (
        f"expected a version string on stdout, got: {result.stdout!r} "
        f"(stderr={result.stderr!r})"
    )


def test_coord_argv_fallback_argv_is_actually_executable(monkeypatch):
    """The direct regression guard: with ``coord`` scrubbed from PATH (the
    #402 scenario), the argv ``coord_argv()`` hands to every driver/queue
    subprocess call must run a real command — invoked here exactly as
    ``Driver``/``launch_drive_in_tmux``/the drive queue invoke it (argv +
    extra args, no shell)."""
    monkeypatch.delenv("COORD_DRIVE_COORD_BIN", raising=False)
    monkeypatch.setattr("coord.drive.shutil.which", lambda name: None)
    argv = coord_argv()
    assert argv[-2:] == ["-m", "coord.cli"]

    result = subprocess.run(
        [*argv, "--version"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert _VERSION_RE.search(result.stdout), (
        f"expected a version string on stdout, got: {result.stdout!r} "
        f"(stderr={result.stderr!r})"
    )
