"""Review-gate commands: `report-result`, `set-review-findings`,
`fix-briefing`, and the shared `_prompt_and_relay_review_verdict` helper
used by both the dispatch and sessions modules. Extracted from
coord/cli.py (#747)."""

from __future__ import annotations

import sys
from pathlib import Path

import click


from coord.commands._common import _CONFIG_OPTION, _load_config


def _collect_review_body_via_editor(
    *, assignment_id: str, summary: str, pre_body: str | None = None
) -> str | None:
    """Open ``$EDITOR`` for the operator to enter the full review findings (#617).

    Last-resort body capture for :func:`_prompt_and_relay_review_verdict` when
    neither a durable ``coord report-result`` nor the (remote-aware)
    transcript-floor produced the findings.  A ``request-changes`` verdict must
    never be recorded bodyless — the write seam refuses it and the fix worker
    would be dispatched with nothing to fix (#607) — so this collects the body
    the operator just wrote in the review session.

    When *pre_body* is provided it is used as the initial seed, so the operator
    edits or confirms already-recovered findings rather than typing from scratch
    (#877 editor-blank fix).

    Returns the entered body (template comment lines stripped) or ``None`` when
    empty / no editor available, so the caller can refuse + print the manual
    ``--body-file`` hint.
    """
    template = (
        "\n\n"
        f"# ── Review findings for assignment {assignment_id} ───────────────\n"
        "# Enter the full findings above (Markdown). Every BLOCKING item, with\n"
        "# file:line. This is exactly what the fix worker is briefed with, and\n"
        "# what the #603 per-issue context store records for every future\n"
        "# iteration on this issue.\n"
        "# Lines starting with '#' are ignored. Save an empty file to cancel.\n"
    )
    if pre_body and pre_body.strip():
        seed = pre_body.strip() + "\n" + template
    else:
        seed = (f"{summary.strip()}\n" if summary.strip() else "") + template
    try:
        edited = click.edit(seed)
    except Exception:  # noqa: BLE001 — no editor / editor failed → treat as cancel
        edited = None
    if not edited:
        return None
    body = "\n".join(
        ln for ln in edited.splitlines() if not ln.lstrip().startswith("#")
    ).strip()
    return body or None


