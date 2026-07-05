"""Tests for coord/acceptance_drivers.py — the tui-tuidriver adapter (#944).

Covers ``parse_test_output``'s two accepted shapes (a single JSON blob, and
libtest's `--format json` JSON-lines event stream) plus ``run_driver``'s
unsupported-kind guard and the real subprocess path.
"""

from __future__ import annotations

import json

import pytest

from coord.acceptance_drivers import (
    DriverError,
    SUPPORTED_KINDS,
    parse_test_output,
    run_driver,
)


class TestParseTestOutputBlob:
    def test_single_json_blob(self) -> None:
        output = json.dumps({
            "tests": [
                {"id": "ms01::shows_menu", "status": "pass"},
                {"id": "ms01::selects_item", "status": "fail", "message": "expected A got B"},
            ]
        })
        tests = parse_test_output(output)
        assert tests == [
            {"id": "ms01::shows_menu", "status": "pass", "message": ""},
            {"id": "ms01::selects_item", "status": "fail", "message": "expected A got B"},
        ]

    def test_blob_ignores_malformed_entries(self) -> None:
        output = json.dumps({"tests": [{"id": "ok"}, {"status": "fail"}, "not-a-dict"]})
        assert parse_test_output(output) == []

    def test_blob_without_tests_key_falls_through_to_lines(self) -> None:
        # A single-object blob with no "tests" key isn't a match for shape 1;
        # since it also isn't a valid libtest line-stream, nothing parses.
        assert parse_test_output(json.dumps({"other": 1})) == []


class TestParseTestOutputLibtestJsonLines:
    def test_ok_and_failed_events(self) -> None:
        lines = [
            json.dumps({"type": "suite", "event": "started", "test_count": 2}),
            json.dumps({"type": "test", "event": "started", "name": "ms01::a"}),
            json.dumps({"type": "test", "name": "ms01::a", "event": "ok"}),
            json.dumps({
                "type": "test", "name": "ms01::b", "event": "failed",
                "stdout": "assertion failed: expected 3 got 4",
            }),
            json.dumps({"type": "suite", "event": "failed"}),
        ]
        tests = parse_test_output("\n".join(lines))
        assert tests == [
            {"id": "ms01::a", "status": "pass", "message": ""},
            {
                "id": "ms01::b", "status": "fail",
                "message": "assertion failed: expected 3 got 4",
            },
        ]

    def test_ignored_event_maps_to_skip(self) -> None:
        line = json.dumps({"type": "test", "name": "ms01::c", "event": "ignored"})
        assert parse_test_output(line) == [{"id": "ms01::c", "status": "skip", "message": ""}]

    def test_non_json_noise_lines_skipped(self) -> None:
        lines = [
            "   Compiling coord-tui v0.1.0",
            "warning: unused variable",
            json.dumps({"type": "test", "name": "ms01::a", "event": "ok"}),
            "",
        ]
        tests = parse_test_output("\n".join(lines))
        assert tests == [{"id": "ms01::a", "status": "pass", "message": ""}]

    def test_empty_output_returns_empty(self) -> None:
        assert parse_test_output("") == []
        assert parse_test_output(None) == []  # type: ignore[arg-type]


class TestRunDriver:
    def test_unsupported_kind_raises(self) -> None:
        with pytest.raises(DriverError, match="not implemented yet"):
            run_driver("web-playwright", "npx playwright test", cwd=".")

    def test_supported_kinds_tuple_has_tui_tuidriver(self) -> None:
        assert "tui-tuidriver" in SUPPORTED_KINDS

    def test_runs_shell_command_and_parses_stdout(self, tmp_path) -> None:
        blob = json.dumps({"tests": [{"id": "a", "status": "pass"}]})
        result = run_driver("tui-tuidriver", f"echo '{blob}'", cwd=str(tmp_path))
        assert result.exit_code == 0
        assert result.ok is True
        assert result.tests == [{"id": "a", "status": "pass", "message": ""}]

    def test_nonzero_exit_still_returns_partial_parse(self, tmp_path) -> None:
        blob = json.dumps({"tests": [{"id": "a", "status": "pass"}]})
        result = run_driver(
            "tui-tuidriver", f"echo '{blob}'; exit 1", cwd=str(tmp_path),
        )
        assert result.exit_code == 1
        assert result.ok is False
        assert result.tests == [{"id": "a", "status": "pass", "message": ""}]

    def test_timeout_raises_driver_error(self, tmp_path) -> None:
        with pytest.raises(DriverError, match="timed out"):
            run_driver("tui-tuidriver", "sleep 5", cwd=str(tmp_path), timeout=1)
