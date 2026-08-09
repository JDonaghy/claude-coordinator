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

* **Coalesced, so a burst of merges is one release.** The workflow's
  ``concurrency`` group cancels a superseded auto-release run, so five
  merges landing in five minutes mint one tag at the tip rather than five.
  Propagation is quiescence-scheduled anyway (#1835's other half), so the
  fleet would have collapsed them regardless; this just stops PyPI carrying
  four versions nothing ever ran.

* **Opt-out via the commit subject.** ``[no release]`` / ``[skip release]``
  in the merge commit message suppresses the tag. An escape hatch that
  lives in the merge itself needs no second action and no repo state.

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
    ".github/workflows/",
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
)

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
            return Decision(False, None, f"commit message carries {marker}")

    if not ships_code(changed_files):
        return Decision(
            False,
            None,
            "no shipping paths touched (docs/tests only) — a PyPI version is "
            "an immutable public name; not spending one on a no-op",
        )

    current = latest_tag(tags)
    if current is None:
        return Decision(True, BOOTSTRAP_TAG, "no vX.Y.Z tag exists yet")

    nxt = bump(current, message)
    return Decision(
        True,
        "v%d.%d.%d" % nxt,
        "shipping paths changed; patch/minor/major bump from v%d.%d.%d" % current,
    )


def _git_tags() -> list[str]:
    out = subprocess.run(
        ["git", "tag", "--list", "v*"], capture_output=True, text=True, check=False
    )
    return [line.strip() for line in out.stdout.splitlines() if line.strip()]


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
    args = parser.parse_args(argv)

    tags = args.tags or _git_tags()
    changed = list(args.changed_files)
    if args.changed_files_from_stdin:
        changed.extend(line.strip() for line in sys.stdin.read().splitlines())

    decision = decide(tags=tags, message=args.message, changed_files=changed)

    lines = [
        f"release={'true' if decision.release else 'false'}",
        f"tag={decision.tag or ''}",
        f"reason={decision.reason}",
    ]
    for line in lines:
        print(line)
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
