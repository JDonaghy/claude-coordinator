"""Tests for #1707: `coord assign --provider NAME` — per-assignment backend
selection so a mixed claude/opencode fleet can dispatch claude on one issue
and opencode on another in the SAME repo, at the SAME time.

Covers:
  - CLI-level validation: an unknown --provider name never reaches dispatch.
  - --provider composes with --dry-run, showing the resolved provider AND
    where it came from in the spec > repo > providers.default precedence
    chain (coord.providers.resolve_provider_name / describe_provider_choice).
  - Omitting --provider reproduces pre-#1707 behaviour exactly (provider
    stays None end-to-end).
  - Two concurrent `coord assign`s in the same repo with different
    --provider values both dispatch and report their distinct providers.
  - --provider naming a human_attended_only backend is still refused for
    this unattended path (coord.providers.guard_unattended_dispatch fires
    unchanged — a --provider flag is not a way around the #437 TOS gate).
  - --provider is rejected together with --interactive (that launcher always
    spawns ClaudePtyProvider() directly; there is no name to select).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from coord import state as state_mod
from coord.cli import main


CONFIG_YAML = """\
repos:
  - name: api
    github: acme/api
    default_branch: main
machines:
  - name: laptop
    host: laptop.tailnet
    repos: [api]
    repo_paths:
      api: /tmp/api
  - name: server
    host: server.tailnet
    repos: [api]
    repo_paths:
      api: /tmp/api
"""

# A repo-level provider override plus an extra named provider, so dry-run
# tests can exercise every link of the spec > repo > providers.default chain.
CONFIG_YAML_WITH_PROVIDERS = """\
repos:
  - name: api
    github: acme/api
    default_branch: main
    provider: repo-provider
machines:
  - name: laptop
    host: laptop.tailnet
    repos: [api]
    repo_paths:
      api: /tmp/api
  - name: server
    host: server.tailnet
    repos: [api]
    repo_paths:
      api: /tmp/api
providers:
  definitions:
    fast-claude:
      type: claude
    repo-provider:
      type: claude
"""

# A provider definition whose type is human-attended-only (claude-pty) —
# used to prove --provider cannot be used to sneak a subscription-billed
# backend onto the unattended dispatch path.
CONFIG_YAML_HUMAN_ATTENDED = """\
repos:
  - name: api
    github: acme/api
    default_branch: main
machines:
  - name: laptop
    host: laptop.tailnet
    repos: [api]
    repo_paths:
      api: /tmp/api
providers:
  definitions:
    interactive-claude:
      type: claude-pty
