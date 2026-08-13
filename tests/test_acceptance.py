"""Tests for coord/acceptance.py — manifest loading + verdict assembly (#944).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coord.acceptance import (
    ForPathResolutionError,
    MOCK_EXT_TO_DRIVER_KIND,
    ManifestData,
    ManifestError,
    acceptance_capability_gap,
    apply_expected_red,
    build_verdict,
    bug_contract_path,
    clear_expected_red_entries,
    dump_manifest_error_hint,
    expected_red_failure_summary,
    failure_summary,
    issue_dirname,
    load_expected_red,
    load_manifest,
    ms_dir_for_issue,
    oracle_loop_contract_block,
    parse_manifest_text,
    resolve_for_path,
)
# Aliased on import: pytest treats any module-level `test_*` name as a
# collectible test function, and `test_ids_for_issue` takes required
# positional args — importing it under its real name breaks collection.
from coord.acceptance import test_ids_for_issue as ids_for_issue
from coord.config import AcceptanceConfig, AcceptanceDriverConfig, Config
from coord.models import Machine, Repo


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


class TestMsDirForIssue:
    def test_missing_dir_returns_none(self, tmp_path: Path) -> None:
        assert ms_dir_for_issue(tmp_path / "tests" / "acceptance", 945) is None

    def test_finds_owning_dir(self, tmp_path: Path) -> None:
        root = tmp_path / "tests" / "acceptance"
        (root / "ms01").mkdir(parents=True)
        (root / "ms02").mkdir(parents=True)
        (root / "ms01" / "manifest.yml").write_text("tests:\n  ms01::a: 944\n")
        (root / "ms02" / "manifest.yml").write_text("tests:\n  ms02::b: 945\n")
        assert ms_dir_for_issue(root, 945) == "ms02"
        assert ms_dir_for_issue(root, 944) == "ms01"

    def test_issue_not_in_any_manifest_returns_none(self, tmp_path: Path) -> None:
        root = tmp_path / "tests" / "acceptance"
        (root / "ms01").mkdir(parents=True)
        (root / "ms01" / "manifest.yml").write_text("tests:\n  ms01::a: 944\n")
        assert ms_dir_for_issue(root, 999) is None

    def test_malformed_manifest_propagates(self, tmp_path: Path) -> None:
        root = tmp_path / "tests" / "acceptance"
        (root / "ms01").mkdir(parents=True)
        (root / "ms01" / "manifest.yml").write_text("tests: [not, a, mapping\n")
        with pytest.raises(ManifestError):
            ms_dir_for_issue(root, 945)


class TestParseManifestText:
    """#1138: parse_manifest_text is the shared parser behind both
    _parse_manifest_file (local disk) and the dispatch-time GitHub-fetch
    reader in coord.milestone_dispatch — exercise its ManifestData.exempt
    output directly rather than only through _parse_manifest_file's
    tests-only view."""

    def test_no_exempt_key_is_empty_frozenset(self) -> None:
        data = parse_manifest_text("tests:\n  a: 944\n")
        assert data == ManifestData(tests={"a": 944}, exempt=frozenset())

    def test_exempt_list_parsed(self) -> None:
        data = parse_manifest_text("tests:\n  a: 944\nexempt: [1125, 1130]\n")
        assert data.exempt == frozenset({1125, 1130})
        assert data.tests == {"a": 944}

    def test_non_list_exempt_ignored(self) -> None:
        data = parse_manifest_text("exempt: 1125\n")
        assert data.exempt == frozenset()

    def test_empty_text_returns_empty_data(self) -> None:
        assert parse_manifest_text("") == ManifestData()

    def test_malformed_yaml_raises_manifest_error(self) -> None:
        with pytest.raises(ManifestError):
            parse_manifest_text("tests: [not, a, mapping\n")

    def test_non_mapping_raises(self) -> None:
        with pytest.raises(ManifestError, match="must be a mapping"):
            parse_manifest_text("- a\n- b\n")


class TestExpectedRedParsing:
    """#2164: the ``expected_red:`` registry parsed off ManifestData."""

    def test_no_key_is_empty_dict(self) -> None:
        data = parse_manifest_text("tests:\n  a: 944\n")
        assert data.expected_red == {}

    def test_parses_issue_scoped_lists(self) -> None:
        data = parse_manifest_text(
            "tests:\n  a: 554\nexpected_red:\n  554:\n    - a\n    - b\n"
        )
        assert data.expected_red == {554: frozenset({"a", "b"})}

    def test_non_dict_value_ignored(self) -> None:
        data = parse_manifest_text("expected_red:\n  554: not-a-list\n")
        assert data.expected_red == {}

    def test_non_dict_expected_red_ignored(self) -> None:
        data = parse_manifest_text("expected_red: [1, 2]\n")
        assert data.expected_red == {}

    def test_non_integer_issue_key_ignored(self) -> None:
        data = parse_manifest_text("expected_red:\n  not-a-number:\n    - a\n")
        assert data.expected_red == {}


