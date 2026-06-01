"""Tests for coord.config — YAML loading and validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from coord.config import ConfigError, _parse_concurrency, load


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
    """tracked_labels() always includes 'coord' plus sorted configured keys."""
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
    # 'coord' is always first; configured keys follow alphabetically.
    assert cfg.pipeline.tracked_labels() == ["coord", "feature", "hotfix"]


def test_pipeline_tracked_labels_coord_not_duplicated(tmp_path: Path) -> None:
    """When 'coord' is explicitly in labels, it is not duplicated."""
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n"
        "  - name: api\n    github: a/a\n"
        "machines:\n"
        "  - name: m\n    host: h\n    repos: [api]\n"
        "pipeline:\n"
        "  labels:\n"
        "    coord: [review, merge]\n"
        "    hotfix: [merge]\n"
    )
    cfg = load(p)
    assert cfg.pipeline.tracked_labels() == ["coord", "hotfix"]


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
    # Default default_gates: Test gate sits between Work and Review (#200).
    assert cfg.pipeline.gates_for_label("coord") == ["test", "review", "merge"]
    assert cfg.pipeline.gates_for_label(None) == ["test", "review", "merge"]


# ── concurrency: daemon-spawn stall mitigations (#299) ───────────────────────

def test_concurrency_defaults() -> None:
    cfg = _parse_concurrency(None)
    assert cfg.bash_wrap_spawn is True
    assert cfg.first_output_timeout == 600.0


def test_concurrency_bash_wrap_spawn_parses() -> None:
    assert _parse_concurrency({"bash_wrap_spawn": False}).bash_wrap_spawn is False
    assert _parse_concurrency({"bash_wrap_spawn": True}).bash_wrap_spawn is True


def test_concurrency_bash_wrap_spawn_rejects_non_bool() -> None:
    with pytest.raises(ConfigError, match="bash_wrap_spawn must be a boolean"):
        _parse_concurrency({"bash_wrap_spawn": "yes"})


def test_concurrency_first_output_timeout_parses() -> None:
    assert _parse_concurrency({"first_output_timeout": 0}).first_output_timeout == 0
    assert _parse_concurrency({"first_output_timeout": 120}).first_output_timeout == 120
    assert _parse_concurrency({"first_output_timeout": 90.5}).first_output_timeout == 90.5


def test_concurrency_first_output_timeout_rejects_negative() -> None:
    with pytest.raises(ConfigError, match="first_output_timeout must be a non-negative number"):
        _parse_concurrency({"first_output_timeout": -1})


def test_concurrency_first_output_timeout_rejects_bool() -> None:
    with pytest.raises(ConfigError, match="first_output_timeout must be a non-negative number"):
        _parse_concurrency({"first_output_timeout": True})


# ── run_cmd per repo (#296) ────────────────────────────────────────────────────

def test_repo_run_cmd_absent(tmp_path: Path) -> None:
    """run_cmd defaults to None when omitted."""
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n"
        "  - name: api\n    github: acme/api\n"
        "machines:\n"
        "  - name: m\n    host: h\n    repos: [api]\n"
    )
    cfg = load(p)
    assert cfg.repo("api").run_cmd is None


def test_repo_run_cmd_present(tmp_path: Path) -> None:
    """run_cmd is parsed and stored on the Repo when provided."""
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n"
        "  - name: ui\n"
        "    github: acme/ui\n"
        "    run_cmd: 'cargo run --example gtk_panel --features gtk'\n"
        "machines:\n"
        "  - name: m\n    host: h\n    repos: [ui]\n"
    )
    cfg = load(p)
    assert cfg.repo("ui").run_cmd == "cargo run --example gtk_panel --features gtk"


def test_repo_run_cmd_non_string_rejected(tmp_path: Path) -> None:
    """run_cmd must be a string; non-string values raise ConfigError."""
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n"
        "  - name: api\n    github: acme/api\n    run_cmd: 42\n"
        "machines:\n"
        "  - name: m\n    host: h\n    repos: [api]\n"
    )
    with pytest.raises(ConfigError, match="run_cmd must be a string"):
        load(p)


# ── Repo.resolve_new_issue_guidance (#316) ───────────────────────────────────


def test_resolve_guidance_returns_default_when_none(tmp_path: Path) -> None:
    """When new_issue_guidance is None, a generic default is returned."""
    from coord.models import Repo

    repo = Repo(name="r", github="o/r", new_issue_guidance=None)
    guidance = repo.resolve_new_issue_guidance(tmp_path)
    assert "Title" in guidance
    assert "Acceptance" in guidance


def test_resolve_guidance_returns_inline_text(tmp_path: Path) -> None:
    """When the value doesn't look like a path, it is returned verbatim."""
    from coord.models import Repo

    text = "**Required:** Title (≤80 chars), What, Acceptance criteria"
    repo = Repo(name="r", github="o/r", new_issue_guidance=text)
    assert repo.resolve_new_issue_guidance(tmp_path) == text


def test_resolve_guidance_reads_file_when_path_exists(tmp_path: Path) -> None:
    """When the value is a path and the file exists, the file contents are returned."""
    from coord.models import Repo

    guidance_dir = tmp_path / "docs"
    guidance_dir.mkdir()
    (guidance_dir / "ISSUE_GUIDANCE.md").write_text("## Guidance\n- Step 1", encoding="utf-8")
    repo = Repo(name="r", github="o/r", new_issue_guidance="docs/ISSUE_GUIDANCE.md")
    result = repo.resolve_new_issue_guidance(tmp_path)
    assert "## Guidance" in result
    assert "Step 1" in result


def test_resolve_guidance_falls_back_to_inline_when_file_missing(tmp_path: Path) -> None:
    """When the value looks like a path but the file is absent, return the value verbatim."""
    from coord.models import Repo

    repo = Repo(name="r", github="o/r", new_issue_guidance="docs/MISSING.md")
    result = repo.resolve_new_issue_guidance(tmp_path)
    # File doesn't exist — value is returned as-is (path string).
    assert result == "docs/MISSING.md"


def test_resolve_guidance_txt_extension_treated_as_path(tmp_path: Path) -> None:
    """A .txt path is also resolved as a file."""
    from coord.models import Repo

    (tmp_path / "GUIDANCE.txt").write_text("Plain text guidance", encoding="utf-8")
    repo = Repo(name="r", github="o/r", new_issue_guidance="GUIDANCE.txt")
    result = repo.resolve_new_issue_guidance(tmp_path)
    assert result == "Plain text guidance"


def test_new_issue_guidance_loaded_from_config(tmp_path: Path) -> None:
    """new_issue_guidance is parsed from coordinator.yml and stored on Repo."""
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n"
        "  - name: api\n"
        "    github: acme/api\n"
        "    new_issue_guidance: 'Title, What, Acceptance'\n"
        "machines:\n"
        "  - name: m\n    host: h\n    repos: [api]\n"
    )
    cfg = load(p)
    assert cfg.repo("api").new_issue_guidance == "Title, What, Acceptance"


def test_new_issue_guidance_non_string_rejected(tmp_path: Path) -> None:
    """new_issue_guidance must be a string; non-string raises ConfigError."""
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n"
        "  - name: api\n    github: acme/api\n    new_issue_guidance: 42\n"
        "machines:\n"
        "  - name: m\n    host: h\n    repos: [api]\n"
    )
    with pytest.raises(ConfigError, match="new_issue_guidance must be a string"):
        load(p)