"""


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(CONFIG_YAML)
    return p


@pytest.fixture
def config_file_with_providers(tmp_path: Path) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(CONFIG_YAML_WITH_PROVIDERS)
    return p


@pytest.fixture
def config_file_human_attended(tmp_path: Path) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(CONFIG_YAML_HUMAN_ATTENDED)
    return p


@pytest.fixture
def coord_dir(tmp_path: Path, coord_db):
    d = tmp_path / "state"
    d.mkdir(parents=True, exist_ok=True)
    return d


class TestProviderValidation:
    def test_unknown_provider_rejected_before_dispatch(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """An unknown --provider name must fail at the CLI, before the issue
        is even fetched from GitHub — it must never reach dispatch()."""
        with patch("coord.github_ops.get_issue") as gi, \
             patch("coord.dispatch.dispatch") as disp:
            result = CliRunner().invoke(
                main,
                [
                    "assign", "laptop", "api", "42",
                    "--config", str(config_file),
                    "--provider", "ghost-provider",
                ],
            )
        assert result.exit_code == 2
        assert "ghost-provider" in result.output
        # The only implicit provider on a bare config is "claude" — the
        # error must list it as a valid name.
        assert "claude" in result.output
        gi.assert_not_called()
        disp.assert_not_called()

    def test_unknown_provider_error_lists_all_configured_names(
        self, config_file_with_providers: Path, coord_dir: Path
    ) -> None:
        result = CliRunner().invoke(
            main,
            [
                "assign", "laptop", "api", "42",
                "--config", str(config_file_with_providers),
                "--provider", "typo-provider",
            ],
        )
        assert result.exit_code == 2
        assert "fast-claude" in result.output
        assert "repo-provider" in result.output
        assert "claude" in result.output

    def test_provider_rejected_with_interactive(
        self, config_file_with_providers: Path, coord_dir: Path
    ) -> None:
        """--provider has no defined meaning under --interactive (which
        always spawns ClaudePtyProvider() directly) — refuse rather than
        silently ignoring it or letting it look like it steered anything."""
        result = CliRunner().invoke(
            main,
            [
                "assign", "laptop", "api", "42",
                "--config", str(config_file_with_providers),
                "--provider", "fast-claude",
                "--interactive",
            ],
        )
        assert result.exit_code == 2
        assert "--interactive" in result.output


class TestProviderDryRun:
    def test_dry_run_shows_explicit_provider_override(
        self, config_file_with_providers: Path, coord_dir: Path
    ) -> None:
        with patch("coord.github_ops.get_issue", return_value={"title": "t"}):
            result = CliRunner().invoke(
                main,
                [
                    "assign", "laptop", "api", "42",
                    "--config", str(config_file_with_providers),
                    "--provider", "fast-claude",
                    "--dry-run",
                ],
            )
        assert result.exit_code == 0, result.output
        assert "fast-claude" in result.output
        assert "explicit --provider" in result.output

    def test_dry_run_shows_repo_default_when_no_flag(
        self, config_file_with_providers: Path, coord_dir: Path
    ) -> None:
        """Omitting --provider on a repo with Repo.provider set surfaces
        the repo default and names it as the source (not the flag)."""
        with patch("coord.github_ops.get_issue", return_value={"title": "t"}):
            result = CliRunner().invoke(
                main,
                [
                    "assign", "laptop", "api", "42",
                    "--config", str(config_file_with_providers),
                    "--dry-run",
                ],
            )
        assert result.exit_code == 0, result.output
        assert "repo-provider" in result.output
        assert "Repo.provider" in result.output

    def test_dry_run_shows_global_default_on_bare_config(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """No providers block, no --provider: falls all the way through to
        the implicit "claude" providers.default."""
        with patch("coord.github_ops.get_issue", return_value={"title": "t"}):
            result = CliRunner().invoke(
                main,
                [
                    "assign", "laptop", "api", "42",
                    "--config", str(config_file),
                    "--dry-run",
                ],
            )
        assert result.exit_code == 0, result.output
        assert "claude" in result.output
        assert "providers.default" in result.output

    def test_dry_run_never_reaches_dispatch(
        self, config_file_with_providers: Path, coord_dir: Path
    ) -> None:
        with patch("coord.github_ops.get_issue", return_value={"title": "t"}), \
             patch("coord.dispatch.dispatch") as disp:
            result = CliRunner().invoke(
                main,
                [
                    "assign", "laptop", "api", "42",
                    "--config", str(config_file_with_providers),
                    "--provider", "fast-claude",
                    "--dry-run",
                ],
            )
        assert result.exit_code == 0
        disp.assert_not_called()


# #1889: providers.labels — an issue-level lever mirroring models.labels, so
# `gh issue edit N --add-label harness:opencode` routes the harness with no
# --provider flag to remember. `repo-provider` also configured so these
# tests can prove the label link outranks it in the precedence chain.
CONFIG_YAML_WITH_PROVIDER_LABELS = """\
repos:
  - name: api
    github: acme/api
    default_branch: main
    provider: repo-provider
machines:
  - name: laptop
    host: laptop.tailnet
    repos: [api]
    repo_paths:
      api: /tmp/api
providers:
  definitions:
    fast-claude:
      type: claude
    repo-provider:
      type: claude
  labels:
    harness:fast-claude: fast-claude
"""


@pytest.fixture
def config_file_with_provider_labels(tmp_path: Path) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(CONFIG_YAML_WITH_PROVIDER_LABELS)
    return p


class TestProviderLabelDryRun:
    """#1889 acceptance: `coord assign --dry-run` names the label as the
    reason a providers.labels match won — the same transparency #1707/#1454
    established for the explicit-flag / repo-default links of the chain."""

    def test_dry_run_labelled_issue_names_the_label(
        self, config_file_with_provider_labels: Path, coord_dir: Path
    ) -> None:
        with patch(
            "coord.github_ops.get_issue",
            return_value={"title": "t", "labels": [{"name": "harness:fast-claude"}]},
        ):
            result = CliRunner().invoke(
                main,
                [
                    "assign", "laptop", "api", "42",
                    "--config", str(config_file_with_provider_labels),
                    "--dry-run",
                ],
            )
        assert result.exit_code == 0, result.output
        assert "fast-claude" in result.output
        assert "via label 'harness:fast-claude'" in result.output

    def test_dry_run_unlabelled_issue_falls_back_to_repo_default(
        self, config_file_with_provider_labels: Path, coord_dir: Path
    ) -> None:
        """The same issue WITHOUT the label resolves to the repo default —
        the label is not sticky, and doesn't leak into unrelated issues."""
        with patch(
            "coord.github_ops.get_issue",
            return_value={"title": "t", "labels": [{"name": "bug"}]},
        ):
            result = CliRunner().invoke(
                main,
                [
                    "assign", "laptop", "api", "42",
                    "--config", str(config_file_with_provider_labels),
                    "--dry-run",
                ],
            )
        assert result.exit_code == 0, result.output
        assert "repo-provider" in result.output
        assert "Repo.provider" in result.output
        assert "via label" not in result.output

    def test_dry_run_explicit_provider_flag_still_beats_label(
        self, config_file_with_provider_labels: Path, coord_dir: Path
    ) -> None:
        """--provider still beats providers.labels — the precedence chain's
        top link is unchanged by #1889."""
        with patch(
            "coord.github_ops.get_issue",
            return_value={"title": "t", "labels": [{"name": "harness:fast-claude"}]},
        ):
            result = CliRunner().invoke(
                main,
                [
                    "assign", "laptop", "api", "42",
                    "--config", str(config_file_with_provider_labels),
                    "--provider", "repo-provider",
                    "--dry-run",
                ],
            )
        assert result.exit_code == 0, result.output
        assert "explicit --provider" in result.output
        assert "via label" not in result.output


