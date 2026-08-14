"""Context building and the whole-registry cost budget (#1628).

The budget matters because this eventually runs on a timer on every agent: a
health check that costs a second of CPU and a network round trip every minute
is itself a fleet-degradation signal.  The issue's rule — "cheap and
non-destructive, whole registry under ~2s, no network beyond the one PyPI
index fetch, no writes" — is asserted here rather than left as a comment.
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from coord.config import HealthConfig
from coord.health import registry
from coord.health.context import build_context, local_checkouts
from coord.health.models import HealthContext


def _repo(name: str, **kwargs) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        default_branch=kwargs.pop("default_branch", "main"),
        develop_branch=kwargs.pop("develop_branch", None),
    )


def _machine(name: str, host: str, repo_paths: dict[str, str]) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        host=host,
        repos=list(repo_paths),
        repo_paths=repo_paths,
        repo_path=lambda rn, _p=repo_paths: _p.get(rn),
    )


def _git_checkout(root: Path, name: str) -> Path:
    path = root / name
    (path / ".git").mkdir(parents=True)
    return path


# ── local_checkouts ──────────────────────────────────────────────────────────


def test_local_checkouts_without_a_config_is_empty() -> None:
    assert local_checkouts(None) == ()


def test_local_checkouts_finds_this_machines_repos(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("coord.health.context.socket.gethostname", lambda: "laptop.local")
    path = _git_checkout(tmp_path, "api")
    config = SimpleNamespace(
        repos=[_repo("api")],
        machines=[_machine("laptop", "laptop.ts.net", {"api": str(path)})],
    )
    (checkout,) = local_checkouts(config)
    assert checkout.name == "api"
    assert checkout.path == path
    assert checkout.default_branch == "main"


def test_local_checkouts_prefers_the_hostname_matched_machine(tmp_path, monkeypatch) -> None:
    """A repo configured on several machines must resolve to *this* box's path."""
    monkeypatch.setattr("coord.health.context.socket.gethostname", lambda: "laptop")
    mine = _git_checkout(tmp_path, "mine")
    theirs = _git_checkout(tmp_path, "theirs")
    config = SimpleNamespace(
        repos=[_repo("api")],
        machines=[
            _machine("server", "server.ts.net", {"api": str(theirs)}),
            _machine("laptop", "laptop.ts.net", {"api": str(mine)}),
        ],
    )
    (checkout,) = local_checkouts(config)
    assert checkout.path == mine


def test_local_checkouts_skips_paths_that_are_not_checkouts_here(tmp_path, monkeypatch) -> None:
    """A path configured for another machine's layout simply isn't here."""
    monkeypatch.setattr("coord.health.context.socket.gethostname", lambda: "laptop")
    config = SimpleNamespace(
        repos=[_repo("api")],
        machines=[_machine("laptop", "laptop.ts.net", {"api": str(tmp_path / "absent")})],
    )
    assert local_checkouts(config) == ()


