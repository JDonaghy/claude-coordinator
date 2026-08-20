"""Black-box tests for the ``coord_web_ci_pin`` check (#2006, epic #2002).

The subject: `coord-web`'s CI must install a `coord` that can actually boot
`coord web --fixture` (the Playwright ``webServer``, #1818), and must not
exact-pin it. See ``docs/ADR_COORD_WEB_CI.md`` for why track-latest wins and
``coord/health/checks/coord_web_ci_pin.py``'s docstring for the two
incidents (``~/.coord-cli-venv`` three releases stale; vimcode#615) that
make "a version spec in another repo's YAML" a thing worth measuring.

Everything here drives the probe through a real on-disk checkout + real
workflow YAML — the same path the health tick takes — rather than poking at
parser internals.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from coord.config import ConfigError, HealthConfig, _parse_health
from coord.health.checks import coord_web_ci_pin as cwp
from coord.health.models import Checkout, HealthContext, Severity

NOW = 1_800_000_000.0


@pytest.fixture(autouse=True)
def _stable_local_version(monkeypatch):
    """Pin the "what coord is on this box" side of the floor comparison.

    Otherwise these tests would grade differently on a source checkout
    (``0+unknown``) than on a machine with a real wheel installed.
    """
    monkeypatch.setattr(cwp, "LOCAL_COORD_VERSION", "0.4.90")


def make_ctx(tmp_path: Path, **kwargs) -> HealthContext:
    home = kwargs.pop("home", tmp_path)
    return HealthContext(
        thresholds=kwargs.pop("thresholds", None) or HealthConfig(),
        home=home,
        coord_dir=kwargs.pop("coord_dir", home / ".coord"),
        now=kwargs.pop("now", NOW),
        checkouts=kwargs.pop("checkouts", ()),
        config=kwargs.pop("config", None),
        allow_network=kwargs.pop("allow_network", True),
    )


def write_ci(root: Path, install_line: str | None, *, name: str = "ci.yml") -> Path:
    """A minimal but structurally real `coord-web` CI workflow."""
    install_step = f"      - run: {install_line}\n" if install_line else ""
    (root / ".github" / "workflows").mkdir(parents=True, exist_ok=True)
    (root / ".github" / "workflows" / name).write_text(
        textwrap.dedent(
            """\
            name: CI
            on:
              pull_request:
                branches: [main]
            jobs:
              checks:
                runs-on: ubuntu-latest
                steps:
                  - uses: actions/checkout@v4
                  - run: npm ci
                  - run: npm run typecheck
              e2e:
                runs-on: ubuntu-latest
                steps:
                  - uses: actions/checkout@v4
                  - run: npm ci
            """
        )
        + install_step
        + "      - run: npm run test:e2e\n",
        encoding="utf-8",
    )
    return root


def make_coord_web(tmp_path: Path, install_line: str | None, *, name: str = "coord-web") -> Path:
    root = tmp_path / "src" / name
    root.mkdir(parents=True, exist_ok=True)
    (root / cwp.COORD_WEB_MARKER).write_text("// webServer: coord web --fixture\n")
    write_ci(root, install_line)
    return root


def probe(tmp_path: Path, install_line: str | None, **ctx_kwargs):
    root = make_coord_web(tmp_path, install_line)
    checkouts = ctx_kwargs.pop("checkouts", (Checkout(name="coord-web", path=root),))
    return cwp.probe_coord_web_ci_pin(make_ctx(tmp_path, checkouts=checkouts, **ctx_kwargs))


# ── absence ──────────────────────────────────────────────────────────────────


def test_no_coord_web_checkout_is_ok_not_a_fault(tmp_path) -> None:
    """Only the machines listing coord-web in repo_paths have one. A worker
    box that never did must not read as broken."""
    result = cwp.probe_coord_web_ci_pin(make_ctx(tmp_path))
    assert result.severity is Severity.OK
    assert result.headroom == "not present on this machine"
    assert result.values["present"] is False


def test_unrelated_checkouts_do_not_resolve_to_coord_web(tmp_path) -> None:
    other = tmp_path / "src" / "vimcode"
    (other / ".github" / "workflows").mkdir(parents=True)
    result = cwp.probe_coord_web_ci_pin(
        make_ctx(tmp_path, checkouts=(Checkout(name="vimcode", path=other),))
    )
    assert result.severity is Severity.OK
    assert result.values["present"] is False


# ── the happy path this ADR decided on ───────────────────────────────────────


def test_tracks_latest_with_server_extra_is_ok(tmp_path) -> None:
    """The exact line coord-web's ci.yml carries today."""
    result = probe(tmp_path, "pip install 'code-coordinator[server]'")
    assert result.severity is Severity.OK
    # The spec string is *visible* — that is half of what #2006 asked for.
    assert "code-coordinator[server]" in result.headroom
    assert "tracks latest" in result.headroom
    (pin,) = result.values["pins"]
    assert pin["dist"] == "code-coordinator"
    assert pin["extras"] == ["server"]
    assert pin["constraint"] is None
    assert pin["workflow"] == "ci.yml"
    assert pin["job"] == "e2e"


