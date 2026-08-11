"""Tests for coord.agent_update — the blue/green venv swap (#1241).

These never shell out to a real `python -m venv` / `pip install` (slow,
network-dependent); `subprocess.run` is replaced with a stub that fakes
just enough of a venv's `bin/` layout for the module's own logic
(existence checks, atomic swap, cleanup-on-failure) to be exercised for
real against the actual filesystem.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from coord.agent_update import (
    current_slot,
    ensure_symlink_layout,
    perform_update,
    rollback,
)


def _make_fake_slot(slot: Path) -> None:
    """Populate *slot* with just enough of a venv's bin/ layout for the
    module's own existence checks to pass, without a real interpreter."""
    (slot / "bin").mkdir(parents=True, exist_ok=True)
    for name in ("python", "pip", "coord"):
        target = slot / "bin" / name
        target.write_text("#!/bin/sh\n")
        target.chmod(0o755)


def _run_stub(
    *,
    venv_ok: bool = True,
    install_ok: bool = True,
    smoke_import_ok: bool = True,
    smoke_coord_ok: bool = True,
    version: str = "9.9.9",
    calls: list | None = None,
):
    """Build a `subprocess.run` replacement that fakes venv/pip/coord.

    Dispatches on the shape of the command rather than exact argv, so it
    tolerates the module's own call-site details changing.
    """

    def _run(cmd, **kwargs):
        cmd = list(cmd)
        if calls is not None:
            calls.append(cmd)
        if "-m" in cmd and "venv" in cmd:
            slot = Path(cmd[-1])
            if not venv_ok:
                return subprocess.CompletedProcess(cmd, 1, "", "venv creation failed\n")
            _make_fake_slot(slot)
            return subprocess.CompletedProcess(cmd, 0, "created\n", "")
        if cmd[0].endswith("/bin/pip") and "install" in cmd:
            if not install_ok:
                return subprocess.CompletedProcess(cmd, 1, "", "pip failed\n")
            return subprocess.CompletedProcess(cmd, 0, "Successfully installed\n", "")
        if cmd[0].endswith("/bin/python") and "-c" in cmd:
            if not smoke_import_ok:
                return subprocess.CompletedProcess(cmd, 1, "", "ModuleNotFoundError\n")
            return subprocess.CompletedProcess(cmd, 0, f"{version}\n", "")
        if cmd[0].endswith("/bin/coord") and "--version" in cmd:
            if not smoke_coord_ok:
                return subprocess.CompletedProcess(cmd, 1, "", "coord is broken\n")
            return subprocess.CompletedProcess(cmd, 0, f"coord, version {version}\n", "")
        raise AssertionError(f"unexpected subprocess.run call: {cmd}")

    return _run


# ── ensure_symlink_layout / current_slot ────────────────────────────────


class TestSymlinkLayout:
    def test_current_slot_none_for_missing_venv(self, tmp_path: Path) -> None:
        assert current_slot(tmp_path / "nope") is None

    def test_current_slot_none_for_plain_directory(self, tmp_path: Path) -> None:
        venv_dir = tmp_path / ".coord-venv"
        venv_dir.mkdir()
        assert current_slot(venv_dir) is None

    def test_migrates_plain_directory_into_blue_slot(self, tmp_path: Path) -> None:
        venv_dir = tmp_path / ".coord-venv"
        venv_dir.mkdir()
        (venv_dir / "marker").write_text("original install\n")

        active = ensure_symlink_layout(venv_dir)

        assert active == tmp_path / ".coord-venv.blue"
        assert venv_dir.is_symlink()
        assert current_slot(venv_dir) == active
        assert (venv_dir / "marker").read_text() == "original install\n"

    def test_migration_is_idempotent(self, tmp_path: Path) -> None:
        venv_dir = tmp_path / ".coord-venv"
        venv_dir.mkdir()
        first = ensure_symlink_layout(venv_dir)
        second = ensure_symlink_layout(venv_dir)
        assert first == second

    def test_migrate_missing_venv_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            ensure_symlink_layout(tmp_path / "nope")


# ── perform_update: happy path ──────────────────────────────────────────


