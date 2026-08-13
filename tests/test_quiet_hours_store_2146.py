"""Operator-set quiet hours: a mutable store, a `/pause` action and a CLI
(#2146).

#1862 gave machines a quiet-hours window, but the only place to declare one
was `machines[i].quiet_hours` in `coordinator.yml` — which on a thin client is
a read-only cache the next command overwrites, and on the daemon host is a
symlink into a git checkout. Setting elitebook to 22:00-08:00 therefore cost
an Opus session, an ssh round trip, a rebase and a push.

Every test here is written against one of #2146's acceptance criteria:

* **Axis independence** — the `_save_state` contract. Four axes now share one
  file; a write to any one must preserve the other three. Tested per
  direction, not with one round trip, because the failure mode is silent
  (a cordon or a deliberate pause vanishing when someone sets a window).
* **Union precedence** — config-only / store-only / both (store wins, wholly,
  not field-merged) / store on a machine with no config block at all.
* **Wrapping boundaries on a store window** — the half-open `[start, end)`
  contract has to survive the new source, not just the config one.
* **`coord unpause` against a store window** — the interaction most likely to
  be missed: it must grant a real override and report the right
  `quiet_until`/`tz`, not report "not paused".
* **A malformed store row is dropped individually** — this store is read on
  every dispatch decision in the fleet.
* **Endpoint + thin client** — `set-quiet` reflected by `GET /pause`,
  malformed → 400 carrying the parser's message, and a thin-client write
  failure that PROPAGATES rather than printing a confirmation (#1563's
  failure class, and the single worst outcome for this feature).
"""
from __future__ import annotations

import json
from datetime import datetime, time, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner

from coord import client as coord_client
from coord import machine_pause as mp
from coord.cli import main
from coord.config import Config, ConfigError
from coord.models import Machine, QuietHours, Repo

CONFIG_WINDOW = QuietHours(start=time(1, 0), end=time(2, 0), tz="UTC")