def test_local_checkouts_carries_the_develop_branch(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("coord.health.context.socket.gethostname", lambda: "laptop")
    path = _git_checkout(tmp_path, "api")
    config = SimpleNamespace(
        repos=[_repo("api", develop_branch="develop")],
        machines=[_machine("laptop", "laptop.ts.net", {"api": str(path)})],
    )
    (checkout,) = local_checkouts(config)
    assert checkout.develop_branch == "develop"
    assert checkout.home_branches == ("main", "develop")


def test_local_checkouts_dedupes_a_shared_path(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("coord.health.context.socket.gethostname", lambda: "laptop")
    path = _git_checkout(tmp_path, "api")
    config = SimpleNamespace(
        repos=[_repo("api")],
        machines=[
            _machine("laptop", "laptop.ts.net", {"api": str(path)}),
            _machine("other", "other.ts.net", {"api": str(path)}),
        ],
    )
    assert len(local_checkouts(config)) == 1


# ── build_context ────────────────────────────────────────────────────────────


def test_build_context_without_a_config_uses_default_thresholds(tmp_path) -> None:
    ctx = build_context(None, home=tmp_path)
    assert ctx.thresholds == HealthConfig()
    assert ctx.coord_dir == tmp_path / ".coord"
    assert ctx.checkouts == ()
    assert ctx.allow_network is True


def test_build_context_takes_thresholds_from_the_config(tmp_path) -> None:
    thresholds = HealthConfig(disk_crit_free_pct=1.0)
    config = SimpleNamespace(repos=[], machines=[], health=thresholds)
    ctx = build_context(config, home=tmp_path)
    assert ctx.thresholds is thresholds


def test_build_context_honours_allow_network(tmp_path) -> None:
    assert build_context(None, home=tmp_path, allow_network=False).allow_network is False


# ── cost budget ──────────────────────────────────────────────────────────────


def _empty_ctx(tmp_path: Path, **kwargs) -> HealthContext:
    return HealthContext(
        thresholds=HealthConfig(),
        home=tmp_path,
        coord_dir=tmp_path / ".coord",
        now=time.time(),
        allow_network=kwargs.pop("allow_network", False),
    )


def test_no_cheap_check_touches_the_network(tmp_path, monkeypatch) -> None:
    """The issue's rule: no network beyond the one PyPI index fetch.

    That fetch lives in ``agent_version``, which is ``cost="network"``. If a
    cheap probe ever grows an HTTP call, this fails — which matters because
    the cheap set is what a per-minute timer will run, and a network call in
    that loop turns the health check into its own outage.
    """
    def _forbidden(*args, **kwargs):
        raise AssertionError("a cheap health probe made an HTTP request")

    import httpx

    monkeypatch.setattr(httpx, "get", _forbidden)
    monkeypatch.setattr(httpx, "post", _forbidden)
    monkeypatch.setattr("coord.health.pypi.fetch_simple_index", _forbidden)
    monkeypatch.setattr(
        "coord.usage_limits.probe_plan_limits",
        lambda **k: (_ for _ in ()).throw(AssertionError("cheap probe ran the usage probe")),
    )

    report = registry.run_all(_empty_ctx(tmp_path))
    assert any(s.startswith("agent_version") for s in report.skipped)
    assert any(s.startswith("plan_usage") for s in report.skipped)


def test_cheap_registry_fits_the_budget_on_an_empty_machine(tmp_path, monkeypatch) -> None:
    """A generous ceiling on the ~2s target — this catches a hang, not a wobble.

    Timing assertions are flaky by nature, so the number is 4x the design
    budget: the failure this guards against is a probe that blocks (an
    un-timeouted subprocess, an unbounded tree walk), not one that is 200ms
    slower than it was.
    """
    # Stub the one cheap probe that shells out, so this measures the engine
    # rather than the host's pip.
    monkeypatch.setattr(
        "coord.health.checks.agent_install.pip_show",
        lambda python, timeout=8.0: {"Name": "claude-coordinator", "Version": "0.0.0"},
    )
    started = time.monotonic()
    report = registry.run_all(_empty_ctx(tmp_path))
    elapsed = time.monotonic() - started
    assert elapsed < 8.0, f"cheap registry took {elapsed:.2f}s (design budget ~2s)"
    assert report.duration_secs == pytest.approx(elapsed, abs=0.5)


def test_probes_are_non_destructive_on_an_empty_state_dir(tmp_path, monkeypatch) -> None:
    """No writes. A health check that creates the thing it is checking for is
    not reporting on the machine, it is changing it."""
    monkeypatch.setattr(
        "coord.health.checks.agent_install.pip_show",
        lambda python, timeout=8.0: {"Name": "claude-coordinator", "Version": "0.0.0"},
    )
    before = sorted(p.name for p in tmp_path.iterdir())
    registry.run_all(_empty_ctx(tmp_path))
    assert sorted(p.name for p in tmp_path.iterdir()) == before
    assert not (tmp_path / ".coord").exists()


def test_every_registered_check_declares_a_known_scope_and_cost() -> None:
    for chk in registry.all_checks():
        assert chk.scope in ("machine", "checkout", "fleet")
        assert chk.cost in (registry.COST_CHEAP, registry.COST_NETWORK)
        assert chk.description, f"{chk.id} has no description"
        assert chk.title


def test_seed_checks_cover_the_issues_table() -> None:
    """Every signal the milestone specified has a check.

    Named explicitly so a probe deleted during a refactor fails loudly rather
    than quietly reducing what the fleet watches.
    """
    ids = {chk.id for chk in registry.all_checks()}
    assert {
        "disk",
        "cargo_targets",
        "worktrees",
        "agent_venv",
        "agent_version",
        "claude_binary",
        # #2237 item 6: layer 1 of graphify (the CLI itself). Without it every
        # graph operation on the machine fails one-by-one for a reason only
        # visible inside a per-HEAD failure record.
        "graphify_cli",
        "repo_branch",
        "repo_dirty",
        "graph",
        "plan_usage",
    } <= ids
