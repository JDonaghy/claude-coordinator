"""`coord/deploy/` must stay byte-identical to the repo-root `deploy/` (#1927).

The unit-drift check (`coord/health/checks/unit_drift.py`) diffs each host's
installed systemd unit against the units *packaged with the installed
release*. Those live under `coord/deploy/` so they ship in the wheel — a
reference that cannot drift with the host's git checkout.

The reviewed source of truth is still the repo-root `deploy/`: it is what
every unit header, doc and provisioning script names, and what the other
`tests/test_deploy_*.py` modules read. The packaged copy is exactly that,
copied. This module is what stops the copy from going stale — the failure
mode is silent and nasty, because a stale packaged unit would make the
release *look* verified while comparing against the wrong file.

Nothing here needs a fleet, a config or a clock: it is a repo-layout
assertion.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE_DIR = REPO_ROOT / "deploy"
PACKAGED_DIR = REPO_ROOT / "coord" / "deploy"

UNIT_GLOBS = ("*.service", "*.timer")


def _units(directory: Path) -> dict[str, Path]:
    out: dict[str, Path] = {}
    for pattern in UNIT_GLOBS:
        for path in directory.glob(pattern):
            out[path.name] = path
    return out


SOURCE_UNITS = _units(SOURCE_DIR)


def test_source_deploy_dir_has_units() -> None:
    """Guards the guard: an empty source dir would make every other
    assertion here vacuously true."""
    assert "coord-serve.service" in SOURCE_UNITS
    assert "coord-agent.service" in SOURCE_UNITS


def test_packaged_dir_covers_every_source_unit() -> None:
    missing = sorted(set(SOURCE_UNITS) - set(_units(PACKAGED_DIR)))
    assert not missing, (
        f"deploy/ has units the wheel would not ship: {missing}. "
        "Run: cp deploy/*.service deploy/*.timer coord/deploy/"
    )


def test_packaged_dir_has_no_units_of_its_own() -> None:
    extra = sorted(set(_units(PACKAGED_DIR)) - set(SOURCE_UNITS))
    assert not extra, (
        f"coord/deploy/ carries units deploy/ does not: {extra}. The "
        "repo-root deploy/ is the reviewed source of truth; delete these or "
        "add them there."
    )


@pytest.mark.parametrize("name", sorted(SOURCE_UNITS))
def test_packaged_unit_is_byte_identical(name: str) -> None:
    """A drifted copy is worse than no copy: the check would report a
    confident green against the wrong reference."""
    packaged = PACKAGED_DIR / name
    assert packaged.exists(), f"missing coord/deploy/{name}"
    assert packaged.read_bytes() == (SOURCE_DIR / name).read_bytes(), (
        f"coord/deploy/{name} has drifted from deploy/{name}. deploy/ is the "
        f"source of truth — run: cp deploy/{name} coord/deploy/{name}"
    )


def test_pyproject_ships_the_packaged_units() -> None:
    """The copy is only worth having if setuptools puts it in the wheel."""
    import tomllib

    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    patterns = data["tool"]["setuptools"]["package-data"]["coord"]
    assert any(p.startswith("deploy/") for p in patterns), (
        "coord/deploy/ is not in [tool.setuptools.package-data]; the wheel "
        "would ship no reference units and every host would fall back to its "
        "own unverified checkout (#1927)"
    )