@pytest.fixture()
def tmp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate the pause/quiet-hours store — it lives at $HOME/.coord/."""
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".coord").mkdir()
    return tmp_path


def _machine(name: str, quiet_hours: QuietHours | None = None) -> Machine:
    return Machine(name=name, host=f"{name}.tail", repos=["api"], quiet_hours=quiet_hours)


def _state(home: Path) -> dict:
    return json.loads((home / ".coord" / "paused_machines.json").read_text())


# ── Axis independence (the `_save_state` contract) ──────────────────────────


def test_setting_quiet_hours_preserves_an_explicit_pause(tmp_home: Path) -> None:
    mp.local_pause("elitebook")
    mp.local_set_quiet_hours("elitebook", start="22:00", end="08:00", tz="UTC")
    assert mp._explicit_paused_set() == {"elitebook"}


def test_setting_quiet_hours_preserves_a_release_cordon(tmp_home: Path) -> None:
    mp.local_set_cordon("server", target_version="0.5.31")
    mp.local_set_quiet_hours("server", start="22:00", end="08:00", tz="UTC")
    assert set(mp.local_cordons()) == {"server"}


def test_setting_quiet_hours_preserves_a_quiet_override(tmp_home: Path) -> None:
    machines = [_machine("elitebook", CONFIG_WINDOW)]
    now = datetime(2026, 1, 15, 1, 30, tzinfo=timezone.utc)
    mp.local_unpause_effective("elitebook", machines, now=now)
    assert mp._quiet_overrides()  # sanity: the override was recorded

    mp.local_set_quiet_hours("elitebook", start="22:00", end="08:00", tz="UTC")
    assert mp._quiet_overrides().get("elitebook")


def test_clearing_quiet_hours_preserves_the_other_three_axes(tmp_home: Path) -> None:
    mp.local_set_cordon("elitebook", target_version="0.5.31")
    # Grant an override BEFORE the explicit pause: an unpause on an
    # explicitly-paused machine lifts that pause instead (kind="resumed").
    mp.local_unpause_effective(
        "elitebook",
        [_machine("elitebook", CONFIG_WINDOW)],
        now=datetime(2026, 1, 15, 1, 30, tzinfo=timezone.utc),
    )
    mp.local_pause("elitebook")
    mp.local_set_quiet_hours("elitebook", start="22:00", end="08:00", tz="UTC")

    assert mp.local_clear_quiet_hours("elitebook") is True

    assert mp._explicit_paused_set() == {"elitebook"}
    assert set(mp.local_cordons()) == {"elitebook"}
    assert mp._quiet_overrides().get("elitebook")


def test_pausing_does_not_clear_quiet_hours(tmp_home: Path) -> None:
    mp.local_set_quiet_hours("elitebook", start="22:00", end="08:00", tz="UTC")
    mp.local_pause("elitebook")
    assert "elitebook" in mp._stored_quiet_windows()


def test_unpausing_does_not_clear_quiet_hours(tmp_home: Path) -> None:
    mp.local_set_quiet_hours("elitebook", start="22:00", end="08:00", tz="UTC")
    mp.local_pause("elitebook")
    mp.local_unpause("elitebook")
    assert "elitebook" in mp._stored_quiet_windows()


def test_cordon_writes_do_not_clear_quiet_hours(tmp_home: Path) -> None:
    mp.local_set_quiet_hours("server", start="22:00", end="08:00", tz="UTC")
    mp.local_set_cordon("server", target_version="0.5.31")
    mp.local_clear_cordon("server")
    assert "server" in mp._stored_quiet_windows()


def test_granting_a_quiet_override_does_not_clear_the_store_window(tmp_home: Path) -> None:
    mp.local_set_quiet_hours("elitebook", start="23:00", end="08:00", tz="UTC")
    machines = [_machine("elitebook")]
    mp.local_unpause_effective(
        "elitebook", machines, now=datetime(2026, 1, 15, 23, 30, tzinfo=timezone.utc),
    )
    assert "elitebook" in mp._stored_quiet_windows()


# ── Union precedence ────────────────────────────────────────────────────────


def test_union_config_only(tmp_home: Path) -> None:
    machines = [_machine("elitebook", CONFIG_WINDOW)]
    now = datetime(2026, 1, 15, 1, 30, tzinfo=timezone.utc)
    assert mp.local_paused_set(machines, now=now) == {"elitebook"}


def test_union_store_only(tmp_home: Path) -> None:
    """A machine with NO `quiet_hours:` block at all — the common case: an
    operator wants a window on a machine whose YAML never declared one."""
    mp.local_set_quiet_hours("elitebook", start="23:00", end="08:00", tz="UTC")
    machines = [_machine("elitebook"), _machine("server")]
    now = datetime(2026, 1, 15, 23, 30, tzinfo=timezone.utc)
    assert mp.local_paused_set(machines, now=now) == {"elitebook"}


def test_union_store_wins_over_config_entirely(tmp_home: Path) -> None:
    """Not a field-level merge: the store entry REPLACES the config block, so
    an instant the config window covers is no longer quiet unless the store
    window covers it too."""
    machines = [_machine("elitebook", CONFIG_WINDOW)]  # 01:00-02:00 UTC
    mp.local_set_quiet_hours("elitebook", start="13:00", end="14:00", tz="UTC")

    # An instant the CONFIG window covers and the STORE window does not: if
    # precedence were a field-level merge (or a union of the two windows),
    # this would still read as quiet.
    config_only = datetime(2026, 1, 15, 1, 30, tzinfo=timezone.utc)
    assert mp.local_paused_set(machines, now=config_only) == set()

    store_only = datetime(2026, 1, 15, 13, 30, tzinfo=timezone.utc)
    assert mp.local_paused_set(machines, now=store_only) == {"elitebook"}


def test_union_reported_source_follows_precedence(tmp_home: Path) -> None:
    machines = [_machine("elitebook", CONFIG_WINDOW), _machine("server")]
    assert mp.local_effective_quiet_hours(machines)["elitebook"]["source"] == "config"

    mp.local_set_quiet_hours("elitebook", start="23:00", end="08:00", tz="America/Chicago")
    rows = mp.local_effective_quiet_hours(machines)
    assert rows["elitebook"] == {
        "start": "23:00", "end": "08:00", "tz": "America/Chicago", "source": "store",
    }
    assert "server" not in rows  # no window from either source


def test_effective_quiet_hours_lists_windows_that_are_not_covering_now(
    tmp_home: Path,
) -> None:
    """The list/dialog surface needs EVERY machine with a window, not just the
    ones asleep this minute — otherwise `--list` at noon shows nothing and
    #2147's dialog has nothing to pre-fill from."""
    mp.local_set_quiet_hours("elitebook", start="23:00", end="08:00", tz="UTC")
    assert set(mp.local_effective_quiet_hours([_machine("elitebook")])) == {"elitebook"}