class TestLoadExpectedRed:
    def test_missing_dir_returns_empty(self, tmp_path: Path) -> None:
        assert load_expected_red(tmp_path / "tests" / "acceptance") == {}

    def test_flattens_issue_to_test_ids(self, tmp_path: Path) -> None:
        root = tmp_path / "tests" / "acceptance"
        ms = root / "ms11"
        ms.mkdir(parents=True)
        (ms / "manifest.yml").write_text(
            "tests:\n  a: 554\n  b: 554\nexpected_red:\n  554:\n    - a\n    - b\n"
        )
        assert load_expected_red(root) == {"a": 554, "b": 554}

    def test_merges_across_slices(self, tmp_path: Path) -> None:
        root = tmp_path / "tests" / "acceptance"
        (root / "ms11").mkdir(parents=True)
        (root / "ms12").mkdir(parents=True)
        (root / "ms11" / "manifest.yml").write_text(
            "expected_red:\n  554:\n    - a\n"
        )
        (root / "ms12" / "manifest.yml").write_text(
            "expected_red:\n  600:\n    - c\n"
        )
        assert load_expected_red(root) == {"a": 554, "c": 600}

    def test_no_expected_red_block_is_empty(self, tmp_path: Path) -> None:
        root = tmp_path / "tests" / "acceptance"
        (root / "ms11").mkdir(parents=True)
        (root / "ms11" / "manifest.yml").write_text("tests:\n  a: 554\n")
        assert load_expected_red(root) == {}


class TestApplyExpectedRed:
    def test_no_expected_red_ids_is_a_no_op(self) -> None:
        verdict = build_verdict([{"id": "a", "status": "fail"}], scope="all")
        result = apply_expected_red(verdict, set())
        assert result["ci_green"] == result["green"] is False
        assert result["unexpected_green"] == []
        assert result["expected_red_still_red"] == []

    def test_expected_red_failure_does_not_block_ci_green(self) -> None:
        """The whole point (#2164 acceptance criterion 1): a sealed slice
        authored red merges without turning the default branch red."""
        verdict = build_verdict(
            [
                {"id": "wide_label_paints_every_glyph", "status": "fail"},
                {"id": "ascii_label_is_unchanged", "status": "pass"},
            ],
            scope="all",
        )
        assert verdict["green"] is False
        result = apply_expected_red(verdict, {"wide_label_paints_every_glyph"})
        assert result["ci_green"] is True
        assert result["expected_red_still_red"] == ["wide_label_paints_every_glyph"]
        assert result["unexpected_green"] == []

    def test_expected_red_that_passes_is_a_hard_failure(self) -> None:
        """Acceptance criterion 2: an expected-red test that PASSES fails
        the run, loudly and distinguishably from an ordinary failure."""
        verdict = build_verdict(
            [{"id": "wide_label_paints_every_glyph", "status": "pass"}], scope="all",
        )
        assert verdict["green"] is True  # raw verdict looks fine...
        result = apply_expected_red(verdict, {"wide_label_paints_every_glyph"})
        assert result["ci_green"] is False  # ...but the CI-facing one isn't.
        assert result["unexpected_green"] == ["wide_label_paints_every_glyph"]

    def test_real_failure_alongside_expected_red_still_blocks(self) -> None:
        verdict = build_verdict(
            [
                {"id": "expected_red_id", "status": "fail"},
                {"id": "unrelated_regression", "status": "fail"},
            ],
            scope="all",
        )
        result = apply_expected_red(verdict, {"expected_red_id"})
        assert result["ci_green"] is False

    def test_empty_test_list_is_not_ci_green_even_with_expected_red(self) -> None:
        verdict = build_verdict([], scope="all")
        result = apply_expected_red(verdict, {"a"})
        assert result["ci_green"] is False


