"""Live cross-machine driver for #878's review-capture confidence matrix.

#878 exists because review-findings capture has been "fixed" repeatedly
(#608 -> #617 -> #877), each patch closing one hole while a new one
surfaced — and mocked tests passed while #607/#617 broke in *production*.
So #877 landing is necessary but not sufficient: this script is the other
half of the confidence gate #878 asks for — **a live, cross-machine proof**
that a ``request-changes`` verdict + its full findings body actually reach
the daemon board, for every (machine, capture-path) cell.

WHAT THIS SCRIPT DOES — and does NOT do
----------------------------------------
This automates only the part #878 says can be automated: the ``/board``
assertion half. It does **not** drive the interactive review itself — no
PTY, no TTY-scraping, no headless ``claude -p`` dispatch. Driving each
review is attended by a human operator (you), because:

* #878's posture is explicit: "Do NOT dispatch this as a headless worker."
* Interactive reviews go through ``claude`` (Max/Pro OAuth) directly, not
  the coordinator — scraping that TTY would violate ToS §3.7.

So the loop per cell is: YOU drive the review interactively on the target
machine to a ``request-changes`` verdict with a real multi-line findings
body, exercise the capture path under test (P1/P2/P3, see RUNBOOK below),
then run THIS script — optionally from a *different* machine than the one
that hosted the review — to assert the board actually has it.

USAGE
-----

    .venv/bin/python scripts/verify_878_review_capture.py \\
        --assignment-id <review-assignment-id> \\
        [--service-url http://dellserver:7435] \\
        [--min-body-lines 3] [--min-body-chars 80]

``--service-url`` defaults to the usual client bootstrap (``--service-url``
flag > ``COORD_SERVICE_URL`` env > ``~/.coord/client.toml``), same
resolution order as the ``coord`` CLI (see ``coord/client.py``). Point it
explicitly at the daemon host's ``coord serve`` port (7435) and run this
script FROM one of the other two machines to get the "cross-machine read"
evidence #878 asks for — the point isn't that *a* machine can read the
board, it's that a DIFFERENT machine than the one that captured the
verdict can.

Exit code 0 = PASS (verdict + full body confirmed), 1 = FAIL, 2 = usage/
connection error. Output is a single line formatted for pasting directly
into the #878 results table:

    machine=<host> assignment=<id> verdict=<verdict> body_chars=<n> \\
        body_lines=<n> cross_machine_hint=<service host> -> PASS|FAIL

RUNBOOK — the 9 cells
----------------------
Machines: precision, elitebook, dellserver. Repeat for each:

**P1 — reviewer runs `coord report-result` (durable)**
  1. On the target machine, dispatch a real interactive review against a
     small throwaway diff (e.g. ``coord assign <machine> <repo> <issue>
     --interactive`` for a review-type assignment, or trigger via the
     Pipeline "Start review" TUI action).
  2. In the interactive session, produce a substantive multi-line
     request-changes verdict.
  3. Before exiting, run:
         coord report-result --assignment <id> --status request-changes \\
             --summary "<the findings body>"
  4. Run this script (ideally from a *different* machine) against that
     assignment id. Expect PASS.

**P2 — reviewer exits WITHOUT report-result (transcript-floor, #617-B)**
  1. Same setup as P1, but close the session (``/exit`` or Ctrl-D) WITHOUT
     running ``coord report-result``.
  2. Trigger recovery (whatever currently invokes the transcript-floor
     read — e.g. the Pipeline "pass -> review" bounce, or ``coord notify``)
     so it reads the session transcript on the review machine's own host
     over ssh.
  3. Run this script. Expect PASS, and confirm the body is the FULL findings
     text recovered from the transcript, not a truncated summary.

**P3 — operator backstop (`_prompt_and_relay_review_verdict`, #877)**
  1. Same setup, but arrange for BOTH P1 and P2 to be dry (no
     ``report-result``, and either no recoverable transcript or the
     transcript-floor path unavailable) so the #877 operator-prompt path
     fires.
  2. Confirm the #877 behaviour while you're here (part of #878's pass
     criteria, not just this script's assertion):
       - board-content gate hits FIRST when findings are already present
         on the board (no editor opens);
       - when the board is empty, the REMOTE transcript-floor is tried
         against the review machine's host BEFORE any editor opens;
       - the editor opens blank ONLY when both are dry, and then it must
         print the host + transcript path it looked at.
  3. Relay/confirm the verdict through the operator prompt.
  4. Run this script. Expect PASS.

For every cell, watch for a blank-editor / retype-from-scratch event — #878
requires ZERO of these across all 9 cells. Record each cell's result
(machine, path, verdict-on-board, body length, cross-machine read Y/N) in
the #878 issue yourself; this script only produces the one-line evidence
per cell, it does not post to GitHub.
"""

