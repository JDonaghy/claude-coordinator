"""#1080: a thin client (``board_service`` configured) must never fall back to
a local ``coordinator.yml`` — ``_load_config`` must always perform a live
``GET /config`` fetch, even when a local file happens to exist with different
(stale/wrong) content.

Root cause (see #947 friction log): the old ``_load_config`` checked
``if not Path(path).exists()`` before deciding whether to consult the daemon,
so a stray local file silently won over the daemon's real config on any thin
client — with no signal that it had gone stale. The fix reorders the check so
"am I a thin client" is the primary branch, checked before local-file
existence.

Covers the three acceptance criteria verbatim:
1. Thin client + local file present with different content → daemon wins.
2. No ``client.toml``/``board_service`` (daemon host) → unchanged local
   resolution, no fetch attempt.
3. Thin client + unreachable daemon → fails clean, no silent fallback to the
   local file that happens to exist.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

import coord.client as cc
from coord.cli import main
from coord.commands._common import _load_config


REMOTE_YAML = """\
repos:
  - name: real-daemon-repo
    github: acme/real
machines:
  - name: server
    host: server.tailnet
    capabilities: [python]
    repos: [real-daemon-repo]
"""

STALE_LOCAL_YAML = """\
repos:
  - name: FAKE-STALE-MARKER
    github: acme/fake
machines:
  - name: laptop
    host: laptop.tailnet
    capabilities: [python]
    repos: [FAKE-STALE-MARKER]
"""


@pytest.fixture
def stale_local_config(tmp_path: Path) -> Path:
    """A local coordinator.yml a thin client must never trust (#1080)."""
    p = tmp_path / "coordinator.yml"
    p.write_text(STALE_LOCAL_YAML)
    return p


def _set_thin_client(monkeypatch) -> None:
    monkeypatch.setattr(
        cc, "resolve_board_service", lambda *a, **k: cc.ServiceConfig("http://daemon:7435")
    )


class TestThinClientAlwaysFetches:
    """Acceptance criterion 1: local file present + different content → daemon wins."""

    def test_load_config_ignores_existing_local_file(self, monkeypatch, stale_local_config):
        _set_thin_client(monkeypatch)
        monkeypatch.setattr(cc, "fetch_remote_config", lambda svc, **kw: _write_remote())

        cfg = _load_config(stale_local_config)

        assert cfg.repos[0].name == "real-daemon-repo"
        assert all(r.name != "FAKE-STALE-MARKER" for r in cfg.repos)

    def test_cli_config_command_ignores_stray_local_file(self, monkeypatch, stale_local_config):
        """Mirrors the issue's own smoke test: `coord config` on a thin client
        with a bogus local coordinator.yml must print the daemon's config."""
        _set_thin_client(monkeypatch)
        monkeypatch.setattr(cc, "fetch_remote_config", lambda svc, **kw: _write_remote())

        runner = CliRunner()
        result = runner.invoke(main, ["config", "--config", str(stale_local_config)])

        assert result.exit_code == 0, result.output
        assert "real-daemon-repo" in result.output
        assert "FAKE-STALE-MARKER" not in result.output


class TestNonThinClientUnchanged:
    """Acceptance criterion 2: no board_service configured → today's local
    resolution, unaffected, no fetch attempted."""

    def test_load_config_uses_local_file_when_no_service(self, monkeypatch, valid_config_path):
        # _no_board_service autouse fixture already keeps resolve_board_service()
        # returning None; assert explicitly and prove fetch is never attempted.
        assert cc.resolve_board_service() is None

        def _boom(*a, **k):
            raise AssertionError("fetch_remote_config must not be called when svc is None")

        monkeypatch.setattr(cc, "fetch_remote_config", _boom)

        cfg = _load_config(valid_config_path)
        assert cfg.repos[0].name == "api"


class TestThinClientUnreachableDaemonFailsClean:
    """Acceptance criterion 3: unreachable daemon fails the same way it did
    before — no silent fallback to a local file that happens to exist."""

    def test_fetch_failure_raises_clean_error_even_with_local_file_present(
        self, monkeypatch, stale_local_config, capsys
    ):
        _set_thin_client(monkeypatch)

        def _unreachable(svc, **kw):
            raise ConnectionError("could not connect")

        monkeypatch.setattr(cc, "fetch_remote_config", _unreachable)

        with pytest.raises(SystemExit) as exc_info:
            _load_config(stale_local_config)

        assert exc_info.value.code == 2
        err = capsys.readouterr().err
        assert "could not fetch config" in err
        # Never silently fell through to the stale local file's content.
        assert "FAKE-STALE-MARKER" not in err

    def test_fetch_failure_when_no_local_file_exists_either(self, monkeypatch, tmp_path):
        """Sanity: the failure mode is identical whether or not a local file
        happens to exist — presence of a local file must never change the
        outcome on a thin client."""
        _set_thin_client(monkeypatch)
        missing_path = tmp_path / "does-not-exist.yml"

        def _unreachable(svc, **kw):
            raise ConnectionError("could not connect")

        monkeypatch.setattr(cc, "fetch_remote_config", _unreachable)

        with pytest.raises(SystemExit) as exc_info:
            _load_config(missing_path)

        assert exc_info.value.code == 2


def _write_remote() -> Path:
    """Stand in for ``fetch_remote_config``'s real behavior: cache the
    daemon's YAML to a fresh temp file and return that path, without
    touching the real ``~/.coord`` cache location."""
    import tempfile

    fd_path = Path(tempfile.mkstemp(suffix=".yml")[1])
    fd_path.write_text(REMOTE_YAML)
    return fd_path