class TestExpectedRedFailureSummary:
    def test_empty_when_no_unexpected_green(self) -> None:
        verdict = apply_expected_red(
            build_verdict([{"id": "a", "status": "fail"}], scope="all"), {"a"},
        )
        assert expected_red_failure_summary(verdict) == ""

    def test_names_the_hard_failure_distinctly(self) -> None:
        verdict = apply_expected_red(
            build_verdict([{"id": "a", "status": "pass"}], scope="all"), {"a"},
        )
        summary = expected_red_failure_summary(verdict)
        assert "HARD FAILURE" in summary
        assert "a" in summary
        assert "NOT an ordinary test failure" in summary


class TestClearExpectedRedEntries:
    ISSUE_EXAMPLE_TEXT = (
        "tests:\n"
        "  ms11_554_wide_tab_labels::wide_label_paints_every_glyph_in_its_own_columns: 554\n"
        "\n"
        "expected_red:\n"
        "  554:\n"
        "    - ms11_554_wide_tab_labels::wide_label_paints_every_glyph_in_its_own_columns\n"
        "    - ms11_554_wide_tab_labels::measured_tab_budget_matches_the_painted_width\n"
        "    # ascii_label_is_unchanged is deliberately absent — it is the control and must be green now\n"
    )

    def test_no_op_when_id_not_present(self) -> None:
        assert clear_expected_red_entries("tests:\n  a: 1\n", 1, {"nope"}) is None

    def test_no_op_when_cleared_ids_empty(self) -> None:
        assert clear_expected_red_entries(self.ISSUE_EXAMPLE_TEXT, 554, set()) is None

    def test_partial_clear_keeps_the_other_id_and_the_comment(self) -> None:
        result = clear_expected_red_entries(
            self.ISSUE_EXAMPLE_TEXT,
            554,
            {"ms11_554_wide_tab_labels::wide_label_paints_every_glyph_in_its_own_columns"},
        )
        assert result is not None
        # The cleared id's *list item* line is gone from expected_red — it
        # legitimately still appears once, in the untouched `tests:` block.
        assert result.count("wide_label_paints_every_glyph_in_its_own_columns") == 1
        assert "    - ms11_554_wide_tab_labels::measured_tab_budget_matches_the_painted_width" in result
        assert "deliberately absent" in result  # comment preserved
        assert "  554:" in result  # issue header preserved (one id remains)
        # Everything outside the expected_red block is untouched byte-for-byte.
        assert result.startswith(
            "tests:\n"
            "  ms11_554_wide_tab_labels::wide_label_paints_every_glyph_in_its_own_columns: 554\n"
        )

    def test_full_clear_drops_the_issue_block(self) -> None:
        result = clear_expected_red_entries(
            self.ISSUE_EXAMPLE_TEXT,
            554,
            {
                "ms11_554_wide_tab_labels::wide_label_paints_every_glyph_in_its_own_columns",
                "ms11_554_wide_tab_labels::measured_tab_budget_matches_the_painted_width",
            },
        )
        assert result is not None
        assert "expected_red" not in result
        assert "554:" not in result
        # The id legitimately still appears once, in the untouched `tests:`
        # block — only its expected_red list-item line is gone.
        assert result.count("wide_label_paints_every_glyph_in_its_own_columns") == 1
        # Unrelated content (the `tests:` block) is untouched.
        assert "tests:\n  ms11_554_wide_tab_labels" in result

    def test_result_is_parseable_and_reflects_the_clear(self) -> None:
        """Round-trip through parse_manifest_text — the whole point is that
        the coordinator can commit this text back as a valid manifest."""
        result = clear_expected_red_entries(
            self.ISSUE_EXAMPLE_TEXT,
            554,
            {"ms11_554_wide_tab_labels::wide_label_paints_every_glyph_in_its_own_columns"},
        )
        assert result is not None
        data = parse_manifest_text(result)
        assert data.expected_red == {
            554: frozenset({"ms11_554_wide_tab_labels::measured_tab_budget_matches_the_painted_width"})
        }
        assert data.tests == {
            "ms11_554_wide_tab_labels::wide_label_paints_every_glyph_in_its_own_columns": 554
        }

    def test_leaves_other_issues_alone(self) -> None:
        text = "expected_red:\n  1:\n    - a\n  2:\n    - b\n"
        result = clear_expected_red_entries(text, 1, {"a"})
        assert result is not None
        assert "2:" in result
        assert "- b" in result
        data = parse_manifest_text(result)
        assert data.expected_red == {2: frozenset({"b"})}

    def test_untouched_when_manifest_has_no_expected_red_block(self) -> None:
        assert clear_expected_red_entries("tests:\n  a: 1\n", 1, {"a"}) is None


