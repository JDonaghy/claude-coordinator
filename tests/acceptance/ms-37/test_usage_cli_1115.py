"""ms-37 / issue #1115 — Usage CLI-1: ``coord usage`` per-issue rendering.

This slice pins the **rendered black-box surface** of ``coord usage`` — the
strings a human reads on stdout — as opposed to #1118 Core, which pins the pure
aggregator's *numbers* (``coord.usage_rollup``). CLI-1 **consumes** Core and
renders it; these tests drive the real Click command end-to-end via
``CliRunner`` and assert on its output.

The two mocks in ``contract.md`` this slice reproduces:

* **Mock 1** — ``coord usage --today --by-issue`` (grouped-by-issue rollup,
  desc sort, grand-total footer).
* **Mock 2** — ``coord usage --issue 502`` (per-stage drill for one issue).

Test seams (all black-box / contract-derivable — no coupling to Core internals
or the wall clock):

* **Board rows** — the CLI fetches assignment rows via
  ``coord.usage.fetch_usage_rows`` (the daemon-board fetch Core #1118 provides,
  the one that preserves ``is_interactive``). We monkeypatch it to return the
  seeded fixture rows. The autouse ``_no_board_service`` fixture already keeps
  the CLI on the local path, so nothing hits a live daemon.
* **Window** — ``--today`` resolves to the local calendar day off the real
  clock. Rather than freeze the clock (timezone-fragile) or patch Core's window
  resolver (couples to its internals), we **shift the fixture rows into the
  current local day**. Only *durations* (``finished − dispatched``) are rendered
  by the mocks, and a uniform shift preserves every duration exactly — so every
  asserted number is unaffected while ``--today`` deterministically includes all
  six legs.
* **Pricing** — injected via a temp ``coordinator.yml`` ``pricing:`` block
  (fixture rates), passed with ``--config``, so the estimate numbers are pinned
  to the contract fixture and never drift with the shipped defaults.

Every expected string below comes straight from ``contract.md`` Mocks 1 & 2 and
§"Semantics the suite pins". We assert on the contract's *meaningful* surface
(rendered numbers, group ordering, the ``~$`` estimate marker, the
unknown-model flag, the footer totals, the per-stage rows/flags) rather than
byte-exact whitespace: the mock is a hand-drawn column sketch, so its exact
spacing is not a contract-meaningful invariant, but its numbers, ordering, and
markers are.
"""

from __future__ import annotations

import datetime as _dt

import pytest
from click.testing import CliRunner

from coord.cli import main

# Fixture board + pricing live in the shared ms-37 conftest. BASE is the epoch
# of the earliest leg (L1 dispatched at BASE + 0.0).
from conftest import BASE  # noqa: E402  (ms-37 conftest, on sys.path under pytest)

pytestmark = pytest.mark.filterwarnings("ignore")

# Estimator ground truth (from contract §"Semantics the suite pins"):
#   L2 (sonnet): 2k*3 + 50k*15 + 500k*0.30 (per-1M) = $0.9060  → #501 est
#   L4 (sonnet): 4k*3 + 80k*15 + 800k*0.30           = $1.4520  → #502 est
EST_501 = "~$0.9060"
EST_502 = "~$1.4520"


# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------

def _shift_into_today(rows: list[dict]) -> list[dict]:
    """Return copies of *rows* with every timestamp shifted so all legs fall
    inside the current local calendar day.

    Anchors the earliest dispatch (L1, at ``BASE``) to **local noon today** so
    the ~100-minute span of the fixture sits comfortably mid-day, away from any
    midnight boundary. Durations are preserved exactly (uniform shift), so every
    rendered number is unchanged; only window membership moves into "today".
    """
    noon_today = _dt.datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
    offset = noon_today.timestamp() - BASE
    shifted: list[dict] = []
    for r in rows:
        r = dict(r)
        r["dispatched_at"] = float(r["dispatched_at"]) + offset
        if r.get("finished_at") is not None:
            r["finished_at"] = float(r["finished_at"]) + offset
        shifted.append(r)
    return shifted


