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

ENV_VAR_CONFIG_YAML = textwrap.dedent("""
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
      bridge_client_id: ${BRIDGE_CLIENT_ID}
      bridge_client_secret: ${BRIDGE_CLIENT_SECRET}
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


@pytest.fixture
def env_var_config_path(tmp_path, monkeypatch):
    # #2336: BRIDGE_CLIENT_ID/BRIDGE_CLIENT_SECRET deliberately left unset in
    # this shell — mirrors a manual `ssh dellserver` session that never
    # sourced ~/.coord/coord-serve.env, unlike the coord-serve systemd unit's
    # EnvironmentFile=.
    monkeypatch.delenv("BRIDGE_CLIENT_ID", raising=False)
    monkeypatch.delenv("BRIDGE_CLIENT_SECRET", raising=False)
    path = tmp_path / "coordinator.yml"
    path.write_text(ENV_VAR_CONFIG_YAML)
    return str(path)


def run(*args):
    return CliRunner().invoke(main, list(args))


def test_portal_group_is_registered():
    result = run("portal", "--help")
    assert result.exit_code == 0
    for sub in ("status", "heartbeat", "push"):
        assert sub in result.output


def test_sync_loop_commands_are_registered():
    """#1982: the loop's operator surface."""
    result = run("portal", "--help")
    assert result.exit_code == 0
    for sub in (
        "sync",
        "outbox",
        "events",
        "enqueue-status",
        "enqueue-design-round",
        "enqueue-preview",
    ):
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


def test_status_reports_credentials_missing_for_unexpanded_env_var(env_var_config_path):
    """#2336: portal.bridge_client_id/secret are non-empty '${VAR}' strings,
    but the env var was never set in this shell — credentials_set must be
    false, not true just because the placeholder text itself is non-empty."""
    result = run("portal", "status", "--config", env_var_config_path, "--json")
    assert result.exit_code == 0, result.output
    assert '"credentials_set": false' in result.output


def test_status_reports_credentials_set_once_env_var_resolves(env_var_config_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_CLIENT_ID", "id-from-env")
    monkeypatch.setenv("BRIDGE_CLIENT_SECRET", "secret-from-env")
    result = run("portal", "status", "--config", env_var_config_path, "--json")
    assert result.exit_code == 0, result.output
    assert '"credentials_set": true' in result.output


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


def test_push_rejects_a_whitespace_only_submission_id_cleanly(config_path):
    """Regression for #2179 review: a caller-error submission_id must come
    back as a clean 'push failed: ...' message via PortalBridgeError, not an
    uncaught ValueError/traceback — portal_push only catches PortalBridgeError."""
    result = run("portal", "push", "--config", config_path, "   ", "1", "shipped")
    assert result.exit_code != 0
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "push failed" in result.output
    assert "submission_id" in result.output


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


# ── #1982: the sync loop's operator surface ─────────────────────────────────


def test_sync_refuses_when_disabled(disabled_config_path):
    result = run("portal", "sync", "--config", disabled_config_path)
    assert result.exit_code != 0
    assert "not enabled" in result.output


def test_sync_runs_a_pass_and_reports_it(config_path, monkeypatch):
    """One full pass over a stubbed portal: pull, push, heartbeat."""
    from coord.portal_sync import enqueue_status

    enqueue_status("sub_1", "in-progress")

    def _get(url, params=None, headers=None, timeout=None):
        class _R:
            status_code = 200

            def json(self):
                return {"events": [], "cursor": "c1", "has_more": False}

        return _R()

    def _post(url, json=None, headers=None, timeout=None):
        class _R:
            status_code = 200

            def json(self):
                if url.endswith("/heartbeat"):
                    return {"ok": True}
                return {"results": [{"submission_id": "sub_1", "outcome": "applied"}]}

        return _R()

    monkeypatch.setattr("httpx.get", _get)
    monkeypatch.setattr("httpx.post", _post)
    result = run("portal", "sync", "--config", config_path)
    assert result.exit_code == 0, result.output
    assert "applied=1" in result.output
    assert "heartbeat=ok" in result.output


def test_enqueue_status_refuses_an_announcement_with_nothing_to_announce():
    """#835: `awaiting-signoff` emails the customer — it must have content."""
    result = run("portal", "enqueue-status", "sub_1", "awaiting-signoff")
    assert result.exit_code != 0
    assert "design_round" in result.output


def test_enqueue_design_round_then_status_queues_both_in_order():
    ok = run(
        "portal", "enqueue-design-round", "sub_1", '{"round": 1, "outcome": "x"}'
    )
    assert ok.exit_code == 0, ok.output
    assert "seq=1" in ok.output

    status = run("portal", "enqueue-status", "sub_1", "awaiting-signoff")
    assert status.exit_code == 0, status.output
    assert "seq=2" in status.output

    listed = run("portal", "outbox")
    assert listed.exit_code == 0
    assert "design_round" in listed.output
    assert "HELD" in listed.output  # the announcement, until its round applies


def test_enqueue_design_round_rejects_invalid_json():
    result = run("portal", "enqueue-design-round", "sub_1", "{not json")
    assert result.exit_code != 0
    assert "not valid JSON" in result.output


def test_enqueue_status_refuses_quality_check_with_no_preview():
    """#2359: same #835 discipline, for the preview-approval gate."""
    result = run("portal", "enqueue-status", "sub_1", "quality-check")
    assert result.exit_code != 0
    assert "preview" in result.output


def test_enqueue_preview_then_status_queues_both_in_order():
    ok = run(
        "portal", "enqueue-preview", "sub_1", "https://pr-42.natal-chart.pages.dev"
    )
    assert ok.exit_code == 0, ok.output
    assert "seq=1" in ok.output

    status = run("portal", "enqueue-status", "sub_1", "quality-check")
    assert status.exit_code == 0, status.output
    assert "seq=2" in status.output

    listed = run("portal", "outbox")
    assert listed.exit_code == 0
    assert "preview" in listed.output
    assert "HELD" in listed.output  # the announcement, until its preview applies


def test_enqueue_preview_rejects_an_empty_url():
    result = run("portal", "enqueue-preview", "sub_1", "")
    assert result.exit_code != 0
    assert "non-empty" in result.output


def test_outbox_is_empty_by_default():
    result = run("portal", "outbox")
    assert result.exit_code == 0
    assert "outbox: empty" in result.output


def test_events_reports_nothing_by_default():
    result = run("portal", "events")
    assert result.exit_code == 0
    assert "no unhandled portal events" in result.output


def test_requeue_reports_an_unknown_row_cleanly():
    result = run("portal", "requeue", "sub_1", "1")
    assert result.exit_code != 0
    assert "no outbox row" in result.output


# ── #2336: state-touching commands refuse to run on a thin client ──────────


@pytest.fixture
def thin_client(monkeypatch):
    """Simulate running on a machine with board_service configured — i.e. a
    thin client, per coord/client.py's resolve_board_service() bootstrap
    contract (flag > env > ~/.coord/client.toml)."""
    monkeypatch.setenv("COORD_SERVICE_URL", "http://dellserver:7435")


@pytest.mark.parametrize(
    "args",
    [
        ("portal", "sync"),
        ("portal", "outbox"),
        ("portal", "events"),
        ("portal", "enqueue-status", "sub_1", "shipped"),
        ("portal", "enqueue-design-round", "sub_1", "{}"),
        ("portal", "enqueue-preview", "sub_1", "https://pr-1.example.pages.dev"),
        ("portal", "enqueue-question", "sub_1", "why?"),
        ("portal", "requeue", "sub_1", "1"),
    ],
)
def test_state_touching_commands_refuse_on_a_thin_client(thin_client, args):
    result = run(*args)
    assert result.exit_code != 0
    assert "must run on the daemon host" in result.output
    assert "dellserver" in result.output


def test_status_heartbeat_and_push_do_not_call_the_thin_client_guard(thin_client):
    """status/heartbeat/push never touch the local DB, so they must not be
    gated by the #2336 guard — only sync/outbox/events/enqueue-*/requeue call
    _refuse_if_thin_client. (A full CLI invocation isn't used here: setting
    COORD_SERVICE_URL also reroutes _load_config's own config fetch to the
    daemon per #1080, which is unrelated to this guard and would make the
    assertion about a network error rather than about this guard.)"""
    import coord.commands.portal as portal_mod

    calls = []
    real_guard = portal_mod._refuse_if_thin_client

    def _tracking_guard(cmd_name):
        calls.append(cmd_name)
        return real_guard(cmd_name)

    portal_mod._refuse_if_thin_client = _tracking_guard
    try:
        result = run("portal", "heartbeat", "--config", "/does/not/exist.yml")
    finally:
        portal_mod._refuse_if_thin_client = real_guard
    assert result.exit_code != 0  # fails for an unrelated reason (bad --config)
    assert calls == []
