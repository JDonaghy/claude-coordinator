"""Tests for coord/acceptance.py — manifest loading + verdict assembly (#944).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coord.acceptance import (
    ManifestError,
    build_verdict,
    dump_manifest_error_hint,
    failure_summary,
    load_manifest,
)
# Aliased on import: pytest treats any module-level `test_*` name as a
# collectible test function, and `test_ids_for_issue` takes required
# positional args — importing it under its real name breaks collection.
from coord.acceptance import test_ids_for_issue as ids_for_issue


class TestLoadManifest:
    def test_missing_dir_returns_empty(self, tmp_path: Path) -> None:
        assert load_manifest(tmp_path / "tests" / "acceptance") == {}

    def test_flat_tests_shape(self, tmp_path: Path) -> None:
        root = tmp_path / "tests" / "acceptance"
        ms = root / "ms01"
        ms.mkdir(parents=True)
        (ms / "manifest.yml").write_text(
            "tests:\n  ms01::shows_menu: 944\n  ms01::selects_item: 944\n"
        )
        manifest = load_manifest(root)
        assert manifest == {"ms01::shows_menu": 944, "ms01::selects_item": 944}

    def test_grouped_issues_shape(self, tmp_path: Path) -> None:
        root = tmp_path / "tests" / "acceptance"
        ms = root / "ms01"
        ms.mkdir(parents=True)
        (ms / "manifest.json").write_text(
            '{"issues": {"944": ["ms01::a", "ms01::b"], "945": ["ms01::c"]}}'
        )
        manifest = load_manifest(root)
        assert manifest == {"ms01::a": 944, "ms01::b": 944, "ms01::c": 945}

    def test_merges_across_multiple_slices(self, tmp_path: Path) -> None:
        root = tmp_path / "tests" / "acceptance"
        (root / "ms01").mkdir(parents=True)
        (root / "ms02").mkdir(parents=True)
        (root / "ms01" / "manifest.yml").write_text("tests:\n  a: 1\n")
        (root / "ms02" / "manifest.yml").write_text("tests:\n  b: 2\n")
        manifest = load_manifest(root)
        assert manifest == {"a": 1, "b": 2}

    def test_malformed_yaml_raises_manifest_error(self, tmp_path: Path) -> None:
        root = tmp_path / "tests" / "acceptance"
        (root / "ms01").mkdir(parents=True)
        (root / "ms01" / "manifest.yml").write_text("tests: [this, is, not, a, mapping\n")
        with pytest.raises(ManifestError):
            load_manifest(root)

    def test_non_mapping_manifest_raises(self, tmp_path: Path) -> None:
        root = tmp_path / "tests" / "acceptance"
        (root / "ms01").mkdir(parents=True)
        (root / "ms01" / "manifest.yml").write_text("- a\n- b\n")
        with pytest.raises(ManifestError, match="must be a mapping"):
            load_manifest(root)

    def test_empty_manifest_file_is_skipped(self, tmp_path: Path) -> None:
        root = tmp_path / "tests" / "acceptance"
        (root / "ms01").mkdir(parents=True)
        (root / "ms01" / "manifest.yml").write_text("")
        assert load_manifest(root) == {}


class TestTestIdsForIssue:
    def test_filters_by_issue(self) -> None:
        manifest = {"a": 1, "b": 1, "c": 2}
        assert ids_for_issue(manifest, 1) == {"a", "b"}
        assert ids_for_issue(manifest, 2) == {"c"}
        assert ids_for_issue(manifest, 3) == set()


class TestBuildVerdict:
    def test_counts_and_green(self) -> None:
        tests = [
            {"id": "a", "status": "pass"},
            {"id": "b", "status": "fail"},
            {"id": "c", "status": "skip"},
        ]
        verdict = build_verdict(tests, scope="issue", issue_number=944)
        assert verdict["total"] == 3
        assert verdict["passed"] == 1
        assert verdict["failed"] == 1
        assert verdict["skipped"] == 1
        assert verdict["green"] is False
        assert verdict["issue"] == 944
        assert verdict["scope"] == "issue"

    def test_green_when_all_pass(self) -> None:
        verdict = build_verdict([{"id": "a", "status": "pass"}], scope="all")
        assert verdict["green"] is True
        assert "issue" not in verdict

    def test_empty_is_not_green(self) -> None:
        verdict = build_verdict([], scope="all")
        assert verdict["green"] is False
        assert verdict["total"] == 0


class TestFailureSummary:
    def test_no_failures_is_empty_string(self) -> None:
        verdict = build_verdict([{"id": "a", "status": "pass"}], scope="all")
        assert failure_summary(verdict) == ""

    def test_lists_failures_with_messages(self) -> None:
        verdict = build_verdict(
            [{"id": "a", "status": "fail", "message": "expected 3 got 4"}],
            scope="all",
        )
        assert failure_summary(verdict) == "a: expected 3 got 4"

    def test_truncates_with_limit(self) -> None:
        tests = [{"id": f"t{i}", "status": "fail", "message": "x"} for i in range(7)]
        verdict = build_verdict(tests, scope="all")
        summary = failure_summary(verdict, limit=3)
        assert summary.count("\n") == 3  # 3 lines + "... and N more"
        assert "and 4 more" in summary


def test_dump_manifest_error_hint_mentions_authoring_issue(tmp_path: Path) -> None:
    hint = dump_manifest_error_hint(tmp_path / "tests" / "acceptance")
    assert "not been authored" in hint