def _pricing_yaml(pricing: dict) -> str:
    """Render the fixture pricing table as a coordinator.yml ``pricing:`` block."""
    lines = ["pricing:"]
    for model, rates in pricing.items():
        lines.append(f"  {model}:")
        for field, value in rates.items():
            lines.append(f"    {field}: {value}")
    return "\n".join(lines) + "\n"


def _config_with_pricing(tmp_path, valid_config_yaml: str, pricing: dict):
    """Write a valid coordinator.yml that also carries the fixture pricing."""
    p = tmp_path / "coordinator.yml"
    p.write_text(valid_config_yaml + "\n" + _pricing_yaml(pricing))
    return p


def _text(result) -> str:
    """CLI text robust across click versions (stderr may be separated)."""
    out = result.output or ""
    try:
        out += result.stderr or ""
    except ValueError:
        pass
    return out


def _run_usage(monkeypatch, tmp_path, valid_config_yaml, board_rows, pricing, *args):
    """Invoke ``coord usage <args>`` against the seeded fixture board.

    Stubs the daemon-board fetch to return the fixture rows shifted into
    today, and injects the fixture pricing via ``--config``.
    """
    shifted = _shift_into_today(board_rows)
    fake_fetch = lambda *a, **k: [dict(r) for r in shifted]  # noqa: E731
    # Primary seam: Core's fetch. Belt-and-suspenders: the name possibly bound
    # into the command module at import time. raising=False so whichever the
    # implementation actually uses, the fixture rows flow through.
    monkeypatch.setattr("coord.usage.fetch_usage_rows", fake_fetch, raising=False)
    monkeypatch.setattr(
        "coord.commands.status.fetch_usage_rows", fake_fetch, raising=False
    )
    cfg = _config_with_pricing(tmp_path, valid_config_yaml, pricing)
    result = CliRunner().invoke(main, ["usage", "--config", str(cfg), *args])
    return result


def _line_with(text: str, *needles: str) -> str:
    """The first line of *text* containing every needle (fails loudly)."""
    for line in text.splitlines():
        if all(n in line for n in needles):
            return line
    raise AssertionError(
        f"no line containing all of {needles!r} in:\n{text}"
    )


# ===========================================================================
# Mock 1 — coord usage --today --by-issue
# ===========================================================================

def test_by_issue_cli_window_header(monkeypatch, tmp_path, valid_config_yaml, board_rows, pricing):
    result = _run_usage(monkeypatch, tmp_path, valid_config_yaml, board_rows, pricing,
                        "--today", "--by-issue")
    assert result.exit_code == 0, _text(result)
    out = _text(result).lower()
    # Contract Mock 1 header: "USAGE — by issue — window: today"
    assert "by issue" in out
    assert "window: today" in out


def test_by_issue_cli_groups_desc_by_total(monkeypatch, tmp_path, valid_config_yaml, board_rows, pricing):
    result = _run_usage(monkeypatch, tmp_path, valid_config_yaml, board_rows, pricing,
                        "--today", "--by-issue")
    assert result.exit_code == 0, _text(result)
    out = _text(result)
    # Both issue rows are present...
    assert "#502" in out
    assert "#501" in out
    # ...and #502 (total $3.4520) sorts before #501 (total $1.4060) — desc.
    assert out.index("#502") < out.index("#501")


def test_by_issue_cli_row_502_values(monkeypatch, tmp_path, valid_config_yaml, board_rows, pricing):
    result = _run_usage(monkeypatch, tmp_path, valid_config_yaml, board_rows, pricing,
                        "--today", "--by-issue")
    assert result.exit_code == 0, _text(result)
    line = _line_with(_text(result), "#502")
    # repo, captured cost, estimated cost, token cols, duration (Mock 1).
    assert "beta" in line
    assert "$2.0000" in line          # captured (L3)
    assert EST_502 in line            # est ~$1.4520 (L4)
    assert "310k" in line             # Σ output tokens
    assert "3.1M" in line             # Σ cache_read tokens
    assert "30m00s" in line           # Σ duration


