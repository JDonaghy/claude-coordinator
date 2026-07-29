"""Tests for coord/prereqs.py — the external-tool prereq manifest and
version probing behind #1570 parts B/D/E.

Mirrors tests/test_github_ops.py's TestGetPrChecks patterns for mocking
`subprocess.run`/`shutil.which` (#1564 established this style for probing
`gh` specifically; this generalizes it).
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

from coord import prereqs
from coord.github_ops import GH_PR_CHECKS_JSON_MIN_VERSION


class _Result:
    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class TestVersionComparison:
    def test_meets_floor_true_when_equal(self) -> None:
        assert prereqs.meets_floor("2.86.0", "2.86.0") is True

    def test_meets_floor_true_when_newer(self) -> None:
        assert prereqs.meets_floor("2.92.0", "2.86.0") is True

    def test_meets_floor_false_when_older(self) -> None:
        assert prereqs.meets_floor("2.45.0", "2.86.0") is False

    def test_meets_floor_handles_different_segment_counts(self) -> None:
        assert prereqs.meets_floor("2.86", "2.86.0") is True
        assert prereqs.meets_floor("2.86.0", "2.86.1") is False


class TestProbe:
    def test_missing_binary_reports_not_found(self) -> None:
        prereq = prereqs.Prereq(
            tool="nope", binary="definitely-not-a-real-binary-xyz",
            version_args=("--version",), version_re=r"(\S+)",
            min_version=None, capability=None, what_breaks="nothing works",
        )
        with patch("coord.prereqs.shutil.which", return_value=None):
            result = prereqs.probe(prereq)
        assert result.found is False
        assert result.version is None
        assert result.ok is False

    def test_found_and_version_parsed(self) -> None:
        prereq = prereqs.Prereq(
            tool="gh", binary="gh", version_args=("--version",),
            version_re=r"gh version (\S+)",
            min_version=GH_PR_CHECKS_JSON_MIN_VERSION, capability=None,
            what_breaks="merge gate breaks",
        )
        with patch("coord.prereqs.shutil.which", return_value="/usr/bin/gh"), \
             patch(
                 "coord.prereqs.subprocess.run",
                 return_value=_Result(stdout="gh version 2.92.0 (2025-01-01)\n"),
             ):
            result = prereqs.probe(prereq)
        assert result.found is True
        assert result.version == "2.92.0"
        assert result.meets_floor is True
        assert result.ok is True

    def test_version_below_floor_fails_ok(self) -> None:
        prereq = prereqs.Prereq(
            tool="gh", binary="gh", version_args=("--version",),
            version_re=r"gh version (\S+)",
            min_version=GH_PR_CHECKS_JSON_MIN_VERSION, capability=None,
            what_breaks="merge gate breaks",
        )
        with patch("coord.prereqs.shutil.which", return_value="/usr/bin/gh"), \
             patch(
                 "coord.prereqs.subprocess.run",
                 return_value=_Result(stdout="gh version 2.45.0 (2024-01-01)\n"),
             ):
            result = prereqs.probe(prereq)
        assert result.found is True
        assert result.meets_floor is False
        assert result.ok is False

    def test_unparseable_version_degrades_to_unknown_not_failure(self) -> None:
        """Matches `_gh_version()`'s existing best-effort contract: an
        output-format change must never false-fail a probe."""
        prereq = prereqs.Prereq(
            tool="gh", binary="gh", version_args=("--version",),
            version_re=r"gh version (\S+)",
            min_version=GH_PR_CHECKS_JSON_MIN_VERSION, capability=None,
            what_breaks="merge gate breaks",
        )
        with patch("coord.prereqs.shutil.which", return_value="/usr/bin/gh"), \
             patch(
                 "coord.prereqs.subprocess.run",
                 return_value=_Result(stdout="something unexpected\n"),
             ):
            result = prereqs.probe(prereq)
        assert result.found is True
        assert result.version is None
        assert result.meets_floor is None
        assert result.ok is True  # unknown, assume fine — not a false failure

    def test_nonzero_returncode_reports_not_found_not_bogus_version(self) -> None:
        """The gtk4 flagship example: `pkg-config --modversion gtk4` prints
        a descriptive "Package ... was not found" error and exits nonzero
        when the dev libs aren't installed. Before the returncode check,
        `_parse_version`'s `(\\S+)` pattern happily extracted "Package" from
        that error text as a bogus version, reporting found=True/ok=True for
        a machine with no GTK4 dev libs at all."""
        prereq = prereqs.Prereq(
            tool="gtk4", binary="pkg-config",
            version_args=("--modversion", "gtk4"), version_re=r"(\S+)",
            min_version=None, capability="gtk", what_breaks="gtk build breaks",
        )
        with patch("coord.prereqs.shutil.which", return_value="/usr/bin/pkg-config"), \
             patch(
                 "coord.prereqs.subprocess.run",
                 return_value=_Result(
                     stderr=(
                         "Package gtk4 was not found in the pkg-config "
                         "search path.\n"
                     ),
                     returncode=1,
                 ),
             ):
            result = prereqs.probe(prereq)
        assert result.found is False
        assert result.version is None
        assert result.ok is False

    def test_hang_or_missing_process_degrades_gracefully(self) -> None:
        prereq = prereqs.Prereq(
            tool="gh", binary="gh", version_args=("--version",),
            version_re=r"gh version (\S+)", min_version=None,
            capability=None, what_breaks="x",
        )
        with patch("coord.prereqs.shutil.which", return_value="/usr/bin/gh"), \
             patch(
                 "coord.prereqs.subprocess.run",
                 side_effect=subprocess.TimeoutExpired(cmd="gh", timeout=10),
             ):
            result = prereqs.probe(prereq)
        assert result.found is True
        assert result.version is None
        assert result.ok is True  # presence-only prereq, no floor to fail


class TestProbeAll:
    def test_baseline_always_probed(self) -> None:
        with patch("coord.prereqs.shutil.which", return_value=None):
            probes = prereqs.probe_all([])
        assert set(probes) == {"git", "gh"}

    def test_capability_prereqs_only_probed_when_declared(self) -> None:
        with patch("coord.prereqs.shutil.which", return_value=None):
            probes = prereqs.probe_all(["rust"])
        assert "cargo" in probes
        assert "gtk4" not in probes
        assert "browser" not in probes

    def test_unrecognised_capability_probes_nothing_extra(self) -> None:
        with patch("coord.prereqs.shutil.which", return_value=None):
            probes = prereqs.probe_all(["some-future-capability"])
        assert set(probes) == {"git", "gh"}

    def test_tool_versions_summary_is_json_friendly(self) -> None:
        with patch("coord.prereqs.shutil.which", return_value=None):
            probes = prereqs.probe_all([])
        summary = prereqs.tool_versions_summary(probes)
        assert summary["git"] == {
            "found": False, "version": None, "min_version": None,
            "meets_floor": None, "capability": None, "ok": False,
        }


class TestUnmetCapabilities:
    def test_empty_when_capability_backs_out(self) -> None:
        probes = {
            "cargo": prereqs.ToolProbe(
                tool="cargo", capability="rust", found=True, version="1.80.0",
                min_version=None, meets_floor=None, what_breaks="",
            ),
        }
        assert prereqs.unmet_capabilities(["rust"], probes) == {}

    def test_flags_missing_tool(self) -> None:
        probes = {
            "gtk4": prereqs.ToolProbe(
                tool="gtk4", capability="gtk", found=False, version=None,
                min_version=None, meets_floor=None, what_breaks="",
            ),
        }
        unmet = prereqs.unmet_capabilities(["gtk"], probes)
        assert "gtk" in unmet
        assert "gtk4" in unmet["gtk"][0]
        assert "not found" in unmet["gtk"][0]

    def test_flags_version_below_floor(self) -> None:
        # Exercised via cargo/rust (a real CAPABILITY_PREREQS entry) rather
        # than gh, which is a baseline prereq never gated by a capability
        # name — the cross-reference logic is capability-name-driven.
        probes = {
            "cargo": prereqs.ToolProbe(
                tool="cargo", capability="rust", found=True, version="1.10.0",
                min_version="1.50.0", meets_floor=False, what_breaks="",
            ),
        }
        unmet = prereqs.unmet_capabilities(["rust"], probes)
        assert "rust" in unmet
        assert "1.10.0" in unmet["rust"][0]
        assert "1.50.0" in unmet["rust"][0]

    def test_unprobed_capability_is_skipped_not_flagged(self) -> None:
        """A capability with no entry in `probes` at all (e.g. an older
        agent's /health didn't probe it) is not reported as unmet — this
        only reports claims it can actually verify."""
        assert prereqs.unmet_capabilities(["gtk"], {}) == {}

    def test_capability_with_no_registered_prereq_is_skipped(self) -> None:
        assert prereqs.unmet_capabilities(["some-custom-capability"], {}) == {}


class TestGhFloorIsSingleSourceOfTruth:
    def test_baseline_gh_prereq_imports_the_floor(self) -> None:
        """#1564's constant stays the single source of truth — this module
        must import it, never hardcode a second copy that can drift."""
        gh_prereq = next(p for p in prereqs.BASELINE_PREREQS if p.tool == "gh")
        assert gh_prereq.min_version == GH_PR_CHECKS_JSON_MIN_VERSION
