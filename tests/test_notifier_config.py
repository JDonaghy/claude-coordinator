"""#1632: the ``notifications:`` block in coordinator.yml."""

from __future__ import annotations

import pytest

from coord.config import ConfigError, NotificationsConfig, _parse_notifications

BASE = {"enabled": True, "ntfy_url": "http://dellserver:7440", "ntfy_topic": "coord"}


def test_absent_block_is_disabled_and_changes_nothing():
    cfg = _parse_notifications(None)
    assert cfg == NotificationsConfig()
    assert cfg.enabled is False
    assert cfg.quiet_hours is None


def test_a_full_block_parses():
    cfg = _parse_notifications({
        **BASE,
        "ntfy_token": "tk_abc",
        "web_base_url": "http://dellserver:7434/",
        "quiet_hours": {"start": "22:00", "end": "08:00", "tz": "America/Chicago"},
        "min_samples": 8,
        "percentile": 95.0,
        "silence_fraction": 0.4,
        "stall_grace_mins": 30.0,
        "urgent_ttl_hours": 6.0,
        "cold_ceiling_mins": {"work": 300, "review": 45},
    })
    assert cfg.enabled is True
    assert cfg.ntfy_url == "http://dellserver:7440"
    # Trailing slashes are canonicalised so the built URL is the same
    # string whichever way the operator wrote it.
    assert cfg.web_base_url == "http://dellserver:7434"
    assert cfg.quiet_hours is not None and cfg.quiet_hours.tz == "America/Chicago"
    assert cfg.min_samples == 8
    assert cfg.cold_ceiling_mins == {"work": 300.0, "review": 45.0}


def test_unknown_option_is_refused():
    with pytest.raises(ConfigError, match="unknown notifications option"):
        _parse_notifications({"ntfy_topik": "typo"})


def test_enabling_ntfy_without_a_destination_is_refused():
    """A notifier that silently delivers nothing is indistinguishable from
    a healthy fleet — which is the one failure this feature exists to
    prevent, so it must fail at config-parse time."""
    with pytest.raises(ConfigError, match="ntfy_url/ntfy_topic"):
        _parse_notifications({"enabled": True})


def test_transport_none_may_be_enabled_without_a_destination():
    cfg = _parse_notifications({"enabled": True, "transport": "none"})
    assert cfg.enabled is True and cfg.transport == "none"


def test_unknown_transport_is_refused():
    with pytest.raises(ConfigError, match="transport must be one of"):
        _parse_notifications({"transport": "carrier-pigeon"})


def test_empty_string_is_a_typo_not_unset():
    with pytest.raises(ConfigError, match="non-empty string"):
        _parse_notifications({"ntfy_url": "   "})


def test_min_samples_below_two_is_refused():
    with pytest.raises(ConfigError, match="population of one"):
        _parse_notifications({**BASE, "min_samples": 1})


@pytest.mark.parametrize(
    "key,value",
    [
        ("percentile", 10.0),      # below 50 is not a "far too long" line
        ("percentile", 101.0),
        ("timeout_secs", 0.0),
        ("urgent_ttl_hours", 0.0),
        ("silence_fraction", 0.0),
        ("stall_grace_mins", -1.0),
    ],
)
def test_out_of_range_numbers_are_refused(key, value):
    with pytest.raises(ConfigError):
        _parse_notifications({**BASE, key: value})


def test_quiet_hours_requires_an_iana_zone():
    """Same rule as machines[i].quiet_hours (#1862): `coord serve` runs on
    UTC, so a naive time-of-day would silently defer at the wrong local
    hour — and a quiet-hours window that opens early swallows daytime
    events."""
    with pytest.raises(ConfigError, match="tz is required"):
        _parse_notifications({**BASE, "quiet_hours": {"start": "22:00", "end": "08:00"}})
    with pytest.raises(ConfigError, match="not a known IANA zone"):
        _parse_notifications({
            **BASE, "quiet_hours": {"start": "22:00", "end": "08:00", "tz": "Mars/Olympus"}
        })


def test_quiet_hours_error_message_names_the_notifications_block():
    """The shared parser must not report a `notifications:` mistake as a
    `machines[i]` one."""
    with pytest.raises(ConfigError, match=r"notifications\.quiet_hours"):
        _parse_notifications({**BASE, "quiet_hours": {"start": "nope", "end": "08:00",
                                                      "tz": "UTC"}})


def test_machine_quiet_hours_still_parse_after_the_shared_refactor():
    from coord.config import _parse_quiet_hours  # noqa: PLC0415

    window = _parse_quiet_hours(
        {"start": "23:00", "end": "07:00", "tz": "UTC"},
        machine_index=0,
        machine_name="dellserver",
    )
    assert window is not None and window.tz == "UTC"
    with pytest.raises(ConfigError, match=r"machines\[0\]"):
        _parse_quiet_hours({"start": "bad"}, machine_index=0, machine_name="dellserver")


def test_config_load_accepts_a_notifications_block(tmp_path):
    import textwrap

    from coord.config import load  # noqa: PLC0415

    path = tmp_path / "coordinator.yml"
    path.write_text(textwrap.dedent("""
        repos:
          - name: coord
            github: owner/coord
        machines:
          - name: dellserver
            host: dellserver
            repos: [coord]
        notifications:
          enabled: true
          ntfy_url: http://dellserver:7440
          ntfy_topic: coord-fleet
          quiet_hours:
            start: "22:00"
            end: "08:00"
            tz: America/Chicago
    """))
    cfg = load(path)
    assert cfg.notifications.enabled is True
    assert cfg.notifications.ntfy_topic == "coord-fleet"
