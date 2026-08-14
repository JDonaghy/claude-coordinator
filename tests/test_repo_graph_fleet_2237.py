"""#2237 items 1-4 + 6: layer 5 goes fleet-wide, gains a repair, and stops
grading everything WARN.

#2220 shipped a five-layer ``coord repo doctor``. Four of its layers read live
FLEET state — layer 2 probes each agent's ``/health`` rather than trusting
config, which is the whole thesis of that issue. Layer 5 alone stat'd the
local disk, which inverted the blind spot it exists to close: **workers run on
dellserver and precision; the operator runs ``repo doctor`` on elitebook.** A
repo with a graph here and none there reported ``✓ 2 check(s) passed``.

Covered here:

* per-machine graph readiness, folded from each agent's existing ``/health``
  (no extra round trip — layer 2 already fetched it);
* the black-box acceptance case: **a repo missing the graph on ONE machine
  only is a finding**, which today's local-only probe structurally cannot see;
* severity, revisited (item 4): one machine missing its graph is WARN; NO
  machine that runs workers having a graph is CRIT and gates, because that is
  a repo where the graph-first rule in every worker prompt cannot be obeyed by
  anyone;
* unprobed machines prove nothing — never CRIT, never OK;
* ``--fix``: the machine-local half (build + ``core.hooksPath``) on every
  machine, refusing when the versioned ``.githooks/`` were never ported;
* a machine with no ``graphify`` CLI produces ONE finding (item 6) instead of
  a silent per-HEAD failure record.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

import coord.network as network_mod
from coord import repo_onboard as ro
from coord.commands.repo import repo_doctor
from coord.graph_health import apply_local_graph_fix
from coord.network import OFFLINE, ONLINE, MachineStatus

from tests.test_cli_repo_2220 import (  # noqa: F401 — fixtures used by name
    CONFIG,
    PR_WORKFLOW,
    _isolated_home,
    _stub_github,
    config_path,
)


def _graph_result(repo: str, path: str, **values) -> dict:
    """One H-1 ``graph`` check result as it arrives inside ``/health``."""
    base = {
        "path": path,
        "present": True,
        "stale": False,
        "hooks_ok": True,
        "hooks_detail": "core.hooksPath=.githooks",
        "hooks_shipped": True,
        "unknown_reason": None,
    }
    base.update(values)
    return {
        "check_id": "graph",
        "scope": "checkout",
        "subject": repo,
        "severity": "ok",
        "headroom": "in sync",
        "values": base,
    }


def _status(machine, *, graph=None, graphify_cli=True, online=True):
    if not online:
        return MachineStatus(machine=machine, state=OFFLINE, reason="connection refused")
    results = []
    if graph is not None:
        results.append(graph)
    if graphify_cli is not None:
        results.append({
            "check_id": "graphify_cli",
            "scope": "machine",
            "severity": "ok" if graphify_cli else "warn",
            "values": {"installed": graphify_cli, "path": "/usr/bin/graphify"},
        })
    return MachineStatus(
        machine=machine, state=ONLINE, latency_ms=2.0,
        health={
            "machine": machine.name,
            "capabilities": list(machine.capabilities),
            "repos": ["api"],
            "degraded": {},
            "health": {"schema": 1, "results": results},
        },
    )


def _machines(config_path):
    from coord.config import load

    return load(config_path).machines


def _checks(report) -> set[str]:
    return {f.check for f in report.findings}


class TestFleetWideProbe:
    def test_missing_on_one_machine_only_is_a_finding(self, config_path):
        """The black-box acceptance case. Local-only probing cannot see this:
        run on the machine that HAS the graph, everything looks green; run on
        the one that doesn't, and the machine that is fine looks broken."""
        from coord.config import load

        cfg = load(config_path)
        laptop, dellserver = cfg.machines
        statuses = [
            _status(laptop, graph=_graph_result("api", "/home/u/src/api")),
            _status(
                dellserver,
                graph=_graph_result(
                    "api", "/home/u/src/api", present=False, severity="warn",
                    unknown_reason="no graphify-out/graph.json (graph never built here)",
                ),
            ),
        ]
        facts = ro.gather_facts(cfg, "api", statuses=statuses, probe_github=False)
        report = ro.evaluate(facts)

        summaries = " ".join(f.summary for f in report.findings if f.layer == "graph")
        assert "graph.machine_not_built" in _checks(report)
        assert "dellserver" in summaries
        # ...and the healthy machine is reported healthy, not dragged in.
        assert "graph.machine_fresh" in _checks(report)
        # One machine missing its graph is residue, not a gate (#2237 item 4):
        # the agent's own self-heal is already rebuilding it.
        assert report.ok

    def test_no_graph_on_any_worker_machine_is_crit(self, config_path):
        """Item 4's other half — the state coord-portal and stick-demo sat in
        for weeks while `crit=0 ... ok=true` reported them fine."""
        from coord.config import load

        cfg = load(config_path)
        statuses = [
            _status(
                m,
                graph=_graph_result(
                    "api", "/home/u/src/api", present=False,
                    unknown_reason="no graphify-out/graph.json (graph never built here)",
                ),
            )
            for m in cfg.machines
        ]
        facts = ro.gather_facts(cfg, "api", statuses=statuses, probe_github=False)
        report = ro.evaluate(facts)

        assert "graph.fleet_not_built" in _checks(report)
        assert not report.ok, "a repo no worker can query the graph for must gate"
        crit = next(f for f in report.findings if f.check == "graph.fleet_not_built")
        assert "laptop" in crit.summary and "dellserver" in crit.summary

    def test_unreachable_machines_are_unknown_never_crit(self, config_path):
        """An offline agent is not evidence of a missing graph. #1525's rule:
        a probe that could not run must never render as the defect."""
        from coord.config import load

        cfg = load(config_path)
        statuses = [_status(m, online=False) for m in cfg.machines]
        facts = ro.gather_facts(cfg, "api", statuses=statuses, probe_github=False)
        report = ro.evaluate(facts)

        assert "graph.fleet_not_probed" in _checks(report)
        assert "graph.fleet_not_built" not in _checks(report)
        assert report.ok

    def test_agent_without_a_health_block_is_unknown_not_missing(self, config_path):
        """An agent too old to publish H-1 results is an unknown, and says so
        in a way that names the fix (update it)."""
        from coord.config import load

        cfg = load(config_path)
        laptop = cfg.machines[0]
        st = MachineStatus(
            machine=laptop, state=ONLINE, latency_ms=1.0,
            health={"machine": laptop.name, "repos": ["api"], "degraded": {}},
        )
        facts = ro.gather_facts(cfg, "api", statuses=[st], probe_github=False)
        report = ro.evaluate(facts)

        f = next(f for f in report.findings if f.check == "graph.machine_not_probed")
        assert "predates" in f.summary or "no graph check" in f.summary
        assert report.ok

    def test_missing_graphify_cli_is_one_finding_for_the_machine(self, config_path):
        """Item 6: without the CLI, every graph operation on that machine
        fails one-by-one for a reason only visible in a per-HEAD failure
        record. One finding, named, with the install command."""
        from coord.config import load

        cfg = load(config_path)
        laptop, dellserver = cfg.machines
        statuses = [
            _status(laptop, graph=_graph_result("api", "/home/u/src/api")),
            _status(
                dellserver,
                graph=_graph_result("api", "/home/u/src/api", present=False),
                graphify_cli=False,
            ),
        ]
        facts = ro.gather_facts(cfg, "api", statuses=statuses, probe_github=False)
        report = ro.evaluate(facts)

        matches = [f for f in report.findings if f.check == "graph.machine_no_graphify_cli"]
        assert len(matches) == 1
        assert "dellserver" in matches[0].summary
        assert "pipx install graphify" in (matches[0].fix or "")

    def test_unported_hooks_are_reported_once_not_per_machine(self, config_path):
        """`.githooks/` is VERSIONED — identical on every machine, and fixed by
        a PR, not by N per-box commands. Reporting it per machine would teach
        an operator to run something that cannot help."""
        from coord.config import load

        cfg = load(config_path)
        statuses = [
            _status(
                m,
                graph=_graph_result(
                    "api", "/home/u/src/api", hooks_ok=False, hooks_shipped=False,
                    hooks_detail="no .githooks/post-checkout in this repo",
                ),
            )
            for m in cfg.machines
        ]
        facts = ro.gather_facts(cfg, "api", statuses=statuses, probe_github=False)
        report = ro.evaluate(facts)

        ported = [f for f in report.findings if f.check == "graph.hooks_not_ported"]
        assert len(ported) == 1
        assert "port .githooks/" in (ported[0].fix or "")
        assert "graph.machine_hooks_missing" not in _checks(report)

    def test_an_agent_too_old_to_answer_gets_no_vote_on_the_hooks(self, config_path):
        """`hooks_shipped` is new in #2237. An agent that does not publish it
        has said nothing — and counting silence as either answer would be
        inventing evidence (#1525's rule). With no votes the finding is not
        asserted from the fleet at all."""
        from coord.config import load

        cfg = load(config_path)
        statuses = []
        for m in cfg.machines:
            graph = _graph_result("api", "/home/u/src/api", hooks_ok=False,
                                  hooks_detail="core.hooksPath is unset")
            del graph["values"]["hooks_shipped"]  # pre-#2237 agent
            statuses.append(_status(m, graph=graph))

        facts = ro.gather_facts(cfg, "api", statuses=statuses, probe_github=False)
        report = ro.evaluate(facts)

        assert "graph.hooks_not_ported" not in _checks(report)
        # ...and the machine-local reading is still offered, since that is the
        # one an operator can act on without knowing which failure it is.
        assert "graph.machine_hooks_missing" in _checks(report)

    def test_hooks_unset_on_one_machine_is_that_machine_s_problem(self, config_path):
        """The other side of the split: the repo ships the hooks, one machine
        never ran the `git config`. That one IS automatable, and --fix does it."""
        from coord.config import load

        cfg = load(config_path)
        laptop, dellserver = cfg.machines
        statuses = [
            _status(laptop, graph=_graph_result("api", "/home/u/src/api")),
            _status(
                dellserver,
                graph=_graph_result(
                    "api", "/home/u/src/api", hooks_ok=False, hooks_shipped=True,
                    hooks_detail="core.hooksPath is unset",
                ),
            ),
        ]
        facts = ro.gather_facts(cfg, "api", statuses=statuses, probe_github=False)
        report = ro.evaluate(facts)

        f = next(f for f in report.findings if f.check == "graph.machine_hooks_missing")
        assert "dellserver" in f.summary
        assert "--fix" in (f.fix or "")


