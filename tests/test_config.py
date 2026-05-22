"""Tests for coord.config — YAML loading and validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from coord.config import ConfigError, load


def test_load_valid_config(valid_config_path: Path) -> None:
    cfg = load(valid_config_path)
    assert [r.name for r in cfg.repos] == ["api", "shared"]
    assert cfg.repo("api").depends_on == ["shared"]
    assert cfg.repo("api").default_branch == "main"
    assert [m.name for m in cfg.machines] == ["laptop", "server"]
    assert cfg.machines[0].repos == ["api", "shared"]


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load(tmp_path / "missing.yml")


def test_empty_file(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text("")
    with pytest.raises(ConfigError, match="empty"):
        load(p)


def test_invalid_yaml(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text("repos: [\n  - name: api\n")  # unterminated
    with pytest.raises(ConfigError, match="Invalid YAML"):
        load(p)


def test_missing_repos(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text("machines: []\n")
    with pytest.raises(ConfigError, match="repos"):
        load(p)


def test_missing_machines(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text("repos:\n  - name: a\n    github: acme/a\n")
    with pytest.raises(ConfigError, match="machines"):
        load(p)


def test_repo_missing_github(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n"
        "  - name: api\n"
        "machines:\n"
        "  - name: m\n    host: h\n    repos: [api]\n"
    )
    with pytest.raises(ConfigError, match="github"):
        load(p)


def test_repo_bad_github_format(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n"
        "  - name: api\n    github: not-a-slug\n"
        "machines:\n"
        "  - name: m\n    host: h\n    repos: [api]\n"
    )
    with pytest.raises(ConfigError, match="owner/repo"):
        load(p)


def test_duplicate_repo(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n"
        "  - name: api\n    github: a/a\n"
        "  - name: api\n    github: b/b\n"
        "machines:\n"
        "  - name: m\n    host: h\n    repos: [api]\n"
    )
    with pytest.raises(ConfigError, match="duplicate repo"):
        load(p)


def test_machine_references_unknown_repo(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n"
        "  - name: api\n    github: a/a\n"
        "machines:\n"
        "  - name: m\n    host: h\n    repos: [ghost]\n"
    )
    with pytest.raises(ConfigError, match="unknown repos"):
        load(p)


def test_unknown_dependency(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n"
        "  - name: api\n    github: a/a\n    depends_on: [ghost]\n"
        "machines:\n"
        "  - name: m\n    host: h\n    repos: [api]\n"
    )
    with pytest.raises(ConfigError, match="depends_on unknown repos"):
        load(p)


def test_self_dependency(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n"
        "  - name: api\n    github: a/a\n    depends_on: [api]\n"
        "machines:\n"
        "  - name: m\n    host: h\n    repos: [api]\n"
    )
    with pytest.raises(ConfigError, match="cannot depend on itself"):
        load(p)


_EXAMPLE_CONFIG = Path(__file__).resolve().parents[1] / "coordinator.yml"


@pytest.mark.skipif(not _EXAMPLE_CONFIG.exists(), reason="coordinator.yml is gitignored")
def test_example_config_at_repo_root() -> None:
    """The committed coordinator.yml must parse cleanly."""
    cfg = load(_EXAMPLE_CONFIG)
    assert len(cfg.repos) > 0
    assert len(cfg.machines) > 0


def test_repo_housekeeping_parsed(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n"
        "  - name: api\n"
        "    github: a/a\n"
        "    housekeeping:\n"
        "      - pip install -e .\n"
        "      - make build\n"
        "machines:\n"
        "  - name: m\n    host: h\n    repos: [api]\n"
    )
    cfg = load(p)
    assert cfg.repo("api").housekeeping == ["pip install -e .", "make build"]


def test_repo_housekeeping_default_empty(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n"
        "  - name: api\n    github: a/a\n"
        "machines:\n"
        "  - name: m\n    host: h\n    repos: [api]\n"
    )
    cfg = load(p)
    assert cfg.repo("api").housekeeping == []


def test_repo_housekeeping_invalid_type(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n"
        "  - name: api\n"
        "    github: a/a\n"
        "    housekeeping: not-a-list\n"
        "machines:\n"
        "  - name: m\n    host: h\n    repos: [api]\n"
    )
    with pytest.raises(ConfigError, match="housekeeping must be a list of strings"):
        load(p)


def test_repo_housekeeping_invalid_element(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n"
        "  - name: api\n"
        "    github: a/a\n"
        "    housekeeping:\n"
        "      - 42\n"
        "machines:\n"
        "  - name: m\n    host: h\n    repos: [api]\n"
    )
    with pytest.raises(ConfigError, match="housekeeping must be a list of strings"):
        load(p)


# ── PipelineConfig helpers ──────────────────────────────────────────────────


def test_pipeline_tracked_labels_defaults_to_coord(tmp_path: Path) -> None:
    """When pipeline.labels is unset, tracked_labels() returns ['coord']."""
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n"
        "  - name: api\n    github: a/a\n"
        "machines:\n"
        "  - name: m\n    host: h\n    repos: [api]\n"
    )
    cfg = load(p)
    assert cfg.pipeline.tracked_labels() == ["coord"]


def test_pipeline_tracked_labels_from_labels_keys(tmp_path: Path) -> None:
    """tracked_labels() returns sorted keys when pipeline.labels is configured."""
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n"
        "  - name: api\n    github: a/a\n"
        "machines:\n"
        "  - name: m\n    host: h\n    repos: [api]\n"
        "pipeline:\n"
        "  labels:\n"
        "    hotfix: [merge]\n"
        "    feature: [review, merge]\n"
    )
    cfg = load(p)
    # Sorted alphabetically for stable ordering.
    assert cfg.pipeline.tracked_labels() == ["feature", "hotfix"]


def test_pipeline_gates_for_label_uses_override(tmp_path: Path) -> None:
    """gates_for_label() returns the override list when the label matches."""
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n"
        "  - name: api\n    github: a/a\n"
        "machines:\n"
        "  - name: m\n    host: h\n    repos: [api]\n"
        "pipeline:\n"
        "  labels:\n"
        "    hotfix: [merge]\n"
    )
    cfg = load(p)
    assert cfg.pipeline.gates_for_label("hotfix") == ["merge"]


def test_pipeline_gates_for_label_falls_back_to_default(tmp_path: Path) -> None:
    """When the label is not in labels, gates_for_label() returns default_gates."""
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n"
        "  - name: api\n    github: a/a\n"
        "machines:\n"
        "  - name: m\n    host: h\n    repos: [api]\n"
    )
    cfg = load(p)
    # Default default_gates is ['review', 'merge'].
    assert cfg.pipeline.gates_for_label("coord") == ["review", "merge"]
    assert cfg.pipeline.gates_for_label(None) == ["review", "merge"]
