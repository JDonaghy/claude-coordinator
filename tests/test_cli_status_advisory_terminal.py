"""#1472: `coord status`'s advisory block ("needs attention — worker exited
cleanly with 0 commits") is sourced from each agent's own completed-
assignment map (`/status`'s `completed` list), which the agent only prunes
by *count* (`_COMPLETED_HISTORY_CAP`), never by GitHub outcome. So once an
advisory entry's issue closes or its branch merges — including the common
case where a human rescues and merges the work by hand — the CLI kept
re-serving the same "The work is UNVERIFIED — review it before testing or
merging." warning forever, even though the work is long since done.

Fix: filter each advisory entry through the shared #522 chokepoint guard
(`github_ops.work_is_terminal`) before rendering, exactly like every other
terminal-state check in this codebase. Fail-open (a lookup failure keeps the
entry visible) and cached per invocation.
"""

from __future__ import annotations

import coord.network as network_mod
from click.testing import CliRunner

from coord import github_ops
from coord.commands.status import status as status_cmd
from coord.network import MachineStatus, StatusResult

# Captured at import time — the real function, immune to the conftest
# autouse `_non_terminal_work` stub which reassigns the module attribute to
# always return False for every other test in the suite.
_REAL_WORK_IS_TERMINAL = github_ops.work_is_terminal


def _advisory_status_payload(
    *, issue_number: int = 1472, branch: str = "issue-1472-fix",
    assignment_id: str = "adv-1",
) -> dict:
    return {
        "active": [],
        "completed": [
            {
                "id": assignment_id,
                "status": "advisory",
                "branch": branch,
                "finished_at": 100.0,
                "zero_commit_reason": "worker exited cleanly but pushed 0 commits",
                "spec": {
                    "repo_name": "api",
                    "issue_number": issue_number,
                    "issue_title": "Some fixed issue",
                },
            }
        ],
    }


def _run_status(valid_config_path, monkeypatch, *, payload: dict) -> str:
    # One online machine ("laptop", per VALID_CONFIG) whose /status reports
    # the advisory entry — exercises the real code path that builds
    # `agent_completed` rather than seeding the board directly.
    def _fake_check_all(machines, timeout=3.0, **kw):
        found = next((m for m in machines if m.name == "laptop"), None)
        assert found is not None
        return [MachineStatus(machine=found, state="online", latency_ms=1.0)]

    monkeypatch.setattr(network_mod, "check_all", _fake_check_all)
    monkeypatch.setattr(
        network_mod, "fetch_status", lambda *a, **k: StatusResult(data=payload)
    )

    runner = CliRunner()
    result = runner.invoke(
        status_cmd,
        ["--config", str(valid_config_path), "--no-reconcile"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    return result.output


def test_advisory_hidden_when_issue_already_closed(
    valid_config_path, monkeypatch, coord_db,
) -> None:
    """The #1472 case: the advisory's issue is closed on GitHub (rescued and
    merged out of band) — the stale "UNVERIFIED" nag must not render."""
    monkeypatch.setattr(github_ops, "work_is_terminal", lambda *a, **k: True)

    output = _run_status(
        valid_config_path, monkeypatch, payload=_advisory_status_payload()
    )

    assert "Advisory" not in output, output
    assert "UNVERIFIED" not in output, output


def test_advisory_shown_when_work_still_live(
    valid_config_path, monkeypatch, coord_db,
) -> None:
    """Sanity check: a genuine 0-commit advisory whose issue is still open
    and branch unmerged must keep showing — the fix must not blanket-hide
    real advisories (the autouse fixture already stubs work_is_terminal to
    False; asserted explicitly here for clarity)."""
    monkeypatch.setattr(github_ops, "work_is_terminal", lambda *a, **k: False)

    output = _run_status(
        valid_config_path, monkeypatch, payload=_advisory_status_payload()
    )

    assert "Advisory (needs attention" in output, output
    assert "#1472: Some fixed issue [api]" in output, output


def test_advisory_terminal_check_is_cached_per_invocation(
    valid_config_path, monkeypatch, coord_db,
) -> None:
    """#1472: two advisory entries sharing the same (repo, issue, branch) —
    e.g. a rework that retried on the same branch — must cost exactly one
    ``gh`` round-trip, not two. Restores the REAL ``work_is_terminal`` (the
    conftest autouse fixture stubs it to always-False) so the ``cache=``
    plumbing between ``_live_advisory_entries`` and
    ``github_ops.work_is_terminal`` is exercised end to end.
    """
    monkeypatch.setattr(github_ops, "work_is_terminal", _REAL_WORK_IS_TERMINAL)

    calls = []

    def _fake_issue_is_closed(repo, issue_number):
        calls.append((repo, issue_number))
        return False

    monkeypatch.setattr(github_ops, "issue_is_closed", _fake_issue_is_closed)
    monkeypatch.setattr(github_ops, "pr_is_merged", lambda *a, **k: False)

    payload = _advisory_status_payload()
    # Duplicate the single advisory entry under a different assignment id,
    # same (repo, issue, branch) — the shape a same-branch rework leaves.
    dup = dict(payload["completed"][0])
    dup["id"] = "adv-2"
    payload["completed"].append(dup)

    output = _run_status(valid_config_path, monkeypatch, payload=payload)

    assert output.count("#1472: Some fixed issue [api]") == 2, output
    assert len(calls) == 1, calls
