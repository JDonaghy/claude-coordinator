"""Tests for coord.config.ProvidersConfig.provider_for_labels /
provider_for_labels_with_reason (#1889).

`providers.labels` mirrors `models.labels`' shape and precedent (#1430,
#1633): a per-issue-label override, resolved deterministically by the
config's own declaration order (not the issue's own label order, which
GitHub controls and this repo does not) and reported with provenance so a
route that might look surprising is self-explaining instead of read from
source.
"""

from __future__ import annotations

from coord.config import ProvidersConfig


class TestProviderForLabels:
    def test_no_labels_configured_returns_none(self) -> None:
        cfg = ProvidersConfig()  # labels defaults to {}
        assert cfg.provider_for_labels(["harness:opencode"]) is None

    def test_matching_label_returns_provider(self) -> None:
        cfg = ProvidersConfig(labels={"harness:opencode": "opencode"})
        assert cfg.provider_for_labels(["harness:opencode"]) == "opencode"

    def test_no_matching_label_returns_none(self) -> None:
        cfg = ProvidersConfig(labels={"harness:opencode": "opencode"})
        assert cfg.provider_for_labels(["bug", "enhancement"]) is None

    def test_empty_issue_labels_returns_none(self) -> None:
        cfg = ProvidersConfig(labels={"harness:opencode": "opencode"})
        assert cfg.provider_for_labels([]) is None

    def test_default_never_returned_by_this_method(self) -> None:
        """provider_for_labels never falls back to `default` itself — that's
        the caller's job (mirrors `ModelsConfig.model_for_labels`'s
        None-passthrough style, and resolve_provider_name's own chain)."""
        cfg = ProvidersConfig(default="claude", labels={})
        assert cfg.provider_for_labels(["documentation"]) is None

    def test_two_configured_labels_on_one_issue_uses_config_declaration_order(
        self,
    ) -> None:
        """#1889 label/label conflict: an issue carrying BOTH configured
        labels (e.g. `harness:opencode` and `harness:claude`) must resolve
        deterministically via `labels`' own declaration order in
        coordinator.yml — NOT the issue's own label order, which GitHub
        controls and this repo does not (mirrors #1633's fix for
        models.labels' tier/type precedence)."""
        cfg = ProvidersConfig(
            labels={"harness:opencode": "opencode", "harness:claude": "claude"},
        )
        # harness:opencode declared first -> wins, regardless of which
        # order the issue lists them in.
        assert (
            cfg.provider_for_labels(["harness:opencode", "harness:claude"])
            == "opencode"
        )
        assert (
            cfg.provider_for_labels(["harness:claude", "harness:opencode"])
            == "opencode"
        )


class TestProviderForLabelsWithReason:
    def test_unambiguous_match_names_matched_label_no_shadow(self) -> None:
        cfg = ProvidersConfig(labels={"harness:opencode": "opencode"})
        provider, matched, shadowed = cfg.provider_for_labels_with_reason(
            ["harness:opencode"]
        )
        assert (provider, matched, shadowed) == ("opencode", "harness:opencode", [])

    def test_no_match_returns_all_none_empty(self) -> None:
        cfg = ProvidersConfig(labels={"harness:opencode": "opencode"})
        assert cfg.provider_for_labels_with_reason(["bug"]) == (None, None, [])

    def test_no_labels_configured_returns_all_none_empty(self) -> None:
        cfg = ProvidersConfig()
        assert cfg.provider_for_labels_with_reason(["harness:opencode"]) == (
            None, None, [],
        )

    def test_ambiguous_match_names_matched_and_shadowed(self) -> None:
        """#1889: when more than one configured label matched, the losing
        candidate(s) are surfaced too — so a route that looks surprising
        (which harness label actually won) is self-explaining at dispatch
        time, mirroring models.labels' #1633 shadowed_labels shape."""
        cfg = ProvidersConfig(
            labels={"harness:opencode": "opencode", "harness:claude": "claude"},
        )
        provider, matched, shadowed = cfg.provider_for_labels_with_reason(
            ["harness:claude", "harness:opencode"]
        )
        assert (provider, matched, shadowed) == (
            "opencode", "harness:opencode", ["harness:claude"],
        )
