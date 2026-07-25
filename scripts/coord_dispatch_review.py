#!/usr/bin/env python3
"""Explicitly dispatch a headless review for one completed work assignment.

This is the escape hatch that ``coord/review.py``'s #555 guard says exists but
which was never actually built as a CLI command::

    # This guard lives only in the automatic bulk path — the explicit
    # `coord review <id>` escape hatch (→ dispatch_review) still lets a
    # human deliberately request a headless review if they want one.

There is no ``coord review``. The only CLI route is ``coord pr <aid>``, which
spawns an entire PR *worker* first even though :func:`dispatch_review` opens the
PR itself. The web dashboard has a ``dispatch_review`` action, but it fails with
``'Repo' object has no attribute 'develop_branch'`` — ``review.py`` reads that
attribute directly where ``branch_model.py`` deliberately uses ``getattr(...,
None)`` for Repo-shaped stand-ins.

So this calls :func:`coord.review.dispatch_review` directly, with a config
parsed locally the same way every ``coord`` command parses it.

WHY THIS IS NOT A BYPASS OF #555: that guard exists so a metered headless
review never *silently* follows a human-attended interactive session. Invoking
this is an explicit, deliberate request — exactly the case the guard's own
comment carves out. ``drive-issue.sh`` only calls it behind ``--force-review``.

Usage::

    coord_dispatch_review.py <work_assignment_id>

Exit codes: 0 dispatched, 1 nothing dispatched, 2 bad usage / not found.
"""

from __future__ import annotations

import sys


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} <work_assignment_id>", file=sys.stderr)
        return 2

    aid = sys.argv[1]

    from coord.board_service import read_board, write_board
    from coord.commands._common import _load_config
    from coord.review import dispatch_review

    config = _load_config(None)
    board = read_board()

    work = board.find_by_id(aid)
    if work is None:
        print(f"error: assignment {aid!r} not found on the board", file=sys.stderr)
        return 2
    if work.status != "done":
        print(
            f"error: assignment {aid} is {work.status!r}, not 'done' — "
            "nothing to review yet",
            file=sys.stderr,
        )
        return 2

    review = dispatch_review(work, board, config)
    if review is None:
        # dispatch_review logs its own reason (no eligible machine, reviews
        # disabled, a review already in flight, ...).
        print("no review dispatched — see the reason logged above", file=sys.stderr)
        return 1

    # Persist only on success: dispatch_review mutates the board in place
    # (review_state, pr_url, the new review row).
    write_board(board)
    print(f"review dispatched: {review.assignment_id} on {review.machine_name}")
    if work.pr_url:
        print(f"  pr: {work.pr_url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
