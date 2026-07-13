"""Tests for coord/acceptance_drivers.py — the tui-tuidriver and cli-pytest
(#1125) adapters (#944).

Covers ``parse_test_output``'s two accepted shapes (a single JSON blob, and
libtest's `--format json` JSON-lines event stream), ``parse_pytest_report_log``
(pytest's built-in ``--report-log`` JSON-lines shape), ``render_run_command``'s
``{ms}`` templating, and ``run_driver``'s unsupported-kind guard + real
subprocess path for both kinds.
"""

from __future__ import annotations

import json
import sys

import pytest

from coord.acceptance_drivers import (
    DriverError,
    SUPPORTED_KINDS,
    parse_pytest_junit_xml,
    parse_test_output,
    render_run_command,
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


class TestRenderRunCommand:
    def test_no_ms_leaves_template_unsubstituted(self) -> None:
        assert (
            render_run_command("pytest tests/acceptance/{ms}")
            == "pytest tests/acceptance/{ms}"
        )

    def test_substitutes_ms(self) -> None:
        assert (
            render_run_command("pytest tests/acceptance/{ms}", ms="ms-37")
            == "pytest tests/acceptance/ms-37"
        )

    def test_command_without_template_is_a_noop(self) -> None:
        assert render_run_command("cargo test", ms="ms-37") == "cargo test"


# A real `--junit-xml` report (pytest 9.1, trimmed) for reference — the shape
# TestParsePytestJunitXml's fixtures below are modeled on:
#
#   <?xml version="1.0" encoding="utf-8"?><testsuites name="pytest tests">
#   <testsuite name="pytest" errors="0" failures="2" skipped="1" tests="4" ...>
#   <testcase classname="test_sample" name="test_pass" time="0.000" />
#   <testcase classname="test_sample" name="test_fail" time="0.001">
#   <failure message="AssertionError: assert 'got-value' == 'expected-value'&#10; ...">
#   ...</failure></testcase>
#   <testcase classname="test_sample" name="test_skip" time="0.000">
#   <skipped type="pytest.skip" message="nope">...</skipped></testcase>
#   <testcase classname="test_sample" name="test_error" time="0.000">
#   <failure message="RuntimeError: boom">...</failure></testcase>
#   </testsuite></testsuites>


class TestParsePytestJunitXml:
    XML = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<testsuites name="pytest tests">'
        '<testsuite name="pytest" errors="0" failures="2" skipped="1" tests="4">'
        '<testcase classname="test_sample" name="test_pass" time="0.000" />'
        '<testcase classname="test_sample" name="test_fail" time="0.001">'
        # Real pytest junit-xml escapes embedded newlines in the "message"
        # attribute as `&#10;` character references — a literal raw newline
        # byte there would be collapsed to a space by XML attribute-value
        # normalization (XML 1.0 §3.3.3), which is exactly why pytest itself
        # emits `&#10;` rather than a raw newline.
        "<failure message=\"AssertionError: assert 'got-value' == 'expected-value'"
        "&#10;  &#10;  - expected-value&#10;  + got-value\">body text here</failure>"
        "</testcase>"
        '<testcase classname="test_sample" name="test_skip" time="0.000">'
        '<skipped type="pytest.skip" message="nope">skip body</skipped>'
        "</testcase>"
        '<testcase classname="test_sample" name="test_error" time="0.000">'
        '<failure message="RuntimeError: boom">error body</failure>'
        "</testcase>"
        "</testsuite></testsuites>"
    )

    def test_pass_fail_skip_and_error_all_parsed(self) -> None:
        tests = parse_pytest_junit_xml(self.XML)
        by_id = {t["id"]: t for t in tests}
        assert set(by_id) == {
            "test_sample::test_pass",
            "test_sample::test_fail",
            "test_sample::test_skip",
            "test_sample::test_error",
        }
        assert by_id["test_sample::test_pass"]["status"] == "pass"
        assert by_id["test_sample::test_skip"]["status"] == "skip"
        assert by_id["test_sample::test_error"]["status"] == "fail"

    def test_assert_eq_failure_surfaces_expected_and_got(self) -> None:
        tests = parse_pytest_junit_xml(self.XML)
        fail = next(t for t in tests if t["id"] == "test_sample::test_fail")
        assert fail["status"] == "fail"
        assert fail["got"] == "'got-value'"
        assert fail["expected"] == "'expected-value'"

    def test_non_assert_failure_leaves_expected_got_empty(self) -> None:
        tests = parse_pytest_junit_xml(self.XML)
        error = next(t for t in tests if t["id"] == "test_sample::test_error")
        assert error["message"] == "RuntimeError: boom"
        assert error["expected"] == ""
        assert error["got"] == ""

    def test_skip_message_is_the_skip_reason(self) -> None:
        tests = parse_pytest_junit_xml(self.XML)
        skip = next(t for t in tests if t["id"] == "test_sample::test_skip")
        assert skip["message"] == "nope"

    def test_pass_has_empty_message(self) -> None:
        tests = parse_pytest_junit_xml(self.XML)
        passed = next(t for t in tests if t["id"] == "test_sample::test_pass")
        assert passed == {
            "id": "test_sample::test_pass", "status": "pass", "message": "",
            "expected": "", "got": "",
        }

    def test_error_tag_treated_same_as_failure(self) -> None:
        xml = (
            '<testsuites><testsuite name="pytest">'
            '<testcase classname="t" name="test_fixture_broke">'
            '<error message="assert 1 == 2">boom</error>'
            "</testcase></testsuite></testsuites>"
        )
        tests = parse_pytest_junit_xml(xml)
        assert tests == [{
            "id": "t::test_fixture_broke", "status": "fail",
            "message": "assert 1 == 2", "expected": "2", "got": "1",
        }]

    def test_no_classname_uses_bare_name(self) -> None:
        xml = (
            '<testsuites><testsuite name="pytest">'
            '<testcase name="test_bare" />'
            "</testsuite></testsuites>"
        )
        assert parse_pytest_junit_xml(xml) == [
            {"id": "test_bare", "status": "pass", "message": "", "expected": "", "got": ""}
        ]

    def test_testcase_without_name_skipped(self) -> None:
        xml = (
            '<testsuites><testsuite name="pytest">'
            '<testcase classname="t" />'
            "</testsuite></testsuites>"
        )
        assert parse_pytest_junit_xml(xml) == []

    def test_empty_input_returns_empty(self) -> None:
        assert parse_pytest_junit_xml("") == []
        assert parse_pytest_junit_xml(None) == []  # type: ignore[arg-type]

    def test_malformed_xml_returns_empty(self) -> None:
        assert parse_pytest_junit_xml("<not valid xml") == []


