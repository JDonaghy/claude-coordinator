"""The 2026-07-30 incidents, replayed against the shipped defaults (#1628).

**This is the point of the milestone.**  If the defaults in
:class:`coord.config.HealthConfig` are too loose to catch the incidents that
motivated the health engine, the engine is decoration — an operator gets a
screen of green while a machine has 0 bytes free.

Three recorded values from that day:

* **elitebook's ``/home`` at 0 bytes free.**  Workers on that machine failed
  in ways that read as coordinator bugs; nothing was watching the one number
  that would have said otherwise a day earlier.
* **78G of cargo target dirs.**  That is what actually consumed the disk.
  ``coord.cargo_cache`` GCs the shared cache at a 20 GiB cap but nothing
  totalled it together with the per-checkout ``target/`` dirs a human
  created by building in a live checkout.
* **vimcode's graph 128.8h stale with hooks disabled.**  A stale graph whose
  checkout has ``core.hooksPath`` unset will not self-heal at any point in
  the future, and every agent querying it in the meantime got answers about
  a commit that was no longer HEAD.

Each is asserted at the *default* thresholds, with no config, so loosening a
default breaks this test on purpose.  A failure here is a decision to stop
catching a class of failure that has already happened once — make it
deliberately or not at all.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from coord.config import HealthConfig
from coord.health.checks import cargo_targets, disk, graph
from coord.health.models import Checkout, HealthContext, Severity

# The values as recorded on the day.
ELITEBOOK_HOME_TOTAL_BYTES = 491_000_000_000  # ~457 GiB /home
ELITEBOOK_HOME_FREE_BYTES = 0
CARGO_TARGETS_TOTAL_BYTES = int(78 * 1024**3)
VIMCODE_GRAPH_STALE_HOURS = 128.8


def _ctx(tmp_path: Path, **kwargs) -> HealthContext:
    """A context carrying the SHIPPED defaults — no overrides, on purpose."""
    return HealthContext(
        thresholds=HealthConfig(**kwargs.pop("threshold_overrides", {})),
        home=tmp_path,
        coord_dir=kwargs.pop("coord_dir", tmp_path / ".coord"),
        now=1_800_000_000.0,
        checkouts=kwargs.pop("checkouts", ()),
    )


# ── incident 1: elitebook /home at 0 bytes free ──────────────────────────────


def test_elitebook_home_at_zero_bytes_free_is_crit_at_defaults(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        disk.os, "stat", lambda p, *a, **k: SimpleNamespace(st_dev=42)
    )
    monkeypatch.setattr(
        disk.shutil,
        "disk_usage",
        lambda p: SimpleNamespace(
            total=ELITEBOOK_HOME_TOTAL_BYTES,
            free=ELITEBOOK_HOME_FREE_BYTES,
            used=ELITEBOOK_HOME_TOTAL_BYTES,
        ),
    )
    ctx = _ctx(tmp_path, threshold_overrides={"disk_paths": ["/home"]})

    (result,) = disk.probe_disk(ctx)

    assert result.severity is Severity.CRIT, (
        "the shipped disk defaults would NOT have caught elitebook's /home at "
        "0 bytes free — that is the incident this milestone exists for"
    )
    assert result.headroom == "100% used (0B free)"
    assert result.values["free_pct"] == 0.0


@pytest.mark.parametrize(
    ("free_pct", "expected"),
    [
        (30.0, Severity.OK),
        (20.0, Severity.OK),
        (14.0, Severity.WARN),  # the incident's day-before shape
        (8.0, Severity.WARN),
        (6.0, Severity.CRIT),
        (0.0, Severity.CRIT),
    ],
)
def test_disk_default_ladder_fires_before_the_wall(
    tmp_path, monkeypatch, free_pct, expected
) -> None:
    """The defaults must WARN with room to act, not only CRIT at the cliff.

    A check that first speaks at 0 bytes free is a post-mortem, not a health
    check — the whole value is the day of warning before the machine dies.
    """
    total = 100_000
    monkeypatch.setattr(disk.os, "stat", lambda p, *a, **k: SimpleNamespace(st_dev=1))
    monkeypatch.setattr(
        disk.shutil,
        "disk_usage",
        lambda p, _t=total, _f=free_pct: SimpleNamespace(
            total=_t, free=int(_t * _f / 100.0), used=_t - int(_t * _f / 100.0)
        ),
    )
    ctx = _ctx(tmp_path, threshold_overrides={"disk_paths": ["/home"]})
    (result,) = disk.probe_disk(ctx)
    assert result.severity is expected


# ── incident 2: 78G of cargo target dirs ─────────────────────────────────────


def test_seventy_eight_gigs_of_cargo_targets_is_crit_at_defaults(
    tmp_path, monkeypatch
) -> None:
    coord_dir = tmp_path / ".coord"
    cache = coord_dir / "cargo-target" / "vimcode"
    cache.mkdir(parents=True)
    checkout_target = tmp_path / "src" / "vimcode" / "target"
    checkout_target.mkdir(parents=True)

    # 43G in the live checkout + 35G in the shared cache = the recorded 78G.
    sizes = {
        str(checkout_target): int(43 * 1024**3),
        str(cache): CARGO_TARGETS_TOTAL_BYTES - int(43 * 1024**3),
    }
    monkeypatch.setattr(
        cargo_targets,
        "_dir_size_budgeted",
        lambda path, deadline: (sizes.get(str(path), 0), True),
    )

    ctx = _ctx(
        tmp_path,
        coord_dir=coord_dir,
        checkouts=(Checkout(name="vimcode", path=tmp_path / "src" / "vimcode"),),
    )
    result = cargo_targets.probe_cargo_targets(ctx)

    assert result.severity is Severity.CRIT, (
        "the shipped cargo-target defaults would NOT have caught the 78G that "
        "filled elitebook's disk"
    )
    assert result.values["total_gb"] == pytest.approx(78.0, abs=0.1)
    # The report must name the offenders — "78G somewhere" is not actionable.
    assert "~/src/vimcode/target" in result.headroom


def test_cargo_totals_the_shared_cache_together_with_checkout_targets(
    tmp_path, monkeypatch
) -> None:
    """Neither source alone would have tripped the threshold.

    The shared cache is GC'd at a 20 GiB cap and a single checkout's target/
    was 43G — under the 60G crit. Only the *sum* is the number that filled
    the disk, which is why this probe totals across sources rather than
    reporting each in isolation.
    """
    coord_dir = tmp_path / ".coord"
    (coord_dir / "cargo-target" / "vimcode").mkdir(parents=True)
    (tmp_path / "src" / "a" / "target").mkdir(parents=True)
    (tmp_path / "src" / "b" / "target").mkdir(parents=True)

    monkeypatch.setattr(
        cargo_targets,
        "_dir_size_budgeted",
        lambda path, deadline: (int(24 * 1024**3), True),  # 3 x 24G = 72G
    )
    ctx = _ctx(
        tmp_path,
        coord_dir=coord_dir,
        checkouts=(
            Checkout(name="a", path=tmp_path / "src" / "a"),
            Checkout(name="b", path=tmp_path / "src" / "b"),
        ),
    )
    result = cargo_targets.probe_cargo_targets(ctx)
    assert len(result.values["dirs"]) == 3
    assert result.severity is Severity.CRIT


# ── incident 3: vimcode graph 128.8h stale, hooks disabled ───────────────────


def _stale_graph_status(age_hours: float):
    from coord.graph_health import GraphStatus

    return GraphStatus(
        repo_path=Path("/home/john/src/vimcode"),
        present=True,
        built_sha="5be69d08",
        head_sha="9da9ff62aaaa",
        in_sync=False,
        age_seconds=age_hours * 3600.0,
        # No manifest.json re-verification — the graph really is behind.
        verified_at=None,
        head_committed_at=None,
    )


def test_vimcode_graph_128h_stale_with_hooks_disabled_is_crit_at_defaults(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "coord.graph_health.graph_status",
        lambda p, default_branch="main": _stale_graph_status(VIMCODE_GRAPH_STALE_HOURS),
    )
    monkeypatch.setattr(
        "coord.graph_health.hooks_path_status",
        lambda p: (
            False,
            "core.hooksPath is unset — worktrees on this machine will NOT get "
            "a linked graph.",
        ),
    )
    ctx = _ctx(
        tmp_path,
        checkouts=(Checkout(name="vimcode", path=tmp_path / "src" / "vimcode"),),
    )

    (result,) = graph.probe_graph(ctx)

    assert result.severity is Severity.CRIT, (
        "the shipped graph defaults would NOT have caught vimcode's 128.8h "
        "stale graph with hooks disabled"
    )
    assert result.headroom == "128.8h stale, hooks disabled -> will not self-heal"
    assert result.values["hooks_ok"] is False
    assert result.values["age_hours"] == pytest.approx(128.8)
    assert "graphify update" in result.detail


def test_hooks_disabled_makes_a_stale_graph_crit_at_any_age(
    tmp_path, monkeypatch
) -> None:
    """Time cannot fix a checkout whose hooks will never fire.

    With hooks working, a 1h-stale graph is a WARN that the next commit
    clears. With them disabled it is a permanent wrong answer, so age is
    irrelevant — this is the distinction the incident turned on.
    """
    monkeypatch.setattr(
        "coord.graph_health.graph_status", lambda p, default_branch="main": _stale_graph_status(1.0)
    )
    monkeypatch.setattr(
        "coord.graph_health.hooks_path_status", lambda p: (False, "core.hooksPath is unset")
    )
    ctx = _ctx(tmp_path, checkouts=(Checkout(name="vimcode", path=tmp_path),))
    (crit,) = graph.probe_graph(ctx)
    assert crit.severity is Severity.CRIT

    monkeypatch.setattr(
        "coord.graph_health.hooks_path_status", lambda p: (True, "core.hooksPath=.githooks")
    )
    (warn,) = graph.probe_graph(ctx)
    assert warn.severity is Severity.WARN


# ── the defaults themselves ──────────────────────────────────────────────────


def test_shipped_defaults_match_the_issue_table() -> None:
    """A guard on the numbers the milestone was specified with.

    Written out literally so a drive-by "tuning" edit to HealthConfig shows
    up as a failing assertion naming the incident it stops catching, rather
    than as a silently quieter fleet.
    """
    defaults = HealthConfig()
    assert (defaults.disk_warn_free_pct, defaults.disk_crit_free_pct) == (15.0, 7.0)
    assert (defaults.cargo_target_warn_gb, defaults.cargo_target_crit_gb) == (40.0, 60.0)
    assert (defaults.worktree_warn_count, defaults.worktree_crit_count) == (3, 10)
    assert (defaults.agent_version_warn_behind, defaults.agent_version_crit_behind) == (1, 2)
    assert defaults.pypi_index_url == "https://pypi.org/simple"
    assert defaults.enabled is True