def test_by_issue_cli_row_501_values(monkeypatch, tmp_path, valid_config_yaml, board_rows, pricing):
    result = _run_usage(monkeypatch, tmp_path, valid_config_yaml, board_rows, pricing,
                        "--today", "--by-issue")
    assert result.exit_code == 0, _text(result)
    line = _line_with(_text(result), "#501")
    assert "alpha" in line
    assert "$0.5000" in line          # captured (L1)
    assert EST_501 in line            # est ~$0.9060 (L2)
    assert "150k" in line             # Σ output tokens
    assert "1.5M" in line             # Σ cache_read tokens
    assert "15m00s" in line           # Σ duration


def test_by_issue_cli_estimate_distinct_from_captured(monkeypatch, tmp_path, valid_config_yaml, board_rows, pricing):
    # Requirement: the estimated ``~$`` (here derived from the interactive legs
    # L2/L4) renders NON-ZERO and VISUALLY DISTINCT from captured ``$``.
    result = _run_usage(monkeypatch, tmp_path, valid_config_yaml, board_rows, pricing,
                        "--today", "--by-issue")
    assert result.exit_code == 0, _text(result)
    line = _line_with(_text(result), "#501")
    # Captured and estimated are different values, both shown, est marked "~".
    assert "$0.5000" in line and EST_501 in line
    assert "$0.9060" != "$0.5000"     # sanity: they are not the same figure
    # The estimate carries the "~" marker; the captured figure does not.
    assert "~$0.9060" in line
    # TODO(test-author): the contract's fixture has no *interactive-only* issue
    # (every issue mixes a captured + an interactive leg), so we prove the
    # estimate path via #501's est being entirely from its interactive leg (L2)
    # and distinct from its captured figure, rather than an issue whose whole
    # cost is estimated. Requirement §Acceptance's "interactive-only issue"
    # isn't representable in the seeded board.


def test_by_issue_cli_unknown_model_flag_rendered(monkeypatch, tmp_path, valid_config_yaml, board_rows, pricing):
    # #502 contains L5 (chat, unknown model) → the group is flagged, never
    # silently priced $0. Mock 1 renders "⚠ unknown-model:1" on the #502 row.
    result = _run_usage(monkeypatch, tmp_path, valid_config_yaml, board_rows, pricing,
                        "--today", "--by-issue")
    assert result.exit_code == 0, _text(result)
    line = _line_with(_text(result), "#502")
    assert "unknown-model:1" in line
    # #501 has no unknown-model leg → no such flag on its row.
    line_501 = _line_with(_text(result), "#501")
    assert "unknown-model" not in line_501


def test_by_issue_cli_grand_total_footer(monkeypatch, tmp_path, valid_config_yaml, board_rows, pricing):
    # Contract Mock 1 footer:
    #   Σ captured $2.5000 · est ~$2.3580 · total $4.8580 · 460k out / 4.6M
    #   cache · 45m00s · 1 in progress
    result = _run_usage(monkeypatch, tmp_path, valid_config_yaml, board_rows, pricing,
                        "--today", "--by-issue")
    assert result.exit_code == 0, _text(result)
    out = _text(result)
    assert "$2.5000" in out           # Σ captured (0.50 + 2.00)
    assert "~$2.3580" in out          # Σ est (0.906 + 1.452)
    assert "$4.8580" in out           # Σ total (captured + est)
    assert "460k" in out              # Σ output tokens (150k + 310k)
    assert "4.6M" in out              # Σ cache_read tokens (1.5M + 3.1M)
    assert "45m00s" in out            # Σ duration (2700s)
    assert "1 in progress" in out     # L6 running


# ===========================================================================
# Mock 2 — coord usage --issue 502 (per-stage drill)
# ===========================================================================

