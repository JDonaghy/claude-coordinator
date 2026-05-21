"""Tests for coord/split_work.py — smart task-splitting for large dispatches."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from coord.config import DispatchConfig, load
from coord.split_work import WorkChunk, analyze_plan, format_chunks_summary


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cfg(max_files: int = 8, auto_split: bool = True) -> DispatchConfig:
    """Build a minimal DispatchConfig for tests."""
    return DispatchConfig(max_files_per_worker=max_files, auto_split=auto_split)


# ---------------------------------------------------------------------------
# analyze_plan — below threshold (no split)
# ---------------------------------------------------------------------------


class TestAnalyzePlanNoSplit:
    def test_empty_files_returns_single_chunk(self) -> None:
        chunks = analyze_plan([], _cfg())
        assert len(chunks) == 1
        assert chunks[0].chunk_id == 1
        assert chunks[0].files == []
        assert chunks[0].depends_on == []

    def test_three_files_no_split(self) -> None:
        files = ["coord/config.py", "coord/models.py", "coord/cli.py"]
        chunks = analyze_plan(files, _cfg(max_files=8))
        assert len(chunks) == 1
        assert chunks[0].chunk_id == 1
        assert set(chunks[0].files) == set(files)
        assert chunks[0].depends_on == []

    def test_exactly_at_threshold_no_split(self) -> None:
        files = [f"coord/file{i}.py" for i in range(8)]
        chunks = analyze_plan(files, _cfg(max_files=8))
        assert len(chunks) == 1

    def test_single_file_no_split(self) -> None:
        chunks = analyze_plan(["coord/config.py"], _cfg())
        assert len(chunks) == 1


# ---------------------------------------------------------------------------
# analyze_plan — above threshold (split triggered)
# ---------------------------------------------------------------------------


class TestAnalyzePlanSplit:
    def test_twelve_files_across_three_dirs_splits_into_three_chunks(self) -> None:
        files = [
            # coord/ subsystem (4 impl files)
            "coord/split_work.py",
            "coord/config.py",
            "coord/cli.py",
            "coord/brain.py",
            # api/ subsystem (4 impl files)
            "api/views.py",
            "api/models.py",
            "api/serializers.py",
            "api/urls.py",
            # tests/ (4 test files)
            "tests/test_split.py",
            "tests/test_config.py",
            "tests/test_brain.py",
            "tests/test_api.py",
        ]
        chunks = analyze_plan(files, _cfg(max_files=8))

        assert len(chunks) == 3, f"Expected 3 chunks, got {len(chunks)}: {[c.files for c in chunks]}"

        # Impl chunks (coord/ and api/) should have no dependencies
        impl_chunks = [c for c in chunks if not c.depends_on]
        assert len(impl_chunks) == 2

        # Test chunk should depend on impl chunks
        test_chunks = [c for c in chunks if c.depends_on]
        assert len(test_chunks) == 1
        test_chunk = test_chunks[0]
        assert set(test_chunk.depends_on) == {c.chunk_id for c in impl_chunks}

    def test_split_respects_max_files_per_worker(self) -> None:
        """Each impl chunk must not exceed max_files_per_worker."""
        files = [f"coord/file{i}.py" for i in range(20)]
        chunks = analyze_plan(files, _cfg(max_files=5))
        for chunk in chunks:
            assert len(chunk.files) <= 5, (
                f"Chunk {chunk.chunk_id} has {len(chunk.files)} files, "
                f"exceeding max_files_per_worker=5"
            )

    def test_all_files_present_in_chunks(self) -> None:
        """No files should be dropped or duplicated after splitting."""
        files = [f"coord/f{i}.py" for i in range(6)] + [f"api/g{i}.py" for i in range(6)]
        chunks = analyze_plan(files, _cfg(max_files=4))
        all_chunk_files = [f for c in chunks for f in c.files]
        assert sorted(all_chunk_files) == sorted(files)

    def test_chunk_ids_are_unique_and_sequential(self) -> None:
        files = [f"coord/f{i}.py" for i in range(5)] + [f"api/g{i}.py" for i in range(5)]
        chunks = analyze_plan(files, _cfg(max_files=3))
        ids = [c.chunk_id for c in chunks]
        assert ids == list(range(1, len(chunks) + 1))

    def test_each_chunk_has_briefing_fragment(self) -> None:
        files = [f"coord/f{i}.py" for i in range(5)] + [f"api/g{i}.py" for i in range(5)]
        chunks = analyze_plan(files, _cfg(max_files=3))
        for chunk in chunks:
            assert chunk.briefing_fragment, f"Chunk {chunk.chunk_id} has empty briefing_fragment"


# ---------------------------------------------------------------------------
# Dependency detection — new module + test files that import it
# ---------------------------------------------------------------------------


class TestDependencyDetection:
    def test_test_file_depends_on_matching_impl_chunk(self) -> None:
        """tests/test_new_module.py should depend on the chunk containing new_module.py."""
        files = [
            "coord/new_module.py",    # new module being created
            "coord/other_a.py",
            "coord/other_b.py",
            "coord/other_c.py",
            "coord/other_d.py",
            "tests/test_new_module.py",  # test that imports new_module
        ]
        # Use threshold=5 to force a split (6 files total > 5)
        chunks = analyze_plan(files, _cfg(max_files=5))

        # Find the test chunk
        test_chunks = [c for c in chunks if any("test_" in f for f in c.files)]
        assert test_chunks, "No test chunk found"
        test_chunk = test_chunks[0]

        # Find the impl chunk containing new_module.py
        new_module_chunk = next(
            c for c in chunks if any("new_module.py" in f for f in c.files)
        )

        # Test chunk should depend on the impl chunk that has new_module.py
        assert new_module_chunk.chunk_id in test_chunk.depends_on, (
            f"Expected test chunk (id={test_chunk.chunk_id}) to depend on "
            f"chunk {new_module_chunk.chunk_id} (has new_module.py), "
            f"but depends_on={test_chunk.depends_on}"
        )

    def test_test_chunk_depends_on_all_impl_when_no_name_match(self) -> None:
        """When test filenames don't match impl modules, depend on all impl chunks."""
        files = [
            "coord/alpha.py",
            "coord/beta.py",
            "coord/gamma.py",
            "api/delta.py",
            "api/epsilon.py",
            "api/zeta.py",
            "tests/test_misc.py",  # no direct name match to any impl file
        ]
        chunks = analyze_plan(files, _cfg(max_files=4))

        test_chunks = [c for c in chunks if any("test_" in f for f in c.files)]
        assert test_chunks, "Expected a test chunk"
        test_chunk = test_chunks[0]

        impl_chunk_ids = {c.chunk_id for c in chunks if not c.depends_on}
        assert set(test_chunk.depends_on) == impl_chunk_ids

    def test_impl_chunks_have_no_dependencies(self) -> None:
        """Implementation chunks should never depend on each other (parallel)."""
        files = [f"coord/f{i}.py" for i in range(5)] + [f"api/g{i}.py" for i in range(5)]
        chunks = analyze_plan(files, _cfg(max_files=3))

        # All test-free chunks are impl chunks and must be independent
        test_file_sets = {"test_", "tests/"}
        impl_chunks = [
            c for c in chunks
            if not any(
                f.startswith("tests/") or Path(f).name.startswith("test_")
                for f in c.files
            )
        ]
        for chunk in impl_chunks:
            assert chunk.depends_on == [], (
                f"Impl chunk {chunk.chunk_id} unexpectedly depends on {chunk.depends_on}"
            )