def _prompt_and_relay_review_verdict(
    *,
    assignment_id: str,
    repo_name: str,
    repo_github: str,
    issue_number: int,
    machine_name: str,
    verdict_cmd_hint: str,
    started_at: float | None = None,
    ssh_target: str | None = None,
) -> bool:
    """Prompt the operator for a review verdict on exit and relay it (#486d / #877).

    Backstop used by BOTH interactive-review exit paths when the reviewer left
    without running `coord report-result` (local or remote — since #590 a
    remote `report-result` routes to the coordinator's shared DB via the daemon,
    so both paths *can* self-report; this prompt only fires when they didn't).

    Without it the verdict silently never reaches the merge gate and the
    Work→Review→Fix flow stalls.  Prompt the operator here (the terminal is a
    TTY) and relay through the same `issue_store` seam `coord report-result`
    uses — which itself routes to the daemon when `board_service` is set.

    **#877 board-content gate (primary)**: before prompting, read the assignment
    from the local DB for ``review_verdict`` + ``review_findings``.  When both
    are present the editor is NOT opened — the already-captured body is used
    directly and the prompt defaults to the captured verdict (not ``[s]``).
    This handles the status=failed-with-valid-verdict inconsistency where the
    board already captured the findings (e.g. via ``notify`` or a previous
    partial attempt) but ``already_recorded`` was False when ``finalize`` ran.

    **#877 remote-transcript backstop (secondary)**: when the board is empty and
    ``ssh_target`` is set, the remote transcript-floor is re-run against the
    session's own host before any editor is opened.  Findings recovered here
    also seed the editor so it is never blank.

    *started_at*: session start timestamp (epoch).  Passed to the transcript-
    floor so only transcripts active during this session are considered.  When
    ``None``, the local scan is skipped and the remote scan uses a permissive
    ``cutoff=0`` (bounded solely by the issue-number gate).

    *ssh_target*: SSH hostname of the machine where the review session ran.
    ``None`` for local sessions; set to ``machine.host`` for remote reviews.

    No-op that prints the manual hint when stdin isn't a TTY AND no pre-
    captured data is available (tests / headless invocations where no prior
    recording happened).
    Returns True when a verdict was recorded.
    """
    # ── Step 1: board-content gate (#877) ───────────────────────────────────
    # Check the local DB first (cheapest) for already-captured verdict+findings.
    # This is intentionally BEFORE the TTY check so headless callers also benefit
    # (auto-relay the captured data without prompting).
    _pre_verdict: str | None = None
    _pre_body: str | None = None
    try:
        from coord.state import load_assignment_review_findings  # noqa: PLC0415

        _cached = load_assignment_review_findings(assignment_id)
        if _cached is not None:
            _pre_verdict, _pre_body = _cached
    except Exception:  # noqa: BLE001 — DB unavailable → fall through
        pass

    # ── Step 2: remote transcript-floor (#877) ──────────────────────────────
    # When board is empty and ssh_target is known, re-run the transcript-floor
    # against the session's own host.  Reuses the same floor that
    # finalize_interactive_exit already attempted — a 2nd pass here catches the
    # flush-race / timing window where the JSONL wasn't fully written yet when
    # finalize ran.  started_at=None falls through to cutoff=0 (all transcripts
    # in the remote listing) — still bounded by the issue-number gate.
    if _pre_verdict is None and ssh_target:
        try:
            from coord.interactive import (  # noqa: PLC0415
                _review_findings_from_transcript,
            )

            _tf = _review_findings_from_transcript(
                issue_number, started_at, assignment_id=assignment_id, ssh_target=ssh_target
            )
            if _tf is not None:
                _pre_verdict = _tf.verdict
                _pre_body = _tf.body
        except Exception:  # noqa: BLE001 — ssh unavailable → fall through
            pass

    # ── Surface pre-captured findings ───────────────────────────────────────
    if _pre_verdict is not None:
        click.echo(f"\n  Captured review verdict: {_pre_verdict!r}")
        if _pre_body:
            _preview = _pre_body[:300].rstrip()
            if len(_pre_body) > 300:
                _preview += f"\n  … ({len(_pre_body)} chars total)"
            click.echo(f"  Findings preview: {_preview}\n")

    # ── Non-TTY path ─────────────────────────────────────────────────────────
    if not sys.stdin.isatty():
        if _pre_verdict is None:
            click.echo(f"  no verdict reported — record it with:\n{verdict_cmd_hint}")
            return False
        # Headless + pre-captured: auto-relay (no prompt, no editor).
        # Applies to CI / daemon scenarios where both verdict AND body are
        # already in the board — no human input needed.
        if _pre_verdict == "request-changes" and not _pre_body:
            click.echo(
                "  headless: captured verdict is request-changes but findings body "
                f"is missing — record manually with:\n{verdict_cmd_hint}"
            )
            return False
        verdict: str = _pre_verdict
        summary: str = ""
        findings_body: str | None = _pre_body
    else:
        # ── TTY prompt ──────────────────────────────────────────────────────
        # Default to the captured verdict when we have one (never [s]kip).
        _default = {"approve": "a", "request-changes": "r"}.get(_pre_verdict or "", "s")
        ans = click.prompt(
            "  Review verdict — [a]pprove / [r]equest-changes / [s]kip",
            type=click.Choice(["a", "r", "s"], case_sensitive=False),
            default=_default,
            show_choices=True,
        )
        verdict = {"a": "approve", "r": "request-changes"}.get(ans.lower(), "")
        if not verdict:
            click.echo(f"  skipped — record the verdict later with:\n{verdict_cmd_hint}")
            return False
        summary = click.prompt(
            "  one-line summary (optional, Enter to skip)", default="", show_default=False
        )

        # ── Findings body for request-changes ───────────────────────────────
        # #617: request-changes MUST carry the full findings body.
        # #877: when the board/transcript already has the body, use it directly
        # (no blank editor).  When missing, open the editor — seeded with
        # whatever partial body we recovered, or blank as a last resort (with a
        # hint pointing the operator at the session host / transcript path).
        findings_body = None
        if verdict == "request-changes":
            if _pre_body:
                # Pre-captured body — use it directly; editor not opened (#877).
                findings_body = _pre_body
                click.echo(
                    f"  Using {len(findings_body)}-char findings body from "
                    "board/transcript (editor not opened)."
                )
            else:
                # No pre-captured body — open editor.
                if ssh_target:
                    click.echo(
                        f"  Findings not recovered from {ssh_target!r}. "
                        "Opening editor — enter the full review (every blocking "
                        "item, file:line)."
                    )
                    click.echo(
                        f"  To fetch the transcript manually: "
                        f"ssh {ssh_target} "
                        r"'find $HOME/.claude/projects -name \"*.jsonl\" "
                        r"| sort -rn | head -10'"
                    )
                else:
                    click.echo(
                        "  request-changes needs your full findings — opening an "
                        "editor (every blocking item, file:line)…"
                    )
                findings_body = _collect_review_body_via_editor(
                    assignment_id=assignment_id, summary=summary
                )
                if not findings_body:
                    click.echo(
                        "  verdict NOT recorded: request-changes requires the findings "
                        "body — recording it without one would strand the fix worker "
                        "(#607). Record it when ready with:\n"
                        f"    coord report-result --assignment {assignment_id} "
                        "--status done --verdict request-changes "
                        f"--body-file /tmp/review-{assignment_id}.md",
                        err=True,
                    )
                    return False

    # ── Relay through issue_store seam ──────────────────────────────────────
    try:
        from coord import issue_store  # noqa: PLC0415

        outcome = issue_store.post_result(
            issue_store.ResultRecord(
                assignment_id=assignment_id,
                machine_name=machine_name,
                repo_name=repo_name,
                repo_github=repo_github,
                issue_number=int(issue_number),
                status="done",
                verdict=verdict,  # type: ignore[arg-type]  # narrowed to approve/request-changes above
                summary=summary,
                branch=None,
                findings_body=findings_body,
            )
        )
        click.echo(
            f"  verdict '{verdict}' recorded (posted_to_github={outcome.posted})."
        )
        if not outcome.findings_written:
            # #650: this is exactly the shape of the original incident — a
            # second exit-prompt capture for a review that already has
            # non-empty findings on the row.  The stored verdict already
            # matched (or `_persist_review_verdict` would have raised), so
            # the good findings were preserved — just tell the operator.
            click.echo(
                "  note: existing review findings were NOT overwritten "
                "(#650 clobber guard) — this assignment already had "
                "different, non-empty findings recorded; they are "
                "preserved. Use `coord report-result ... --force` if this "
                "overwrite was intentional."
            )
        if outcome.error:
            click.echo(f"  github post warning: {outcome.error}", err=True)
        return True
    except Exception as exc:  # noqa: BLE001 — best-effort; fall back to the hint
        click.echo(
            f"  warning: failed to record verdict inline: {exc}\n{verdict_cmd_hint}",
            err=True,
        )
        return False