def test_quiet_paused_names_still_ignores_a_none_machines_arg(tmp_home: Path) -> None:
    """The documented pre-#1862 contract: `machines=None` → unchanged
    behaviour. All eight routing call sites pass `config.machines`."""
    mp.local_set_quiet_hours("elitebook", start="23:00", end="08:00", tz="UTC")
    now = datetime(2026, 1, 15, 23, 30, tzinfo=timezone.utc)
    assert mp.quiet_paused_names(None, now=now) == set()
    assert mp.local_paused_set(None, now=now) == set()


# ── Wrapping-window boundaries on a STORE-set window ────────────────────────


def test_store_window_covered_at_start(tmp_home: Path) -> None:
    mp.local_set_quiet_hours("elitebook", start="22:00", end="08:00", tz="UTC")
    machines = [_machine("elitebook")]
    at_start = datetime(2026, 1, 15, 22, 0, tzinfo=timezone.utc)
    assert mp.local_paused_set(machines, now=at_start) == {"elitebook"}


def test_store_window_not_covered_at_end(tmp_home: Path) -> None:
    """Half-open `[start, end)`: the machine wakes up exactly at `end`."""
    mp.local_set_quiet_hours("elitebook", start="22:00", end="08:00", tz="UTC")
    machines = [_machine("elitebook")]
    at_end = datetime(2026, 1, 16, 8, 0, tzinfo=timezone.utc)
    assert mp.local_paused_set(machines, now=at_end) == set()
    one_before = datetime(2026, 1, 16, 7, 59, tzinfo=timezone.utc)
    assert mp.local_paused_set(machines, now=one_before) == {"elitebook"}


def test_store_window_honours_a_non_utc_tz(tmp_home: Path) -> None:
    """23:00 Chicago is 05:00 UTC in January — a store window must pin to the
    same UTC instant a config one would, not to the daemon's clock."""
    mp.local_set_quiet_hours("elitebook", start="23:00", end="08:00", tz="America/Chicago")
    machines = [_machine("elitebook")]
    assert mp.local_paused_set(
        machines, now=datetime(2026, 1, 16, 5, 0, tzinfo=timezone.utc)
    ) == {"elitebook"}
    assert mp.local_paused_set(
        machines, now=datetime(2026, 1, 15, 23, 0, tzinfo=timezone.utc)
    ) == set()


# ── `coord unpause` against a STORE-set window ──────────────────────────────


def test_unpause_grants_an_override_against_a_store_window(tmp_home: Path) -> None:
    mp.local_set_quiet_hours("elitebook", start="22:00", end="08:00", tz="America/Chicago")
    machines = [_machine("elitebook")]  # no config block at all
    now = datetime(2026, 1, 16, 5, 0, tzinfo=timezone.utc)  # 23:00 Chicago

    outcome = mp.local_unpause_effective("elitebook", machines, now=now)
    assert outcome.changed is True
    assert outcome.kind == "quiet_override"
    assert outcome.quiet_until == "08:00"
    assert outcome.tz == "America/Chicago"

    # And it actually took: same window, next read, no longer paused.
    assert "elitebook" not in mp.local_paused_set(machines, now=now)