# A provider definition of a genuinely different backend TYPE (opencode) —
# used to prove a machine that hasn't declared `provider:opencode` gets
# refused, naming the machine that DOES (#1711). Deliberately does NOT mock
# `coord.dispatch.dispatch` (unlike the classes above) — this is a
# black-box exercise of the real dispatch() refusal path end-to-end through
# the CLI, only the HTTP boundary (`coord.dispatch.httpx.post`) is stubbed.
CONFIG_YAML_MIXED_OPENCODE_FLEET = """\
repos:
  - name: api
    github: acme/api
    default_branch: main
    provider: opencode
machines:
  - name: laptop
    host: laptop.tailnet
    repos: [api]
    repo_paths:
      api: /tmp/api
  - name: workstation
    host: workstation.tailnet
    repos: [api]
    repo_paths:
      api: /tmp/api
    capabilities: ["provider:opencode"]
providers:
  definitions:
    opencode:
      type: opencode
      # #1798: pin a model in opencode's own namespace. These tests are
      # about the machine-capability gate (#1711), not model resolution —
      # an unpinned opencode provider would otherwise fall through to
      # models.default ("sonnet", a Claude alias) and get refused by the
      # separate #1798 model/provider compatibility gate before reaching
      # the assertions these tests actually care about.
      model: opencode/big-pickle
"""


@pytest.fixture
def config_file_mixed_opencode_fleet(tmp_path: Path) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(CONFIG_YAML_MIXED_OPENCODE_FLEET)
    return p


class TestProviderMachineCapabilityRefusalBlackBox:
    """#1711 black-box: `coord assign` end-to-end against the real
    `coord.dispatch.dispatch()` — a machine without the declared
    `provider:opencode` capability is refused at dispatch, naming the
    machine that DOES advertise it, never reaching the agent server."""

    def test_assign_refuses_opencode_on_a_machine_without_the_capability(
        self, config_file_mixed_opencode_fleet: Path, coord_dir: Path,
    ) -> None:
        with patch("coord.github_ops.get_issue", return_value={"title": "t", "labels": []}), \
             patch("coord.github_ops.post_issue_comment"), \
             patch("coord.github_ops.check_branch_exists", return_value=False), \
             patch("coord.claim.find_work_claim", return_value=None), \
             patch("coord.dispatch.httpx.post") as post:
            result = CliRunner().invoke(
                main,
                [
                    "assign", "laptop", "api", "7",
                    "--config", str(config_file_mixed_opencode_fleet),
                    "--skip-freshness",
                ],
            )
        assert result.exit_code == 1, result.output
        assert "laptop" in result.output
        assert "opencode" in result.output
        assert "workstation" in result.output, (
            "the refusal must name the machine that DOES advertise the "
            f"capability; got: {result.output}"
        )
        post.assert_not_called()

    def test_assign_succeeds_on_the_capable_machine(
        self, config_file_mixed_opencode_fleet: Path, coord_dir: Path,
    ) -> None:
        import httpx as _httpx

        mock_resp = _httpx.Response(
            200, json={"id": "asn-1"},
            request=_httpx.Request("POST", "http://workstation.tailnet:7433/assign"),
        )
        with patch("coord.github_ops.get_issue", return_value={"title": "t", "labels": []}), \
             patch("coord.github_ops.post_issue_comment"), \
             patch("coord.github_ops.check_branch_exists", return_value=False), \
             patch("coord.claim.find_work_claim", return_value=None), \
             patch("coord.dispatch.httpx.post", return_value=mock_resp) as post:
            result = CliRunner().invoke(
                main,
                [
                    "assign", "workstation", "api", "7",
                    "--config", str(config_file_mixed_opencode_fleet),
                    "--skip-freshness",
                ],
            )
        assert result.exit_code == 0, result.output
        post.assert_called_once()