def _prompt_and_relay_test_verdict(
    *,
    work_assignment_id: str,
    smoke_assignment_id: str,
    repo_name: str,
    repo_github: str,
    issue_number: int,
    machine_name: str,
    verdict_cmd_hint: str,
) -> bool:
    """Prompt the operator for a Test-gate verdict on exit and relay it (#923).

    Backstop for interactive SMOKE-OF sessions that exit without the agent
    running ``coord test --passed|--fail|--skipped``.  Without it the test
    verdict is silently lost, the Test box greys, and the merge gate blocks
    with no error message.

    **Idempotent**: reads ``work_assignment_id`` from the board first; when
    ``test_state`` is already set (the agent DID run ``coord test``), this
    function prints a confirmation and returns True immediately — no
    double-prompt.

    Mirrors :func:`_prompt_and_relay_review_verdict` (which handles the REVIEW
    stage).  Same contract for headless callers: no-op + hint when stdin is
    not a TTY.

    *work_assignment_id*: the WORK assignment being tested (passed as
    ``smoke_of`` at dispatch time).  The test verdict is ALWAYS recorded on
    the work row, not the smoke session row.

    *smoke_assignment_id*: the smoke session assignment id (used for log
    messages only).

    Returns True when a verdict was successfully recorded.
    """
    # Context label for the log lines below — identifies which repo/issue/
    # machine/smoke-session this backstop is acting on (#923 review nit: these
    # params were previously accepted but never read).
    _ctx = (
        f"{repo_name} ({repo_github}) issue #{issue_number} on {machine_name} "
        f"[smoke={smoke_assignment_id}]"
    )

    # ── Idempotency gate ──────────────────────────────────────────────────────
    # Read the WORK row (not the smoke session) from the board.  If it already
    # has test_state set the agent self-reported — do nothing.
    try:
        from coord.board_service import read_board as _read_board_tv  # noqa: PLC0415

        _board = _read_board_tv()
        _work = _board.find_by_id(work_assignment_id)
        if _work is not None and (_work.test_state or "").strip():
            click.echo(
                f"  test verdict already recorded: {_work.test_state!r} for "
                f"{_ctx} (agent used `coord test` — no operator prompt needed)"
            )
            return True
    except Exception:  # noqa: BLE001 — board unavailable → fall through to prompt
        pass

    # ── Non-TTY path ──────────────────────────────────────────────────────────
    if not sys.stdin.isatty():
        click.echo(
            f"  no test verdict recorded for {_ctx} — record it with:\n"
            f"{verdict_cmd_hint}"
        )
        return False

    # ── TTY prompt ────────────────────────────────────────────────────────────
    ans = click.prompt(
        "  Test verdict — [p]assed / [f]ailed / [s]kip",
        type=click.Choice(["p", "f", "s"], case_sensitive=False),
        default="s",
        show_choices=True,
    )
    if ans.lower() == "s":
        click.echo(
            f"  skipped — record the test verdict later with:\n{verdict_cmd_hint}"
        )
        return False

    test_state = {"p": "passed", "f": "failed"}[ans.lower()]
    reason: str = ""
    if test_state == "failed":
        reason = click.prompt(
            "  failure reason (what was checked, expected vs actual, repro "
            "steps, suspected files — this IS the fix worker's brief)",
            default="",
            show_default=False,
        ).strip()

    # ── Relay via daemon-routed record_test_verdict ────────────────────────────
    try:
        from coord.state import record_test_verdict as _record_tv  # noqa: PLC0415

        _record_tv(
            assignment_id=work_assignment_id,
            test_state=test_state,
            test_reason=reason if reason else None,
            # Mirror to legacy smoke_test columns (pipeline.py reads both).
            smoke_test="pass" if test_state == "passed" else "fail",
            smoke_test_reason=reason if test_state == "failed" and reason else None,
        )
        click.echo(
            f"  test verdict '{test_state}' recorded for work assignment "
            f"{work_assignment_id} ({_ctx})."
        )
        return True
    except Exception as exc:  # noqa: BLE001 — best-effort; fall back to the hint
        click.echo(
            f"  warning: failed to record test verdict for {_ctx}: {exc}\n"
            f"{verdict_cmd_hint}",
            err=True,
        )
        return False