class TestRunDriverCliPytest:
    def test_supported_kinds_tuple_has_cli_pytest(self) -> None:
        assert "cli-pytest" in SUPPORTED_KINDS

    def test_runs_real_pytest_and_parses_junit_xml(self, tmp_path) -> None:
        (tmp_path / "test_sample.py").write_text(
            "def test_pass():\n"
            "    assert True\n"
            "\n"
            "def test_fail():\n"
            "    got = 'got-value'\n"
            "    expected = 'expected-value'\n"
            "    assert got == expected\n"
        )
        result = run_driver(
            "cli-pytest",
            f'"{sys.executable}" -m pytest test_sample.py -p no:cacheprovider',
            cwd=str(tmp_path),
        )
        assert result.exit_code == 1
        assert result.ok is False
        by_id = {t["id"]: t for t in result.tests}
        assert by_id["test_sample::test_pass"]["status"] == "pass"
        fail = by_id["test_sample::test_fail"]
        assert fail["status"] == "fail"
        assert fail["got"] == "'got-value'"
        assert fail["expected"] == "'expected-value'"

    def test_ms_template_rendered_before_running(self, tmp_path) -> None:
        # Two ms dirs; only one contains a test. If `{ms}` weren't
        # substituted, "pytest {ms}" would fail to resolve any path and
        # collect zero tests — so a green single-test result proves the
        # substitution pointed pytest at the right directory.
        (tmp_path / "ms-37").mkdir()
        (tmp_path / "ms-37" / "test_sample.py").write_text(
            "def test_pass():\n    assert True\n"
        )
        (tmp_path / "ms-38").mkdir()
        (tmp_path / "ms-38" / "test_sample.py").write_text(
            "def test_pass():\n    assert False\n"
        )
        result = run_driver(
            "cli-pytest",
            f'"{sys.executable}" -m pytest {{ms}} -p no:cacheprovider',
            cwd=str(tmp_path),
            ms="ms-37",
        )
        assert result.exit_code == 0
        assert len(result.tests) == 1
        test = result.tests[0]
        assert test["status"] == "pass"
        assert test["id"].endswith("test_sample::test_pass")

    def test_crash_before_report_written_returns_no_tests(self, tmp_path) -> None:
        # A command that dies before pytest ever runs (e.g. a typo) leaves no
        # junit-xml file behind — surfaced as "0 tests found", not a crash.
        result = run_driver(
            "cli-pytest", "exit 2", cwd=str(tmp_path),
        )
        assert result.exit_code == 2
        assert result.tests == []
