"""#2237 item 5: an ABSENT graph self-heals, not only a stale one.

#1729 shipped the agent-side self-heal and it works — for *drift*. A graph
that is simply **gone** was a different classification and nothing rebuilt it:

* ``graph_status`` returns early when there is no ``graphify-out/graph.json``
  (``unknown_reason`` set, ``stale`` never computed), so the self-heal's
  ``if not values.get("stale"): continue`` skipped it forever, and
* graphify's own hooks open with ``[ ! -f graphify-out/graph.json ] && exit 0``,
  which ``coord.graph_health``'s module docstring already calls "a permanent
  off-switch once the graph is purged".

Two independent mechanisms declining to rebuild a graph that is not there made
one ``rm -rf graphify-out/`` — or simply a fresh clone on a machine that never
had one — permanent until a human noticed. This suite is the acceptance test
for closing that: **``rm -rf graphify-out/`` on a healthy checkout is repaired
automatically, with no operator running anything.**

The guard that makes it safe gets equal billing: a repo where the build
genuinely cannot succeed must get exactly ONE attempt per HEAD, not one per
health poll. That is #1729's guard 3, and it works on a never-built checkout
only because ``graph_status`` now reports ``head_sha`` even with no graph on
disk (there is nothing else to key on).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from coord.agent import RUNNING
from coord.graph_health import graph_status

from tests.test_agent_graph_self_heal import (
    _git,
    _graph_result,
    _init_repo,
    _server,
    _spec,
    _write_graph,
)


def _mark_running(server, repo: Path) -> None:
    """Park a RUNNING assignment on *server* — guard 1's trigger."""
    from coord.agent import AgentAssignment  # noqa: PLC0415

    with server._lock:
        server._assignments["fake-running"] = AgentAssignment(
            id="fake-running", spec=_spec(repo), status=RUNNING
        )


# ── the classification boundary that disabled the heal ───────────────────────


def test_absent_graph_still_reports_head_sha(tmp_path: Path) -> None:
    """A checkout with no graph knows perfectly well what HEAD it is on.

    Returning ``head_sha=None`` here is what made the absent case unhealable:
    guard 3 keys its once-per-HEAD bookkeeping on that sha, so with no sha the
    only two options are "retry every poll forever" (the 2026-08-02 shape) or
    "never try at all" (the shape this issue is about).
    """
    repo = _init_repo(tmp_path / "repo")
    st = graph_status(repo)

    assert st.present is False
    assert st.unknown_reason and "never built here" in st.unknown_reason
    assert st.head_sha == _git(repo, "rev-parse", "HEAD")
    # An absent graph is not "stale" — that distinction is deliberate and the
    # self-heal now keys on `present` instead of routing everything through it.
    assert st.stale is False


# ── the acceptance test: rm -rf graphify-out/ repairs itself ─────────────────


def test_purged_graph_is_rebuilt_without_an_operator(tmp_path: Path, monkeypatch) -> None:
    """#2237's headline acceptance bullet."""
    repo = _init_repo(tmp_path / "repo")
    _write_graph(repo, built_sha=_git(repo, "rev-parse", "HEAD"))
    server = _server(tmp_path, repo)

    # Healthy to start with: nothing to heal.
    calls: list[Path] = []

    def _fake_update(repo_path: Path):
        calls.append(repo_path)
        _write_graph(repo_path, built_sha=_git(repo_path, "rev-parse", "HEAD"))
        return True, "rebuilt"

    monkeypatch.setattr("coord.agent._graphify_update", _fake_update)
    assert _graph_result(server)["severity"] == "ok"
    assert calls == []

    # The off-switch: purge the graph the way an operator (or a botched
    # worktree reap) would.
    shutil.rmtree(repo / "graphify-out")
    server._local_health_cache = None

    result = _graph_result(server)

    assert calls == [repo], "an absent graph must trigger the same rebuild a stale one does"
    assert result["severity"] == "ok"
    assert result["values"]["present"] is True