class TestFleetReportFoldIn:
    """#2237 item 4's other consequence: `coord doctor` folds repo doctor in,
    so a WARN-forever layer 5 never escalated there either — which is *why*
    two repos ran for weeks with no graph and nothing said so out loud."""

    def test_a_graph_blind_repo_reaches_the_fleet_report(self, config_path):
        from coord.config import load

        cfg = load(config_path)
        statuses = [
            _status(m, graph=_graph_result("api", "/home/u/src/api", present=False))
            for m in cfg.machines
        ]
        facts = ro.gather_facts(cfg, "api", statuses=statuses, probe_github=False)
        lines = ro.doctor_summary_lines(ro.evaluate(facts))

        assert any("graph.fleet_not_built" in line for _, line in lines)
        assert all(is_problem for is_problem, _ in lines)

    def test_one_machine_missing_its_graph_stays_out_of_the_fleet_report(
        self, config_path
    ):
        """A report that is always red is a report nobody reads. The self-heal
        is already rebuilding this one."""
        from coord.config import load

        cfg = load(config_path)
        laptop, dellserver = cfg.machines
        statuses = [
            _status(laptop, graph=_graph_result("api", "/home/u/src/api")),
            _status(
                dellserver,
                graph=_graph_result("api", "/home/u/src/api", present=False),
            ),
        ]
        facts = ro.gather_facts(cfg, "api", statuses=statuses, probe_github=False)

        assert ro.doctor_summary_lines(ro.evaluate(facts)) == []


