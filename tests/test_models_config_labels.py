"""Tests for coord.config.ModelsConfig.model_for_labels / model_for_estimate (#1430).

`models.labels` was fully parsed/validated but read by nothing — every
dispatch ran `models.default` regardless of the issue's tier/category label,
and the plan stage's ESTIMATE was captured and discarded. These are the two
resolver methods every dispatch site now consults.
"""

from __future__ import annotations

from coord.config import ModelsConfig, describe_model_choice


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

    def test_tier_label_beats_type_label_regardless_of_issue_order(self) -> None:
        """#1633: precedence must NOT depend on GitHub's issue-label order.

        `tier:*` entries are documented size-tier *overrides* over the
        type-label entries (`bug`/`enhancement`/...) — and must win
        regardless of which order the issue's labels happen to be in.
        """
        cfg = ModelsConfig(labels={"bug": "sonnet", "tier:large": "opus"})
        assert cfg.model_for_labels(["bug", "tier:large"]) == "opus"
        assert cfg.model_for_labels(["tier:large", "bug"]) == "opus"

    def test_tier_small_beats_bug_regardless_of_issue_order(self) -> None:
        cfg = ModelsConfig(labels={"bug": "sonnet", "tier:small": "haiku"})
        assert cfg.model_for_labels(["bug", "tier:small"]) == "haiku"
        assert cfg.model_for_labels(["tier:small", "bug"]) == "haiku"

    def test_type_label_only_is_unchanged(self) -> None:
        """An issue with only a type label (no tier label) still resolves
        via that label, same as before #1633."""
        cfg = ModelsConfig(labels={"enhancement": "sonnet", "tier:large": "opus"})
        assert cfg.model_for_labels(["enhancement"]) == "sonnet"

    def test_no_matching_label_falls_through_to_default_via_caller(self) -> None:
        """`model_for_labels` never resolves `default` itself (that's the
        caller's job — see `test_default_never_returned_by_this_method`),
        but an issue with no configured label present still yields `None`
        so the `... or config.models.default` idiom falls through."""
        cfg = ModelsConfig(default="opus", labels={"tier:large": "opus"})
        assert cfg.model_for_labels(["documentation"]) is None
        resolved = cfg.model_for_labels(["documentation"]) or cfg.default
        assert resolved == "opus"

    def test_with_reason_names_matched_and_shadowed_labels(self) -> None:
        """#1633: when more than one configured label matched, the losing
        candidate(s) are surfaced too, so a route that looks surprising is
        self-explaining at dispatch time."""
        cfg = ModelsConfig(labels={"bug": "sonnet", "tier:large": "opus"})
        model, matched, shadowed = cfg.model_for_labels_with_reason(["bug", "tier:large"])
        assert (model, matched, shadowed) == ("opus", "tier:large", ["bug"])
        # Unambiguous match -> nothing shadowed.
        model, matched, shadowed = cfg.model_for_labels_with_reason(["bug"])
        assert (model, matched, shadowed) == ("sonnet", "bug", [])

    def test_with_reason_tie_break_uses_config_order_not_issue_order(self) -> None:
        """Two `tier:*` labels on the same issue (or two type labels) is an
        edge case, but must still resolve deterministically — via the
        config's own declaration order, not the issue's label order."""
        cfg = ModelsConfig(
            labels={"tier:small": "haiku", "tier:large": "opus"}
        )
        # tier:small declared first in the config -> wins, regardless of
        # which order the issue lists them in.
        assert cfg.model_for_labels(["tier:large", "tier:small"]) == "haiku"
        assert cfg.model_for_labels(["tier:small", "tier:large"]) == "haiku"

    def test_default_never_returned_by_this_method(self) -> None:
        """model_for_labels never falls back to `default` itself — that's
        the caller's job (mirrors `resolve()`'s None-passthrough style)."""
        cfg = ModelsConfig(default="sonnet", labels={})
        assert cfg.model_for_labels(["documentation"]) is None


class TestDescribeModelChoiceShadowing:
    """#1633 acceptance: "The dry-run output names the matched label, and
    names the shadowed one when a match was ambiguous." These exercise the
    exact string dispatch sites print (`coord assign --dry-run` /
    `coord approve` / `coord milestone dispatch` all funnel through
    `describe_model_choice`)."""

    def test_unambiguous_match_names_only_the_matched_label(self) -> None:
        reason = describe_model_choice(
            resolved_model="opus", matched_label="tier:large", shadowed_labels=[],
        )
        assert reason == "opus (via label 'tier:large')"
        assert "shadow" not in reason

    def test_ambiguous_match_names_matched_and_shadowed_label(self) -> None:
        """Two configured labels on one issue: the winner is named, and the
        loser is named too so the route is self-explaining."""
        reason = describe_model_choice(
            resolved_model="opus",
            matched_label="tier:large",
            shadowed_labels=["enhancement"],
        )
        assert reason == "opus (via label 'tier:large', shadowing 'enhancement')"

    def test_multiple_shadowed_labels_all_named(self) -> None:
        reason = describe_model_choice(
            resolved_model="opus",
            matched_label="tier:large",
            shadowed_labels=["bug", "enhancement"],
        )
        assert reason == (
            "opus (via label 'tier:large', shadowing 'bug', 'enhancement')"
        )

    def test_explicit_reason_wins_even_with_shadowed_labels(self) -> None:
        """An explicit --model always wins the phrasing outright, regardless
        of what label matching found underneath it."""
        reason = describe_model_choice(
            resolved_model="haiku",
            explicit_reason="explicit --model",
            matched_label="tier:large",
            shadowed_labels=["enhancement"],
        )
        assert reason == "haiku (explicit --model)"

    def test_no_match_ignores_shadowed_labels(self) -> None:
        reason = describe_model_choice(resolved_model="sonnet", shadowed_labels=[])
        assert reason == "sonnet (default; no label match)"


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