def test_ge_floor_is_ok_and_reported(tmp_path) -> None:
    """A `>=` floor documents a minimum without freezing what resolves."""
    result = probe(tmp_path, "pip install 'code-coordinator[server]>=0.0.1'")
    assert result.severity is Severity.OK
    assert "floor 0.0.1" in result.headroom


def test_bare_pip_install_without_quotes_still_parses(tmp_path) -> None:
    result = probe(tmp_path, "pip install code-coordinator[server]")
    assert result.severity is Severity.OK
    assert result.values["pins"][0]["extras"] == ["server"]


def test_uv_pip_install_is_recognised_as_an_install(tmp_path) -> None:
    result = probe(tmp_path, "uv pip install 'code-coordinator[server]'")
    assert result.severity is Severity.OK


# ── the failures worth a severity ────────────────────────────────────────────


def test_no_install_step_at_all_warns(tmp_path) -> None:
    """`coord web --fixture` is coord-web's Playwright webServer — with no
    install step the job cannot boot it."""
    result = probe(tmp_path, None)
    assert result.severity is Severity.WARN
    assert "installs no coord CLI" in result.headroom
    assert result.values["pins"] == []
    assert "ADR_COORD_WEB_CI" in result.detail


def test_missing_server_extra_is_crit(tmp_path) -> None:
    """#1237: the bare distribution is client-only — `coord web` cannot serve."""
    result = probe(tmp_path, "pip install code-coordinator")
    assert result.severity is Severity.CRIT
    assert "client-only" in result.headroom
    assert "#1237" in result.detail
    assert "ci.yml:e2e" in result.detail


def test_wrong_extra_does_not_count_as_the_server_extra(tmp_path) -> None:
    result = probe(tmp_path, "pip install 'code-coordinator[dev]'")
    assert result.severity is Severity.CRIT
    assert result.values["pins"][0]["extras"] == ["dev"]


def test_tombstone_distribution_name_is_crit(tmp_path) -> None:
    """#2106: `claude-coordinator` is a permanent PyPI tombstone — CI asking
    for it is frozen on its last-ever release, forever, silently."""
    result = probe(tmp_path, "pip install 'claude-coordinator[server]'")
    assert result.severity is Severity.CRIT
    assert "dead distribution name" in result.headroom
    assert "#2106" in result.detail


def test_tombstone_outranks_the_missing_extra(tmp_path) -> None:
    """Both wrong: renaming to a client-only `code-coordinator` would not
    help, so the tombstone is the finding to lead with."""
    result = probe(tmp_path, "pip install claude-coordinator")
    assert result.severity is Severity.CRIT
    assert "dead distribution name" in result.headroom


@pytest.mark.parametrize("op", ["==", "===", "~="])
def test_exact_pin_warns(tmp_path, op) -> None:
    """The `~/.coord-cli-venv` failure shape: a frozen spec goes green while
    the coord users actually run has already broken the contract."""
    result = probe(tmp_path, f"pip install 'code-coordinator[server]{op}0.4.90'")
    assert result.severity is Severity.WARN
    assert "exact-pins coord" in result.headroom
    assert "rots silently" in result.detail
    assert result.values["pins"][0]["constraint"] == f"{op}0.4.90"


def test_floor_ahead_of_local_coord_warns(tmp_path) -> None:
    """A floor naming a release that is not out yet is unsatisfiable — the
    kind of thing a hand-edited YAML acquires and nothing notices."""
    result = probe(tmp_path, "pip install 'code-coordinator[server]>=999.0.0'")
    assert result.severity is Severity.WARN
    assert "floor is ahead" in result.headroom
    assert "999.0.0" in result.detail
    assert "0.4.90" in result.detail


def test_floor_equal_to_local_coord_is_ok(tmp_path) -> None:
    result = probe(tmp_path, "pip install 'code-coordinator[server]>=0.4.90'")
    assert result.severity is Severity.OK


def test_less_precise_floor_is_satisfied_by_a_patch_release(tmp_path) -> None:
    """`>=0.4` is satisfied by 0.4.90 — comparing tuples of unequal length
    without padding would call that "ahead"."""
    result = probe(tmp_path, "pip install 'code-coordinator[server]>=0.4'")
    assert result.severity is Severity.OK


def test_unknown_local_version_never_fabricates_a_floor_finding(tmp_path, monkeypatch) -> None:
    """`coord.__version__` is `0+unknown` in a source checkout. Grading a
    floor against that would flag every floor on every dev box."""
    monkeypatch.setattr(cwp, "LOCAL_COORD_VERSION", "0+unknown")
    result = probe(tmp_path, "pip install 'code-coordinator[server]>=999.0.0'")
    assert result.severity is Severity.OK


# ── parser robustness: this is reading someone else's hand-written YAML ──────


