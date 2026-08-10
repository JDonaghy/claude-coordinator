"""`POST /restart-services` — the rest of a python-lane roll (#2069).

``POST /update`` swaps the venv and restarts ONLY ``coord-agent``. Before
this endpoint existed, ``coord-serve``, ``coord-web`` and
``coord-drive-queue`` kept running the generation they started with until a
human restarted them by hand — the concrete cost the issue names: v0.5.13
fixed a bug inside ``coord/review.py``, which runs *inside* ``coord-serve``,
and the daemon kept serving v0.5.8's code under a v0.5.13 label until someone
noticed and restarted it manually.

What is tested here is the HTTP contract `coord/commands/release.py`'s
``_restart_sibling_services`` depends on: which unit gets touched (only ones
actually running on this host — a topology fact, never assumed), that
``coord-agent`` itself is refused (it has its own restart path already), and
that the response distinguishes "restarted", "not running here" and
"restart failed" rather than collapsing them into one boolean.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from coord import agent_app
from coord.agent import AgentServer
from coord.agent_app import build_app
from coord.health.checks import spawned_coord


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    server = AgentServer(
        machine_name="dellserver",
        capabilities=["python"],
        repos=[],
        state_dir=tmp_path / "state",
        worker_command=lambda spec: ["/bin/true"],
        repo_paths={},
    )
    return TestClient(build_app(server))


@pytest.fixture(autouse=True)
def _under_systemd(monkeypatch):
    """Every test here models a systemd-managed host unless it says otherwise."""
    monkeypatch.setattr(agent_app, "_running_under_systemd", lambda: True)


def test_restarts_only_units_actually_running_here(client, monkeypatch):
    monkeypatch.setattr(
        spawned_coord, "running_unit_pids",
        lambda units: {"coord-serve": 111} if "coord-serve" in units else {},
    )
    restarted = []

    def _fake_restart(unit, *, timeout=30.0):
        restarted.append(unit)
        return True, "active"

    monkeypatch.setattr(agent_app, "_restart_sibling_unit", _fake_restart)

    resp = client.post("/restart-services", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert restarted == ["coord-serve"]
    assert body["units"]["coord-serve"] == {"restarted": True, "detail": "active"}
    # coord-web / coord-drive-queue were considered (the default set) but
    # never actually running here, so nothing was touched for them.
    assert body["units"]["coord-web"]["restarted"] is None
    assert body["units"]["coord-drive-queue"]["restarted"] is None
    assert "not running" in body["units"]["coord-web"]["detail"]


def test_a_specific_units_list_is_honoured(client, monkeypatch):
    monkeypatch.setattr(
        spawned_coord, "running_unit_pids", lambda units: {u: 1 for u in units}
    )
    restarted = []
    monkeypatch.setattr(
        agent_app, "_restart_sibling_unit",
        lambda unit, *, timeout=30.0: (restarted.append(unit), (True, "active"))[1],
    )

    resp = client.post("/restart-services", json={"units": ["coord-web"]})
    assert resp.status_code == 200
    assert restarted == ["coord-web"]
    assert set(resp.json()["units"]) == {"coord-web"}


def test_coord_agent_is_refused_it_restarts_itself_already(client):
    """/update, /rollback and /restart already restart coord-agent. Doing it
    again from here would race those endpoints' own restart threads."""
    resp = client.post("/restart-services", json={"units": ["coord-agent"]})
    assert resp.status_code == 400
    assert "coord-agent" in resp.json()["error"]


def test_an_unknown_unit_name_is_refused(client):
    resp = client.post("/restart-services", json={"units": ["sshd"]})
    assert resp.status_code == 400
    assert "sshd" in resp.json()["error"]


def test_a_failed_restart_is_500_and_named(client, monkeypatch):
    monkeypatch.setattr(
        spawned_coord, "running_unit_pids", lambda units: {"coord-serve": 1}
    )
    monkeypatch.setattr(
        agent_app, "_restart_sibling_unit",
        lambda unit, *, timeout=30.0: (False, "still activating 30s after restart"),
    )
    resp = client.post("/restart-services", json={"units": ["coord-serve"]})
    assert resp.status_code == 500
    body = resp.json()
    assert body["units"]["coord-serve"]["restarted"] is False
    assert "still activating" in body["units"]["coord-serve"]["detail"]


def test_a_bodyless_post_defaults_to_every_restartable_unit(client, monkeypatch):
    seen = {}
    monkeypatch.setattr(
        spawned_coord, "running_unit_pids",
        lambda units: seen.setdefault("units", set(units)) and {} or {},
    )
    resp = client.post("/restart-services")
    assert resp.status_code == 200
    assert seen["units"] == agent_app.RESTARTABLE_SIBLING_UNITS


def test_not_running_under_systemd_is_a_no_op_200(client, monkeypatch):
    monkeypatch.setattr(agent_app, "_running_under_systemd", lambda: False)
    resp = client.post("/restart-services", json={})
    assert resp.status_code == 200
    assert resp.json()["units"] == {}


def test_the_endpoint_is_in_the_served_openapi_spec(client):
    spec = client.get("/openapi.json").json()
    assert "/restart-services" in spec["paths"]