def test_unpause_override_against_a_store_window_expires_with_that_window(
    tmp_home: Path,
) -> None:
    mp.local_set_quiet_hours("elitebook", start="23:00", end="08:00", tz="UTC")
    machines = [_machine("elitebook")]
    mp.local_unpause_effective(
        "elitebook", machines, now=datetime(2026, 1, 15, 23, 30, tzinfo=timezone.utc),
    )
    next_night = datetime(2026, 1, 16, 23, 30, tzinfo=timezone.utc)
    assert "elitebook" in mp.local_paused_set(machines, now=next_night)


# ── Malformed rows ──────────────────────────────────────────────────────────


def test_a_malformed_store_row_is_dropped_individually(tmp_home: Path) -> None:
    """One bad row must not blank the whole read — this store is consulted on
    every dispatch decision in the fleet."""
    mp.local_set_quiet_hours("good", start="23:00", end="08:00", tz="UTC")
    raw = _state(tmp_home)
    raw["quiet_hours"]["bad_tz"] = {"start": "23:00", "end": "08:00", "tz": "Mars/Olympus"}
    raw["quiet_hours"]["no_tz"] = {"start": "23:00", "end": "08:00"}
    raw["quiet_hours"]["bad_time"] = {"start": "25:99", "end": "08:00", "tz": "UTC"}
    raw["quiet_hours"]["not_a_dict"] = "22:00-08:00"
    (tmp_home / ".coord" / "paused_machines.json").write_text(json.dumps(raw))

    assert set(mp._stored_quiet_windows()) == {"good"}
    now = datetime(2026, 1, 15, 23, 30, tzinfo=timezone.utc)
    machines = [_machine("good"), _machine("bad_tz"), _machine("no_tz")]
    assert mp.local_paused_set(machines, now=now) == {"good"}


def test_a_non_dict_quiet_hours_key_degrades_to_empty(tmp_home: Path) -> None:
    (tmp_home / ".coord" / "paused_machines.json").write_text(
        json.dumps({"paused": ["laptop"], "quiet_hours": ["nonsense"]})
    )
    assert mp._stored_quiet_windows() == {}
    assert mp.local_paused_set() == {"laptop"}


# ── Validation is SHARED with the config path ───────────────────────────────


@pytest.mark.parametrize(
    "kwargs",
    [
        {"start": "22:00", "end": "08:00", "tz": ""},          # tz required
        {"start": "22:00", "end": "08:00", "tz": "Mars/X"},    # not an IANA zone
        {"start": "22:00", "end": "22:00", "tz": "UTC"},       # start == end
        {"start": "25:00", "end": "08:00", "tz": "UTC"},       # not 24h HH:MM
        {"start": "10pm", "end": "08:00", "tz": "UTC"},
    ],
)
def test_store_rejects_exactly_what_coordinator_yml_rejects(
    tmp_home: Path, kwargs: dict
) -> None:
    with pytest.raises(ConfigError):
        mp.local_set_quiet_hours("elitebook", **kwargs)
    assert mp._quiet_hours_records() == {}  # nothing half-written


# ── Display provenance ──────────────────────────────────────────────────────


def test_describe_pause_state_names_a_store_window_as_set_here(tmp_home: Path) -> None:
    mp.local_set_quiet_hours("elitebook", start="23:00", end="08:00", tz="UTC")
    m = _machine("elitebook", CONFIG_WINDOW)
    now = datetime(2026, 1, 15, 23, 30, tzinfo=timezone.utc)
    state = mp.describe_pause_state(m, {"elitebook"}, now=now)
    assert state is not None
    assert state.kind == "quiet"
    assert "08:00" in state.detail
    assert "set here" in state.detail


def test_describe_pause_state_names_a_config_window_as_coordinator_yml(
    tmp_home: Path,
) -> None:
    m = _machine("elitebook", CONFIG_WINDOW)
    now = datetime(2026, 1, 15, 1, 30, tzinfo=timezone.utc)
    state = mp.describe_pause_state(m, {"elitebook"}, now=now)
    assert state is not None
    assert "from coordinator.yml" in state.detail