class TestOracleLoopContractBlock:
    def test_empty_when_issue_not_authored(self, tmp_path: Path) -> None:
        root = tmp_path / "tests" / "acceptance"
        assert oracle_loop_contract_block(root, "api", 945) == ""

    def test_empty_on_malformed_manifest(self, tmp_path: Path) -> None:
        # Fail-soft (#603-style): a manifest read hiccup must never blow up
        # the dispatch hot path — it degrades to "no block" instead.
        root = tmp_path / "tests" / "acceptance"
        (root / "ms01").mkdir(parents=True)
        (root / "ms01" / "manifest.yml").write_text("tests: [not, a, mapping\n")
        assert oracle_loop_contract_block(root, "api", 945) == ""

    def test_block_names_contract_path_and_run_command(self, tmp_path: Path) -> None:
        root = tmp_path / "tests" / "acceptance"
        (root / "ms25").mkdir(parents=True)
        (root / "ms25" / "manifest.yml").write_text("tests:\n  ms25::a: 945\n")
        block = oracle_loop_contract_block(root, "api", 945)
        assert block.startswith("## 🔒 Oracle-loop acceptance contract")
        assert "tests/acceptance/ms25/contract.md" in block
        assert "coord acceptance run --repo api --issue 945" in block
        assert "tests/acceptance/**" in block
        assert "STUCK:" in block
        # #846: the contract points a churning worker at `coord acceptance
        # stall` (in addition to a STUCK: line for the interactive log).
        assert "coord acceptance stall --repo api --issue 945" in block

    def test_block_points_at_the_mocks_dir_and_says_satisfy_not_edit(
        self, tmp_path: Path
    ) -> None:
        """#1542: for a web slice the `.html` mocks under `mocks/` are part
        of the sealed contract, not just `contract.md` — the worker briefing
        must say so plainly, and must not soften "may not edit
        tests/acceptance/**" into something that reads as "edit the
        assertions to match the app."""
        root = tmp_path / "tests" / "acceptance"
        (root / "ms25").mkdir(parents=True)
        (root / "ms25" / "manifest.yml").write_text("tests:\n  ms25::a: 945\n")
        block = oracle_loop_contract_block(root, "api", 945)
        assert "tests/acceptance/ms25/mocks/" in block
        assert "must satisfy" in block
        assert "not the other way around" in block


class TestIssueDirnameAndBugContractPath:
    """#1964 (docs/TEST_FIRST_BUG_LANE.md): the bug lane's single-issue
    counterpart to ms_dirname/gate_a_contract_path — no milestone number
    anywhere in the name."""

    def test_issue_dirname(self) -> None:
        assert issue_dirname(1234) == "issue-1234"

    def test_bug_contract_path(self) -> None:
        assert bug_contract_path(1234) == "tests/acceptance/issue-1234/contract.md"


