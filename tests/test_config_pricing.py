"""Tests for the `pricing:` block in coordinator.yml (#1118's cost estimator
config — per-canonical-model per-1M-token rates consumed by
`coord.usage_rollup`)."""

from __future__ import annotations

from pathlib import Path

import pytest

from coord.config import ConfigError, ModelRates, PricingConfig, load


BASE = """\
repos:
  - name: coord-tui
    github: acme/coord-tui
machines:
  - name: laptop
    host: laptop.tail
    repos: [coord-tui]
"""


def test_pricing_absent_defaults_to_builtin_rates(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(BASE)
    cfg = load(p)
    assert cfg.pricing == PricingConfig()
    for model in ("sonnet", "opus", "haiku", "fable"):
        rates = cfg.pricing.rates_for(model)
        assert rates is not None
        assert rates.input > 0
        assert rates.output > 0


def test_pricing_builtin_defaults_match_official_anthropic_rates(
    tmp_path: Path,
) -> None:
    """Pin the exact shipped per-1M-token defaults (not just ``> 0``).

    #1290: the shipped Opus default carried ``15.00/75.00/1.50/18.75`` —
    Opus 3 / 4.0 / 4.1 era list pricing, mistakenly pinned as "verified" at
    #1118 review. Current Opus (4.6 through 4.8) list price is
    ``5.00/25.00/0.50/6.25``, corrected here. Also pins the new Fable row
    added in the same change. Pin every field for every canonical tier so a
    future edit can't silently drift again the same way.
    """
    p = tmp_path / "coordinator.yml"
    p.write_text(BASE)
    cfg = load(p)
    expected = {
        "sonnet": ModelRates(input=3.00, output=15.00, cache_read=0.30, cache_creation=3.75),
        "opus": ModelRates(input=5.00, output=25.00, cache_read=0.50, cache_creation=6.25),
        "haiku": ModelRates(input=1.00, output=5.00, cache_read=0.10, cache_creation=1.25),
        "fable": ModelRates(input=10.00, output=50.00, cache_read=1.00, cache_creation=12.50),
    }
    for model, rates in expected.items():
        assert cfg.pricing.rates_for(model) == rates


def test_pricing_unmapped_model_has_no_rates(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(BASE)
    cfg = load(p)
    assert cfg.pricing.rates_for("(unknown)") is None
    assert cfg.pricing.rates_for("some-future-model") is None


def test_pricing_full_override_replaces_all_four_rates(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        BASE
        + "pricing:\n"
        "  sonnet:\n"
        "    input: 1.0\n"
        "    output: 2.0\n"
        "    cache_read: 0.1\n"
        "    cache_creation: 1.25\n"
    )
    cfg = load(p)
    assert cfg.pricing.rates_for("sonnet") == ModelRates(
        input=1.0, output=2.0, cache_read=0.1, cache_creation=1.25
    )


def test_pricing_partial_override_keeps_other_default_rates(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(BASE + "pricing:\n  opus:\n    output: 99.0\n")
    cfg = load(p)
    defaults = PricingConfig()
    rates = cfg.pricing.rates_for("opus")
    assert rates is not None
    assert rates.output == 99.0
    default_opus = defaults.rates_for("opus")
    assert default_opus is not None
    assert rates.input == default_opus.input
    assert rates.cache_read == default_opus.cache_read
    assert rates.cache_creation == default_opus.cache_creation


def test_pricing_adds_new_model_key(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        BASE
        + "pricing:\n"
        "  claude-mythos-5:\n"
        "    input: 10.0\n"
        "    output: 50.0\n"
    )
    cfg = load(p)
    rates = cfg.pricing.rates_for("claude-mythos-5")
    assert rates is not None
    assert rates.input == 10.0
    assert rates.output == 50.0
    assert rates.cache_read == 0.0
    assert rates.cache_creation == 0.0
    # Built-in tiers are untouched by adding an unrelated key.
    assert cfg.pricing.rates_for("sonnet") == PricingConfig().rates_for("sonnet")


def test_pricing_block_must_be_mapping(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(BASE + "pricing: [1, 2]\n")
    with pytest.raises(ConfigError, match="'pricing' must be a mapping"):
        load(p)


def test_pricing_entry_must_be_mapping(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(BASE + "pricing:\n  sonnet: 3.0\n")
    with pytest.raises(ConfigError, match=r"pricing\['sonnet'\] must be a mapping"):
        load(p)


def test_pricing_rate_must_be_non_negative_number(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(BASE + "pricing:\n  sonnet:\n    input: -1.0\n")
    with pytest.raises(ConfigError, match=r"pricing\['sonnet'\].input"):
        load(p)


def test_pricing_rate_rejects_non_numeric(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(BASE + "pricing:\n  sonnet:\n    input: \"cheap\"\n")
    with pytest.raises(ConfigError, match=r"pricing\['sonnet'\].input"):
        load(p)
