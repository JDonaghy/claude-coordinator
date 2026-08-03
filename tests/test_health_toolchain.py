"""Unit tests for coord.health.checks.toolchain (#1629, H-2).

Three layers, matching the module's own docstring:

1. applicability + local version detection (table-driven, #1629 scope item 3)
2. CI-pin parsing out of workflow YAML — including the issue's own motivating
   example, `dtolnay/rust-toolchain@stable`, which must resolve to "unknown",
   never a fabricated version.
3. the fleet-scope skew judgement: OK / WARN / CRIT per the issue's own
   table, driven through the registry so a wrong scope/id/decorator typo
   fails here instead of silently never running on the daemon.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coord.config import HealthConfig
from coord.health.checks import toolchain
from coord.health.models import FleetSnapshot, HealthContext, Severity
from coord.health.registry import run_all

NOW = 1_800_000_000.0


def make_ctx(tmp_path: Path, **kwargs) -> HealthContext:
    thresholds = kwargs.pop("thresholds", None) or HealthConfig()
    home = kwargs.pop("home", tmp_path)
    ctx = HealthContext(
        thresholds=thresholds,
        home=home,
        coord_dir=kwargs.pop("coord_dir", home / ".coord"),
        now=kwargs.pop("now", NOW),
        checkouts=kwargs.pop("checkouts", ()),
        config=kwargs.pop("config", None),
        allow_network=kwargs.pop("allow_network", True),
    )
    fleet = kwargs.pop("fleet", None)
    if fleet is not None:
        ctx.fleet = fleet
    return ctx


def _checkout(tmp_path: Path, name: str = "api") -> "object":
    from coord.health.models import Checkout

    return Checkout(name=name, path=tmp_path)


def _fake_completed(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


# ── applicability (table-driven detection) ───────────────────────────────────


def test_repo_toolchain_kinds_from_dir_markers(tmp_path: Path) -> None:
    (tmp_path / "tui").mkdir()
    (tmp_path / "coord").mkdir()
    (tmp_path / "coord" / "dashboard" / "webapp").mkdir(parents=True)
    assert set(toolchain.repo_toolchain_kinds(tmp_path)) == {"rustc", "python", "node"}


def test_repo_toolchain_kinds_from_root_markers(tmp_path: Path) -> None:
    """vimcode/quadraui: pure-Rust repos with no nested tui/ dir — the root
    Cargo.toml is what says "this repo needs rustc"."""
    (tmp_path / "Cargo.toml").write_text("[package]\n")
    assert toolchain.repo_toolchain_kinds(tmp_path) == ["rustc"]


def test_repo_toolchain_kinds_empty_when_nothing_matches(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    assert toolchain.repo_toolchain_kinds(tmp_path) == []


# ── local version detection ──────────────────────────────────────────────────


def test_detect_local_version_parses_rustc_output(monkeypatch) -> None:
    monkeypatch.setattr(
        toolchain.subprocess, "run",
        lambda *a, **k: _fake_completed("rustc 1.95.0 (abcdef 2026-04-14)\n"),
    )
    spec = toolchain.TOOLCHAIN_SPECS[0]
    assert spec.kind == "rustc"
    assert toolchain.detect_local_version(spec) == "1.95.0"


def test_detect_local_version_missing_binary_is_none(monkeypatch) -> None:
    def _boom(*a, **k):
        raise FileNotFoundError("no such file")

    monkeypatch.setattr(toolchain.subprocess, "run", _boom)
    assert toolchain.detect_local_version(toolchain.TOOLCHAIN_SPECS[0]) is None


def test_detect_local_version_nonzero_exit_is_none(monkeypatch) -> None:
    monkeypatch.setattr(
        toolchain.subprocess, "run", lambda *a, **k: _fake_completed("", returncode=1)
    )
    assert toolchain.detect_local_version(toolchain.TOOLCHAIN_SPECS[0]) is None


def test_detect_local_version_timeout_is_none(monkeypatch) -> None:
    def _timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd="rustc", timeout=5.0)

    monkeypatch.setattr(toolchain.subprocess, "run", _timeout)
    assert toolchain.detect_local_version(toolchain.TOOLCHAIN_SPECS[0]) is None


def test_local_toolchain_label_joins_multiple_kinds(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "tui").mkdir()
    (tmp_path / "coord").mkdir()
    versions = {"rustc": "rustc 1.95.0 (abcdef 2026-04-14)", "python3": "Python 3.12.4"}

    def _run(argv, **k):
        return _fake_completed(versions[argv[0]])

    monkeypatch.setattr(toolchain.subprocess, "run", _run)
    label = toolchain.local_toolchain_label(tmp_path)
    assert label == "rustc 1.95.0, python 3.12.4"


def test_local_toolchain_label_none_when_nothing_applies(tmp_path: Path) -> None:
    assert toolchain.local_toolchain_label(tmp_path) is None


def test_local_toolchain_label_none_when_undetectable(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "tui").mkdir()
    monkeypatch.setattr(toolchain.subprocess, "run", lambda *a, **k: _fake_completed("", 1))
    assert toolchain.local_toolchain_label(tmp_path) is None


def test_toolchain_label_for_machine_reads_h1_report_block() -> None:
    block = {
        "results": [
            {"check_id": "toolchain_versions", "subject": "rustc", "values": {"version": "1.95.0"}},
            {"check_id": "toolchain_versions", "subject": "python", "values": {"version": "3.12.4"}},
            {"check_id": "disk", "subject": "/home", "values": {"used_pct": 80}},
        ]
    }
    assert toolchain.toolchain_label_for_machine(block) == "rustc 1.95.0, python 3.12.4"
    assert toolchain.toolchain_label_for_machine(block, kinds=["python"]) == "python 3.12.4"


def test_toolchain_label_for_machine_none_when_no_block() -> None:
    assert toolchain.toolchain_label_for_machine(None) is None
    assert toolchain.toolchain_label_for_machine({}) is None


def test_toolchain_label_for_machine_skips_errored_results() -> None:
    block = {"results": [
        {"check_id": "toolchain_versions", "subject": "rustc", "error": "boom",
         "values": {"version": "1.95.0"}},
    ]}
    # An errored result still carries stale `values` in this synthetic shape;
    # the real probe never emits `version` alongside `error`, but the reader
    # only trusts subject+version being present — belt-and-braces, not load-
    # bearing behaviour under test here. (No `error` filtering is applied by
    # this reader; the machine-scope probe itself never emits both.)
    assert toolchain.toolchain_label_for_machine(block) == "rustc 1.95.0"


# ── CI pin parsing ────────────────────────────────────────────────────────────


def _write_workflow(checkout: Path, name: str, body: str) -> None:
    wf_dir = checkout / ".github" / "workflows"
    wf_dir.mkdir(parents=True, exist_ok=True)
    (wf_dir / name).write_text(body)


def test_ci_toolchain_versions_no_workflows_dir_is_all_none(tmp_path: Path) -> None:
    out = toolchain.ci_toolchain_versions(tmp_path)
    assert out == {"rustc": None, "python": None, "node": None}


def test_ci_toolchain_versions_reads_literal_python_pin(tmp_path: Path) -> None:
    _write_workflow(
        tmp_path, "test.yml",
        "on: push\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n"
        "      - uses: actions/checkout@v4\n"
        "      - uses: actions/setup-python@v5\n"
        "        with:\n          python-version: '3.12'\n",
    )
    out = toolchain.ci_toolchain_versions(tmp_path)
    assert out["python"] == "3.12"
    assert out["rustc"] is None


def test_ci_toolchain_versions_floating_rust_toolchain_is_unknown(tmp_path: Path) -> None:
    """The issue's own motivating example: `dtolnay/rust-toolchain@stable`
    floats — CI's actually-resolved version changes over time and can only
    be read from a live run. Reporting it as a literal version would be
    fabrication; this must stay None."""
    _write_workflow(
        tmp_path, "cargo-test.yml",
        "on: push\njobs:\n  cargo-test:\n    runs-on: ubuntu-latest\n    steps:\n"
        "      - uses: dtolnay/rust-toolchain@stable\n",
    )
    out = toolchain.ci_toolchain_versions(tmp_path)
    assert out["rustc"] is None


def test_ci_toolchain_versions_explicit_rust_pin_via_with(tmp_path: Path) -> None:
    _write_workflow(
        tmp_path, "cargo-test.yml",
        "on: push\njobs:\n  cargo-test:\n    runs-on: ubuntu-latest\n    steps:\n"
        "      - uses: dtolnay/rust-toolchain@stable\n"
        "        with:\n          toolchain: '1.97.1'\n",
    )
    out = toolchain.ci_toolchain_versions(tmp_path)
    assert out["rustc"] == "1.97.1"


def test_ci_toolchain_versions_explicit_rust_pin_via_ref(tmp_path: Path) -> None:
    _write_workflow(
        tmp_path, "cargo-test.yml",
        "on: push\njobs:\n  cargo-test:\n    runs-on: ubuntu-latest\n    steps:\n"
        "      - uses: dtolnay/rust-toolchain@1.75.0\n",
    )
    out = toolchain.ci_toolchain_versions(tmp_path)
    assert out["rustc"] == "1.75.0"


def test_ci_toolchain_versions_setup_node_no_with_block_is_unknown(tmp_path: Path) -> None:
    """Regression (#1629 review): `actions/setup-node@v4` with no explicit
    `with: node-version:` is the common CI pattern (`.nvmrc`-driven Node) —
    `v4` is the ACTION's own release tag, not a Node version, and must never
    be fabricated into a pin. Reproduces the exact false-CRIT report from the
    review: this used to resolve to `"v4"`, and `ci_matches_machine("v4",
    "20.11.0")` is False, which surfaced as a bogus fleet_toolchain_skew CRIT."""
    _write_workflow(
        tmp_path, "test.yml",
        "on: push\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n"
        "      - uses: actions/checkout@v4\n"
        "      - uses: actions/setup-node@v4\n",
    )
    out = toolchain.ci_toolchain_versions(tmp_path)
    assert out["node"] is None


def test_ci_toolchain_versions_setup_python_no_with_block_is_unknown(tmp_path: Path) -> None:
    """Same regression as above for `actions/setup-python@v5` with no
    explicit `with: python-version:` (default runner Python)."""
    _write_workflow(
        tmp_path, "test.yml",
        "on: push\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n"
        "      - uses: actions/setup-python@v5\n",
    )
    out = toolchain.ci_toolchain_versions(tmp_path)
    assert out["python"] is None


def test_ci_toolchain_versions_unrelated_action_is_ignored(tmp_path: Path) -> None:
    _write_workflow(
        tmp_path, "test.yml",
        "on: push\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n"
        "      - uses: actions/checkout@v4\n"
        "      - run: echo hi\n",
    )
    out = toolchain.ci_toolchain_versions(tmp_path)
    assert out == {"rustc": None, "python": None, "node": None}


def test_ci_toolchain_versions_malformed_yaml_is_fail_soft(tmp_path: Path) -> None:
    _write_workflow(tmp_path, "broken.yml", "not: [valid: yaml: at all")
    out = toolchain.ci_toolchain_versions(tmp_path)
    assert out == {"rustc": None, "python": None, "node": None}


# ── ci_matches_machine: prefix-tolerant against a possibly-imprecise pin ─────


@pytest.mark.parametrize(
    ("ci_version", "machine_version", "expected"),
    [
        ("3.12", "3.12.4", True),  # CI names a minor version, machine is full triplet
        ("3.12", "3.13.0", False),
        ("1.97.1", "1.97.1", True),  # exact precision required when CI gives it
        ("1.97.1", "1.97.2", False),
        ("20", "20.11.0", True),
        ("", "1.0.0", False),
        ("1.0.0", "", False),
    ],
)
def test_ci_matches_machine(ci_version, machine_version, expected) -> None:
    assert toolchain.ci_matches_machine(ci_version, machine_version) is expected


# ── machine-scope probe ──────────────────────────────────────────────────────


def test_probe_toolchain_versions_none_when_no_checkout_needs_anything(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path, checkouts=(_checkout(tmp_path),))
    assert toolchain.probe_toolchain_versions(ctx) is None


def test_probe_toolchain_versions_ok_when_detected(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "tui").mkdir()
    monkeypatch.setattr(
        toolchain.subprocess, "run", lambda *a, **k: _fake_completed("rustc 1.95.0 (x)\n")
    )
    ctx = make_ctx(tmp_path, checkouts=(_checkout(tmp_path),))
    results = toolchain.probe_toolchain_versions(ctx)
    assert len(results) == 1
    r = results[0]
    assert r.check_id == "toolchain_versions"
    assert r.scope == "machine"
    assert r.subject == "rustc"
    assert r.severity is Severity.OK
    assert r.values["version"] == "1.95.0"


def test_probe_toolchain_versions_unknown_when_binary_missing(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "tui").mkdir()
    monkeypatch.setattr(toolchain.subprocess, "run", lambda *a, **k: _fake_completed("", 1))
    ctx = make_ctx(tmp_path, checkouts=(_checkout(tmp_path),))
    results = toolchain.probe_toolchain_versions(ctx)
    assert len(results) == 1
    assert results[0].severity is Severity.UNKNOWN


def test_probe_toolchain_versions_dedupes_across_checkouts(tmp_path: Path, monkeypatch) -> None:
    """Two checkouts both needing rustc must not run `rustc --version` (or
    emit a result) twice."""
    from coord.health.models import Checkout

    c1 = tmp_path / "repo1"
    c2 = tmp_path / "repo2"
    (c1 / "tui").mkdir(parents=True)
    (c2 / "tui").mkdir(parents=True)
    calls = []

    def _run(argv, **k):
        calls.append(argv)
        return _fake_completed("rustc 1.95.0 (x)\n")

    monkeypatch.setattr(toolchain.subprocess, "run", _run)
    ctx = make_ctx(
        tmp_path,
        checkouts=(Checkout(name="repo1", path=c1), Checkout(name="repo2", path=c2)),
    )
    results = toolchain.probe_toolchain_versions(ctx)
    assert len(results) == 1
    assert len(calls) == 1


# ── fleet-scope skew judgement (driven through the registry) ────────────────


def _fleet_ctx(*, machines=None, daemon_host=None, config=None, fleet=True):
    ctx = HealthContext(
        thresholds=HealthConfig(),
        home=Path("/nonexistent-home"),
        coord_dir=Path("/nonexistent-home/.coord"),
        now=NOW,
        allow_network=False,
        config=config,
    )
    if fleet:
        ctx.fleet = FleetSnapshot(machines=machines or {}, daemon_host=daemon_host or {})
    return ctx


def _run_skew(ctx) -> list:
    from coord.health import checks  # noqa: F401 — registers every check module

    report = run_all(ctx, scopes=("fleet",))
    return [r for r in report.results if r.check_id == "fleet_toolchain_skew"]


def _cfg_with_machines(*names_and_repos):
    from types import SimpleNamespace

    machines = [
        SimpleNamespace(name=name, repo_paths={r: f"/x/{name}/{r}" for r in repos})
        for name, repos in names_and_repos
    ]
    return SimpleNamespace(machines=machines)


def _machine_entry(kind: str, version: str) -> dict:
    return {"state": "online", "checks": {"results": [
        {"check_id": "toolchain_versions", "subject": kind, "values": {"version": version}},
    ]}}


def test_skew_no_fleet_snapshot_is_unknown() -> None:
    [r] = _run_skew(_fleet_ctx(fleet=False))
    assert r.severity is Severity.UNKNOWN
    assert "no fleet snapshot" in r.headroom


def test_skew_no_repo_toolchain_kinds_is_unknown() -> None:
    [r] = _run_skew(_fleet_ctx(daemon_host={}))
    assert r.severity is Severity.UNKNOWN
    assert "no repo has a resolvable toolchain" in r.headroom


def test_skew_no_candidate_machines_is_unknown() -> None:
    cfg = _cfg_with_machines(("laptop", []))  # laptop has no repo_paths for "api"
    [r] = _run_skew(
        _fleet_ctx(
            config=cfg,
            daemon_host={"repo_toolchain_kinds": {"api": ["rustc"]}},
        )
    )
    assert r.severity is Severity.UNKNOWN
    assert "no machine advertises a checkout" in r.headroom


def test_skew_all_agree_no_ci_is_ok() -> None:
    cfg = _cfg_with_machines(("laptop", ["api"]), ("server", ["api"]))
    [r] = _run_skew(
        _fleet_ctx(
            config=cfg,
            machines={
                "laptop": _machine_entry("rustc", "1.95.0"),
                "server": _machine_entry("rustc", "1.95.0"),
            },
            daemon_host={"repo_toolchain_kinds": {"api": ["rustc"]}, "ci_toolchains": {}},
        )
    )
    assert r.severity is Severity.OK
    assert "all machines on rustc 1.95.0" in r.headroom
    assert r.subject == "api:rustc"


def test_skew_all_agree_and_matches_ci_prefix_is_ok() -> None:
    cfg = _cfg_with_machines(("laptop", ["api"]))
    [r] = _run_skew(
        _fleet_ctx(
            config=cfg,
            machines={"laptop": _machine_entry("python", "3.12.4")},
            daemon_host={
                "repo_toolchain_kinds": {"api": ["python"]},
                "ci_toolchains": {"api": {"python": "3.12"}},
            },
        )
    )
    assert r.severity is Severity.OK
    assert "matches CI's 3.12" in r.headroom


def test_skew_machines_disagree_no_ci_is_warn_and_names_machines() -> None:
    cfg = _cfg_with_machines(("laptop", ["api"]), ("server", ["api"]))
    [r] = _run_skew(
        _fleet_ctx(
            config=cfg,
            machines={
                "laptop": _machine_entry("rustc", "1.95.0"),
                "server": _machine_entry("rustc", "1.93.1"),
            },
            daemon_host={"repo_toolchain_kinds": {"api": ["rustc"]}, "ci_toolchains": {}},
        )
    )
    assert r.severity is Severity.WARN
    assert "laptop" in r.detail and "server" in r.detail
    assert "2 rustc versions" in r.headroom


def test_skew_machine_differs_from_ci_is_crit_and_names_the_offenders() -> None:
    """The issue's own black-box acceptance bullet: three machines with
    differing versions, a CI version to compare against — CRIT, and the
    offending machines are named, not just "something disagrees"."""
    cfg = _cfg_with_machines(
        ("dellserver", ["vimcode"]), ("precision", ["vimcode"]), ("elitebook", ["vimcode"]),
    )
    [r] = _run_skew(
        _fleet_ctx(
            config=cfg,
            machines={
                "dellserver": _machine_entry("rustc", "1.95.0"),
                "precision": _machine_entry("rustc", "1.95.0"),
                "elitebook": _machine_entry("rustc", "1.93.1"),
            },
            daemon_host={
                "repo_toolchain_kinds": {"vimcode": ["rustc"]},
                "ci_toolchains": {"vimcode": {"rustc": "1.97.1"}},
            },
        )
    )
    assert r.severity is Severity.CRIT
    assert r.subject == "vimcode:rustc"
    for offender in ("dellserver", "precision", "elitebook"):
        assert offender in r.headroom or offender in r.detail
    assert "1.97.1" in r.headroom
    assert r.values["ci_version"] == "1.97.1"
    assert r.values["versions"] == {
        "dellserver": "1.95.0", "precision": "1.95.0", "elitebook": "1.93.1",
    }


def test_skew_machines_agree_with_each_other_but_not_ci_is_still_crit() -> None:
    """Machine-vs-machine agreement is not enough — vimcode#615's exact
    shape: every fleet machine agreed with each other and still built
    against a toolchain CI doesn't run."""
    cfg = _cfg_with_machines(("laptop", ["api"]), ("server", ["api"]))
    [r] = _run_skew(
        _fleet_ctx(
            config=cfg,
            machines={
                "laptop": _machine_entry("rustc", "1.95.0"),
                "server": _machine_entry("rustc", "1.95.0"),
            },
            daemon_host={
                "repo_toolchain_kinds": {"api": ["rustc"]},
                "ci_toolchains": {"api": {"rustc": "1.97.1"}},
            },
        )
    )
    assert r.severity is Severity.CRIT
    assert "laptop" in r.headroom and "server" in r.headroom


def test_skew_missing_machine_data_is_reported_not_dropped() -> None:
    cfg = _cfg_with_machines(("laptop", ["api"]), ("server", ["api"]))
    [r] = _run_skew(
        _fleet_ctx(
            config=cfg,
            machines={"laptop": _machine_entry("rustc", "1.95.0")},  # server never reported
            daemon_host={"repo_toolchain_kinds": {"api": ["rustc"]}, "ci_toolchains": {}},
        )
    )
    assert r.severity is Severity.UNKNOWN  # agreement among those that DID answer isn't fleet agreement
    assert "server" in r.detail
    assert r.values["missing"] == ["server"]


def test_skew_no_machine_has_reported_any_version_is_unknown() -> None:
    cfg = _cfg_with_machines(("laptop", ["api"]))
    [r] = _run_skew(
        _fleet_ctx(
            config=cfg,
            machines={},
            daemon_host={"repo_toolchain_kinds": {"api": ["rustc"]}, "ci_toolchains": {}},
        )
    )
    assert r.severity is Severity.UNKNOWN
    assert "no rustc version data yet" in r.headroom


def test_skew_multiple_kinds_for_one_repo_each_get_their_own_row() -> None:
    cfg = _cfg_with_machines(("laptop", ["coordinator"]))
    machines = {
        "laptop": {"state": "online", "checks": {"results": [
            {"check_id": "toolchain_versions", "subject": "rustc", "values": {"version": "1.95.0"}},
            {"check_id": "toolchain_versions", "subject": "python", "values": {"version": "3.12.4"}},
        ]}},
    }
    results = _run_skew(
        _fleet_ctx(
            config=cfg,
            machines=machines,
            daemon_host={
                "repo_toolchain_kinds": {"coordinator": ["rustc", "python"]},
                "ci_toolchains": {},
            },
        )
    )
    subjects = {r.subject for r in results}
    assert subjects == {"coordinator:rustc", "coordinator:python"}
    assert all(r.severity is Severity.OK for r in results)