class TestPerformUpdateHappyPath:
    def test_first_update_migrates_and_swaps_to_green(self, tmp_path: Path) -> None:
        venv_dir = tmp_path / ".coord-venv"
        venv_dir.mkdir()
        (venv_dir / "marker").write_text("gen0\n")

        with patch("coord.agent_update.subprocess.run", side_effect=_run_stub(version="1.2.3")):
            result = perform_update(venv_dir, "code-coordinator[server]", target_version="1.2.3")

        assert result.ok is True
        assert result.swapped is True
        assert result.new_version == "1.2.3"
        assert current_slot(venv_dir) == tmp_path / ".coord-venv.green"
        # The pre-migration install survives as the (now-inactive) blue slot.
        assert (tmp_path / ".coord-venv.blue" / "marker").read_text() == "gen0\n"

    def test_second_update_swaps_back_to_blue_and_reuses_it(self, tmp_path: Path) -> None:
        venv_dir = tmp_path / ".coord-venv"
        venv_dir.mkdir()

        with patch("coord.agent_update.subprocess.run", side_effect=_run_stub(version="1.0.0")):
            perform_update(venv_dir, "pkg", target_version="1.0.0")
        assert current_slot(venv_dir) == tmp_path / ".coord-venv.green"

        with patch("coord.agent_update.subprocess.run", side_effect=_run_stub(version="2.0.0")):
            result = perform_update(venv_dir, "pkg", target_version="2.0.0")

        assert result.ok is True
        assert current_slot(venv_dir) == tmp_path / ".coord-venv.blue"
        # Exactly one prior generation is ever kept: green (now inactive)
        # still exists...
        assert (tmp_path / ".coord-venv.green").exists()
        # ...and blue was rebuilt fresh for this update, not left as gen0.
        assert not (tmp_path / ".coord-venv.blue" / "stale-gen0-marker").exists()

    def test_pins_exact_version_in_pip_install_spec(self, tmp_path: Path) -> None:
        venv_dir = tmp_path / ".coord-venv"
        venv_dir.mkdir()
        calls: list = []

        with patch(
            "coord.agent_update.subprocess.run",
            side_effect=_run_stub(version="3.4.5", calls=calls),
        ):
            perform_update(venv_dir, "code-coordinator[server]", target_version="3.4.5")

        pip_calls = [c for c in calls if c[0].endswith("/bin/pip")]
        assert len(pip_calls) == 1
        assert "code-coordinator[server]==3.4.5" in pip_calls[0]

    def test_no_pin_when_target_version_omitted(self, tmp_path: Path) -> None:
        venv_dir = tmp_path / ".coord-venv"
        venv_dir.mkdir()
        calls: list = []

        with patch(
            "coord.agent_update.subprocess.run",
            side_effect=_run_stub(version="3.4.5", calls=calls),
        ):
            perform_update(venv_dir, "code-coordinator[server]")

        pip_calls = [c for c in calls if c[0].endswith("/bin/pip")]
        assert "code-coordinator[server]" in pip_calls[0]
        assert not any("==" in arg for arg in pip_calls[0])


# ── perform_update: torn-install simulation (the core acceptance test) ──