class TestBugLaneNeedsNoMilestone:
    """#1964: end-to-end proof that a hand-authored `issue-NN/` slice —
    with no `ms-NN/` directory anywhere in the tree, and no milestone
    involved at any step — is discovered, scoped, and injected into the
    worker briefing by the exact same machinery an `ms-NN/` slice uses.
    This is the acceptance bar for "no milestone ceremony required"."""

    def test_manifest_and_contract_block_work_with_only_an_issue_dir(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "tests" / "acceptance"
        bug_dir = root / issue_dirname(1234)
        bug_dir.mkdir(parents=True)
        (bug_dir / "manifest.yml").write_text("tests:\n  issue_1234::popup_border: 1234\n")

        assert ms_dir_for_issue(root, 1234) == "issue-1234"
        manifest = load_manifest(root)
        assert manifest == {"issue_1234::popup_border": 1234}

        block = oracle_loop_contract_block(root, "vimcode", 1234)
        assert bug_contract_path(1234) in block
        assert "coord acceptance run --repo vimcode --issue 1234" in block
        # No ms-* dir exists anywhere — confirm the fixture itself proves the
        # negative, not just that the assertions above happened to pass.
        assert not list(root.glob("ms-*"))


class TestAcceptanceCapabilityGap:
    """#966: cheap detection mirroring `coord.smoke.pick_smoke_machine`'s
    candidate filter — no remote-exec plumbing, just "is this the wrong
    host?" so callers can fail loudly instead of running on hardware that
    can't actually support the driver."""

    @staticmethod
    def _config(*, here_caps: list[str], other_caps: list[str]) -> Config:
        return Config(
            repos=[Repo(name="webapp", github="acme/webapp")],
            machines=[
                Machine(
                    name="here", host="here.tail", capabilities=here_caps,
                    repos=["webapp"],
                ),
                Machine(
                    name="other", host="other.tail", capabilities=other_caps,
                    repos=["webapp"],
                ),
            ],
        )

    def test_no_capability_required_is_never_a_gap(self, monkeypatch) -> None:
        monkeypatch.setattr("socket.gethostname", lambda: "here")
        cfg = self._config(here_caps=[], other_caps=["browser"])
        assert acceptance_capability_gap("", "webapp", cfg) is None

    def test_local_host_already_has_capability_no_gap(self, monkeypatch) -> None:
        monkeypatch.setattr("socket.gethostname", lambda: "here")
        cfg = self._config(here_caps=["browser"], other_caps=[])
        assert acceptance_capability_gap("browser", "webapp", cfg) is None

    def test_local_host_missing_capability_returns_other_machine(self, monkeypatch) -> None:
        monkeypatch.setattr("socket.gethostname", lambda: "here")
        cfg = self._config(here_caps=[], other_caps=["browser"])
        gap = acceptance_capability_gap("browser", "webapp", cfg)
        assert gap is not None
        assert gap.name == "other"

    def test_no_other_machine_has_it_either_no_gap(self, monkeypatch) -> None:
        # Nothing to route to — failing wouldn't be actionable.
        monkeypatch.setattr("socket.gethostname", lambda: "here")
        cfg = self._config(here_caps=[], other_caps=[])
        assert acceptance_capability_gap("browser", "webapp", cfg) is None

    def test_unrecognized_host_gets_benefit_of_the_doubt(self, monkeypatch) -> None:
        # This process's hostname doesn't match any configured machine —
        # could be a dev box outside the fleet with everything installed.
        monkeypatch.setattr("socket.gethostname", lambda: "nowhere")
        cfg = self._config(here_caps=[], other_caps=["browser"])
        assert acceptance_capability_gap("browser", "webapp", cfg) is None

    def test_other_machine_without_repo_access_is_not_a_candidate(self, monkeypatch) -> None:
        monkeypatch.setattr("socket.gethostname", lambda: "here")
        cfg = Config(
            repos=[Repo(name="webapp", github="acme/webapp")],
            machines=[
                Machine(name="here", host="here.tail", capabilities=[], repos=["webapp"]),
                Machine(
                    name="other", host="other.tail", capabilities=["browser"],
                    repos=["some-other-repo"],
                ),
            ],
        )
        assert acceptance_capability_gap("browser", "webapp", cfg) is None


class TestWorkedExampleWebMock:
    """#1542: `tests/acceptance/ms-example/mocks/home-active.html` is the
    committed worked example the "next test-author copies a real file, not
    a description" — this pins the acceptance criteria that make it a valid
    `web-playwright` mock rather than just some HTML file: self-contained
    (no external assets it can't render without), carries its own CSS, and
    exposes hooks (`data-testid`) a test-author would actually assert
    against. `ms-example` is deliberately NOT a real milestone number so it
    is inert to every milestone-scanning code path (`load_manifest`,
    `ms_dir_for_issue`, `resolve_for_path`'s GitHub-dir listing) — this test
    reads it straight off disk instead."""

    MOCK_PATH = (
        Path(__file__).resolve().parent.parent
        / "tests" / "acceptance" / "ms-example" / "mocks" / "home-active.html"
    )

    def test_mock_file_exists(self) -> None:
        assert self.MOCK_PATH.is_file(), f"missing worked example: {self.MOCK_PATH}"

    def test_mock_is_self_contained_no_external_assets(self) -> None:
        html = self.MOCK_PATH.read_text()
        assert "<link" not in html.lower(), "no external stylesheet — CSS must be inline"
        assert "src=\"http" not in html.lower()
        assert "src='http" not in html.lower()

    def test_mock_carries_its_own_inline_css(self) -> None:
        html = self.MOCK_PATH.read_text()
        assert "<style>" in html

    def test_mock_exposes_testable_hooks(self) -> None:
        html = self.MOCK_PATH.read_text()
        assert 'data-testid="pipeline-card"' in html
        assert 'role="tab"' in html
        assert 'aria-selected="true"' in html

    def test_mock_extension_resolves_to_web_playwright(self) -> None:
        assert MOCK_EXT_TO_DRIVER_KIND[self.MOCK_PATH.suffix] == "web-playwright"


class TestMockExtToDriverKindRegistry:
    """#1542: the single source of truth for mock-suffix -> driver-kind
    resolution — every consumer (`resolve_for_path`, and the mock-author /
    test-author briefings' human-facing descriptions) must agree with this
    table rather than re-deriving it."""

    def test_html_maps_to_web_playwright(self) -> None:
        assert MOCK_EXT_TO_DRIVER_KIND[".html"] == "web-playwright"

    def test_screen_and_out_are_unchanged(self) -> None:
        assert MOCK_EXT_TO_DRIVER_KIND[".screen"] == "tui-tuidriver"
        assert MOCK_EXT_TO_DRIVER_KIND[".out"] == "cli-pytest"


class TestResolveForPath:
    """#1453 review finding 1: the ONE place ``--for-path`` is derived from
    a milestone's Gate-A mock kind (``*.screen`` -> ``tui-tuidriver``,
    ``*.out`` -> ``cli-pytest``, docs/ORACLE_LOOP.md) — shared by
    ``coord/drive.py``'s JIT-authoring dispatch (and, per the pinned #1453
    review guidance, #1460's eventual TUI-menu equivalent)."""

    @staticmethod
    def _routed_config() -> Config:
        return Config(
            repos=[Repo(name="claude-coordinator", github="john/claude-coordinator")],
            machines=[],
            acceptance=AcceptanceConfig(
                drivers={
                    "claude-coordinator": AcceptanceDriverConfig(
                        routes=[
                            AcceptanceDriverConfig(
                                match="coord/**", kind="cli-pytest", run="pytest",
                            ),
                            AcceptanceDriverConfig(
                                match="tui/**", kind="tui-tuidriver", run="cargo test",
                            ),
                            AcceptanceDriverConfig(
                                match="coord/dashboard/webapp/**", kind="web-playwright",
                                run="npx playwright test",
                            ),
                        ]
                    )
                }
            ),
        )

    def test_repo_with_no_driver_at_all_returns_none(self) -> None:
        cfg = Config(repos=[Repo(name="api", github="acme/api")], machines=[])
        assert resolve_for_path(cfg, cfg.repo("api"), 37) is None

    def test_unrouted_flat_driver_returns_none_without_listing_anything(self) -> None:
        cfg = Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[],
            acceptance=AcceptanceConfig(
                drivers={"api": AcceptanceDriverConfig(kind="cli-pytest", run="pytest")}
            ),
        )
        calls: list[tuple] = []
        result = resolve_for_path(
            cfg, cfg.repo("api"), 37,
            list_mock_dir=lambda *a: calls.append(a) or (),
        )
        assert result is None
        assert calls == []

    def test_screen_mocks_resolve_to_the_tui_tuidriver_route(self) -> None:
        cfg = self._routed_config()
        result = resolve_for_path(
            cfg, cfg.repo("claude-coordinator"), 38,
            list_mock_dir=lambda *a: ("plans-base.screen", "plans-detail.screen"),
        )
        assert result == "tui/**"

    def test_out_mocks_resolve_to_the_cli_pytest_route(self) -> None:
        cfg = self._routed_config()
        result = resolve_for_path(
            cfg, cfg.repo("claude-coordinator"), 37,
            list_mock_dir=lambda *a: ("usage_by_issue.out",),
        )
        assert result == "coord/**"

    def test_html_mocks_resolve_to_the_web_playwright_route(self) -> None:
        """#1542: the hand-authored HTML wireframe shape (docs/ORACLE_LOOP.md)
        resolves the same way `.screen`/`.out` already do — the whole point
        of registering `.html` in `MOCK_EXT_TO_DRIVER_KIND` is that this
        derivation needs no kind-specific code path."""
        cfg = self._routed_config()
        result = resolve_for_path(
            cfg, cfg.repo("claude-coordinator"), 42,
            list_mock_dir=lambda *a: ("home-active.html", "home-empty.html"),
        )
        assert result == "coord/dashboard/webapp/**"

    def test_passes_repo_github_mocks_path_and_default_branch_to_the_lister(self) -> None:
        cfg = self._routed_config()
        calls: list[tuple] = []
        resolve_for_path(
            cfg, cfg.repo("claude-coordinator"), 38,
            list_mock_dir=lambda *a: calls.append(a) or ("x.screen",),
        )
        assert calls == [
            ("john/claude-coordinator", "tests/acceptance/ms-38/mocks", "main"),
        ]

    def test_unrecognized_extensions_are_ignored_not_fatal(self) -> None:
        cfg = self._routed_config()
        result = resolve_for_path(
            cfg, cfg.repo("claude-coordinator"), 38,
            list_mock_dir=lambda *a: ("README.md", "a.screen"),
        )
        assert result == "tui/**"

    def test_no_recognized_mocks_raises(self) -> None:
        cfg = self._routed_config()
        with pytest.raises(ForPathResolutionError, match="no recognized mock files"):
            resolve_for_path(
                cfg, cfg.repo("claude-coordinator"), 38,
                list_mock_dir=lambda *a: (),
            )

    def test_mixed_mock_kinds_raises(self) -> None:
        cfg = self._routed_config()
        with pytest.raises(ForPathResolutionError, match="mixed mock kinds"):
            resolve_for_path(
                cfg, cfg.repo("claude-coordinator"), 38,
                list_mock_dir=lambda *a: ("a.screen", "b.out"),
            )

    def test_kind_with_no_matching_route_raises(self) -> None:
        cfg = Config(
            repos=[Repo(name="claude-coordinator", github="john/claude-coordinator")],
            machines=[],
            acceptance=AcceptanceConfig(
                drivers={
                    "claude-coordinator": AcceptanceDriverConfig(
                        routes=[
                            AcceptanceDriverConfig(
                                match="coord/**", kind="cli-pytest", run="pytest",
                            ),
                        ]
                    )
                }
            ),
        )
        with pytest.raises(ForPathResolutionError, match="matches 0 routes"):
            resolve_for_path(
                cfg, cfg.repo("claude-coordinator"), 38,
                list_mock_dir=lambda *a: ("a.screen",),
            )

    def test_error_message_names_the_no_acceptance_and_manual_for_path_escape_hatches(
        self,
    ) -> None:
        cfg = self._routed_config()
        with pytest.raises(ForPathResolutionError) as exc:
            resolve_for_path(
                cfg, cfg.repo("claude-coordinator"), 38,
                list_mock_dir=lambda *a: (),
            )
        assert "--no-acceptance" in str(exc.value)
        assert "--for-path" in str(exc.value)