def test_a_comment_naming_the_package_is_not_a_pin(tmp_path) -> None:
    """Grading prose would manufacture findings from documentation."""
    root = make_coord_web(tmp_path, None)
    wf = root / ".github" / "workflows" / "ci.yml"
    wf.write_text(
        wf.read_text(encoding="utf-8")
        + "      - run: echo 'we deliberately do not use claude-coordinator here'\n",
        encoding="utf-8",
    )
    result = cwp.probe_coord_web_ci_pin(
        make_ctx(tmp_path, checkouts=(Checkout(name="coord-web", path=root),))
    )
    assert result.severity is Severity.WARN
    assert "installs no coord CLI" in result.headroom


def test_unparseable_workflow_yaml_does_not_crash_the_probe(tmp_path) -> None:
    root = make_coord_web(tmp_path, "pip install 'code-coordinator[server]'")
    (root / ".github" / "workflows" / "broken.yml").write_text(
        "jobs: [oh no: {{{\n", encoding="utf-8"
    )
    result = cwp.probe_coord_web_ci_pin(
        make_ctx(tmp_path, checkouts=(Checkout(name="coord-web", path=root),))
    )
    assert result.severity is Severity.OK


def test_underscore_spelling_normalises_to_the_dist_name(tmp_path) -> None:
    result = probe(tmp_path, "pip install 'code_coordinator[server]'")
    assert result.severity is Severity.OK
    assert result.values["pins"][0]["dist"] == "code-coordinator"


def test_worst_pin_across_multiple_workflows_wins(tmp_path) -> None:
    """A second workflow that gets it wrong must not be masked by a first
    one that gets it right."""
    root = make_coord_web(tmp_path, "pip install 'code-coordinator[server]'")
    write_ci(root, "pip install code-coordinator", name="nightly.yml")
    result = cwp.probe_coord_web_ci_pin(
        make_ctx(tmp_path, checkouts=(Checkout(name="coord-web", path=root),))
    )
    assert result.severity is Severity.CRIT
    assert "nightly.yml:e2e" in result.detail
    assert len(result.values["pins"]) == 2


# ── checkout discovery ───────────────────────────────────────────────────────


def test_marker_file_finds_a_renamed_checkout(tmp_path) -> None:
    """A silently-off lane is indistinguishable from a healthy one, so a repo
    rename must not turn this check off: `playwright.acceptance.config.ts` at
    the root IS the coupling."""
    root = make_coord_web(tmp_path, "pip install code-coordinator", name="webui")
    result = cwp.probe_coord_web_ci_pin(
        make_ctx(tmp_path, checkouts=(Checkout(name="webui", path=root),))
    )
    assert result.severity is Severity.CRIT


def test_configured_checkout_wins_over_discovery(tmp_path) -> None:
    good = make_coord_web(tmp_path, "pip install 'code-coordinator[server]'", name="coord-web")
    bad = make_coord_web(tmp_path, "pip install code-coordinator", name="elsewhere")
    ctx = make_ctx(
        tmp_path,
        thresholds=HealthConfig(coord_web_checkout=str(bad)),
        checkouts=(Checkout(name="coord-web", path=good),),
    )
    result = cwp.probe_coord_web_ci_pin(ctx)
    assert result.severity is Severity.CRIT
    assert result.values["checkout"] == str(bad)


def test_configured_checkout_expands_tilde(tmp_path) -> None:
    root = make_coord_web(tmp_path, "pip install 'code-coordinator[server]'")
    ctx = make_ctx(
        tmp_path,
        thresholds=HealthConfig(coord_web_checkout="~/src/coord-web"),
        home=tmp_path,
    )
    result = cwp.probe_coord_web_ci_pin(ctx)
    assert result.values["checkout"] == str(root)
    assert result.severity is Severity.OK


def test_configured_but_absent_checkout_is_absent_not_broken(tmp_path) -> None:
    ctx = make_ctx(tmp_path, thresholds=HealthConfig(coord_web_checkout=str(tmp_path / "nope")))
    result = cwp.probe_coord_web_ci_pin(ctx)
    assert result.severity is Severity.OK
    assert result.values["present"] is False


# ── config plumbing ──────────────────────────────────────────────────────────


def test_health_block_accepts_coord_web_checkout() -> None:
    cfg = _parse_health({"coord_web_checkout": "~/src/coord-web"})
    assert cfg.coord_web_checkout == "~/src/coord-web"


def test_health_block_rejects_an_empty_coord_web_checkout() -> None:
    """Standing convention: null means 'discover it', not 'disable the lane';
    an empty string is an operator typo that would resolve to the CWD."""
    with pytest.raises(ConfigError):
        _parse_health({"coord_web_checkout": "   "})


def test_check_is_registered_and_machine_scoped() -> None:
    from coord.health.registry import discover, get

    discover()
    chk = get(cwp.CHECK_ID)
    assert chk is not None
    assert chk.scope == "machine"
