"""Tests for the `audit:` block in coordinator.yml (#1036's `audit.max_rows`
and #1038's `audit.level`, the Audit Trail epic's config knobs)."""

from __future__ import annotations

from pathlib import Path

import pytest

from coord.config import AuditConfig, ConfigError, load


BASE = """\
repos:
  - name: coord-tui
    github: acme/coord-tui
machines:
  - name: laptop
    host: laptop.tail
    repos: [coord-tui]
"""


def test_audit_absent_defaults_to_unlimited(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(BASE)
    cfg = load(p)
    assert cfg.audit == AuditConfig()
    assert cfg.audit.max_rows == 0
    assert cfg.audit.level == "operational"


def test_audit_parses_max_rows(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(BASE + "audit:\n  max_rows: 5000\n")
    cfg = load(p)
    assert cfg.audit.max_rows == 5000


def test_audit_max_rows_must_be_non_negative_int(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(BASE + "audit:\n  max_rows: -1\n")
    with pytest.raises(ConfigError, match="audit.max_rows"):
        load(p)


def test_audit_max_rows_rejects_non_int(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(BASE + "audit:\n  max_rows: \"lots\"\n")
    with pytest.raises(ConfigError, match="audit.max_rows"):
        load(p)


def test_audit_block_must_be_mapping(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(BASE + "audit: [1, 2]\n")
    with pytest.raises(ConfigError, match="'audit' must be a mapping"):
        load(p)


def test_audit_parses_level_business(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(BASE + "audit:\n  level: business\n")
    cfg = load(p)
    assert cfg.audit.level == "business"


def test_audit_parses_level_operational(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(BASE + "audit:\n  level: operational\n")
    cfg = load(p)
    assert cfg.audit.level == "operational"


def test_audit_level_rejects_invalid_value(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(BASE + "audit:\n  level: verbose\n")
    with pytest.raises(ConfigError, match="audit.level"):
        load(p)


def test_audit_level_rejects_non_string(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(BASE + "audit:\n  level: 1\n")
    with pytest.raises(ConfigError, match="audit.level"):
        load(p)