@click.command(
    "report-result",
    help=(
        "Report the outcome of an interactive session through the "
        "coordinator's issue_store seam (#466). "
        "REQUIRED for review sessions where the verdict can only come "
        "from the agent."
    ),
)


@click.option(
    "--assignment", "assignment_id_opt", default=None,
    help="The assignment id (defaults to $COORD_ASSIGNMENT_ID).",
)


@click.option(
    "--status",
    type=click.Choice(["done", "blocked", "already-implemented"]),
    required=True,
    help=(
        "Terminal result: `done` = work landed; `blocked` = cannot proceed; "
        "`already-implemented` = nothing to do (advisory)."
    ),
)


@click.option(
    "--verdict",
    type=click.Choice(["approve", "request-changes"]),
    default=None,
    help=(
        "Review verdict — only meaningful for review sessions where no "
        "commits are pushed. Recorded so the merge-gate sees the same "
        "field a claude-p reviewer would have populated."
    ),
)


@click.option(
    "--summary", default="",
    help="One-paragraph summary posted on the issue under the result.",
)


@click.option(
    "--body-file", "body_file",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help=(
        "Path to a file with the FULL findings body (markdown). For a REVIEW "
        "session, write your complete review here and pass this — it is persisted "
        "on the assignment AND posted to the issue under a machine-parseable "
        "marker, so the fix worker is briefed with the actual findings (from any "
        "machine, via the GitHub message bus), not just the one-line --summary. "
        "REQUIRED with `--verdict request-changes` (#580)."
    ),
)


@click.option(
    "--body", "body_inline", default=None,
    help=(
        "Inline alternative to --body-file (the full findings body as a string, "
        "e.g. --body \"$(cat findings.md)\"). One of --body/--body-file is "
        "required with `--verdict request-changes`."
    ),
)


@click.option(
    "--audit-json", "audit_json_file",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help=(
        "Milestone Outcome Audit (#886 Phase 2) ONLY — path to a JSON file "
        "with the structured verdict: "
        '{"bottom_line": "...", "goals": [{"goal": "...", "metric_before": '
        '"...", "metric_after": "...", "verdict": "met|partial|gap", '
        '"evidence": "..."}]}. Routes the result through the audit dual-write '
        "path (assignment row + epic comment + #603 context store) instead of "
        "the generic done-comment body. Only valid on a --audit-of assignment."
    ),
)


