"""Shared Gate-A fixtures for ms-37 (Spend & Time Observability, epic #1117).

The seeded board rows + pricing table below ARE the assertion fixtures from
``contract.md`` (the "Fixture — seeded board" and "Fixture — pricing table"
sections). Every expected number the acceptance suite pins is derived from
these, so the suite is self-contained and never drifts with real Anthropic
prices or the shipped ``pricing:`` config defaults.

These fixtures are shared across the milestone's per-issue slices
(#1118 Core, #1115 CLI-1, #1119 CLI-2). Author new slices against them —
do not clone divergent copies.
"""

from __future__ import annotations

import pytest

# An arbitrary fixed unix instant. All six legs are dispatched within
# ``[BASE, BASE + 86400)`` so an explicit window covering that interval is the
# deterministic stand-in for the contract's ``window = today`` preset (a preset
# would depend on the wall clock; an explicit half-open interval does not).
BASE = 1_700_000_000.0
DAY = 86_400.0

# Contract "Fixture — seeded board" — 6 legs, 2 issues, 2 repos, mixed
# interactive/non-interactive, one unknown-model leg (L5), one running leg (L6).
# Token columns not given in the contract table (cache_creation) are 0.
BOARD_ROWS = [
    {  # L1
        "issue_number": 501, "issue_title": "Alpha feature", "repo_name": "alpha",
        "type": "work", "model": "sonnet", "is_interactive": False, "status": "merged",
        "cost_usd": 0.50,
        "input_tokens": 10_000, "output_tokens": 100_000,
        "cache_read_tokens": 1_000_000, "cache_creation_tokens": 0,
        "dispatched_at": BASE + 0.0, "finished_at": BASE + 600.0,        # 600s
    },
    {  # L2
        "issue_number": 501, "issue_title": "Alpha feature", "repo_name": "alpha",
        "type": "review", "model": "sonnet", "is_interactive": True, "status": "done",
        "cost_usd": None,
        "input_tokens": 2_000, "output_tokens": 50_000,
        "cache_read_tokens": 500_000, "cache_creation_tokens": 0,
        "dispatched_at": BASE + 1_000.0, "finished_at": BASE + 1_300.0,  # 300s
    },
    {  # L3
        "issue_number": 502, "issue_title": "Beta feature", "repo_name": "beta",
        "type": "work", "model": "opus", "is_interactive": False, "status": "merged",
        "cost_usd": 2.00,
        "input_tokens": 20_000, "output_tokens": 200_000,
        "cache_read_tokens": 2_000_000, "cache_creation_tokens": 0,
        "dispatched_at": BASE + 2_000.0, "finished_at": BASE + 3_200.0,  # 1200s
    },
    {  # L4
        "issue_number": 502, "issue_title": "Beta feature", "repo_name": "beta",
        "type": "smoke", "model": "sonnet", "is_interactive": True, "status": "done",
        "cost_usd": None,
        "input_tokens": 4_000, "output_tokens": 80_000,
        "cache_read_tokens": 800_000, "cache_creation_tokens": 0,
        "dispatched_at": BASE + 4_000.0, "finished_at": BASE + 4_400.0,  # 400s
    },
    {  # L5 — unknown model
        "issue_number": 502, "issue_title": "Beta feature", "repo_name": "beta",
        "type": "chat", "model": "(unknown)", "is_interactive": True, "status": "done",
        "cost_usd": None,
        "input_tokens": 1_000, "output_tokens": 30_000,
        "cache_read_tokens": 300_000, "cache_creation_tokens": 0,
        "dispatched_at": BASE + 5_000.0, "finished_at": BASE + 5_200.0,  # 200s
    },
    {  # L6 — running (no finished_at)
        "issue_number": 502, "issue_title": "Beta feature", "repo_name": "beta",
        "type": "work", "model": "sonnet", "is_interactive": False, "status": "running",
        "cost_usd": None,
        "input_tokens": 0, "output_tokens": 0,
        "cache_read_tokens": 0, "cache_creation_tokens": 0,
        "dispatched_at": BASE + 6_000.0, "finished_at": None,
    },
]

# Contract "Fixture — pricing table" (per 1M tokens; FIXTURE values, NOT the
# shipped defaults — the shipped defaults are verified separately at review).
PRICING = {
    "sonnet": {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_creation": 3.75},
    "opus": {"input": 15.00, "output": 75.00, "cache_read": 1.50, "cache_creation": 18.75},
}


@pytest.fixture
def board_rows():
    """The 6 seeded legs (fresh copy per test so mutation can't leak)."""
    return [dict(r) for r in BOARD_ROWS]


@pytest.fixture
def pricing():
    """The fixture pricing table (per-1M-token rates by canonical model)."""
    return {m: dict(rates) for m, rates in PRICING.items()}


@pytest.fixture
def today_window():
    """Explicit half-open ``[start, end)`` spanning the fixture day — the
    deterministic resolution of the contract's ``window = today`` preset."""
    from coord.usage_rollup import Window

    return Window(BASE, BASE + DAY)
