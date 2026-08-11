"""Tests for the #1671 agent-startup diagnostics: `_startup_diagnostic_lines`
and `_log_install_location` in `coord.commands.agent_ops`.

#1671: every machine's `rust` capability read unmet even though `cargo` was
installed, because the capability probe resolves through the *agent
process's* PATH — and a systemd user unit's PATH is minimal (omits
`~/.cargo/bin`) unless the unit says otherwise. These two helpers make that
condition loud at agent startup (PATH + install location + any
declared-but-unmet capability) instead of requiring an operator to notice a
`coord doctor` red and go SSH in.

Per the #1671 test-scope note: never shell out to the real cargo/chromium
binary here — fake `shutil.which`/`subprocess.run` the same way
tests/test_prereqs.py does. "A worker can run cargo build" is a fleet
property, not a unit test.
"""

from __future__ import annotations

import subprocess

from unittest.mock import patch

from coord.commands.agent_ops import _log_install_location, _startup_diagnostic_lines


class _Result:
    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class TestStartupDiagnosticLines:
    def test_reports_resolved_path(self) -> None:
        with patch("coord.prereqs.shutil.which", return_value=None):
            lines = _startup_diagnostic_lines([], path_env="/venv/bin:/usr/bin")
        assert lines[0] == "coord agent: PATH=/venv/bin:/usr/bin"

    def test_falls_back_to_process_environ_when_path_env_not_given(self) -> None:
        with patch("coord.prereqs.shutil.which", return_value=None), \
             patch.dict("os.environ", {"PATH": "/from/environ"}, clear=False):
            lines = _startup_diagnostic_lines([])
        assert lines[0] == "coord agent: PATH=/from/environ"

    def test_no_capabilities_declared_is_all_ok_no_warning(self) -> None:
        with patch("coord.prereqs.shutil.which", return_value="/usr/bin/git"), \
             patch(
                 "coord.prereqs.subprocess.run",
                 return_value=_Result(stdout="git version 2.43.0\n"),
             ):
            lines = _startup_diagnostic_lines([], path_env="/usr/bin")
        assert not any("WARNING" in line for line in lines)
        assert any("all probe OK" in line for line in lines)

    def test_declared_capability_with_missing_binary_logs_loud_warning(self) -> None:
        """The #1671 flagship case: `rust` declared, `cargo` not on PATH —
        must produce a WARNING line naming the capability and the reason,
        not a silent/aggregate-only result."""

        def which(binary: str) -> str | None:
            return "/usr/bin/git" if binary == "git" else None

        with patch("coord.prereqs.shutil.which", side_effect=which), \
             patch(
                 "coord.prereqs.subprocess.run",
                 return_value=_Result(stdout="git version 2.43.0\n"),
             ):
            lines = _startup_diagnostic_lines(["rust"], path_env="/usr/bin")
        warnings = [line for line in lines if "WARNING" in line]
        assert len(warnings) == 1
        assert "rust" in warnings[0]
        assert "cargo not found" in warnings[0]
        assert "#1570 D" in warnings[0]

    def test_unrecognised_capability_is_not_flagged(self) -> None:
        with patch("coord.prereqs.shutil.which", return_value=None):
            lines = _startup_diagnostic_lines(["some-future-capability"], path_env="/x")
        assert not any("WARNING" in line for line in lines)


class TestLogInstallLocation:
    def test_pypi_install_reports_location(self) -> None:
        pip_show_output = (
            "Name: code-coordinator\n"
            "Version: 0.4.94\n"
            "Location: /home/john/.coord-venv/lib/python3.12/site-packages\n"
        )
        with patch(
            "coord.health.checks.agent_install.subprocess.run",
            return_value=_Result(stdout=pip_show_output),
        ):
            line = _log_install_location()
        assert "0.4.94" in line
        assert "pypi install" in line
        assert "site-packages" in line
        assert "EDITABLE" not in line

    def test_editable_install_flags_it(self) -> None:
        pip_show_output = (
            "Name: code-coordinator\n"
            "Version: 0.5.0\n"
            "Editable project location: /home/john/src/claude-coordinator\n"
            "Location: /home/john/.coord-venv/lib/python3.12/site-packages\n"
        )
        with patch(
            "coord.health.checks.agent_install.subprocess.run",
            return_value=_Result(stdout=pip_show_output),
        ):
            line = _log_install_location()
        assert "EDITABLE" in line
        assert "/home/john/src/claude-coordinator" in line
        assert "#1628" in line

    def test_pip_show_failure_degrades_to_unknown_not_a_crash(self) -> None:
        with patch(
            "coord.health.checks.agent_install.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="pip", timeout=8),
        ):
            line = _log_install_location()
        assert "install location unknown" in line

    def test_package_not_installed_degrades_to_unknown(self) -> None:
        with patch(
            "coord.health.checks.agent_install.subprocess.run",
            return_value=_Result(returncode=1),
        ):
            line = _log_install_location()
        assert "install location unknown" in line
