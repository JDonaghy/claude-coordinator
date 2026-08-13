"""#2179: ``coord portal`` — status/heartbeat/push over the sync bridge client."""

from __future__ import annotations

import textwrap

import pytest
from click.testing import CliRunner

from coord.cli import main

CONFIG_YAML = textwrap.dedent("""
    repos:
      - name: coord
        github: owner/coord
    machines:
      - name: dellserver
        host: dellserver
        repos: [coord]
    portal:
      enabled: true
      base_url: https://intake.heurontech.com
      bridge_client_id: id-123
      bridge_client_secret: secret-456
""")

DISABLED_CONFIG_YAML = textwrap.dedent("""
    repos:
      - name: coord
        github: owner/coord
    machines:
      - name: dellserver
        host: dellserver
        repos: [coord]
""")


@pytest.fixture
def config_path(tmp_path):
    path = tmp_path / "coordinator.yml"
    path.write_text(CONFIG_YAML)
    return str(path)


@pytest.fixture
def disabled_config_path(tmp_path):
    path = tmp_path / "coordinator.yml"
    path.write_text(DISABLED_CONFIG_YAML)
    return str(path)


def run(*args):
    return CliRunner().invoke(main, list(args))


def test_portal_group_is_registered():
    result = run("portal", "--help")
    assert result.exit_code == 0
    for sub in ("status", "heartbeat", "push"):
        assert sub in result.output


def test_status_reports_disabled_by_default(disabled_config_path):
    result = run("portal", "status", "--config", disabled_config_path)
    assert result.exit_code == 0
    assert "disabled" in result.output


def test_status_reports_enabled_and_credentials(config_path):
    result = run("portal", "status", "--config", config_path)
    assert result.exit_code == 0
    assert "ENABLED" in result.output
    assert "intake.heurontech.com" in result.output
    assert "credentials=set" in result.output


def test_heartbeat_refuses_when_disabled(disabled_config_path):
    result = run("portal", "heartbeat", "--config", disabled_config_path)
    assert result.exit_code != 0
    assert "not enabled" in result.output


def test_push_refuses_when_disabled(disabled_config_path):
    result = run("portal", "push", "--config", disabled_config_path, "sub_1", "1", "shipped")
    assert result.exit_code != 0
    assert "not enabled" in result.output


def test_push_rejects_an_unrecognised_status(config_path):
    result = run("portal", "push", "--config", config_path, "sub_1", "1", "not-a-status")
    assert result.exit_code != 0
    assert "Invalid value" in result.output or "invalid" in result.output.lower()


def test_heartbeat_sends_and_reports_success(config_path, monkeypatch):
    def _post(url, json=None, headers=None, timeout=None):
        class _R:
            status_code = 200

            def json(self):
                return {"ok": True}

        return _R()

    monkeypatch.setattr("httpx.post", _post)
    result = run("portal", "heartbeat", "--config", config_path)
    assert result.exit_code == 0
    assert "sent" in result.output


def test_push_sends_and_reports_applied(config_path, monkeypatch):
    seen = {}

    def _post(url, json=None, headers=None, timeout=None):
        seen["json"] = json

        class _R:
            status_code = 200

            def json(self):
                return {"results": [{"submission_id": "sub_1", "outcome": "applied"}]}

        return _R()

    monkeypatch.setattr("httpx.post", _post)
    result = run("portal", "push", "--config", config_path, "sub_1", "3", "shipped")
    assert result.exit_code == 0
    assert "applied" in result.output
    assert seen["json"] == {
        "updates": [{"submission_id": "sub_1", "revision": 3, "fields": {"status": "shipped"}}]
    }


def test_push_reports_rejection_as_failure(config_path, monkeypatch):
    def _post(url, json=None, headers=None, timeout=None):
        class _R:
            status_code = 200

            def json(self):
                return {
                    "results": [
                        {"submission_id": "sub_1", "outcome": "rejected", "reason": "unknown_submission"}
                    ]
                }

        return _R()

    monkeypatch.setattr("httpx.post", _post)
    result = run("portal", "push", "--config", config_path, "sub_1", "3", "shipped")
    assert result.exit_code != 0
    assert "rejected" in result.output
    assert "unknown_submission" in result.output
