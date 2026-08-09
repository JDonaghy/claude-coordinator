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
    ships_code,
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


def test_a_docs_merge_cuts_nothing():
    decision = decide(
        tags=["v0.4.110"],
        message="docs: rewrite the runbook",
        changed_files=["docs/AGENT_OPERATIONS.md"],
    )
    assert not decision.release
    assert decision.tag is None
    assert "docs/tests only" in decision.reason


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