# ── the machine-local repair itself ─────────────────────────────────────────


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t.com"], cwd=str(path), check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.name", "T"], cwd=str(path), check=True, capture_output=True
    )
    (path / "README").write_text("x\n")
    subprocess.run(["git", "add", "README"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "i"], cwd=str(path), check=True, capture_output=True)
    return path


def _port_hooks(repo: Path) -> None:
    hooks = repo / ".githooks"
    hooks.mkdir(exist_ok=True)
    (hooks / "post-checkout").write_text("#!/bin/sh\nexit 0\n")


class TestApplyLocalGraphFix:
    def test_refuses_when_the_repo_never_ported_the_hooks(self, tmp_path: Path):
        """A graph nothing will ever refresh is worse than an obviously
        missing one — it LOOKS fixed. And `git config core.hooksPath` at a
        directory that does not exist disables every hook in the checkout."""
        repo = _init_repo(tmp_path / "repo")
        result = apply_local_graph_fix(repo)

        assert result.refused and "post-checkout" in result.refused
        assert result.steps == []
        assert not result.ok
        # Nothing was touched.
        assert (
            subprocess.run(
                ["git", "config", "--get", "core.hooksPath"],
                cwd=str(repo), capture_output=True, text=True,
            ).stdout.strip()
            == ""
        )

    def test_sets_hooks_path_and_builds(self, tmp_path: Path, monkeypatch):
        repo = _init_repo(tmp_path / "repo")
        _port_hooks(repo)

        built: list[Path] = []

        def _fake_update(repo_path, *, timeout=600.0):
            built.append(repo_path)
            out = repo_path / "graphify-out"
            out.mkdir(exist_ok=True)
            (out / "graph.json").write_text("{}")
            (out / "GRAPH_REPORT.md").write_text("- Built from commit: `deadbeef`\n")
            return True, "built"

        monkeypatch.setattr("coord.graph_health.run_graphify_update", _fake_update)
        result = apply_local_graph_fix(repo)

        assert result.ok and result.changed, result.to_dict()
        assert built == [repo]
        assert (
            subprocess.run(
                ["git", "config", "--get", "core.hooksPath"],
                cwd=str(repo), capture_output=True, text=True,
            ).stdout.strip()
            == ".githooks"
        )

    def test_is_idempotent(self, tmp_path: Path, monkeypatch):
        """Re-running must be a no-op, not a rebuild — this is what makes it
        safe to fan out across every machine on every doctor run."""
        repo = _init_repo(tmp_path / "repo")
        _port_hooks(repo)
        out = repo / "graphify-out"
        out.mkdir()
        (out / "graph.json").write_text("{}")
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True
        ).stdout.strip()
        (out / "GRAPH_REPORT.md").write_text(f"- Built from commit: `{head}`\n")

        built: list[Path] = []
        monkeypatch.setattr(
            "coord.graph_health.run_graphify_update",
            lambda p, timeout=600.0: (built.append(p), (True, "built"))[1],
        )

        first = apply_local_graph_fix(repo)
        second = apply_local_graph_fix(repo)

        assert first.ok and second.ok
        assert built == [], "a current graph must never be rebuilt by --fix"
        assert second.changed is False

    def test_never_hijacks_a_hooks_path_someone_else_set(self, tmp_path: Path, monkeypatch):
        repo = _init_repo(tmp_path / "repo")
        _port_hooks(repo)
        subprocess.run(
            ["git", "config", "core.hooksPath", ".other-hooks"],
            cwd=str(repo), check=True, capture_output=True,
        )
        monkeypatch.setattr(
            "coord.graph_health.run_graphify_update", lambda p, timeout=600.0: (True, "built")
        )

        result = apply_local_graph_fix(repo)
        step = next(s for s in result.steps if s.action == "hooks_path")

        assert step.ok is False and step.changed is False
        assert ".other-hooks" in step.detail
        assert (
            subprocess.run(
                ["git", "config", "--get", "core.hooksPath"],
                cwd=str(repo), capture_output=True, text=True,
            ).stdout.strip()
            == ".other-hooks"
        )


