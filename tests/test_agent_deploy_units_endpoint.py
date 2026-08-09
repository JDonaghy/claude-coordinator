"""`POST /deploy-units` — the agent side of the `deploy/**` lane (#1831/#1835).

The property that makes this endpoint safe to call from an unattended
propagation timer, and the one worth a test: **it restarts nothing.** A
`systemctl --user daemon-reload` re-reads unit files; it does not restart
running services, so no in-flight headless worker dies here. That is the
whole reason it can be a synchronous 200 rather than `/update`'s
fire-and-forget 202.

The filesystem behaviour itself lives in `tests/test_deploy_units_lane.py`;
what is tested here is the HTTP contract the propagation shell depends on.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from coord import deploy_units as du
from coord.agent import AgentServer
from coord.agent_app import build_app


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


@pytest.fixture()
def lane(tmp_path, monkeypatch):
    """A packaged unit set and an installed one, both under tmp_path."""
    ref = tmp_path / "packaged"
    ref.mkdir()
    (ref / "coord-agent.service").write_text(
        "[Service]\nExecStart=coord agent --machine <MACHINE_NAME> --port <PORT>\n"
    )
    dest = tmp_path / "systemd-user"
    dest.mkdir()
    (dest / "coord-agent.service").write_text("[Service]\nExecStart=stale\n")

    real_install = du.install_units
    reloads: list[bool] = []

    def _install(**kwargs):
        kwargs.setdefault("reference_dir", ref)
        kwargs.setdefault("target_dir", dest)
        kwargs["reference_dir"] = ref
        kwargs["target_dir"] = dest
        return real_install(**kwargs)

    monkeypatch.setattr(du, "install_units", _install)
    monkeypatch.setattr(
        du, "daemon_reload",
        lambda **k: (reloads.append(True), (True, "daemon-reload ok"))[1],
    )
    return dest, reloads


def test_the_endpoint_deploys_and_reloads(client, lane):
    dest, reloads = lane
    resp = client.post("/deploy-units", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["reloaded"] is True
    # The machine name came from the *server*, so the template rendered for
    # this host rather than being copied verbatim (#1928).
    assert "--machine dellserver" in (dest / "coord-agent.service").read_text()
    assert reloads == [True]


def test_a_dry_run_writes_nothing_and_never_reloads(client, lane):
    dest, reloads = lane
    before = (dest / "coord-agent.service").read_text()
    resp = client.post("/deploy-units", json={"dry_run": True})
    assert resp.status_code == 200
    assert resp.json()["dry_run"] is True
    assert (dest / "coord-agent.service").read_text() == before
    assert reloads == []


def test_nothing_to_do_still_answers_200(client, lane):
    dest, reloads = lane
    client.post("/deploy-units", json={})
    reloads.clear()
    resp = client.post("/deploy-units", json={})
    assert resp.status_code == 200
    assert resp.json()["changed"] is False
    # No change => no reload. A reload is cheap but not free, and "we
    # reloaded" in the record should mean something actually moved.
    assert reloads == []


def test_a_bodyless_post_is_accepted(client, lane):
    """The propagation shell POSTs `{}`; a timer retrying with no body at all
    must not 500."""
    assert client.post("/deploy-units").status_code == 200


def test_the_endpoint_is_in_the_served_openapi_spec(client):
    spec = client.get("/openapi.json").json()
    assert "/deploy-units" in spec["paths"]
