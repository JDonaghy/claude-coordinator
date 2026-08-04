"""End-to-end tests for `coord diagnose --config-provenance` — driven through
the real Click command against real git checkouts, asserting on rendered
output (CLAUDE.md black-box coverage bar).

#1779: the daemon host's ~/.coord/coordinator.yml is now a symlink into the
coord-settings checkout, so content drift is structurally impossible — but
three narrower, equally-invisible failure modes remain: the symlink getting
replaced by a plain file, the checkout going dirty, and the checkout falling
behind (or ahead of) origin. This drives `coord diagnose --config-provenance`
itself, hermetically, via $COORD_CONFIG / $COORD_SETTINGS_DIR env overrides —
never touching the real $HOME.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from coord.commands.status import diagnose

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=30.0,
    )


def _make_checkout(root: Path) -> Path:
    checkout = root / "coord-settings"
    checkout.mkdir(parents=True)
    _git("init", "-q", ".", cwd=checkout)
    (checkout / "coord").mkdir()
    (checkout / "coord" / "coordinator.yml").write_text(
        "repos: []\nmachines: []\n", encoding="utf-8"
    )
    _git("add", "-A", cwd=checkout)
    _git("commit", "-q", "-m", "init", cwd=checkout)
    remote = root / "coord-settings-remote.git"
    _git("init", "-q", "--bare", str(remote), cwd=root)
    _git("remote", "add", "origin", str(remote), cwd=checkout)
    _git("push", "-q", "-u", "origin", "HEAD:main", cwd=checkout)
    return checkout


def _run(monkeypatch, tmp_path: Path, checkout: Path, live: Path) -> str:
    monkeypatch.setenv("COORD_SETTINGS_DIR", str(checkout))
    monkeypatch.setenv("COORD_CONFIG", str(live))
    # --config here is the UNRELATED "which coordinator.yml does the CLI
    # itself load" option (_CONFIG_OPTION) — --config-provenance never reads
    # it, but a nonexistent default path must not make the command explode.
    result = CliRunner().invoke(
        diagnose,
        ["--config-provenance", "--config", str(tmp_path / "unused-coordinator.yml")],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    return result.output


# ── named acceptance tests ───────────────────────────────────────────────────


def test_neutral_skip_when_no_coord_settings_checkout(tmp_path: Path, monkeypatch) -> None:
    """#1779 acceptance: a machine with no checkout reports a neutral skip."""
    live = tmp_path / "home" / ".coord" / "coordinator.yml"
    out = _run(monkeypatch, tmp_path, tmp_path / "no-such-checkout", live)

    assert "REGRESSION" not in out
    assert "⚠" not in out
    assert "no coord-settings checkout" in out
    assert "CONFIG_PROVENANCE: checkout=absent skip=true" in out


def test_flags_a_live_config_that_is_not_a_symlink(tmp_path: Path, monkeypatch) -> None:
    """#1779 acceptance: a live config that is NOT a symlink into the
    checkout is reported distinctly and prominently — the regression case
    `coord init`/scp/an editor can silently cause."""
    checkout = _make_checkout(tmp_path)
    live = tmp_path / "home" / ".coord" / "coordinator.yml"
    live.parent.mkdir(parents=True)
    live.write_text("repos: []\n", encoding="utf-8")  # plain file, not a symlink

    out = _run(monkeypatch, tmp_path, checkout, live)

    assert "REGRESSION" in out
    assert "REGULAR FILE" in out
    assert "CONFIG_PROVENANCE: checkout=present symlinked=false" in out


# ── other distinctly-reported states ─────────────────────────────────────────


def test_reports_a_clean_in_sync_symlinked_config(tmp_path: Path, monkeypatch) -> None:
    checkout = _make_checkout(tmp_path)
    live = tmp_path / "home" / ".coord" / "coordinator.yml"
    live.parent.mkdir(parents=True)
    live.symlink_to(checkout / "coord" / "coordinator.yml")

    out = _run(monkeypatch, tmp_path, checkout, live)

    assert "symlinked into the checkout" in out
    assert "checkout is clean" in out
    assert "in sync with" in out
    assert "REGRESSION" not in out
    assert "CONFIG_PROVENANCE: checkout=present symlinked=true dirty=false behind=0 ahead=0" in out


def test_reports_uncommitted_changes_distinctly_from_symlink_regression(
    tmp_path: Path, monkeypatch
) -> None:
    checkout = _make_checkout(tmp_path)
    live = tmp_path / "home" / ".coord" / "coordinator.yml"
    live.parent.mkdir(parents=True)
    live.symlink_to(checkout / "coord" / "coordinator.yml")
    live.write_text("repos: []\n# edited live\n", encoding="utf-8")

    out = _run(monkeypatch, tmp_path, checkout, live)

    assert "uncommitted changes" in out
    assert "REGRESSION" not in out


def test_reports_behind_origin_distinctly_from_dirty_and_regression(
    tmp_path: Path, monkeypatch
) -> None:
    checkout = _make_checkout(tmp_path)
    live = tmp_path / "home" / ".coord" / "coordinator.yml"
    live.parent.mkdir(parents=True)
    live.symlink_to(checkout / "coord" / "coordinator.yml")

    (checkout / "coord" / "coordinator.yml").write_text(
        "repos: []\n# newer, pushed\n", encoding="utf-8"
    )
    _git("commit", "-q", "-am", "newer", cwd=checkout)
    # Explicit refspec: local/remote branch names differ (see the sibling
    # unit test in test_fleet_config_health.py for why a bare push fails).
    _git("push", "-q", "origin", "HEAD:main", cwd=checkout)
    _git("reset", "-q", "--hard", "HEAD~1", cwd=checkout)

    out = _run(monkeypatch, tmp_path, checkout, live)

    assert "behind" in out
    assert "not yet deployed" in out
    assert "uncommitted changes" not in out
    assert "REGRESSION" not in out
    assert "CONFIG_PROVENANCE: checkout=present symlinked=true dirty=false behind=1 ahead=0" in out