def test_never_built_checkout_is_built_on_the_first_health_tick(
    tmp_path: Path, monkeypatch
) -> None:
    """The fresh-clone case — a machine that never had a graph at all, which
    is how coord-portal and stick-demo got where they are."""
    repo = _init_repo(tmp_path / "repo")
    server = _server(tmp_path, repo)

    calls: list[Path] = []

    def _fake_update(repo_path: Path):
        calls.append(repo_path)
        _write_graph(repo_path, built_sha=_git(repo_path, "rev-parse", "HEAD"))
        return True, "built"

    monkeypatch.setattr("coord.agent._graphify_update", _fake_update)

    assert _graph_result(server)["values"]["present"] is True
    assert calls == [repo]


# ── guard 3 still holds for a build that cannot succeed ──────────────────────


def test_unbuildable_checkout_is_attempted_once_per_head(tmp_path: Path, monkeypatch) -> None:
    """No graph AND no way to build one (no graphify on PATH, a refusal, ...)
    must not turn the health tick into a rebuild loop."""
    repo = _init_repo(tmp_path / "repo")
    server = _server(tmp_path, repo)

    calls: list[Path] = []

    def _failing_update(repo_path: Path):
        calls.append(repo_path)
        return False, "graphify: command not found on this machine's PATH"

    monkeypatch.setattr("coord.agent._graphify_update", _failing_update)

    first = _graph_result(server)
    server._local_health_cache = None
    second = _graph_result(server)

    assert len(calls) == 1, "guard 3: one attempt per HEAD, never a retry loop"
    # Guard 4: every poll keeps saying WHY, not just the one that tried.
    for result in (first, second):
        assert result["severity"] == "warn"
        assert "command not found" in (result["values"]["self_heal_failed_reason"] or "")

    # HEAD moves -> one fresh attempt, because the reason may have moved too.
    (repo / "file2").write_text("x\n")
    subprocess.run(["git", "add", "file2"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "second"], cwd=str(repo), check=True, capture_output=True
    )
    server._local_health_cache = None
    _graph_result(server)
    assert len(calls) == 2


def test_absent_graph_on_a_busy_machine_is_not_rebuilt(tmp_path: Path, monkeypatch) -> None:
    """Guard 1 is unchanged by #2237: a RUNNING assignment still wins."""
    repo = _init_repo(tmp_path / "repo")
    server = _server(tmp_path, repo)

    calls: list[Path] = []
    monkeypatch.setattr(
        "coord.agent._graphify_update", lambda p: (calls.append(p), (True, "ok"))[1]
    )

    _mark_running(server, repo)

    assert _graph_result(server)["values"]["present"] is False
    assert calls == []


# ── item 7: the idle-gate's cost is measured, not inferred ──────────────────


def test_health_reports_how_often_the_idle_gate_skipped_the_heal(
    tmp_path: Path, monkeypatch
) -> None:
    """#2237 item 7. dellserver runs the drive-queue tick and is the fleet's
    busiest host, so the machine most likely to drift is the least likely to
    get a heal window — and until this counter existed, "never idle enough to
    heal" was indistinguishable from "never needed a heal". Measure first;
    only then decide whether the guard should become a heal window."""
    repo = _init_repo(tmp_path / "repo")
    _write_graph(repo, built_sha=_git(repo, "rev-parse", "HEAD"))
    server = _server(tmp_path, repo)
    monkeypatch.setattr("coord.agent._graphify_update", lambda p: (True, "ok"))

    health = server.health()
    assert health["graph_self_heal"]["passes"] == 1
    assert health["graph_self_heal"]["skipped_active"] == 0
    assert health["graph_self_heal"]["last_skip_at"] is None

    _mark_running(server, repo)
    server._local_health_cache = None

    health = server.health()
    assert health["graph_self_heal"]["skipped_active"] == 1
    assert health["graph_self_heal"]["last_skip_at"] is not None
    assert health["graph_self_heal"]["passes"] == 1, "a skipped pass is not a pass"