# ---------------------------------------------------------------------------
# Config parsing — DispatchConfig
# ---------------------------------------------------------------------------


class TestDispatchConfigParsing:
    def test_defaults_when_dispatch_absent(self, tmp_path: Path) -> None:
        p = tmp_path / "coordinator.yml"
        p.write_text(
            "repos:\n  - name: api\n    github: acme/api\n"
            "machines:\n  - name: m\n    host: h\n    repos: [api]\n"
        )
        cfg = load(p)
        assert cfg.dispatch.max_files_per_worker == 8
        assert cfg.dispatch.auto_split is True

    def test_custom_max_files(self, tmp_path: Path) -> None:
        p = tmp_path / "coordinator.yml"
        p.write_text(
            "repos:\n  - name: api\n    github: acme/api\n"
            "machines:\n  - name: m\n    host: h\n    repos: [api]\n"
            "dispatch:\n  max_files_per_worker: 4\n"
        )
        cfg = load(p)
        assert cfg.dispatch.max_files_per_worker == 4

    def test_auto_split_false(self, tmp_path: Path) -> None:
        p = tmp_path / "coordinator.yml"
        p.write_text(
            "repos:\n  - name: api\n    github: acme/api\n"
            "machines:\n  - name: m\n    host: h\n    repos: [api]\n"
            "dispatch:\n  auto_split: false\n"
        )
        cfg = load(p)
        assert cfg.dispatch.auto_split is False

    def test_both_options(self, tmp_path: Path) -> None:
        p = tmp_path / "coordinator.yml"
        p.write_text(
            "repos:\n  - name: api\n    github: acme/api\n"
            "machines:\n  - name: m\n    host: h\n    repos: [api]\n"
            "dispatch:\n  max_files_per_worker: 3\n  auto_split: false\n"
        )
        cfg = load(p)
        assert cfg.dispatch.max_files_per_worker == 3
        assert cfg.dispatch.auto_split is False

    def test_invalid_max_files_raises(self, tmp_path: Path) -> None:
        from coord.config import ConfigError
        p = tmp_path / "coordinator.yml"
        p.write_text(
            "repos:\n  - name: api\n    github: acme/api\n"
            "machines:\n  - name: m\n    host: h\n    repos: [api]\n"
            "dispatch:\n  max_files_per_worker: 0\n"
        )
        with pytest.raises(ConfigError, match="positive integer"):
            load(p)

    def test_invalid_auto_split_type_raises(self, tmp_path: Path) -> None:
        from coord.config import ConfigError
        p = tmp_path / "coordinator.yml"
        p.write_text(
            "repos:\n  - name: api\n    github: acme/api\n"
            "machines:\n  - name: m\n    host: h\n    repos: [api]\n"
            "dispatch:\n  auto_split: yes_please\n"
        )
        with pytest.raises(ConfigError, match="boolean"):
            load(p)