@click.option(
    "--force",
    is_flag=True,
    default=False,
    help=(
        "Confirm overwriting already-captured review findings for this "
        "assignment (#650). Without it, a --verdict request-changes/--body "
        "write that would replace non-empty existing review_findings with a "
        "different body is refused — a second capture for the same "
        "assignment is, by construction, a re-run (finishing the exit "
        "process twice, a stray reattach), never a legitimate new review."
    ),
)


@_CONFIG_OPTION
def report_result(
    assignment_id_opt: str | None,
    status: str,
    verdict: str | None,
    summary: str,
    body_file: str | None,
    body_inline: str | None,
    audit_json_file: str | None,
    force: bool,
    config_path: Path,
) -> None:
    """``coord report-result --assignment <id> --status <s> [--verdict <v>] --summary <text>``

    The single coordinator-mediated command an interactive Claude
    session may invoke before it exits.  Writes the outcome through the
    :mod:`coord.issue_store` seam (same path the git-floor backstop
    uses), so the GitHub message bus and the local DB see a
    structurally-identical completion regardless of which mechanism
    produced it.
    """
    import os as _os  # noqa: PLC0415

    from coord import issue_store  # noqa: PLC0415
    from coord.client import resolve_board_service  # noqa: PLC0415

    assignment_id = assignment_id_opt or _os.environ.get("COORD_ASSIGNMENT_ID")
    if not assignment_id:
        click.echo(
            "error: --assignment is required (or set $COORD_ASSIGNMENT_ID)",
            err=True,
        )
        sys.exit(2)

    # #886 Phase 2: Milestone Outcome Audit structured verdict. Parsed and
    # validated FIRST — before any board/config resolution — so an obvious
    # malformed-JSON typo fails fast without needing a reachable daemon/config
    # (issue_store._validate_result re-validates server-side too, the one seam
    # every caller funnels through, but this saves the agent a round trip).
    audit_goals: list[dict] | None = None
    audit_bottom_line: str | None = None
    if audit_json_file:
        import json as _json  # noqa: PLC0415

        try:
            audit_payload = _json.loads(Path(audit_json_file).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            click.echo(
                f"error: could not read/parse --audit-json {audit_json_file!r}: {exc}",
                err=True,
            )
            sys.exit(2)
        if not isinstance(audit_payload, dict) or not isinstance(
            audit_payload.get("goals"), list
        ):
            click.echo(
                "error: --audit-json must be a JSON object with a 'goals' list, "
                'e.g. {"bottom_line": "...", "goals": [{"goal": "...", '
                '"metric_before": "...", "metric_after": "...", "verdict": '
                '"met|partial|gap", "evidence": "..."}]}',
                err=True,
            )
            sys.exit(2)
        audit_goals = audit_payload["goals"]
        audit_bottom_line = audit_payload.get("bottom_line") or None
        for _goal in audit_goals:
            _verdict = _goal.get("verdict") if isinstance(_goal, dict) else None
            if _verdict not in ("met", "partial", "gap"):
                click.echo(
                    f"error: --audit-json goal {_goal!r} has invalid verdict "
                    f"{_verdict!r} (expected one of 'met'/'partial'/'gap')",
                    err=True,
                )
                sys.exit(2)

    repo_github: str | None = None
    repo_name: str | None = None
    machine_name: str | None = None
    issue_number: int | None = None
    branch: str | None = None

    svc = resolve_board_service()
    prefetch_failed = False
    if svc is not None:
        # Thin client (#590): no local DB/config — resolve the assignment's
        # identity from the daemon, then let issue_store.post_result route the
        # write back to the daemon's shared DB.  This is what lets a remote
        # interactive session self-report instead of the old "do NOT run
        # report-result" workaround.
        #
        # #1336: this prefetch is an *enrichment*, not a prerequisite — the
        # verdict write MUST NOT be discarded because a read was slow.  It
        # uses the point endpoint (GET /assignment/{id}; falls back to the
        # full /board payload only for a pre-#1336 daemon), gets the write
        # timeout (it rides a write operation), and on ANY failure we warn and
        # proceed with the POST: the daemon owns the DB and resolves the
        # identity fields server-side.
        from coord.client import (  # noqa: PLC0415
            _WRITE_TIMEOUT,
            fetch_assignment,
            fetch_board_payload,
        )

        row = None
        try:
            row = fetch_assignment(svc, assignment_id, timeout=_WRITE_TIMEOUT)
            if row is None:
                # 404: unknown id — or a daemon that predates the point
                # endpoint (an unmatched route is also a 404).  One
                # compatibility fallback through the collection payload.
                payload = fetch_board_payload(svc, timeout=_WRITE_TIMEOUT)
                row = next(
                    (
                        a
                        for a in payload.get("assignments", [])
                        if a.get("assignment_id") == assignment_id
                    ),
                    None,
                )
        except Exception as exc:  # noqa: BLE001
            prefetch_failed = True
            click.echo(
                f"warning: identity prefetch from {svc.url} failed ({exc}).\n"
                "  This is a slow/failed BOARD READ, not a write failure — "
                "proceeding to record the verdict; the board service resolves "
                "the assignment's repo/machine/issue itself (#1336).",
                err=True,
            )
        if row is not None:
            repo_github = row.get("repo_github")
            repo_name = row.get("repo_name")
            machine_name = row.get("machine_name")
            issue_number = row.get("issue_number")
            branch = row.get("branch")
    else:
        from coord.state import build_board, load_dispatched  # noqa: PLC0415

        cfg = _load_config(config_path)

        # Look up the assignment metadata.  Prefer the dispatched ledger
        # because it always has repo_github, then fall back to the live
        # board for in-flight rows that haven't been queried elsewhere.
        record = next(
            (r for r in load_dispatched() if r.get("assignment_id") == assignment_id),
            None,
        )
        if record is not None:
            repo_github = record.get("repo_github")
            repo_name = record.get("repo_name")
            machine_name = record.get("machine_name")
            issue_number = record.get("issue_number")

        board = build_board()
        assignment_obj = board.find_by_id(assignment_id)
        if assignment_obj is not None:
            repo_name = repo_name or assignment_obj.repo_name
            machine_name = machine_name or assignment_obj.machine_name
            issue_number = issue_number or assignment_obj.issue_number
            branch = assignment_obj.branch
            if repo_github is None:
                repo_cfg = cfg.repo(assignment_obj.repo_name)
                if repo_cfg is not None:
                    repo_github = repo_cfg.github

        # Final fallback: if a config repo matches the recorded repo_name,
        # use its github slug.
        if repo_github is None and repo_name is not None:
            repo_cfg = cfg.repo(repo_name)
            if repo_cfg is not None:
                repo_github = repo_cfg.github

    if not (repo_github and repo_name and machine_name and issue_number):
        if svc is not None and prefetch_failed:
            # #1336 invariant 4: a failed READ must never discard the WRITE.
            # Post the record with blank identity — the daemon fills it in
            # from its own assignments row (`_enrich_result_identity`).
            click.echo(
                "  identity unresolved locally — the board service will "
                "resolve it from its own DB.",
                err=True,
            )
        else:
            click.echo(
                f"error: could not resolve assignment {assignment_id!r} from "
                "board/dispatched ledger; pass --assignment with a known id "
                "or run from the originating coordinator machine.",
                err=True,
            )
            sys.exit(1)

    findings_body: str | None = None
    if body_file:
        try:
            findings_body = Path(body_file).read_text(encoding="utf-8").strip() or None
        except OSError as exc:
            click.echo(
                f"warning: could not read --body-file {body_file!r}: {exc}",
                err=True,
            )
    if findings_body is None and body_inline and body_inline.strip():
        findings_body = body_inline.strip()

    # #580: a request-changes verdict MUST carry the reviewer's findings.
    # Recording it with only a one-line --summary silently discards the
    # objections, so the iteration-N+1 fix agent gets dispatched with nothing
    # to fix. Require the body (file or inline) and fail loudly otherwise.
    if verdict == "request-changes" and not findings_body:
        click.echo(
            "error: --verdict request-changes requires the review body — pass "
            "--body-file <path> (or --body \"<text>\") with your full findings "
            "(every blocking item, file:line). The one-line --summary is not "
            "enough; it's what the fix worker is briefed with.\n"
            "  Write your findings to a file and re-run, e.g.:\n"
            f"  coord report-result --assignment {assignment_id} --status done "
            "--verdict request-changes --summary <one-line> "
            f"--body-file /tmp/review-{assignment_id}.md",
            err=True,
        )
        sys.exit(2)

    # #949: push the worktree's commits BEFORE recording 'done' so completed
    # interactive work is never stranded.  report-result is the "done" path for
    # interactive fix/merge/work sessions; when a session declares done here
    # (rather than through finalize_interactive_exit, which is bypassed on a
    # detached/reattached tmux), nothing else pushes: the stale-session reaper
    # skips the already-'done' row, and the worker itself may have been told not
    # to push (e.g. a repo CLAUDE.md).  Without this, the commit sits unpushed in
    # ~/.coord/worktrees/<aid> and the next test/review agent tests stale code —
    # the confirmed root cause of #407 and #782.  Best-effort, non-force: it
    # fast-forwards real work and safely no-ops on a diverged/rebased branch (the
    # merge agent owns force-pushes).  Review sessions have no worktree, so the
    # .exists() guard skips them.
    if status == "done":
        from coord.interactive import _git_push  # noqa: PLC0415
        from coord.state import COORD_DIR  # noqa: PLC0415

        _wt = COORD_DIR / "worktrees" / assignment_id
        if _wt.exists():
            _pushed, _perr = _git_push(_wt)
            if _pushed:
                click.echo(f"  pushed {branch or 'HEAD'} to origin (#949)")
            else:
                click.echo(
                    f"  ⚠ WARNING: could not push commits for {assignment_id} "
                    f"to origin: {_perr}\n"
                    f"    work is committed but UNPUSHED in {_wt}\n"
                    f"    push manually:  git -C {_wt} push origin HEAD",
                    err=True,
                )

    record_obj = issue_store.ResultRecord(
        assignment_id=assignment_id,
        machine_name=machine_name or "",
        repo_name=repo_name or "",
        repo_github=repo_github or "",
        # 0 = "unresolved" (only reachable on the prefetch-failed thin-client
        # path above) — the daemon's identity enrichment treats falsy fields
        # as blanks to fill from its own assignments row.
        issue_number=int(issue_number) if issue_number else 0,
        status=status,  # type: ignore[arg-type]
        verdict=verdict,  # type: ignore[arg-type]
        summary=summary,
        branch=branch,
        findings_body=findings_body,
        audit_goals=audit_goals,
        audit_bottom_line=audit_bottom_line,
        allow_overwrite_findings=force,
    )
    try:
        outcome = issue_store.post_result(record_obj)
    except ValueError as exc:
        click.echo(f"error: {exc}", err=True)
        sys.exit(2)
    except RuntimeError as exc:
        # #886/#990: the verdict write couldn't be durably confirmed (retries
        # exhausted, or a readback mismatch) — surface this loudly instead of
        # reporting success while the merge-gate-critical review_verdict (or
        # the audit run_number versioning invariant) column never actually
        # landed. Re-running the identical command is the recovery path.
        # (A #650 clobber-guard refusal is reported below via
        # `outcome.findings_written`, not as an exception — the verdict
        # itself still lands cleanly in that case.)
        click.echo(f"error: {exc}", err=True)
        sys.exit(1)

    click.echo(
        f"result recorded: status={outcome.status} event={outcome.event} "
        f"posted_to_github={outcome.posted}"
    )
    if not outcome.findings_written:
        click.echo(
            "  note: review findings were NOT overwritten (#650 clobber "
            "guard) — non-empty findings already existed for this "
            "assignment and the stored verdict already matches; the "
            "previously captured findings are preserved. Re-run with "
            "--force if this overwrite was intentional."
        )
    if outcome.error:
        click.echo(f"  github post warning: {outcome.error}", err=True)


@click.command(
    "set-review-findings",
    help=(
        "Write review findings to the DB for a completed review assignment (#587). "
        "Used by the TUI rework dialog so the fix worker is briefed with the "
        "reviewer's feedback even when the review ran as a human-attended "
        "claude-pty session (which produces no parseable log)."
    ),
)


@click.argument("assignment_id")
@click.option(
    "--findings",
    required=True,
    help=(
        "The reviewer's findings, in plain text or markdown. Written as the "
        "REVIEW_BODY so `_load_review_findings` can serve it from the DB "
        "cache on the next `coord assign --fix-of` dispatch."
    ),
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help=(
        "Confirm overwriting already-captured, non-empty review findings "
        "for this assignment (#650) — the TUI rework dialog's write is "
        "otherwise refused when the row already carries a different "
        "findings body, so a re-run of the dialog can't silently stomp a "
        "good capture with a placeholder."
    ),
)


@_CONFIG_OPTION
def set_review_findings(
    assignment_id: str,
    findings: str,
    force: bool,
    config_path: Path,
) -> None:
    """``coord set-review-findings <id> --findings <text>``

    Persist review findings for a human-attended (claude-pty) review whose
    verdict was already recorded via ``coord report-result --verdict
    request-changes``.  The DB cache written here is the first source
    ``_load_review_findings`` checks, so the subsequent ``coord assign
    --fix-of`` dispatch will read it and brief the fix worker correctly
    instead of emitting the "(No structured findings were captured)" fallback.

    #650: refuses to replace already-captured, non-empty findings with a
    different body unless ``--force`` is passed — a single assignment backs
    exactly one review, so a second, differing write here is a re-run of the
    dialog, never a legitimate new review.
    """
    from coord.state import update_assignment_review_findings  # noqa: PLC0415

    findings_text = findings.strip()
    if not findings_text:
        click.echo("error: --findings must not be empty", err=True)
        sys.exit(2)

    written = update_assignment_review_findings(
        assignment_id,
        verdict="request-changes",
        body=findings_text,
        allow_overwrite=force,
    )
    if not written:
        click.echo(
            f"refused: assignment {assignment_id} already has different, "
            "non-empty review findings recorded (#650 clobber guard) — "
            "re-run with --force to overwrite them.",
            err=True,
        )
        sys.exit(1)
    click.echo(f"findings recorded for {assignment_id}")


@click.command("fix-briefing")
@click.argument("aid")
@_CONFIG_OPTION
def fix_briefing_cmd(aid: str, config_path: Path) -> None:
    """Print the briefing a `--fix-of <aid>` fix worker would receive — the
    per-issue context block + the resolved findings / test-failure story (#603).

    coord-tui shells out to this to preview the fix in the fail→fix / rework
    confirm dialog so the operator sees exactly what the worker is briefed with
    before launching.  Output is the briefing text ONLY (stdout).  AID is either
    a request-changes REVIEW id or a test-failed WORK id (mirrors --fix-of).
    """
    from types import SimpleNamespace

    from coord.auto_loop import _build_fix_briefing, _load_review_findings
    from coord.board_service import read_board
    from coord.state import COORD_DIR as _CTX_COORD_DIR, issue_context_block

    cfg = _load_config(config_path)
    board = read_board()
    target = board.find_by_id(aid)
    if target is None:
        click.echo(f"error: no assignment {aid} on the board.", err=True)
        sys.exit(2)

    # Mirror the --fix-of fork (cli.py): a test-failed WORK id fixes itself
    # (findings = test_reason); a request-changes REVIEW id fixes its linked work.
    fix_from_test_fail = (
        target.type != "review" and getattr(target, "test_state", None) == "failed"
    )
    if fix_from_test_fail:
        work = target
    elif target.type == "review":
        work = (
            board.find_by_id(target.review_of_assignment_id)
            if target.review_of_assignment_id else None
        )
    else:
        click.echo(
            f"error: {aid} is not a fixable target "
            f"(type={target.type!r}, test_state={getattr(target, 'test_state', None)!r}).",
            err=True,
        )
        sys.exit(2)
    if work is None or not work.branch:
        click.echo("error: no linked work assignment with a branch to fix.", err=True)
        sys.exit(2)

    repo_cfg = cfg.repo(work.repo_name)
    repo_github = repo_cfg.github if repo_cfg else work.repo_name
    next_iteration = (work.review_iteration or 0) + 1
    max_iter = cfg.pipeline.max_review_iterations
    if fix_from_test_fail:
        story = (getattr(work, "test_reason", None) or "").strip()
        findings_body = (
            "The manual smoke test FAILED. The operator reported:\n\n"
            f"> {story}\n\nReproduce the failure, fix the root cause, and "
            "re-validate before pushing."
            if story else
            "The manual smoke test FAILED (no reason text was recorded). Pull the "
            "branch, reproduce the failure the operator hit, and fix the root "
            "cause before pushing."
        )
    else:
        _log = _CTX_COORD_DIR / "logs" / f"{aid}.log"
        try:
            findings = _load_review_findings(
                target, str(_log) if _log.exists() else None, None,
                repo_github=repo_github,
            )
        except Exception:  # noqa: BLE001
            findings = None
        findings_body = (
            findings.body.strip()
            if findings and (getattr(findings, "body", "") or "").strip()
            else (
                f"(No structured findings were captured for review {aid}.) "
                f"The review verdict was {target.review_verdict or 'request-changes'!r}. "
                "Read the reviewer's feedback and address every blocking item "
                "before pushing."
            )
        )
    fix_briefing = _build_fix_briefing(
        work, SimpleNamespace(body=findings_body), next_iteration, max_iter
    )
    ctx = issue_context_block(work.repo_name, work.issue_number)
    click.echo(ctx + fix_briefing, nl=False)