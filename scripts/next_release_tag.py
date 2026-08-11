#!/usr/bin/env python3
"""Decide whether a merge to ``main`` cuts a release, and under what tag.

#1835 (PKG-7): merging a PR to ``main`` is meant to be the *only* human
action in a release. ``publish.yml`` (#1242) already turns one ``v*`` tag
into one complete Release; the missing step was somebody typing ``git tag``.
This script is that step's judgement, factored out of
``.github/workflows/auto-release.yml`` so it is unit-testable
(``tests/test_next_release_tag.py``) instead of being untested YAML — the
usual fate of release automation, and the reason release automation is
usually discovered to be broken during a release.

THE TRIGGER POLICY, AND WHY THIS ONE
------------------------------------
#1835 asks for one policy, justified. The options it lists are every merge,
path-filtered, label-gated, and batched. This implements **path-filtered,
with a commit-message opt-out, coalesced by a workflow concurrency group**:

* **Path-filtered, not every-merge.** A PyPI release is immutable and its
  version numbers are a public, permanent record. A docs-only or
  tests-only merge changes nothing a user of the wheel can observe, so
  minting a version for it spends an irreversible name on a no-op and adds
  a fleet-wide restart (propagation) with zero payload. :func:`ships_code`
  is the filter, and it is deliberately *inclusive* — `deploy/**` and
  `install-agent.sh` ship (#1831: that lane's release was three unit files
  and a shell script), and anything unrecognised counts as shipping, so the
  failure mode is a superfluous release rather than a change that silently
  never ships.

* **Not label-gated.** A label is a human action after the merge, which is
  the exact thing this slice removes.

* **Not batched on a timer.** Batching decouples "what merged" from "what
  released", so a bisect over releases stops matching the history — and the
  reason to batch (PyPI noise) is better solved by coalescing, below.

* **Coalesced when merges land inside the same publish window, not
  otherwise.** ``auto-release.yml``'s ``concurrency`` group used to cancel a
  superseded run outright, so five merges landing in five minutes minted one
  tag at the tip rather than five. #2035 flipped ``cancel-in-progress`` to
  ``false``: cancelling mid-run could abort between the PyPI upload and the
  GitHub Release, leaving a half-published version. What ``false`` actually
  buys is queuing — GitHub holds at most one running plus one *pending* run
  per group and collapses a burst of pending merges into the last of them —
  so coalescing now only catches merges landing *during* the running
  publish, roughly a 15-minute window. At the cadence this repo has actually
  seen (v0.5.1 -> v0.5.26 in under 48 hours, one release per merge), that
  window rarely has two merges in it, so coalescing is not the thing keeping
  PyPI version count down — :func:`ships_code` is. A merge landing after a
  run finishes gets its own release; that is the trigger policy's intent
  (path-filtered per merge), not a bug.

* **Opt-out via the commit subject.** ``[no release]`` / ``[skip release]``
  in the merge commit message suppresses the tag. An escape hatch that
  lives in the merge itself needs no second action and no repo state.

WHY ``tui/`` STAYS A SHIPPING PREFIX (#2081)
---------------------------------------------
A `tui/`-only merge cuts a PyPI version whose wheel is byte-identical to its
predecessor except the version string, and it moves the fleet's expected
version, so every host's `coord release verify` goes red for a change that
cannot reach any of them — coord-tui is a per-host binary with no remote
install path (see `lane_is_out_of_reach` in `coord/release_propagate.py`).
That is a real cost, and #2081 asked, explicitly, whether `tui/` should keep
driving a PyPI/expected-version bump at all, or only a GitHub Release (PKG-6,
#1242, attaches the coord-tui binaries there and does need an artifact home).

Judged not worth doing here: the two are not separable without touching how a
release is *built*, which #2081 puts out of scope (`publish.yml`'s jobs are
one call, one tag, one Release — #1238/PKG-2's whole point was removing a
second version to drift from the first). Splitting them would mean either a
second tag namespace for coord-tui, or teaching `publish.yml` to skip the
PyPI/verify-published leg for some tags but not others — real surgery on the
publish pipeline, not a path-filter tweak. `tui/` therefore stays in
:data:`SHIPPING_PREFIXES`, deliberately, and this paragraph — plus
``test_tui_only_still_ships`` in ``tests/test_next_release_tag.py`` — is that
decision on the record rather than left implicit.

#2102 REVISITS THE OTHER HALF: THE WHEEL, NOT THE TAG
------------------------------------------------------
The operator call above (2026-08-10) is that a `tui/`-only merge must still
cut ONE tag and ONE GitHub Release — `coord tui update --version X.Y.Z` needs
somewhere to fetch the binary from — but must publish no PyPI wheel, since the
wheel a `tui/`-only range would build is byte-identical to its predecessor
apart from the version string. That is deliberately *not* a change to
:func:`ships_code`/:data:`SHIPPING_PREFIXES` above: the tag/release decision
and the wheel decision are now two separate questions, answered by
:func:`ships_code` and :func:`ships_wheel` respectively. Keeping them separate
functions (rather than teaching one function two return values) is what lets
each be pinned by its own tests without the two drifting apart — see
``test_tui_only_still_ships`` (ships_code) and
``test_tui_only_ships_no_wheel`` (ships_wheel) in
``tests/test_next_release_tag.py``.

:func:`ships_wheel` is computed independently inside `publish.yml` (via
``--wheel-for-tag``), not threaded through from `auto-release.yml`'s merge-time
decision — the same policy has to hold for a hand-pushed tag and for the
`already_released_tag` recovery path, neither of which runs `decide()` at all,
so the wheel question has to be answerable from the tag and git history alone.

VERSION SELECTION
-----------------
Patch bump off the highest existing ``vX.Y.Z`` tag. ``[minor]`` / ``[major]``
in the merge commit message bump that component instead. There is no version
literal anywhere to edit — #1238 (PKG-2) made the tag itself the version via
setuptools-scm — which is what makes an automated tag safe at all: nothing
can disagree with it.

Usage::

    next_release_tag.py --tags-from-git --message "$(git log -1 --pretty=%B)" \
                        --changed-files-from-stdin

Writes ``release=true|false``, ``tag=vX.Y.Z`` and ``reason=...`` to
``$GITHUB_OUTPUT`` when that variable is set, and always prints them.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass

#: Path prefixes whose contents end up in a wheel, a binary, or a host's
#: systemd directory. Anything here is shipping code.
SHIPPING_PREFIXES: tuple[str, ...] = (
    "coord/",
    "tui/",
    "deploy/",
    "scripts/",
    "pyproject.toml",
    "MANIFEST.in",
    "install-agent.sh",
)

#: Path prefixes that demonstrably ship nothing. Kept as an explicit
#: allowlist rather than "everything not in SHIPPING_PREFIXES": an unknown
#: new top-level directory must default to *shipping*, so the failure mode is
#: a superfluous release, never a change that silently never reaches a host.
NON_SHIPPING_PREFIXES: tuple[str, ...] = (
    "docs/",
    "tests/",
    "graphify-out/",
    "README.md",
    "GOAL.md",
    "CLAUDE.md",
    "LICENSE",
    ".github/ISSUE_TEMPLATE/",
    ".gitignore",
    # CLAUDE.md: ".githooks/** is a fifth deploy surface whose failure mode
    # is the opposite of the other four — a merged hook is live on every
    # machine at the next fetch, no release, no restart." Minting a release
    # for it would be pure waste: fleet-wide propagation for a change that's
    # already everywhere.
    ".githooks/",
    # #2081: a workflow file reaches no wheel, no binary and no host's unit
    # directory — the version of a workflow that RUNS is whatever is at the
    # tip of main, and a tag changes nothing about that. This is the
    # `.githooks/` case again, same comment applying verbatim. Even
    # `publish.yml`/`release-tui.yml`, which decide how a release's assets are
    # *built*, never need a version bump to take effect — only a re-run
    # (`workflow_dispatch`) — so there is no workflow-file change that
    # legitimately wants an automatic patch bump. v0.5.7's entire release
    # range was one file, `.github/workflows/test.yml`: a release that shipped
    # nothing and moved the fleet's expected version for no payload.
    ".github/workflows/",
)

#: SHIPPING_PREFIXES entries that ship a release (a tag, a GitHub Release)
#: but never reach the PyPI wheel specifically (#2102). `tui/` is the only
#: member: the coord-tui binaries live on the GitHub Release, never inside
#: the wheel, so a range that touches nothing else has no wheel content to
#: publish. See :func:`ships_wheel`.
WHEEL_EXCLUDED_SHIPPING_PREFIXES: tuple[str, ...] = ("tui/",)

#: Case-insensitive markers in the merge commit message that suppress the
#: release entirely.
SKIP_MARKERS: tuple[str, ...] = ("[no release]", "[skip release]", "[no-release]")

_MINOR_MARKERS = ("[minor]", "[feature]")
_MAJOR_MARKERS = ("[major]", "[breaking]")

_TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")

#: The version minted when the repo has no ``vX.Y.Z`` tag at all. Only
#: reachable on a fresh fork; a bare `v0.0.1` is a far better outcome than
#: guessing at history.
BOOTSTRAP_TAG = "v0.0.1"


@dataclass(frozen=True)
class Decision:
    release: bool
    tag: str | None
    reason: str
    #: Should THIS range's wheel be uploaded to PyPI (#2102)? Only meaningful
    #: when ``release`` is True — a merge that cuts no release obviously ships
    #: no wheel either. Kept as its own field (not folded into ``release``)
    #: because `publish.yml` must still cut the tag and the GitHub Release for
    #: a `tui/`-only range; only the PyPI upload is conditional.
    wheel: bool = True


def parse_tag(tag: str) -> tuple[int, int, int] | None:
    """``v1.2.3`` -> ``(1, 2, 3)``; anything else -> ``None``.

    Deliberately strict: a pre-release or a ``v1.2`` must not be picked as
    "the latest release" and then patch-bumped into a version that collides
    with something already on PyPI.
    """
    match = _TAG_RE.match(tag.strip())
    if not match:
        return None
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def latest_tag(tags: list[str]) -> tuple[int, int, int] | None:
    """Highest parseable ``vX.Y.Z`` in *tags*, by version order not string order."""
    parsed = [p for p in (parse_tag(t) for t in tags) if p is not None]
    return max(parsed) if parsed else None


def ships_code(changed_files: list[str]) -> bool:
    """Does this change reach a wheel, a binary or a host's unit directory?

    An empty change list means "we could not tell", which must resolve to
    True — an undetectable diff that silently never releases is the failure
    this whole slice exists to end.
    """
    if not changed_files:
        return True
    for path in changed_files:
        path = path.strip()
        if not path:
            continue
        if any(path.startswith(prefix) for prefix in NON_SHIPPING_PREFIXES):
            continue
        if any(path.startswith(prefix) for prefix in SHIPPING_PREFIXES):
            return True
        # Unrecognised: default to shipping. See NON_SHIPPING_PREFIXES.
        return True
    return False


def ships_wheel(changed_files: list[str]) -> bool:
    """Does this change reach the PyPI wheel specifically? (#2102)

    A `tui/`-only range still :func:`ships_code` — it still cuts a tag and a
    GitHub Release carrying the coord-tui binaries — but the wheel that range
    would build is byte-identical to its predecessor apart from the version
    string, so uploading it is pure waste and moves the fleet's PyPI-derived
    "expected version" for a change that reaches no wheel-installed lane.

    Every other :data:`SHIPPING_PREFIXES` entry either lands inside the wheel
    (`coord/`, `pyproject.toml`, the `deploy/*` package data) or is packaging
    machinery whose own change legitimately wants a wheel built and verified
    end to end (`scripts/`, `MANIFEST.in`, `install-agent.sh`) — so this is
    :func:`ships_code`'s exact allow/deny walk, with `tui/` carved out as a
    non-wheel shipping prefix rather than a non-shipping one. An empty change
    list fails open for the same reason ``ships_code`` does: an undetectable
    diff must never silently skip a wheel that should have shipped.
    """
    if not changed_files:
        return True
    for path in changed_files:
        path = path.strip()
        if not path:
            continue
        if any(path.startswith(prefix) for prefix in NON_SHIPPING_PREFIXES):
            continue
        if any(path.startswith(prefix) for prefix in WHEEL_EXCLUDED_SHIPPING_PREFIXES):
            continue
        # Recognised wheel-shipping prefix, or unrecognised (fail open): ship it.
        return True
    return False


def previous_tag(tags: list[str], target: str) -> tuple[int, int, int] | None:
    """The highest parseable ``vX.Y.Z`` strictly below *target*, or ``None``.

    Used from the *publish* side, where a tag has already been chosen (by
    ``decide()``, or by a human running ``git tag`` by hand) and the question
    is "what range did this tag release?" — the mirror image of
    :func:`latest_tag` + :func:`bump`, which pick a *new* tag from the merge
    side. Filtering strictly-below handles *target* already being present in
    *tags* (the normal case: by the time `publish.yml` runs, the tag it was
    handed has already been pushed) without a special case.
    """
    target_parsed = parse_tag(target)
    parsed = [p for p in (parse_tag(t) for t in tags) if p is not None]
    if target_parsed is not None:
        parsed = [p for p in parsed if p < target_parsed]
    return max(parsed) if parsed else None


def bump(current: tuple[int, int, int], message: str) -> tuple[int, int, int]:
    """Next version, honouring ``[major]``/``[minor]`` in the commit message."""
    major, minor, patch = current
    lowered = message.lower()
    if any(marker in lowered for marker in _MAJOR_MARKERS):
        return major + 1, 0, 0
    if any(marker in lowered for marker in _MINOR_MARKERS):
        return major, minor + 1, 0
    return major, minor, patch + 1


def decide(*, tags: list[str], message: str, changed_files: list[str]) -> Decision:
    """The whole policy, as one pure function."""
    lowered = message.lower()
    for marker in SKIP_MARKERS:
        if marker in lowered:
            return Decision(False, None, f"commit message carries {marker}", wheel=False)

    if not ships_code(changed_files):
        return Decision(
            False,
            None,
            "no shipping paths touched (docs/tests only) — a PyPI version is "
            "an immutable public name; not spending one on a no-op",
            wheel=False,
        )

    wheel = ships_wheel(changed_files)
    current = latest_tag(tags)
    if current is None:
        return Decision(True, BOOTSTRAP_TAG, "no vX.Y.Z tag exists yet", wheel=wheel)

    nxt = bump(current, message)
    reason = "shipping paths changed; patch/minor/major bump from v%d.%d.%d" % current
    if not wheel:
        reason += (
            " (tui/-only — no PyPI wheel to publish; GitHub Release + "
            "coord-tui binaries only, #2102)"
        )
    return Decision(True, "v%d.%d.%d" % nxt, reason, wheel=wheel)


def _git_tags() -> list[str]:
    out = subprocess.run(
        ["git", "tag", "--list", "v*"], capture_output=True, text=True, check=False
    )
    return [line.strip() for line in out.stdout.splitlines() if line.strip()]


def _git_diff_names(base: str | None, target: str) -> list[str]:
    """Paths that changed in *target*'s release range, for ``--wheel-for-tag``.

    Mirrors ``auto-release.yml``'s "Collect changed paths" step: diff from
    the previous release tag when there is one, otherwise show *target*'s own
    commit tree (a repo's very first release). Failures degrade to an empty
    list, same as an unresolvable diff anywhere else in this script — which
    :func:`ships_wheel` fails open on, publishing the wheel rather than
    silently dropping one that should have shipped.
    """
    if base:
        out = subprocess.run(
            ["git", "diff", "--name-only", base, target],
            capture_output=True, text=True, check=False,
        )
    else:
        out = subprocess.run(
            ["git", "show", "--pretty=format:", "--name-only", target],
            capture_output=True, text=True, check=False,
        )
    return [line.strip() for line in out.stdout.splitlines() if line.strip()]


def _write_output(lines: list[str]) -> None:
    for line in lines:
        print(line)
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--message", default="", help="Merge commit message.")
    parser.add_argument(
        "--tag",
        action="append",
        default=[],
        dest="tags",
        help="An existing tag (repeatable). Default: read them from git.",
    )
    parser.add_argument(
        "--changed-file",
        action="append",
        default=[],
        dest="changed_files",
        help="A path changed by this merge (repeatable).",
    )
    parser.add_argument(
        "--changed-files-from-stdin",
        action="store_true",
        help="Read newline-separated changed paths from stdin as well.",
    )
    parser.add_argument(
        "--wheel-for-tag",
        default=None,
        metavar="TAG",
        help=(
            "Instead of the merge-time release decision above, print whether "
            "TAG's own release range (diffed against the previous vX.Y.Z tag, "
            "found among --tag/git) ships PyPI wheel content (#2102). This is "
            "how `publish.yml` decides, independent of how TAG was created — "
            "a merge-triggered auto-release, the `already_released_tag` "
            "recovery path, or a hand-pushed tag all resolve the same way."
        ),
    )
    args = parser.parse_args(argv)

    tags = args.tags or _git_tags()

    if args.wheel_for_tag:
        target = args.wheel_for_tag.strip()
        base = previous_tag(tags, target)
        base_str = ("v%d.%d.%d" % base) if base else None
        changed = _git_diff_names(base_str, target)
        wheel = ships_wheel(changed)
        _write_output([f"wheel={'true' if wheel else 'false'}"])
        return 0

    changed = list(args.changed_files)
    if args.changed_files_from_stdin:
        changed.extend(line.strip() for line in sys.stdin.read().splitlines())

    decision = decide(tags=tags, message=args.message, changed_files=changed)

    _write_output(
        [
            f"release={'true' if decision.release else 'false'}",
            f"tag={decision.tag or ''}",
            f"reason={decision.reason}",
            f"wheel={'true' if decision.wheel else 'false'}",
        ]
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