class TestPerformUpdateNeverTorn:
    """#1241's black-box acceptance criterion: a failure partway through
    must never leave the live venv observing a partial install — the live
    `coord` package is always either fully the old version or fully the
    new one."""

    def test_venv_creation_failure_leaves_live_slot_untouched(self, tmp_path: Path) -> None:
        venv_dir = tmp_path / ".coord-venv"
        venv_dir.mkdir()
        (venv_dir / "marker").write_text("still-live\n")
        before = ensure_symlink_layout(venv_dir)

        with patch("coord.agent_update.subprocess.run", side_effect=_run_stub(venv_ok=False)):
            result = perform_update(venv_dir, "pkg", target_version="1.0.0")

        assert result.ok is False
        assert result.swapped is False
        assert current_slot(venv_dir) == before
        assert (venv_dir / "marker").read_text() == "still-live\n"

    def test_pip_failure_removes_half_built_slot_and_leaves_live_untouched(
        self, tmp_path: Path
    ) -> None:
        venv_dir = tmp_path / ".coord-venv"
        venv_dir.mkdir()
        before = ensure_symlink_layout(venv_dir)

        with patch(
            "coord.agent_update.subprocess.run", side_effect=_run_stub(install_ok=False)
        ):
            result = perform_update(venv_dir, "pkg", target_version="1.0.0")

        assert result.ok is False
        assert current_slot(venv_dir) == before
        # The half-built next slot (venv created, pip install torn/failed)
        # must not survive to be mistaken for a real install later.
        assert not (tmp_path / ".coord-venv.green").exists()

    def test_smoke_import_failure_removes_next_slot_and_never_swaps(
        self, tmp_path: Path
    ) -> None:
        """This is the exact ModuleNotFoundError scenario from #1241's
        motivating incident (state.py importing board_service before it
        existed) — caught by the smoke check before the swap ever happens."""
        venv_dir = tmp_path / ".coord-venv"
        venv_dir.mkdir()
        before = ensure_symlink_layout(venv_dir)

        with patch(
            "coord.agent_update.subprocess.run",
            side_effect=_run_stub(smoke_import_ok=False),
        ):
            result = perform_update(venv_dir, "pkg", target_version="1.0.0")

        assert result.ok is False
        assert current_slot(venv_dir) == before
        assert not (tmp_path / ".coord-venv.green").exists()

    def test_smoke_coord_version_failure_never_swaps(self, tmp_path: Path) -> None:
        venv_dir = tmp_path / ".coord-venv"
        venv_dir.mkdir()
        before = ensure_symlink_layout(venv_dir)

        with patch(
            "coord.agent_update.subprocess.run",
            side_effect=_run_stub(smoke_coord_ok=False),
        ):
            result = perform_update(venv_dir, "pkg", target_version="1.0.0")

        assert result.ok is False
        assert current_slot(venv_dir) == before

    def test_version_mismatch_against_target_fails_the_smoke_check(
        self, tmp_path: Path
    ) -> None:
        """A stale index resolving to the wrong version must fail loud, not
        silently swap onto an install that isn't actually the pinned
        target."""
        venv_dir = tmp_path / ".coord-venv"
        venv_dir.mkdir()

        with patch(
            "coord.agent_update.subprocess.run",
            side_effect=_run_stub(version="0.0.1"),
        ):
            result = perform_update(venv_dir, "pkg", target_version="9.9.9")

        assert result.ok is False
        assert not (tmp_path / ".coord-venv.green").exists()

    def test_stale_next_slot_from_interrupted_update_is_rebuilt_fresh(
        self, tmp_path: Path
    ) -> None:
        venv_dir = tmp_path / ".coord-venv"
        venv_dir.mkdir()
        stale = tmp_path / ".coord-venv.green"
        stale.mkdir()
        (stale / "half-written-junk").write_text("torn install from a killed update\n")

        with patch("coord.agent_update.subprocess.run", side_effect=_run_stub(version="1.0.0")):
            result = perform_update(venv_dir, "pkg", target_version="1.0.0")

        assert result.ok is True
        assert not (stale / "half-written-junk").exists()


# ── rollback ─────────────────────────────────────────────────────────────


class TestRollback:
    def test_rollback_with_no_previous_generation_fails(self, tmp_path: Path) -> None:
        venv_dir = tmp_path / ".coord-venv"
        venv_dir.mkdir()
        ensure_symlink_layout(venv_dir)

        result = rollback(venv_dir)

        assert result.ok is False
        assert "no previous generation" in (result.error or "")

    def test_rollback_flips_back_to_previous_slot(self, tmp_path: Path) -> None:
        venv_dir = tmp_path / ".coord-venv"
        venv_dir.mkdir()

        with patch("coord.agent_update.subprocess.run", side_effect=_run_stub(version="1.0.0")):
            perform_update(venv_dir, "pkg", target_version="1.0.0")
        blue = tmp_path / ".coord-venv.blue"
        green = tmp_path / ".coord-venv.green"
        assert current_slot(venv_dir) == green

        with patch("coord.agent_update.subprocess.run", side_effect=_run_stub(version="1.0.0")):
            result = rollback(venv_dir)

        assert result.ok is True
        assert result.swapped is True
        assert current_slot(venv_dir) == blue

    def test_rollback_refuses_when_previous_slot_fails_smoke_check(
        self, tmp_path: Path
    ) -> None:
        venv_dir = tmp_path / ".coord-venv"
        venv_dir.mkdir()

        with patch("coord.agent_update.subprocess.run", side_effect=_run_stub(version="1.0.0")):
            perform_update(venv_dir, "pkg", target_version="1.0.0")
        current_before = current_slot(venv_dir)

        with patch(
            "coord.agent_update.subprocess.run",
            side_effect=_run_stub(smoke_import_ok=False),
        ):
            result = rollback(venv_dir)

        assert result.ok is False
        assert current_slot(venv_dir) == current_before

    def test_rollback_on_unmigrated_venv_fails(self, tmp_path: Path) -> None:
        venv_dir = tmp_path / ".coord-venv"
        venv_dir.mkdir()

        result = rollback(venv_dir)

        assert result.ok is False
        assert "not a migrated blue/green venv" in (result.error or "")
