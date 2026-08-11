"""The merge-triggered release *policy*, pinned (#1835, PKG-7).

`.github/workflows/auto-release.yml` pushes a tag with no human in the loop.
The decision behind that push is the one part of the pipeline that can be
wrong in a way nothing downstream catches: `publish.yml` faithfully publishes
whatever tag it is handed, and PyPI uploads are immutable. So the policy
lives in `scripts/next_release_tag.py` as a pure function, and this module is
what stops it drifting.

What is actually load-bearing here (and why each test exists):

* **Unrecognised paths must count as shipping.** The costly failure in this
  fleet is "a merged fix is not a live fix", so the filter must fail *open*
  to a superfluous release, never closed to a change that silently never
  reaches a host.
* **`latest_tag` must order by version, not by string.** `v0.4.9` >
  `v0.4.109` lexically; getting that wrong mints a tag that already exists
  or, worse, walks the version line backwards.
* **A tag must never be re-derived by two paths.** #1238 removed every
  version literal precisely so the tag is the single source; the bump is
  therefore purely a function of the tag history.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from next_release_tag import (  # noqa: E402
    BOOTSTRAP_TAG,
    bump,
    decide,
    latest_tag,
    parse_tag,
    previous_tag,
    ships_code,
    ships_wheel,
)


# ── parse_tag / latest_tag ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("v0.4.110", (0, 4, 110)),
        ("v1.0.0", (1, 0, 0)),
        ("  v2.3.4  ", (2, 3, 4)),
        ("0.4.110", None),  # no leading v
        ("v0.4", None),  # not three components
        ("v0.4.110rc1", None),  # pre-release: never a bump base
        ("v0.0.0-dryrun", None),  # publish.yml's dry-run spelling
    ],
)
def test_parse_tag_is_strict(raw, expected):
    assert parse_tag(raw) == expected


def test_latest_tag_orders_numerically_not_lexically():
    """`v0.4.9` sorts above `v0.4.110` as a string. That would mint v0.4.10,
    a version already long since published — and PyPI would reject it while
    the tag stayed on main, leaving a release that half-happened."""
    assert latest_tag(["v0.4.9", "v0.4.110", "v0.4.87"]) == (0, 4, 110)


def test_latest_tag_ignores_unparseable_tags():
    assert latest_tag(["v0.0.0-dryrun", "nightly", "v1.2.3"]) == (1, 2, 3)


def test_latest_tag_of_nothing_is_none():
    assert latest_tag(["nightly", "v0.4"]) is None


# ── ships_code ───────────────────────────────────────────────────────────


def test_docs_and_tests_only_ships_nothing():
    assert not ships_code(["docs/AGENT_OPERATIONS.md", "tests/test_foo.py"])


def test_a_single_coord_file_ships():
    assert ships_code(["docs/README.md", "coord/cli.py"])


def test_the_deploy_lane_ships():
    """#1831/#1543: a release whose whole mechanism is unit files must still
    cut a release. This is the case a naive `coord/**`-only filter drops."""
    assert ships_code(["deploy/coord-agent.service"])
    assert ships_code(["install-agent.sh"])


def test_unrecognised_paths_default_to_shipping():
    """A new top-level directory nobody taught this filter about must fail
    OPEN. A superfluous release costs a version number; a missed one costs
    the thing this whole slice exists to prevent."""
    assert ships_code(["some_new_toplevel_dir/thing.py"])


def test_an_undetectable_diff_ships():
    """An empty changed-file list means the workflow could not resolve the
    diff, not that the merge was empty."""
    assert ships_code([])


def test_githooks_only_ships_nothing():
    """CLAUDE.md: '.githooks/** is a fifth deploy surface whose failure mode
    is the opposite of the other four — a merged hook is live on every
    machine at the next fetch, no release, no restart.' Cutting a release
    (and a fleet-wide propagation restart) for it would be pure waste."""
    assert not ships_code([".githooks/post-checkout"])


def test_workflow_files_only_ship_nothing():
    """#2081: v0.5.7's entire release range was one file,
    `.github/workflows/test.yml`. A workflow reaches no wheel, no binary and
    no host's unit directory — the version that RUNS is whatever is at the
    tip of main, so a tag changes nothing about it. Same case as
    `.githooks/`, same fix: it must not drive a release."""
    assert not ships_code([".github/workflows/test.yml"])
    assert not ships_code([".github/workflows/publish.yml", ".github/workflows/release-tui.yml"])


def test_workflow_and_docs_together_still_ship_nothing():
    assert not ships_code([".github/workflows/test.yml", "docs/AGENT_OPERATIONS.md"])


def test_tui_only_still_ships():
    """#2081 asked, explicitly, whether a `tui/`-only merge should keep
    driving a PyPI/expected-version bump given coord-tui has no remote
    install path (`lane_is_out_of_reach` — the fleet's release-verify lanes
    go red for a change that can never reach them). Judged not worth
    splitting from how a release is built (out of this issue's scope, see
    `scripts/next_release_tag.py`'s module docstring): `tui/` stays a
    shipping prefix, on purpose, and this test is that decision pinned."""
    assert ships_code(["tui/src/panels/queue.rs"])


# ── ships_wheel (#2102) ──────────────────────────────────────────────────


def test_tui_only_ships_no_wheel():
    """#2102 revisits the half #2081 deferred: a `tui/`-only range still
    cuts a release (see `test_tui_only_still_ships` above) but its wheel is
    byte-identical to its predecessor apart from the version string, so
    publishing it to PyPI is pure waste. Pinned alongside `ships_code`'s
    `tui/`-still-ships test so the two decisions cannot silently drift back
    together."""
    assert not ships_wheel(["tui/src/panels/queue.rs"])
    assert not ships_wheel(["tui/src/main.rs", "tui/Cargo.toml"])


def test_a_mixed_tui_and_coord_range_still_ships_a_wheel():
    """A range touching `coord/` as well as `tui/` publishes the wheel
    exactly as today — the exclusion is `tui/`-ONLY, not `tui/`-present."""
    assert ships_wheel(["coord/cli.py", "tui/src/panels/queue.rs"])


def test_docs_and_tests_only_ships_no_wheel_either():
    assert not ships_wheel(["docs/AGENT_OPERATIONS.md", "tests/test_foo.py"])


def test_a_single_coord_file_ships_a_wheel():
    assert ships_wheel(["coord/cli.py"])


def test_the_deploy_lane_ships_a_wheel():
    """`deploy/*` mirrors `coord/deploy/*`, which IS package data (see
    pyproject.toml's `[tool.setuptools.package-data]`) — this is not a
    `tui/`-shaped exemption."""
    assert ships_wheel(["deploy/coord-agent.service"])
    assert ships_wheel(["install-agent.sh"])


def test_workflow_files_only_ship_no_wheel():
    assert not ships_wheel([".github/workflows/test.yml"])


def test_an_undetectable_diff_ships_a_wheel():
    """Same fail-open reasoning as `ships_code`: an unresolvable diff must
    never silently skip a wheel that should have shipped."""
    assert ships_wheel([])


def test_unrecognised_paths_ship_a_wheel_too():
    assert ships_wheel(["some_new_toplevel_dir/thing.py"])


# ── previous_tag ─────────────────────────────────────────────────────────


def test_previous_tag_is_the_one_directly_below_the_target():
    tags = ["v0.5.19", "v0.5.20", "v0.5.21"]
    assert previous_tag(tags, "v0.5.21") == (0, 5, 20)


def test_previous_tag_excludes_the_target_itself_when_present():
    """By the time `publish.yml` runs, the tag it was handed has already
    been pushed and is itself in the tag list — it must not be mistaken for
    its own predecessor."""
    tags = ["v0.5.20", "v0.5.21"]
    assert previous_tag(tags, "v0.5.21") == (0, 5, 20)


def test_previous_tag_of_the_first_release_is_none():
    assert previous_tag(["v0.0.1"], "v0.0.1") is None
    assert previous_tag([], "v0.0.1") is None


def test_previous_tag_ignores_unparseable_tags():
    assert previous_tag(["nightly", "v0.5.20", "v0.5.21"], "v0.5.21") == (0, 5, 20)


# ── bump ─────────────────────────────────────────────────────────────────


def test_default_bump_is_patch():
    assert bump((0, 4, 110), "fix(#123): a thing") == (0, 4, 111)


def test_minor_marker_bumps_minor_and_zeroes_patch():
    assert bump((0, 4, 110), "feat(#123): a thing [minor]") == (0, 5, 0)


def test_major_marker_bumps_major_and_zeroes_the_rest():
    assert bump((0, 4, 110), "[BREAKING] rework the board wire") == (1, 0, 0)


def test_major_wins_over_minor():
    assert bump((0, 4, 110), "[minor] [major] both named") == (1, 0, 0)


# ── decide: the whole policy ─────────────────────────────────────────────


def test_a_normal_code_merge_cuts_the_next_patch():
    decision = decide(
        tags=["v0.4.109", "v0.4.110"],
        message="fix(#1926): stale row fallback",
        changed_files=["coord/commands/merge.py"],
    )
    assert decision.release
    assert decision.tag == "v0.4.111"
    assert decision.wheel


def test_a_docs_merge_cuts_nothing():
    decision = decide(
        tags=["v0.4.110"],
        message="docs: rewrite the runbook",
        changed_files=["docs/AGENT_OPERATIONS.md"],
    )
    assert not decision.release
    assert decision.tag is None
    assert "docs/tests only" in decision.reason


def test_a_workflow_only_merge_cuts_nothing():
    """#2081: v0.5.7 was this exact merge — one changed file,
    `.github/workflows/test.yml` — and it minted a PyPI version anyway."""
    decision = decide(
        tags=["v0.5.6"],
        message="ci: bump the test matrix",
        changed_files=[".github/workflows/test.yml"],
    )
    assert not decision.release
    assert decision.tag is None


def test_a_tui_only_merge_still_cuts_the_next_patch():
    """#2081: judged (see the module docstring) not worth splitting `tui/`
    out of the shipping filter, so it still bumps like any other shipping
    path. Pinned so a future change to this behaviour is a deliberate diff,
    not a silent regression either way."""
    decision = decide(
        tags=["v0.5.20"],
        message="fix(#2064): SSE 404 is terminal",
        changed_files=["tui/src/panels/queue.rs"],
    )
    assert decision.release
    assert decision.tag == "v0.5.21"


def test_a_tui_only_merge_cuts_a_tag_but_no_wheel():
    """#2102: the half #2081 deferred. One tag, one GitHub Release — but the
    wheel that range would build is a no-op, so it must not be published."""
    decision = decide(
        tags=["v0.5.20"],
        message="fix(#2064): SSE 404 is terminal",
        changed_files=["tui/src/panels/queue.rs"],
    )
    assert decision.release
    assert decision.tag == "v0.5.21"
    assert not decision.wheel
    assert "no PyPI wheel" in decision.reason


def test_a_mixed_merge_cuts_a_tag_and_a_wheel():
    decision = decide(
        tags=["v0.5.20"],
        message="fix(#2100): also touch coord/",
        changed_files=["coord/cli.py", "tui/src/panels/queue.rs"],
    )
    assert decision.release
    assert decision.wheel


@pytest.mark.parametrize("marker", ["[no release]", "[skip release]", "[NO RELEASE]"])
def test_the_commit_message_opt_out_wins_over_everything(marker):
    """The escape hatch lives in the merge itself so it needs no second
    human action — which is the property this whole slice is about."""
    decision = decide(
        tags=["v0.4.110"],
        message=f"fix(#1): something {marker}",
        changed_files=["coord/cli.py"],
    )
    assert not decision.release


def test_a_repo_with_no_release_tags_bootstraps():
    decision = decide(tags=[], message="feat: first", changed_files=["coord/cli.py"])
    assert decision.release
    assert decision.tag == BOOTSTRAP_TAG


def test_the_decision_never_reuses_an_existing_tag():
    """Idempotence's other half: the workflow refuses to move an existing
    tag, but the policy must not *propose* one either."""
    tags = ["v0.4.108", "v0.4.109", "v0.4.110"]
    decision = decide(tags=tags, message="fix: x", changed_files=["coord/cli.py"])
    assert decision.tag not in tags
