"""#2208: `--config <scratch-file>` on the daemon host must not wipe the
shared ``machines`` table.

Root cause: ``_load_config`` always called ``_save_config_snapshot``, which
unconditionally ``DELETE FROM machines`` + re-inserts from whatever config was
just loaded — including a throwaway ``--config /tmp/mini2.yml`` a worker
pointed at purely so ``coord.config.load()`` had something to parse. The
#584 guard immediately above the ``DELETE`` only protects a *thin client*
(``board_service`` configured); it does nothing about a host-side invocation
carrying a non-canonical ``--config``, which is exactly the dangerous case:
on the daemon host that guard is inert and the write always lands.

Fix: ``_save_config_snapshot`` now takes the ``config_path`` the config was
actually loaded from and skips the write (emitting a one-line stderr note
instead of failing silently) unless that path is exactly what
``coord.config.resolve_config_path()`` resolves to on its own — i.e. unless
the caller did not actually override anything. This covers the acceptance
criteria verbatim:

1. ``--config <tmpfile>`` whose ``machines:`` differ from the canonical
   config leaves the ``machines`` table unchanged (the exact regression).
2. The same command with the canonical config still snapshots normally.
3. A thin client still never writes (the #584 guard is preserved).
4. The ``machines`` table after a ``--config`` override still lists the
   real fleet — a plain follow-up run (no override) repairs it either way.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from coord.cli import main
from coord.commands._common import _load_config, _save_config_snapshot
from coord.config import Config
from coord.models import Machine, Repo


REAL_FLEET_YAML = """\
repos:
  - name: real-repo
    github: acme/real-repo
machines:
  - name: laptop
    host: laptop.tailnet
    capabilities: [python]
    repos: [real-repo]
  - name: server
    host: server.tailnet
    capabilities: [python, docker]
    repos: [real-repo]
"""

# Mirrors the issue's exact repro file (#2180's worker's /tmp/mini2.yml):
# just enough for coord.config.load() to parse, explicitly not a real
# fleet machine.
SCRATCH_YAML = """\
# Not a real fleet machine -- just enough for coord.config.load() to parse.
repos:
  - name: scratch-repo
    github: acme/scratch-repo
machines:
  - name: ci-runner
    host: ci-runner