# ── `coord repo doctor --fix`, end to end ───────────────────────────────────


class TestRepoDoctorFix:
    def test_fix_posts_to_every_machine_and_reports_per_machine(
        self, config_path, monkeypatch
    ):
        """The point of routing through the agents: the machine running the
        command is the one that matters LEAST."""
        cfg_machines = _machines(config_path)
        monkeypatch.setattr(
            network_mod, "check_all",
            lambda *a, **k: [
                _status(m, graph=_graph_result("api", "/home/u/src/api"))
                for m in cfg_machines
            ],
        )

        posted: list[tuple[str, dict]] = []

        class _Resp:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "repo": "api",
                    "repo_path": "/home/u/src/api",
                    "refused": None,
                    "ok": True,
                    "changed": True,
                    "steps": [
                        {"action": "hooks_path", "ok": True, "changed": True,
                         "detail": "set core.hooksPath=.githooks"},
                        {"action": "build", "ok": True, "changed": True,
                         "detail": "ran graphify update ."},
                    ],
                }

        def _post(url, json=None, timeout=None):
            posted.append((url, json))
            return _Resp()

        import httpx

        monkeypatch.setattr(httpx, "post", _post)

        result = CliRunner().invoke(
            repo_doctor,
            ["api", "--config", str(config_path), "--fix", "--no-github"],
            catch_exceptions=False,
        )

        assert [u for u, _ in posted] == [
            "http://laptop.tailnet:7433/graph-fix",
            "http://dellserver.tailnet:7433/graph-fix",
        ], result.output
        assert all(body["repo"] == "api" for _, body in posted)
        assert "set core.hooksPath=.githooks" in result.output
        assert result.output.count("ran graphify update .") == 2

    def test_fix_reports_a_refusal_as_remaining_work(self, config_path, monkeypatch):
        """"Refuses (and says so) when the versioned hooks are missing" —
        a refusal must never render as a success."""
        cfg_machines = _machines(config_path)
        monkeypatch.setattr(
            network_mod, "check_all",
            lambda *a, **k: [
                _status(m, graph=_graph_result("api", "/home/u/src/api"))
                for m in cfg_machines
            ],
        )

        class _Resp:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "repo": "api",
                    "repo_path": "/home/u/src/api",
                    "refused": "repo does not ship .githooks/post-checkout — port the hooks first",
                    "ok": False,
                    "changed": False,
                    "steps": [],
                }

        import httpx

        monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp())

        result = CliRunner().invoke(
            repo_doctor,
            ["api", "--config", str(config_path), "--fix", "--no-github"],
            catch_exceptions=False,
        )

        assert "refused" in result.output
        assert "port the hooks first" in result.output

    def test_unreachable_machine_is_skipped_loudly(self, config_path, monkeypatch):
        """An agent we could not reach is NOT known to be fine — saying
        nothing about it is how a repair silently covers half a fleet."""
        cfg_machines = _machines(config_path)
        monkeypatch.setattr(
            network_mod, "check_all",
            lambda *a, **k: [
                _status(cfg_machines[0], graph=_graph_result("api", "/home/u/src/api")),
                _status(cfg_machines[1], online=False),
            ],
        )

        import httpx

        posted: list[str] = []

        class _Resp:
            def raise_for_status(self):
                return None

            def json(self):
                return {"repo": "api", "repo_path": "", "refused": None,
                        "ok": True, "changed": False, "steps": []}

        monkeypatch.setattr(
            httpx, "post", lambda url, **k: (posted.append(url), _Resp())[1]
        )

        result = CliRunner().invoke(
            repo_doctor,
            ["api", "--config", str(config_path), "--fix", "--no-github"],
            catch_exceptions=False,
        )

        assert posted == ["http://laptop.tailnet:7433/graph-fix"]
        assert "dellserver" in result.output
        assert "not reachable" in result.output