def test_issue_drill_cli_header_captured_and_est(monkeypatch, tmp_path, valid_config_yaml, board_rows, pricing):
    # Mock 2 header: "#502  beta   $2.0000 captured  +  ~$1.4520 est"
    result = _run_usage(monkeypatch, tmp_path, valid_config_yaml, board_rows, pricing, "--issue", "502")
    assert result.exit_code == 0, _text(result)
    out = _text(result)
    assert "#502" in out
    assert "beta" in out
    assert "$2.0000" in out
    assert "captured" in out
    assert EST_502 in out
    assert "est" in out


def test_issue_drill_cli_stage_rows_in_order(monkeypatch, tmp_path, valid_config_yaml, board_rows, pricing):
    # Mock 2 per-stage order (oldest-first): work(opus) / smoke(sonnet) /
    # chat(unknown) / work(sonnet, running).
    result = _run_usage(monkeypatch, tmp_path, valid_config_yaml, board_rows, pricing, "--issue", "502")
    assert result.exit_code == 0, _text(result)
    out = _text(result)
    i_work_opus = out.index("opus")
    i_smoke = out.index("smoke")
    i_chat = out.index("chat")
    assert i_work_opus < i_smoke < i_chat


def test_issue_drill_cli_work_opus_captured_no_est(monkeypatch, tmp_path, valid_config_yaml, board_rows, pricing):
    # work/opus row: captured $2.0000, NO estimate (Mock 2 shows "—" in est col).
    result = _run_usage(monkeypatch, tmp_path, valid_config_yaml, board_rows, pricing, "--issue", "502")
    assert result.exit_code == 0, _text(result)
    line = _line_with(_text(result), "opus")
    assert "$2.0000" in line
    assert "200k" in line             # output tokens
    assert "20m00s" in line           # duration
    assert "~$" not in line           # captured leg is never also estimated


def test_issue_drill_cli_smoke_sonnet_estimated(monkeypatch, tmp_path, valid_config_yaml, board_rows, pricing):
    # smoke/sonnet row: no captured cost, est ~$1.4520 (Mock 2).
    result = _run_usage(monkeypatch, tmp_path, valid_config_yaml, board_rows, pricing, "--issue", "502")
    assert result.exit_code == 0, _text(result)
    line = _line_with(_text(result), "smoke")
    assert EST_502 in line            # ~$1.4520
    assert "80k" in line              # output tokens
    assert "6m40s" in line            # duration
    assert "I" in line                # interactive flag


def test_issue_drill_cli_unknown_model_marker(monkeypatch, tmp_path, valid_config_yaml, board_rows, pricing):
    # chat/(unknown) row: unmapped model → "n/a" estimate marker, never $0
    # (Mock 2 renders "n/a*" with a "*unknown model" footnote).
    result = _run_usage(monkeypatch, tmp_path, valid_config_yaml, board_rows, pricing, "--issue", "502")
    assert result.exit_code == 0, _text(result)
    out = _text(result)
    line = _line_with(out, "chat")
    assert "(unknown)" in line
    assert "n/a" in line              # no estimate possible for an unknown model
    assert "30k" in line              # output tokens
    assert "3m20s" in line            # duration
    # A visible note explains the unknown model somewhere in the output.
    assert "unknown model" in out.lower()


def test_issue_drill_cli_running_leg(monkeypatch, tmp_path, valid_config_yaml, board_rows, pricing):
    # work/sonnet running leg (L6): 0 tokens, no captured/est, status "running",
    # no duration (Mock 2 shows dashes + "running").
    result = _run_usage(monkeypatch, tmp_path, valid_config_yaml, board_rows, pricing, "--issue", "502")
    assert result.exit_code == 0, _text(result)
    out = _text(result)
    assert "running" in out
    running_line = _line_with(out, "running")
    # A running/0-token leg is neither captured nor estimated (Mock 2: dashes).
    assert "~$" not in running_line   # no estimate for a 0-token leg
    assert "$" not in running_line    # no captured cost either