class TestProviderDispatch:
    def test_provider_threaded_to_proposal(
        self, config_file_with_providers: Path, coord_dir: Path
    ) -> None:
        with patch("coord.github_ops.get_issue", return_value={"title": "Fix bug"}), \
             patch("coord.dispatch.dispatch", return_value={"id": "p-1"}) as disp, \
             patch("coord.github_ops.post_issue_comment"), \
             patch("coord.github_ops.check_branch_exists", return_value=False), \
             patch("coord.claim.find_work_claim", return_value=None):
            result = CliRunner().invoke(
                main,
                [
                    "assign", "laptop", "api", "7",
                    "--config", str(config_file_with_providers),
                    "--provider", "fast-claude",
                ],
            )
        assert result.exit_code == 0, result.output
        proposal = disp.call_args[0][0]
        assert proposal.provider == "fast-claude"

    def test_omitting_provider_reproduces_prior_behaviour(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """No --provider flag: Proposal.provider stays None, byte-identical
        to every `coord assign` dispatched before #1707 landed."""
        with patch("coord.github_ops.get_issue", return_value={"title": "Fix bug"}), \
             patch("coord.dispatch.dispatch", return_value={"id": "p-2"}) as disp, \
             patch("coord.github_ops.post_issue_comment"), \
             patch("coord.github_ops.check_branch_exists", return_value=False), \
             patch("coord.claim.find_work_claim", return_value=None):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "7", "--config", str(config_file)],
            )
        assert result.exit_code == 0, result.output
        proposal = disp.call_args[0][0]
        assert proposal.provider is None

    def test_two_concurrent_assignments_same_repo_different_providers(
        self, config_file_with_providers: Path, coord_dir: Path
    ) -> None:
        """The operator requirement this issue exists for: claude on one
        issue, opencode (here: a second named provider) on another, in the
        SAME repo, both dispatched — each reporting its own distinct
        provider."""
        dispatched_ids = iter(["conc-1", "conc-2"])

        def _fake_dispatch(proposal, config, **kwargs):
            return {"id": next(dispatched_ids), "_provider_name": proposal.provider or "claude"}

        with patch("coord.github_ops.get_issue", return_value={"title": "t"}), \
             patch("coord.dispatch.dispatch", side_effect=_fake_dispatch) as disp, \
             patch("coord.github_ops.post_issue_comment"), \
             patch("coord.github_ops.check_branch_exists", return_value=False), \
             patch("coord.claim.find_work_claim", return_value=None):
            result_a = CliRunner().invoke(
                main,
                [
                    "assign", "laptop", "api", "10",
                    "--config", str(config_file_with_providers),
                    "--provider", "fast-claude",
                ],
            )
            result_b = CliRunner().invoke(
                main,
                [
                    "assign", "server", "api", "11",
                    "--config", str(config_file_with_providers),
                    "--provider", "repo-provider",
                ],
            )

        assert result_a.exit_code == 0, result_a.output
        assert result_b.exit_code == 0, result_b.output
        assert disp.call_count == 2

        proposals = [c[0][0] for c in disp.call_args_list]
        by_issue = {p.issue_number: p for p in proposals}
        assert by_issue[10].provider == "fast-claude"
        assert by_issue[11].provider == "repo-provider"
        assert by_issue[10].repo_name == by_issue[11].repo_name == "api"

        records = {r["issue_number"]: r for r in state_mod.load_dispatched()}
        assert records[10]["provider_name"] == "fast-claude"
        assert records[11]["provider_name"] == "repo-provider"

    def test_human_attended_only_provider_refused_for_unattended_dispatch(
        self, config_file_human_attended: Path, coord_dir: Path
    ) -> None:
        """#1707 acceptance: --provider naming a human_attended_only backend
        is still refused on this (non --interactive) path — the #437
        structural TOS gate (coord.providers.guard_unattended_dispatch)
        fires exactly as it does for a Repo.provider/providers.default
        override; --provider is not a bypass."""
        with patch("coord.github_ops.get_issue", return_value={"title": "t"}), \
             patch("coord.claim.find_work_claim", return_value=None):
            result = CliRunner().invoke(
                main,
                [
                    "assign", "laptop", "api", "7",
                    "--config", str(config_file_human_attended),
                    "--provider", "interactive-claude",
                ],
            )
        assert result.exit_code == 1
        assert "dispatch failed" in result.output
        assert "human_attended_only=True" in result.output
        assert "--interactive" in result.output
