"""Unit tests for :mod:`coord.fleet_config_health` — #1779.

Drives real git checkouts (a fake ``coord-settings`` checkout + a fake live
``coordinator.yml`` path) so the symlink/dirty/behind-origin detection is
exercised against real filesystem + git state, not mocks.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from coord.fleet_config_health import (
    ConfigProvenance,
    config_provenance,
    default_live_config_path,
    default_settings_dir,
    format_provenance_lines,
    summary_line,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


# ── helpers ──────────────────────────────────────────────────────────────────


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=30.0,
    )


def _make_checkout(root: Path, *, push: bool = True) -> Path:
    """A coord-settings checkout with a tracked coord/coordinator.yml,
    optionally with an ``origin`` remote-tracking ref already recorded
    locally (as if a prior ``git fetch``/``push`` happened — no network call
    is ever made by the code under test)."""
    checkout = root / "coord-settings"
    checkout.mkdir(parents=True)
    _git("init", "-q", ".", cwd=checkout)
    (checkout / "coord").mkdir()
    (checkout / "coord" / "coordinator.yml").write_text(
        "repos: []\nmachines: []\n", encoding="utf-8"
    )
    _git("add", "-A", cwd=checkout)
    _git("commit", "-q", "-m", "init", cwd=checkout)
    if push:
        remote = root / "coord-settings-remote.git"
        _git("init", "-q", "--bare", str(remote), cwd=root)
        _git("remote", "add", "origin", str(remote), cwd=checkout)
        _git("push", "-q", "-u", "origin", "HEAD:main", cwd=checkout)
    return checkout


# ── env-resolution helpers ───────────────────────────────────────────────────


def test_default_settings_dir_honours_env_override(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("COORD_SETTINGS_DIR", str(tmp_path / "custom"))
    assert default_settings_dir() == tmp_path / "custom"


def test_default_settings_dir_falls_back_to_home_src(monkeypatch) -> None:
    monkeypatch.delenv("COORD_SETTINGS_DIR", raising=False)
    assert default_settings_dir() == Path.home() / "src" / "coord-settings"


def test_default_live_config_path_honours_coord_config_env(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("COORD_CONFIG", str(tmp_path / "coordinator.yml"))
    assert default_live_config_path() == tmp_path / "coordinator.yml"


def test_default_live_config_path_falls_back_to_home_coord(monkeypatch) -> None:
    monkeypatch.delenv("COORD_CONFIG", raising=False)
    assert default_live_config_path() == Path.home() / ".coord" / "coordinator.yml"


# ── config_provenance: the four states ───────────────────────────────────────


def test_no_checkout_is_a_neutral_skip(tmp_path: Path) -> None:
    """#1779 acceptance: a machine with no coord-settings checkout at all
    (every agent/thin-client/ephemeral worker) must report skip, not a
    warning — this is the expected shape almost everywhere."""
    live = tmp_path / "home" / ".coord" / "coordinator.yml"
    prov = config_provenance(live_path=live, checkout_dir=tmp_path / "no-such-checkout")

    assert prov.checkout_present is False
    assert prov.skip is True
    assert prov.regression is False
    assert prov.healthy is False  # not healthy, but distinctly not a regression either

    lines = format_provenance_lines(prov)
    assert len(lines) == 1
    assert "no coord-settings checkout" in lines[0]
    assert "✗" not in lines[0] and "⚠" not in lines[0]
    assert summary_line(prov) == "CONFIG_PROVENANCE: checkout=absent skip=true"


def test_live_config_that_is_not_a_symlink_is_a_named_regression(tmp_path: Path) -> None:
    """#1779 acceptance: the highest-value finding. `coord init`, scp, or an
    editor writing-and-renaming silently swaps the symlink for a plain file —
    the fleet is running an untracked config again, and this must be reported
    distinctly and prominently, not folded into a generic 'drift' line."""
    checkout = _make_checkout(tmp_path)
    live = tmp_path / "home" / ".coord" / "coordinator.yml"
    live.parent.mkdir(parents=True)
    live.write_text("repos: []\n", encoding="utf-8")  # plain file — the regression

    prov = config_provenance(live_path=live, checkout_dir=checkout)

    assert prov.checkout_present is True
    assert prov.is_symlink is False
    assert prov.in_checkout is False
    assert prov.regression is True
    assert prov.skip is False

    lines = format_provenance_lines(prov)
    assert len(lines) == 1
    assert "REGRESSION" in lines[0]
    assert "not a symlink" in lines[0].lower() or "NOT a symlink" in lines[0]
    assert "REGULAR FILE" in lines[0]
    assert summary_line(prov) == (
        "CONFIG_PROVENANCE: checkout=present symlinked=false dirty=false behind=0 ahead=0"
    )


def test_live_config_missing_entirely_is_also_a_regression(tmp_path: Path) -> None:
    checkout = _make_checkout(tmp_path)
    live = tmp_path / "home" / ".coord" / "coordinator.yml"  # never created

    prov = config_provenance(live_path=live, checkout_dir=checkout)

    assert prov.regression is True
    lines = format_provenance_lines(prov)
    assert "does not exist" in lines[0]


def test_symlink_pointing_outside_the_checkout_is_a_regression(tmp_path: Path) -> None:
    checkout = _make_checkout(tmp_path)
    other = tmp_path / "unrelated.yml"
    other.write_text("repos: []\n", encoding="utf-8")
    live = tmp_path / "home" / ".coord" / "coordinator.yml"
    live.parent.mkdir(parents=True)
    live.symlink_to(other)

    prov = config_provenance(live_path=live, checkout_dir=checkout)

    assert prov.is_symlink is True
    assert prov.in_checkout is False
    assert prov.regression is True
    lines = format_provenance_lines(prov)
    assert "REGRESSION" in lines[0]
    assert "is a symlink" in lines[0]


def _symlinked_live(tmp_path: Path, checkout: Path) -> Path:
    live = tmp_path / "home" / ".coord" / "coordinator.yml"
    live.parent.mkdir(parents=True)
    live.symlink_to(checkout / "coord" / "coordinator.yml")
    return live


def test_clean_in_sync_symlinked_config_is_fully_healthy(tmp_path: Path) -> None:
    checkout = _make_checkout(tmp_path)
    live = _symlinked_live(tmp_path, checkout)

    prov = config_provenance(live_path=live, checkout_dir=checkout)

    assert prov.regression is False
    assert prov.dirty is False
    assert prov.behind == 0
    assert prov.ahead == 0
    assert prov.healthy is True

    lines = format_provenance_lines(prov)
    joined = "\n".join(lines)
    assert "symlinked into the checkout" in joined
    assert "checkout is clean" in joined
    assert "in sync with" in joined
    assert summary_line(prov) == (
        "CONFIG_PROVENANCE: checkout=present symlinked=true dirty=false behind=0 ahead=0"
    )


def test_uncommitted_edit_through_the_symlink_is_reported_as_dirty(tmp_path: Path) -> None:
    """#1779: a direct edit to the live path writes THROUGH the symlink into
    the checkout's working tree — recoverable, but must be reported
    distinctly from the not-a-symlink regression and from behind-origin."""
    checkout = _make_checkout(tmp_path)
    live = _symlinked_live(tmp_path, checkout)
    # Edit through the symlink, as an operator hand-editing the live path would.
    live.write_text("repos: []\nmachines: []\n# local edit\n", encoding="utf-8")

    prov = config_provenance(live_path=live, checkout_dir=checkout)

    assert prov.regression is False
    assert prov.dirty is True
    assert prov.dirty_files
    assert prov.healthy is False

    lines = format_provenance_lines(prov)
    joined = "\n".join(lines)
    assert "uncommitted changes" in joined
    assert "REGRESSION" not in joined
    assert summary_line(prov).startswith("CONFIG_PROVENANCE: checkout=present symlinked=true dirty=true")


def test_checkout_behind_origin_is_reported_distinctly(tmp_path: Path) -> None:
    """#1779: someone pushed a reviewed config change that was never pulled
    onto this host — the reviewed intent and the running fleet disagree.
    Must not require a fetch: the remote-tracking ref from the earlier push
    already recorded it locally."""
    checkout = _make_checkout(tmp_path)
    live = _symlinked_live(tmp_path, checkout)
    # A second, pushed commit the local checkout hasn't pulled.
    (checkout / "coord" / "coordinator.yml").write_text(
        "repos: []\nmachines: []\n# newer\n", encoding="utf-8"
    )
    _git("commit", "-q", "-am", "newer config", cwd=checkout)
    # Explicit refspec: the local branch (whatever `git init`'s default is,
    # e.g. "master") and the remote branch ("main") don't share a name, so a
    # bare `git push` would refuse under push.default=simple/current.
    _git("push", "-q", "origin", "HEAD:main", cwd=checkout)
    _git("reset", "-q", "--hard", "HEAD~1", cwd=checkout)  # local host never pulled it

    prov = config_provenance(live_path=live, checkout_dir=checkout)

    assert prov.regression is False
    assert prov.dirty is False
    assert prov.behind == 1
    assert prov.ahead == 0
    assert prov.healthy is False

    lines = format_provenance_lines(prov)
    joined = "\n".join(lines)
    assert "behind" in joined
    assert "not yet deployed" in joined
    assert "uncommitted changes" not in joined
    assert "REGRESSION" not in joined
    assert summary_line(prov) == (
        "CONFIG_PROVENANCE: checkout=present symlinked=true dirty=false behind=1 ahead=0"
    )


def test_checkout_ahead_of_origin_is_reported(tmp_path: Path) -> None:
    checkout = _make_checkout(tmp_path)
    live = _symlinked_live(tmp_path, checkout)
    (checkout / "coord" / "coordinator.yml").write_text(
        "repos: []\nmachines: []\n# not yet pushed\n", encoding="utf-8"
    )
    _git("commit", "-q", "-am", "local only", cwd=checkout)

    prov = config_provenance(live_path=live, checkout_dir=checkout)

    assert prov.behind == 0
    assert prov.ahead == 1
    lines = format_provenance_lines(prov)
    assert "ahead of" in "\n".join(lines)


def test_no_upstream_configured_reports_sync_unknown_not_healthy(tmp_path: Path) -> None:
    """No remote at all (e.g. a checkout cloned without one, or origin
    removed) must surface as unknown, never as a false 'in sync'."""
    checkout = _make_checkout(tmp_path, push=False)
    live = _symlinked_live(tmp_path, checkout)

    prov = config_provenance(live_path=live, checkout_dir=checkout)

    assert prov.sync_unknown_reason is not None
    assert prov.in_sync is False
    assert prov.healthy is False
    lines = format_provenance_lines(prov)
    assert "sync vs origin unknown" in "\n".join(lines)


def test_remote_tracking_config_is_never_the_live_path() -> None:
    """#1779 acceptance: coordinator.remote.yml (the thin-client GET /config
    cache — coord.client.REMOTE_CONFIG_CACHE) must never be inspected here.
    Static guard: the default live path resolution can never point at it."""
    assert default_live_config_path().name != "coordinator.remote.yml"
    assert default_live_config_path().name == "coordinator.yml"


def test_config_provenance_dataclass_defaults_are_unhealthy_and_not_skipped() -> None:
    """Sanity: a bare ConfigProvenance (as if something forgot to populate
    it) must not silently read as skip=True or healthy=True."""
    prov = ConfigProvenance(live_path=Path("/x"), checkout_dir=Path("/y"))
    assert prov.skip is True  # checkout_present defaults False -> correctly a skip
    assert prov.healthy is False