def test_describe_pause_state_can_render_from_a_fetched_map(tmp_home: Path) -> None:
    """A thin client has no local store — it renders from the daemon's
    `effective_quiet_hours()` map, or it would report a store-set window as a
    flat, wrong "PAUSED"."""
    m = _machine("elitebook")  # nothing known locally
    now = datetime(2026, 1, 15, 23, 30, tzinfo=timezone.utc)
    remote = {
        "elitebook": {"start": "23:00", "end": "08:00", "tz": "UTC", "source": "store"}
    }
    state = mp.describe_pause_state(m, {"elitebook"}, now=now, quiet_hours=remote)
    assert state is not None
    assert state.kind == "quiet"
    assert "set here" in state.detail


# ── Daemon endpoint ─────────────────────────────────────────────────────────


def _app(tmp_path: Path):
    from coord.dao import SqliteStore
    from coord.serve_app import build_app

    cfg = Config(
        repos=[Repo(name="api", github="acme/api")],
        machines=[
            Machine(name="elitebook", host="elitebook.tail", repos=["api"]),
            Machine(name="server", host="server.tail", repos=["api"]),
        ],
    )
    return build_app(SqliteStore(tmp_path / "board.db"), cfg)


def test_set_quiet_endpoint_is_reflected_by_get_pause(tmp_home: Path) -> None:
    from starlette.testclient import TestClient

    with TestClient(_app(tmp_home)) as cli:
        assert cli.get("/pause").json()["quiet_hours"] == {}

        resp = cli.post(
            "/pause",
            json={
                "machine": "elitebook", "action": "set-quiet",
                "start": "22:00", "end": "08:00", "tz": "America/Chicago",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["changed"] is True
        assert resp.json()["window"] == {
            "start": "22:00", "end": "08:00", "tz": "America/Chicago", "source": "store",
        }

        assert cli.get("/pause").json()["quiet_hours"] == {
            "elitebook": {
                "start": "22:00", "end": "08:00",
                "tz": "America/Chicago", "source": "store",
            }
        }

        # And clearing it round-trips, reporting changed=False the second time.
        assert cli.post(
            "/pause", json={"machine": "elitebook", "action": "clear-quiet"}
        ).json()["changed"] is True
        assert cli.post(
            "/pause", json={"machine": "elitebook", "action": "clear-quiet"}
        ).json()["changed"] is False
        assert cli.get("/pause").json()["quiet_hours"] == {}


def test_set_quiet_endpoint_400s_with_the_parsers_own_message(tmp_home: Path) -> None:
    from starlette.testclient import TestClient

    with TestClient(_app(tmp_home)) as cli:
        resp = cli.post(
            "/pause",
            json={
                "machine": "elitebook", "action": "set-quiet",
                "start": "22:00", "end": "08:00",  # no tz
            },
        )
        assert resp.status_code == 400
        assert "tz is required" in resp.json()["error"]

        resp = cli.post(
            "/pause",
            json={
                "machine": "elitebook", "action": "set-quiet",
                "start": "22:00", "end": "22:00", "tz": "UTC",
            },
        )
        assert resp.status_code == 400
        assert "must differ" in resp.json()["error"]

        # Nothing was written by either rejection.
        assert cli.get("/pause").json()["quiet_hours"] == {}


def test_set_quiet_endpoint_does_not_disturb_a_pause(tmp_home: Path) -> None:
    from starlette.testclient import TestClient

    with TestClient(_app(tmp_home)) as cli:
        cli.post("/pause", json={"machine": "server", "action": "pause"})
        cli.post(
            "/pause",
            json={
                "machine": "server", "action": "set-quiet",
                "start": "22:00", "end": "08:00", "tz": "UTC",
            },
        )
        assert cli.get("/pause").json()["paused"] == ["server"]
        cli.post("/pause", json={"machine": "server", "action": "clear-quiet"})
        assert cli.get("/pause").json()["paused"] == ["server"]


def test_thin_client_set_reaches_the_daemon(tmp_home: Path, monkeypatch) -> None:
    """The whole point: a laptop's `coord quiet-hours` must write the DAEMON's
    store, which is the copy its dispatch tick reads."""
    from starlette.testclient import TestClient

    with TestClient(_app(tmp_home)) as cli:
        monkeypatch.setattr(coord_client.httpx, "get", cli.get)
        monkeypatch.setattr(coord_client.httpx, "post", cli.post)
        monkeypatch.setattr(
            coord_client, "resolve_board_service",
            lambda *a, **k: coord_client.ServiceConfig(url="http://testserver"),
        )

        stored = mp.set_quiet_hours("elitebook", start="22:00", end="08:00", tz="UTC")
        assert stored["tz"] == "UTC"
        assert mp.effective_quiet_hours()["elitebook"]["source"] == "store"

        assert mp.clear_quiet_hours("elitebook") is True
        assert mp.effective_quiet_hours() == {}


def test_thin_client_write_failure_propagates(tmp_home: Path, monkeypatch) -> None:
    """#1563's failure class, which #2146 must not rebuild: a write that can't
    reach the daemon RAISES rather than reporting success — the local store is
    not a fallback, it is a different machine's state."""
    import httpx

    monkeypatch.setattr(
        mp, "_resolve_service", lambda: coord_client.ServiceConfig(url="http://unreachable")
    )

    def _boom(*args, **kwargs):
        raise httpx.ConnectError("no route to daemon")

    monkeypatch.setattr(coord_client, "post_record", _boom)

    with pytest.raises(httpx.HTTPError):
        mp.set_quiet_hours("elitebook", start="22:00", end="08:00", tz="UTC")
    with pytest.raises(httpx.HTTPError):
        mp.clear_quiet_hours("elitebook")
    # Crucially: it did NOT quietly write the client's own copy.
    assert not (tmp_home / ".coord" / "paused_machines.json").exists()


def test_effective_quiet_hours_read_is_fail_soft(tmp_home: Path, monkeypatch) -> None:
    """Reads degrade to "no windows known" rather than wedging a status
    render — same posture as `paused_set()`/`cordons()`."""
    import httpx

    monkeypatch.setattr(
        mp, "_resolve_service", lambda: coord_client.ServiceConfig(url="http://unreachable")
    )

    def _boom(*args, **kwargs):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(coord_client.httpx, "get", _boom)
    assert mp.effective_quiet_hours() == {}


# ── `coord quiet-hours` CLI ─────────────────────────────────────────────────


CONFIG_YAML = (
    "repos:\n"
    "  - name: api\n"
    "    github: acme/api\n"
    "machines:\n"
    "  - name: elitebook\n"
    "    host: elitebook.tail\n"
    "    repos: [api]\n"
    "  - name: server\n"
    "    host: server.tail\n"
    "    repos: [api]\n"
    "    quiet_hours:\n"
    "      start: \"01:00\"\n"
    "      end: \"02:00\"\n"
    "      tz: UTC\n"
)


@pytest.fixture()
def cfg_path(tmp_home: Path) -> Path:
    path = tmp_home / "coordinator.yml"
    path.write_text(CONFIG_YAML)
    return path


def _run(cfg_path: Path, *args: str):
    return CliRunner().invoke(main, ["quiet-hours", "--config", str(cfg_path), *args])


def test_cli_set_echoes_the_resolved_zone(cfg_path: Path) -> None:
    result = _run(cfg_path, "elitebook", "22:00-08:00", "--tz", "America/Chicago")
    assert result.exit_code == 0, result.output
    assert "22:00-08:00" in result.output
    # #2146: the RESOLVED zone is echoed, so a wrong default is visible
    # immediately rather than at the wrong hour tonight.
    assert "America/Chicago" in result.output
    assert mp._stored_quiet_windows()["elitebook"].tz == "America/Chicago"


def test_cli_set_rejects_a_malformed_window(cfg_path: Path) -> None:
    result = _run(cfg_path, "elitebook", "2200 to 0800", "--tz", "UTC")
    assert result.exit_code == 2
    assert "22:00-08:00" in result.output
    assert mp._quiet_hours_records() == {}


def test_cli_set_relays_the_validators_message(cfg_path: Path) -> None:
    result = _run(cfg_path, "elitebook", "22:00-22:00", "--tz", "UTC")
    assert result.exit_code == 1
    assert "must differ" in result.output


def test_cli_clear_with_nothing_set_says_so(cfg_path: Path) -> None:
    """#2146 acceptance: `--clear` on a machine with no store entry must
    report "nothing set", not claim success."""
    result = _run(cfg_path, "elitebook", "--clear")
    assert result.exit_code == 0, result.output
    assert "nothing set" in result.output


def test_cli_clear_removes_a_store_window_and_names_the_config_fallback(
    cfg_path: Path,
) -> None:
    mp.local_set_quiet_hours("server", start="22:00", end="08:00", tz="UTC")
    result = _run(cfg_path, "server", "--clear")
    assert result.exit_code == 0, result.output
    assert "cleared" in result.output
    # "server" still has a coordinator.yml block, which this cannot clear —
    # saying so is the difference between "quiet hours are off" and "quiet
    # hours reverted to the version-controlled window".
    assert "coordinator.yml" in result.output
    assert mp._quiet_hours_records() == {}


def test_cli_list_shows_source_for_both_kinds(cfg_path: Path) -> None:
    mp.local_set_quiet_hours("elitebook", start="22:00", end="08:00", tz="UTC")
    result = _run(cfg_path, "--list")
    assert result.exit_code == 0, result.output
    assert "elitebook" in result.output and "set here" in result.output
    assert "server" in result.output and "coordinator.yml" in result.output


def test_cli_list_with_nothing_set_says_so(cfg_path: Path, tmp_path: Path) -> None:
    empty = tmp_path / "empty.yml"
    empty.write_text(
        "repos:\n  - name: api\n    github: acme/api\n"
        "machines:\n  - name: elitebook\n    host: e.tail\n    repos: [api]\n"
    )
    result = _run(empty, "--list")
    assert result.exit_code == 0, result.output
    assert "no quiet hours set" in result.output


def test_cli_print_yaml_emits_a_paste_ready_block(cfg_path: Path) -> None:
    mp.local_set_quiet_hours("elitebook", start="22:00", end="08:00", tz="America/Chicago")
    result = _run(cfg_path, "elitebook", "--print-yaml")
    assert result.exit_code == 0, result.output
    assert "quiet_hours:" in result.output
    assert 'start: "22:00"' in result.output
    assert 'end: "08:00"' in result.output
    assert 'tz: "America/Chicago"' in result.output


def test_cli_print_yaml_block_actually_parses_as_config(cfg_path: Path) -> None:
    """The promotion path is only worth anything if the emitted block loads —
    which is why the store and `coordinator.yml` share one validator."""
    import textwrap

    from coord.config import load as load_config

    mp.local_set_quiet_hours("elitebook", start="22:00", end="08:00", tz="America/Chicago")
    block = "\n".join(
        line for line in _run(cfg_path, "elitebook", "--print-yaml").output.splitlines()
        if line.strip() and not line.strip().startswith("#")
    )
    promoted = cfg_path.parent / "promoted.yml"
    promoted.write_text(
        "repos:\n  - name: api\n    github: acme/api\n"
        "machines:\n  - name: elitebook\n    host: e.tail\n    repos: [api]\n"
        + textwrap.indent(textwrap.dedent(block), "    ")
    )
    machine = load_config(promoted).machines[0]
    assert machine.quiet_hours is not None
    assert machine.quiet_hours.tz == "America/Chicago"
    assert machine.quiet_hours.start == time(22, 0)


def test_cli_print_yaml_without_a_known_window_errors(cfg_path: Path) -> None:
    result = _run(cfg_path, "elitebook", "--print-yaml")
    assert result.exit_code == 1
    assert "no quiet hours known" in result.output


def test_cli_set_failure_does_not_print_a_confirmation(
    cfg_path: Path, monkeypatch
) -> None:
    import httpx

    def _boom(*args, **kwargs):
        raise httpx.ConnectError("no route to daemon")

    monkeypatch.setattr(mp, "set_quiet_hours", _boom)
    result = _run(cfg_path, "elitebook", "22:00-08:00", "--tz", "UTC")
    assert result.exit_code == 1
    assert "error" in result.output
    assert "quiet hours set" not in result.output
