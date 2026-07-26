"""Tests for coord.config.ModelsConfig.model_for_labels / model_for_estimate (#1430).

`models.labels` was fully parsed/validated but read by nothing — every
dispatch ran `models.default` regardless of the issue's tier/category label,
and the plan stage's ESTIMATE was captured and discarded. These are the two
resolver methods every dispatch site now consults.
"""

from __future__ import annotations

from coord.config import ModelsConfig


class TestModelForLabels:
    def test_no_labels_configured_returns_none(self) -> None:
        cfg = ModelsConfig()  # labels defaults to {}
        assert cfg.model_for_labels(["bug", "tier:large"]) is None

    def test_matching_label_returns_alias(self) -> None:
        cfg = ModelsConfig(labels={"tier:small": "haiku", "tier:large": "opus"})
        assert cfg.model_for_labels(["tier:small"]) == "haiku"

    def test_no_matching_label_returns_none(self) -> None:
        cfg = ModelsConfig(labels={"tier:small": "haiku"})
        assert cfg.model_for_labels(["bug", "enhancement"]) is None

    def test_empty_issue_labels_returns_none(self) -> None:
        cfg = ModelsConfig(labels={"tier:small": "haiku"})
        assert cfg.model_for_labels([]) is None

    def test_multi_label_precedence_is_issue_label_order(self) -> None:
        """#1430 acceptance: multi-label precedence must be deterministic.
        The issue's own label order decides — the first label (in the
        order GitHub returns them for the issue) that has a `labels` entry
        wins, mirroring `coord.brain.resolve_required_gates`'s convention
        for `pipeline.labels`."""
        cfg = ModelsConfig(labels={"bug": "sonnet", "tier:large": "opus"})
        # "bug" listed first on the issue -> wins, even though tier:large
        # is "more specific" — the issue's label order is the sole knob.
        assert cfg.model_for_labels(["bug", "tier:large"]) == "sonnet"
        # Reverse the issue's label order -> the winner flips too.
        assert cfg.model_for_labels(["tier:large", "bug"]) == "opus"

    def test_default_never_returned_by_this_method(self) -> None:
        """model_for_labels never falls back to `default` itself — that's
        the caller's job (mirrors `resolve()`'s None-passthrough style)."""
        cfg = ModelsConfig(default="sonnet", labels={})
        assert cfg.model_for_labels(["documentation"]) is None


class TestModelForEstimate:
    def test_trivial_and_small_map_to_bottom_rung(self) -> None:
        cfg = ModelsConfig(escalation=["haiku", "sonnet", "opus"])
        assert cfg.model_for_estimate("trivial") == "haiku"
        assert cfg.model_for_estimate("small") == "haiku"

    def test_medium_maps_to_middle_rung(self) -> None:
        cfg = ModelsConfig(escalation=["haiku", "sonnet", "opus"])
        assert cfg.model_for_estimate("medium") == "sonnet"

    def test_large_maps_to_top_rung(self) -> None:
        cfg = ModelsConfig(escalation=["haiku", "sonnet", "opus"])
        assert cfg.model_for_estimate("large") == "opus"

    def test_case_and_whitespace_insensitive(self) -> None:
        cfg = ModelsConfig(escalation=["haiku", "sonnet", "opus"])
        assert cfg.model_for_estimate("  LARGE  ") == "opus"

    def test_unrecognised_estimate_returns_none(self) -> None:
        cfg = ModelsConfig(escalation=["haiku", "sonnet", "opus"])
        assert cfg.model_for_estimate("gargantuan") is None

    def test_empty_estimate_returns_none(self) -> None:
        cfg = ModelsConfig(escalation=["haiku", "sonnet", "opus"])
        assert cfg.model_for_estimate("") is None
        assert cfg.model_for_estimate(None) is None

    def test_empty_escalation_returns_none(self) -> None:
        cfg = ModelsConfig(escalation=[])
        assert cfg.model_for_estimate("large") is None

    def test_clamps_to_short_ladder(self) -> None:
        """A custom two-rung ladder shouldn't index out of range for 'large'."""
        cfg = ModelsConfig(escalation=["sonnet", "opus"])
        assert cfg.model_for_estimate("large") == "opus"
        assert cfg.model_for_estimate("medium") == "opus"
        assert cfg.model_for_estimate("trivial") == "sonnet"

    def test_single_rung_ladder(self) -> None:
        cfg = ModelsConfig(escalation=["sonnet"])
        assert cfg.model_for_estimate("trivial") == "sonnet"
        assert cfg.model_for_estimate("large") == "sonnet"