from __future__ import annotations

import argparse
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from coord.client import fetch_board_payload, resolve_board_service  # noqa: E402
from coord.state import _parse_review_findings_blob  # noqa: E402

EXPECTED_VERDICT = "request-changes"


def _find_assignment(payload: dict, assignment_id: str) -> dict | None:
    for a in payload.get("assignments", []):
        if a.get("assignment_id") == assignment_id:
            return a
    return None


def verify(
    assignment_id: str,
    *,
    service_url: str | None,
    service_token: str | None,
    min_body_lines: int,
    min_body_chars: int,
) -> tuple[bool, str]:
    """Fetch /board and assert the review verdict + full findings body landed.

    Returns ``(passed, one_line_report)``.
    """
    svc = resolve_board_service(flag_url=service_url, flag_token=service_token)
    if svc is None:
        return False, (
            "ERROR: no daemon service configured — pass --service-url, set "
            "COORD_SERVICE_URL, or configure ~/.coord/client.toml"
        )

    try:
        payload = fetch_board_payload(svc)
    except Exception as e:  # noqa: BLE001 — surface a clean failure line
        return False, f"ERROR: could not reach board daemon at {svc.url}: {e}"

    row = _find_assignment(payload, assignment_id)
    if row is None:
        return False, (
            f"FAIL: assignment {assignment_id!r} not found on board at {svc.url} "
            f"(reviewed on a different daemon? wrong id?)"
        )

    parsed = _parse_review_findings_blob(row.get("review_findings"))
    verdict = row.get("review_verdict")
    host = socket.gethostname()

    def report(passed: bool, detail: str) -> tuple[bool, str]:
        body_chars = len(parsed[1]) if parsed else 0
        body_lines = parsed[1].count("\n") + 1 if parsed and parsed[1] else 0
        status = "PASS" if passed else "FAIL"
        line = (
            f"machine={host} assignment={assignment_id} verdict={verdict} "
            f"body_chars={body_chars} body_lines={body_lines} "
            f"daemon={svc.url} -> {status}"
        )
        if detail:
            line += f" ({detail})"
        return passed, line

    if parsed is None:
        return report(False, "no review_findings on board (verdict relay never landed)")

    found_verdict, body = parsed

    if found_verdict != EXPECTED_VERDICT:
        return report(
            False, f"expected verdict {EXPECTED_VERDICT!r}, board has {found_verdict!r}"
        )
    if verdict is not None and verdict != found_verdict:
        # review_verdict column and the review_findings blob's verdict field
        # should always agree (state.py writes both together) — a mismatch
        # means something wrote one without the other.
        return report(
            False,
            f"review_verdict column ({verdict!r}) disagrees with "
            f"review_findings blob ({found_verdict!r})",
        )
    if not body or not body.strip():
        return report(False, "findings body is empty")

    lines = body.count("\n") + 1
    chars = len(body)
    if lines < min_body_lines:
        return report(
            False,
            f"body has only {lines} line(s), expected >= {min_body_lines} "
            "(looks like a truncated one-line summary, not the full findings)",
        )
    if chars < min_body_chars:
        return report(
            False,
            f"body has only {chars} char(s), expected >= {min_body_chars} "
            "(looks truncated)",
        )

    return report(True, "")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Assert a request-changes verdict + full findings body reached the "
            "daemon board for a #878 review-capture matrix cell."
        )
    )
    parser.add_argument(
        "--assignment-id", required=True, help="review assignment id to check"
    )
    parser.add_argument(
        "--service-url",
        default=None,
        help="daemon board URL, e.g. http://dellserver:7435 "
        "(default: COORD_SERVICE_URL env or ~/.coord/client.toml)",
    )
    parser.add_argument(
        "--service-token", default=None, help="bearer token, if the daemon requires one"
    )
    parser.add_argument(
        "--min-body-lines",
        type=int,
        default=3,
        help="minimum newline-delimited lines the findings body must have (default: 3)",
    )
    parser.add_argument(
        "--min-body-chars",
        type=int,
        default=80,
        help="minimum character length the findings body must have (default: 80)",
    )
    args = parser.parse_args()

    passed, line = verify(
        args.assignment_id,
        service_url=args.service_url,
        service_token=args.service_token,
        min_body_lines=args.min_body_lines,
        min_body_chars=args.min_body_chars,
    )
    print(line)
    if passed:
        return 0
    if line.startswith("ERROR:"):
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