"""


def _real_fleet_config() -> Config:
    return Config(
        repos=[Repo(name="real-repo", github="acme/real-repo")],
        machines=[
            Machine(name="laptop", host="laptop.tailnet",
                     capabilities=["python"], repos=["real-repo"]),
            Machine(name="server", host="server.tailnet",
                     capabilities=["python", "docker"], repos=["real-repo"]),
        ],
    )


@pytest.fixture
def canonical_config_path(tmp_path: Path, monkeypatch) -> Path:
    """Point ``resolve_config_path()``'s canonical home at a real-looking
    fleet config, mirroring ``~/.coord/coordinator.yml`` on the daemon host.
    """
    import coord.config as cfgmod

    monkeypatch.delenv("COORD_CONFIG", raising=False)
    p = tmp_path / "home-coordinator.yml"
    p.write_text(REAL_FLEET_YAML)
    monkeypatch.setattr(cfgmod, "USER_CONFIG_PATH", p)
    monkeypatch.setattr(cfgmod, "DEFAULT_CONFIG_PATH", tmp_path / "absent-cwd-coordinator.yml")
    return p


@pytest.fixture
def scratch_config_path(tmp_path: Path) -> Path:
    p = tmp_path / "mini2.yml"
    p.write_text(SCRATCH_YAML)
    return p


def _machine_names(coord_db) -> list[str]:
    rows = coord_db.execute("SELECT name FROM machines ORDER BY name").fetchall()
    return [r["name"] for r in rows]


class TestNonCanonicalConfigLeavesMachinesUnchanged:
    """Acceptance criterion 1 (the exact regression): --config <tmpfile>
    whose machines: differ from the canonical config leaves `machines`
    unchanged. This must fail before the fix."""

    def test_load_config_with_scratch_override_does_not_wipe_machines(
        self, coord_db, canonical_config_path, scratch_config_path, capsys
    ):
        # Seed the table with the real fleet, as if a prior `coord config`
        # had already snapshotted it from the canonical file.
        _save_config_snapshot(_real_fleet_config())
        assert _machine_names(coord_db) == ["laptop", "server"]

        cfg = _load_config(scratch_config_path)

        # The scratch config parsed fine — that's its whole point...
        assert [m.name for m in cfg.machines] == ["ci-runner"]
        # ...but the shared table is untouched.
        assert _machine_names(coord_db) == ["laptop", "server"]

        err = capsys.readouterr().err
        assert "non-canonical" in err
        assert str(scratch_config_path) in err

    def test_cli_config_command_with_scratch_override_does_not_wipe_machines(
        self, coord_db, canonical_config_path, scratch_config_path
    ):
        """Mirrors the issue's exact repro: a `coord ... --config
        /tmp/mini2.yml` invocation on the host must not clobber `machines`,
        and /board would agree with `coord config`'s own parsed-config
        output since neither touches the shared table here."""
        _save_config_snapshot(_real_fleet_config())

        runner = CliRunner()
        result = runner.invoke(main, ["config", "--config", str(scratch_config_path)])

        assert result.exit_code == 0, result.output
        assert "ci-runner" in result.output  # `coord config` itself is unaffected
        assert _machine_names(coord_db) == ["laptop", "server"]


class TestCanonicalConfigStillSnapshotsNormally:
    """Acceptance criterion 2: the same command with the canonical config
    still snapshots normally."""

    def test_load_config_with_explicit_canonical_path_snapshots(
        self, coord_db, canonical_config_path
    ):
        # Explicitly passing --config pointed at the very file the default
        # resolution would have picked is NOT an override — it must still
        # write.
        cfg = _load_config(canonical_config_path)
        assert [m.name for m in cfg.machines] == ["laptop", "server"]
        assert _machine_names(coord_db) == ["laptop", "server"]

    def test_load_config_with_no_override_snapshots(self, coord_db, canonical_config_path):
        """The default resolution path (no explicit --config at all) must
        still write — this is what `coord config` on the daemon host relies
        on to repair a wiped table per the issue's documented runbook."""
        cfg = _load_config(None)
        assert [m.name for m in cfg.machines] == ["laptop", "server"]
        assert _machine_names(coord_db) == ["laptop", "server"]


class TestThinClientStillNeverWrites:
    """Acceptance criterion 3: a thin client still never writes (the #584
    guard is preserved), independent of whether the resolved path looks
    canonical."""

    def test_thin_client_does_not_write_even_with_canonical_looking_path(
        self, coord_db, canonical_config_path, monkeypatch
    ):
        import coord.client as cc

        monkeypatch.setattr(
            cc, "resolve_board_service",
            lambda *a, **k: cc.ServiceConfig("http://daemon:7435"),
        )
        monkeypatch.setattr(cc, "fetch_remote_config", lambda svc, **kw: canonical_config_path)

        _load_config(canonical_config_path)

        assert _machine_names(coord_db) == []


class TestOverrideThenRepairRestoresRealFleet:
    """Acceptance criterion 4: the machines table after a --config override
    still lists the real fleet, and a plain follow-up run agrees."""

    def test_repair_after_override(self, coord_db, canonical_config_path, scratch_config_path):
        _save_config_snapshot(_real_fleet_config())

        _load_config(scratch_config_path)  # the bogus override: now a no-op
        assert _machine_names(coord_db) == ["laptop", "server"]

        # Even if the table HAD been left stale, a plain `coord config` run
        # (no override) repairs it from the canonical file.
        _load_config(None)
        assert _machine_names(coord_db) == ["laptop", "server"]