# ---------------------------------------------------------------------------
# CLI approve — shows split advisory when threshold exceeded
# ---------------------------------------------------------------------------


class TestApproveSplitAdvisory:
    @pytest.fixture
    def config_file(self, tmp_path: Path) -> Path:
        p = tmp_path / "coordinator.yml"
        p.write_text(
            "repos:\n  - name: api\n    github: acme/api\n"
            "machines:\n  - name: m\n    host: h\n    repos: [api]\n"
            "dispatch:\n  max_files_per_worker: 3\n"
        )
        return p

    def _make_proposal(self, files: list[str]):
        from coord.models import Proposal
        return Proposal(
            id=1,
            machine_name="m",
            repo_name="api",
            issue_number=42,
            issue_title="Big task",
            rationale="lots of work",
            files_likely=files,
        )

    def test_approve_shows_split_advisory_when_threshold_exceeded(
        self, config_file: Path, coord_db
    ) -> None:
        from coord.cli import main
        from coord.state import save_proposals

        # 9 files across 3 dirs > threshold of 3 per worker
        files = [
            "coord/a.py", "coord/b.py", "coord/c.py",
            "api/d.py", "api/e.py", "api/f.py",
            "tests/test_a.py", "tests/test_b.py", "tests/test_c.py",
        ]
        save_proposals([self._make_proposal(files)])

        runner = CliRunner()
        result = runner.invoke(main, ["approve", "1", "--config", str(config_file), "--dry-run"])

        assert result.exit_code == 0, result.output
        # Should warn about splitting
        assert "consider splitting" in result.output or "threshold" in result.output

    def test_approve_no_split_advisory_when_below_threshold(
        self, config_file: Path, coord_db
    ) -> None:
        from coord.cli import main
        from coord.state import save_proposals

        # 3 files <= threshold of 3
        files = ["coord/a.py", "coord/b.py", "coord/c.py"]
        save_proposals([self._make_proposal(files)])

        runner = CliRunner()
        result = runner.invoke(main, ["approve", "1", "--config", str(config_file), "--dry-run"])

        assert result.exit_code == 0, result.output
        # Should NOT show any split advisory
        assert "consider splitting" not in result.output
        assert "⚠" not in result.output

    def test_approve_no_split_advisory_when_auto_split_disabled(
        self, tmp_path: Path, coord_db
    ) -> None:
        from coord.cli import main
        from coord.state import save_proposals

        config_file = tmp_path / "coordinator.yml"
        config_file.write_text(
            "repos:\n  - name: api\n    github: acme/api\n"
            "machines:\n  - name: m\n    host: h\n    repos: [api]\n"
            "dispatch:\n  max_files_per_worker: 3\n  auto_split: false\n"
        )

        # 9 files, would normally trigger split advisory
        files = [
            "coord/a.py", "coord/b.py", "coord/c.py",
            "api/d.py", "api/e.py", "api/f.py",
            "tests/test_a.py", "tests/test_b.py", "tests/test_c.py",
        ]
        save_proposals([self._make_proposal(files)])

        runner = CliRunner()
        result = runner.invoke(main, ["approve", "1", "--config", str(config_file), "--dry-run"])

        assert result.exit_code == 0, result.output
        # auto_split=false means no advisory
        assert "consider splitting" not in result.output
        assert "⚠" not in result.output


# ---------------------------------------------------------------------------
# format_chunks_summary
# ---------------------------------------------------------------------------


class TestFormatChunksSummary:
    def test_single_chunk_returns_empty(self) -> None:
        chunks = [WorkChunk(chunk_id=1, files=["a.py"], briefing_fragment="a.py")]
        assert format_chunks_summary(chunks) == ""

    def test_two_chunks_summary(self) -> None:
        chunks = [
            WorkChunk(chunk_id=1, files=["a.py", "b.py"], briefing_fragment="coord: a.py, b.py"),
            WorkChunk(
                chunk_id=2,
                files=["test_a.py"],
                briefing_fragment="tests: test_a.py",
                depends_on=[1],
            ),
        ]
        summary = format_chunks_summary(chunks)
        assert "2 chunks" in summary
        assert "Chunk 1" in summary
        assert "Chunk 2" in summary
        assert "after chunk 1" in summary