# ── the agent endpoint ──────────────────────────────────────────────────────


class TestAgentGraphFixEndpoint:
    @pytest.fixture
    def server(self, tmp_path):
        from coord.agent import AgentServer

        repo = _init_repo(tmp_path / "repo")
        _port_hooks(repo)
        return AgentServer(
            machine_name="test",
            capabilities=["python"],
            repos=["api"],
            state_dir=tmp_path / "state",
            worker_command=lambda spec: ["/bin/true"],
            repo_paths={"api": str(repo)},
        ), repo

    def test_fix_graph_builds_and_wires_the_checkout(self, server, monkeypatch):
        agent, repo = server
        monkeypatch.setattr(
            "coord.graph_health.run_graphify_update", lambda p, timeout=600.0: (True, "built")
        )

        result = agent.fix_graph("api")

        assert result["repo"] == "api"
        assert result["ok"] is True
        actions = {s["action"] for s in result["steps"]}
        assert actions == {"hooks_path", "build"}

    def test_unknown_repo_is_a_refusal_not_an_exception(self, server):
        """The caller is sweeping every machine — one bad answer must not
        take the other machines' answers down with it."""
        agent, _ = server
        result = agent.fix_graph("nope")

        assert result["refused"] and "no repo_path" in result["refused"]
        assert result["ok"] is False
