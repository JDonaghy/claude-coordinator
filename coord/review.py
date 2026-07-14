"""Adversarial code review — dispatch an independent reviewer when a worker finishes.

When `reviews.auto_dispatch` is enabled in `coordinator.yml`, completion of a
"work" assignment triggers a fresh `claude -p` session on a *different* machine
that reads the diff, runs tests, and posts a `gh pr review`. The reviewer has
zero shared context with the worker — that's the whole point.

Public entry points:

- `pick_reviewer_machine(...)`  — choose an idle machine different from the
  worker, with a single-machine fallback.
- `build_review_briefing(...)`  — assemble the reviewer's prompt from the
  repo's CLAUDE.md, the generic checklist, and any repo-specific overrides.
- `dispatch_review(...)`        — full path: find/open PR, pick reviewer,
  build briefing, send to agent server, add a review `Assignment` to the
  board. Called from reconcile when a work assignment transitions to done.

Why a separate module: the work-dispatch path (`coord/dispatch.py`) is shaped
around `Proposal` objects from the brain. Reviews are triggered by completion
events on the board and target an existing PR, so they share little of that
plumbing — keeping them apart avoids twisting both shapes.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from typing import Iterable
from dataclasses import dataclass
from pathlib import Path

import httpx

from coord import github_ops
from coord.config import Config, ReviewsConfig
from coord.dispatch import AGENT_PORT
from coord.models import CLOSES_ISSUE_TYPES, WORK_LIKE_TYPES, Assignment, Board, Machine

log = logging.getLogger(__name__)


# ── Review output parsing ────────────────────────────────────────────────────

@dataclass
class ReviewFindings:
    """Structured review output extracted from a reviewer worker log."""
    verdict: str  # "approve" or "request-changes"
    body: str


# Matches the structured block the reviewer is instructed to emit at end of session.
# Allows optional leading/trailing whitespace and tolerates both LF and CRLF.
# Accepts canonical verdicts (approve / request-changes) and short aliases
# (PASS → approve, FAIL → request-changes) for workers that use the shorter form.
#
# The `REVIEW_BODY:` marker is OPTIONAL (#608): reviewers commonly emit the
# verdict line followed directly by Markdown findings and `END_REVIEW`, omitting
# the `REVIEW_BODY:` header. When it's absent the body is everything between the
# verdict line and `END_REVIEW`. `END_REVIEW` stays the required terminator, so a
# stray "REVIEW_VERDICT:" in prose (with no terminator) still won't match.
_REVIEW_BLOCK_RE = re.compile(
    r"REVIEW_VERDICT:\s*(approve|request-changes|pass|fail)\s*[\r\n]+"
    r"(?:REVIEW_BODY:\s*[\r\n]+)?(.*?)[\r\n]*END_REVIEW",
    re.DOTALL | re.IGNORECASE,
)

# Map short-form aliases to the canonical verdicts understood by post_pr_review.
_VERDICT_ALIASES: dict[str, str] = {
    "pass": "approve",
    "fail": "request-changes",
}


# ── #248: machine-readable review header ────────────────────────────────────
#
# When the coordinator posts a review comment back to GitHub it prepends a
# short HTML comment carrying the verdict in machine-readable form.  The
# header is invisible to humans on the PR but lets the TUI render a verdict
# badge and lets the coordinator session check the verdict without reading
# the full prose body (which can be several KB).
#
# Format:
#     <!-- coord:review verdict=request-changes blocking=2 nonblocking=5 \
#          nits=2 reviewer=elitebook assignment=144ffa027a31 -->
#
# `verdict` is always present.  Counts are best-effort: when the prose
# body uses recognisable section headings, the coordinator counts items
# under each; when it can't, those tokens are omitted (parser tolerates
# missing tokens).
_REVIEW_HEADER_RE = re.compile(
    r"<!--\s*coord:review\s+([^>]+?)\s*-->",
    re.IGNORECASE,
)

# Maps human section-heading keywords (case-insensitive) to the count
# category they belong to.  The heuristic walks the prose body, splits
# on markdown headings, and bucketises bullet-list items under each.
_SECTION_KEYWORDS: dict[str, tuple[str, ...]] = {
    "blocking": ("blocking", "required change", "must fix", "must-fix",
                 "changes required"),
    "nonblocking": ("non-blocking", "non blocking", "concerns",
                    "should fix", "should-fix", "observations"),
    "nits": ("nits", "nit:", "polish", "minor", "style"),
}


def format_review_header(
    *,
    verdict: str,
    reviewer_machine: str | None = None,
    assignment_id: str | None = None,
    blocking: int | None = None,
    nonblocking: int | None = None,
    nits: int | None = None,
) -> str:
    """Build the HTML-comment header that machines parse.

    `verdict` is required; everything else is optional and only emitted
    when provided.  Returns a single line (no trailing newline).
    """
    parts = [f"verdict={verdict}"]
    if blocking is not None:
        parts.append(f"blocking={blocking}")
    if nonblocking is not None:
        parts.append(f"nonblocking={nonblocking}")
    if nits is not None:
        parts.append(f"nits={nits}")
    if reviewer_machine:
        parts.append(f"reviewer={reviewer_machine}")
    if assignment_id:
        parts.append(f"assignment={assignment_id}")
    return f"<!-- coord:review {' '.join(parts)} -->"


def parse_review_header(body: str) -> dict[str, str | int] | None:
    """Extract the coord:review header from *body*, or ``None`` when missing.

    Numeric tokens (``blocking``, ``nonblocking``, ``nits``) are returned as
    ``int``; everything else stays a ``str``.  Tolerates extra whitespace
    and unknown tokens.
    """
    m = _REVIEW_HEADER_RE.search(body)
    if not m:
        return None
    out: dict[str, str | int] = {}
    for token in m.group(1).split():
        if "=" not in token:
            continue
        key, value = token.split("=", 1)
        key = key.lower()
        if key in ("blocking", "nonblocking", "nits"):
            try:
                out[key] = int(value)
            except ValueError:
                continue
        else:
            out[key] = value
    return out if "verdict" in out else None


def estimate_review_counts(
    body: str,
) -> tuple[int | None, int | None, int | None]:
    """Best-effort count of (blocking, nonblocking, nits) bullets in *body*.

    Walks markdown sections.  A section is recognised when its heading
    contains one of `_SECTION_KEYWORDS`; counts are the number of `- ` /
    `* ` / `1. ` bullets directly under that section (until the next
    heading).  Returns ``(None, None, None)`` when no recognised
    sections appear — the heuristic refuses to guess.
    """
    counts: dict[str, int | None] = {"blocking": None, "nonblocking": None, "nits": None}
    current: str | None = None
    bullet_re = re.compile(r"^\s*(?:[-*]|\d+\.)\s+\S")
    # Check buckets in order of keyword specificity so that "Non-blocking
    # concerns" doesn't accidentally match the `blocking` bucket first.
    ordered_buckets = ("nonblocking", "nits", "blocking")

    for raw in body.splitlines():
        line = raw.rstrip()
        if line.startswith("#"):
            # Found a heading — figure out which bucket (if any) it maps to.
            heading_text = line.lstrip("#").strip().lower()
            current = None
            for bucket in ordered_buckets:
                if any(kw in heading_text for kw in _SECTION_KEYWORDS[bucket]):
                    current = bucket
                    # Initialise the count for this bucket so it shows as 0
                    # (not None) even when the section is empty.
                    if counts[current] is None:
                        counts[current] = 0
                    break
            continue
        if current is not None and bullet_re.match(line):
            counts[current] = (counts[current] or 0) + 1

    return counts["blocking"], counts["nonblocking"], counts["nits"]


def _parse_review_text(text: str) -> ReviewFindings | None:
    """Extract the last ReviewFindings block from *text*, or None."""
    matches = list(_REVIEW_BLOCK_RE.finditer(text))
    if not matches:
        return None
    m = matches[-1]
    verdict_raw = m.group(1).lower().strip()
    # Normalize aliases: PASS → approve, FAIL → request-changes.
    verdict = _VERDICT_ALIASES.get(verdict_raw, verdict_raw)
    body = m.group(2).strip()
    if verdict not in ("approve", "request-changes"):
        return None
    return ReviewFindings(verdict=verdict, body=body)


def _parse_review_from_lines(
    lines: Iterable[str],
    *,
    stream_json: bool,
) -> ReviewFindings | None:
    """Shared core: extract review findings from log lines.

    `lines` may be any iterable of strings (file iterator, ``str.splitlines()``,
    ``httpx.Response.text.splitlines()``). Used by both `parse_review_from_log`
    (local file) and `parse_review_from_agent` (HTTP fetch).
    """
    from coord.worker_events import _assistant_text, parse_event  # noqa: PLC0415

    if not stream_json:
        text = "\n".join(lines)
        return _parse_review_text(text)

    all_texts: list[str] = []
    for line in lines:
        event = parse_event(line.rstrip("\n"))
        if event is None:
            continue
        if event.type == "assistant":
            text = _assistant_text(event)
            if text:
                all_texts.append(text)
    # Search from the end — the reviewer emits the verdict last.
    for text in reversed(all_texts):
        findings = _parse_review_text(text)
        if findings is not None:
            return findings
    # Fallback: search the full concatenated text (handles multi-turn output).
    return _parse_review_text("\n".join(all_texts))


def parse_review_from_log(log_path: str | Path) -> ReviewFindings | None:
    """Parse review findings from a completed reviewer worker log.

    Handles both stream-json (``--output-format stream-json``) and plain-text
    log formats. Returns ``None`` if the file does not exist or contains no
    structured review output.
    """
    from coord.worker_events import is_stream_json  # noqa: PLC0415

    p = Path(log_path)
    if not p.exists():
        return None

    if is_stream_json(p):
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                return _parse_review_from_lines(f, stream_json=True)
        except OSError:
            return None
    else:
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        return _parse_review_from_lines(text.splitlines(), stream_json=False)


def parse_review_from_agent(
    host: str,
    assignment_id: str,
    port: int = 7433,
    timeout: float = 15.0,
) -> ReviewFindings | None:
    """Fetch a reviewer worker's log via the agent's ``/logs/<id>`` endpoint
    and parse the verdict.

    Use this instead of `parse_review_from_log` when the worker ran on a
    remote agent and the log file isn't on the coordinator's local
    filesystem. Returns ``None`` on network failure, empty log, or no
    structured review output.
    """
    import httpx  # noqa: PLC0415

    url = f"http://{host}:{port}/logs/{assignment_id}"
    try:
        resp = httpx.get(url, timeout=timeout)
        resp.raise_for_status()
        text = resp.text
    except (httpx.HTTPError, httpx.TimeoutException):
        return None
    if not text:
        return None
    lines = text.splitlines()
    # Detect format the same way `is_stream_json` does for files: the first
    # non-comment, non-blank line starts with `{`.
    stream_json = False
    for line in lines:
        stripped = line.strip()
        if not stripped or line.startswith("#"):
            continue
        stream_json = stripped.startswith("{")
        break
    return _parse_review_from_lines(lines, stream_json=stream_json)


def fetch_review_findings_from_github(
    repo_github: str,
    issue_number: int,
    assignment_id: str,
) -> ReviewFindings | None:
    """Recover a review's findings from the GitHub message bus.

    Interactive (claude-pty) reviews don't produce a parseable log; their full
    body is instead posted to the issue under a `coord:review-findings` marker
    by `report-result --body-file` (via the issue_store seam).  This reads those
    comments back, so a fix worker on ANY machine can recover the findings even
    when the review ran elsewhere and isn't in the local DB — GitHub is the one
    store every machine already reaches.  Returns ``None`` on any failure.
    """
    import json as _json  # noqa: PLC0415
    import subprocess as _sp  # noqa: PLC0415

    from coord.comments import extract_findings_block  # noqa: PLC0415

    if not (repo_github and assignment_id):
        return None
    try:
        out = _sp.run(
            ["gh", "issue", "view", str(issue_number), "--repo", repo_github,
             "--json", "comments"],
            capture_output=True, text=True, timeout=20,
        )
        if out.returncode != 0:
            return None
        comments = _json.loads(out.stdout or "{}").get("comments", [])
    except (_sp.TimeoutExpired, OSError, ValueError):
        return None
    # Newest-first so a re-review's findings win over an earlier iteration's.
    for c in reversed(comments):
        hit = extract_findings_block(c.get("body", ""), assignment_id)
        if hit is not None:
            verdict, body = hit
            return ReviewFindings(verdict=verdict or "request-changes", body=body)
    return None


REVIEWER_SYSTEM_PROMPT = """\
You are an independent code reviewer dispatched by the coordinator. \
Your job is to find problems — do NOT rubber-stamp.

Rules:
- You have a fresh session. You have NO context from the worker who wrote \
this code. Treat the diff as if you're reading it for the first time.
- You are NOT allowed to run any `gh` commands. The coordinator posts the \
review on your behalf after your session ends.
- DO NOT run the project's test suite, build, or any other command — a human \
reviewer reads the diff, they don't run the suite, and on some projects (e.g. \
headless GUI apps) running it hangs the session. You MAY read project files \
for context. Build/test validation is the separate pre-merge smoke gate's job.
- You are NOT allowed to push commits or modify the PR's code. You only \
review.

How to review:
1. Read the project's CLAUDE.md for project conventions.
2. Read the PR diff using `git diff` or the briefing instructions.
3. Check the diff against the review checklist in your briefing.
4. For each finding, cite the specific file:line and the rule it violates.
5. At the END of your session, output your verdict in this exact format:

REVIEW_VERDICT: approve
REVIEW_BODY:
<your full review text in markdown>
END_REVIEW

Or for requesting changes:

REVIEW_VERDICT: request-changes
REVIEW_BODY:
<your full review text in markdown>
END_REVIEW

If the diff is clean, approve — but be thorough first.\
"""


# ── Machine selection ───────────────────────────────────────────────────────

@dataclass
class ReviewerChoice:
    machine: Machine
    same_as_worker: bool
    rationale: str


def pick_reviewer_machine(
    worker_machine_name: str,
    repo_name: str,
    board: Board,
    config: Config,
) -> ReviewerChoice | None:
    """Pick a reviewer machine — different from the worker if possible.

    Independence comes from a fresh session with no shared context, not from
    physical machine separation, so a same-machine fallback still produces a
    useful review — but we warn the caller via `same_as_worker=True`.

    Returns None when no machine can handle this repo.
    """
    from coord.machine_pause import paused_set
    paused = paused_set()
    candidates = [
        m for m in config.machines
        if m.can_work_on(repo_name) and m.name not in paused
    ]
    if not candidates:
        return None

    busy = {a.machine_name for a in board.active if a.status in ("pending", "running")}

    different = [
        m for m in candidates
        if m.name != worker_machine_name and m.name not in busy
    ]
    if different:
        return ReviewerChoice(
            machine=different[0],
            same_as_worker=False,
            rationale=(
                f"chose {different[0].name} — different machine from worker "
                f"({worker_machine_name})"
            ),
        )

    # Fallback 1: any different machine, even if busy.
    different_busy = [m for m in candidates if m.name != worker_machine_name]
    if different_busy:
        return ReviewerChoice(
            machine=different_busy[0],
            same_as_worker=False,
            rationale=(
                f"chose {different_busy[0].name} — different machine from "
                f"worker, currently busy (review will queue)"
            ),
        )

    # Fallback 2: same machine (only one available). Reduced independence.
    same = next((m for m in candidates if m.name == worker_machine_name), None)
    if same is None:
        return None
    return ReviewerChoice(
        machine=same,
        same_as_worker=True,
        rationale=(
            f"only {worker_machine_name} can handle {repo_name}; using same "
            f"machine — reviewer session is fresh but not on separate hardware"
        ),
    )


def _ranked_reviewer_candidates(
    worker_machine_name: str,
    repo_name: str,
    board: Board,
    config: Config,
) -> list[tuple[Machine, bool]]:
    """Return **all** candidate reviewer machines in priority order.

    Each element is ``(machine, same_as_worker)``.  Priority mirrors
    ``pick_reviewer_machine``:

    1. Different from the worker, currently **idle** — best independence, no
       queue delay.
    2. Different from the worker, currently **busy** — independence preserved;
       the review will queue on that agent.
    3. **Same** machine as the worker — last resort; fresh session but no
       hardware separation.

    Returns an empty list when no configured machine handles *repo_name*.
    Used by ``dispatch_review`` to iterate candidates instead of committing to
    a single pick, so a rejected agent (e.g. a 400 from config drift) can
    fall through to the next rather than silently failing (#904).
    """
    from coord.machine_pause import paused_set  # noqa: PLC0415

    paused = paused_set()
    candidates = [
        m for m in config.machines
        if m.can_work_on(repo_name) and m.name not in paused
    ]
    if not candidates:
        return []

    busy = {a.machine_name for a in board.active if a.status in ("pending", "running")}

    result: list[tuple[Machine, bool]] = []
    for m in candidates:
        if m.name != worker_machine_name and m.name not in busy:
            result.append((m, False))   # different + idle
    for m in candidates:
        if m.name != worker_machine_name and m.name in busy:
            result.append((m, False))   # different + busy (will queue)
    for m in candidates:
        if m.name == worker_machine_name:
            result.append((m, True))    # same machine — last resort
    return result


# ── Briefing construction ───────────────────────────────────────────────────

def _read_repo_claude_md(repo_path: Path) -> str | None:
    """Return the contents of CLAUDE.md at the repo root, or None.

    The coordinator runs on the machine that dispatches; the reviewer runs on
    its own machine and will re-read CLAUDE.md there. We embed the content
    here so the briefing is self-contained — if the reviewer's checkout is
    behind, the worker's diff still gets reviewed against the rules the
    coordinator thought were current.
    """
    candidate = repo_path / "CLAUDE.md"
    if not candidate.exists():
        return None
    try:
        return candidate.read_text()
    except OSError:
        return None


def _diff_touched_sealed_paths(diff_text: str, sealed_paths: list[str]) -> list[str]:
    """Return the sealed path prefixes actually touched by *diff_text*.

    Scans unified-diff file-header lines (``diff --git a/X b/Y``, ``---
    a/X``, ``+++ b/X``) for a path that starts with one of *sealed_paths* —
    cheap, dependency-free tamper detection (#944 sealing v1) ahead of a real
    diff parser. Pure function, easy to test.
    """
    touched: set[str] = set()
    for line in diff_text.splitlines():
        candidates: list[str] = []
        if line.startswith("diff --git "):
            for part in line.split()[1:]:
                if part.startswith("a/") or part.startswith("b/"):
                    candidates.append(part[2:])
        elif line.startswith("--- a/"):
            candidates.append(line[len("--- a/"):])
        elif line.startswith("+++ b/"):
            candidates.append(line[len("+++ b/"):])
        for c in candidates:
            for sealed in sealed_paths:
                if c.startswith(sealed):
                    touched.add(sealed)
    return sorted(touched)


def build_review_briefing(
    *,
    pr_number: int | None,
    pr_url: str | None,
    repo_github: str,
    repo_name: str,
    issue_number: int,
    issue_title: str,
    issue_body: str,
    branch: str | None,
    worker_machine: str,
    same_as_worker: bool,
    reviews_cfg: ReviewsConfig,
    repo_claude_md: str | None,
    default_branch: str = "main",
    review_iteration: int = 0,
    diff_text: str | None = None,
    sealed_paths: list[str] | None = None,
) -> str:
    """Assemble the reviewer's prompt. Pure function — easy to test.

    When *review_iteration* > 0 the work is a re-review of a fix worker's
    commits (a prior round requested changes). The "What to do" section is
    then scoped to the fix delta instead of the whole PR (#476): re-reviewing
    the entire PR every round repeats work, wastes tokens, and surfaces fresh
    non-blocking nits that bounce an already-correct PR into another fix cycle.

    When *diff_text* is non-empty (#612) the coordinator has already computed
    the merge-base (three-dot) diff and it is embedded verbatim, so the
    reviewer reviews exactly the branch's own changes — there is nothing for it
    to get wrong. A reviewer that deviates to a two-dot/stale-base diff would
    surface code merged to the default branch *after* the branch was cut as
    spurious deletions and flag it as a regression (#546). When *diff_text* is
    None the existing three-dot ``git diff`` fallback instructions stand.

    *sealed_paths* (#944, docs/ORACLE_LOOP.md sealing v1) lists path prefixes
    the worker must never touch — today just ``tests/acceptance/`` for repos
    with an oracle-loop acceptance driver configured. When non-empty a
    reviewer instruction is always appended; if *diff_text* is also given and
    actually touches one of the paths, a blocking "TAMPER DETECTED" banner is
    prepended instead of a soft reminder — this is the "reviewer flags any
    diff that touches tests/acceptance/**" tamper-detection policy.
    """

    lines: list[str] = []
    lines.append(f"# Review assignment: {repo_github} PR #{pr_number}")
    lines.append("")
    lines.append(f"You are reviewing the worker's work on issue #{issue_number}: {issue_title}")
    lines.append("")
    lines.append("## Context")
    lines.append(f"- Repo: {repo_github} (local name: {repo_name})")
    lines.append(f"- Branch: {branch or '(unknown)'}")
    if pr_url:
        lines.append(f"- PR URL: {pr_url}")
    lines.append(f"- Worker machine: {worker_machine}")
    if same_as_worker:
        lines.append(
            "- NOTE: only one machine is configured for this repo, so you are "
            "running on the same machine as the worker. Your session is still "
            "fresh (no shared context), but be extra rigorous."
        )
    lines.append("")

    lines.append("## Issue")
    lines.append(f"**#{issue_number}: {issue_title}**")
    if issue_body.strip():
        lines.append("")
        lines.append(issue_body.strip())
    lines.append("")

    if repo_claude_md:
        lines.append("## Project rules (from CLAUDE.md)")
        lines.append("")
        lines.append(repo_claude_md.strip())
        lines.append("")

    lines.append("## Review checklist")
    lines.append("")
    if reviews_cfg.checklist:
        for item in reviews_cfg.checklist:
            lines.append(f"- {item}")
    else:
        lines.append("- Does the diff actually solve issue #" + str(issue_number) + "?")
        lines.append("- Do tests pass? Any regressions?")
        lines.append("- Are there CLAUDE.md violations?")
        lines.append("- Did the worker stay within the assigned file scope?")
        lines.append("- Any security issues (injection, auth bypass, credential exposure)?")

    overrides = reviews_cfg.repo_overrides.get(repo_name, [])
    if overrides:
        lines.append("")
        lines.append(f"### Repo-specific focus ({repo_name})")
        for item in overrides:
            lines.append(f"- {item}")

    if reviews_cfg.reviewer_prompt.strip():
        lines.append("")
        lines.append("## Additional instructions")
        lines.append(reviews_cfg.reviewer_prompt.strip())

    if diff_text and diff_text.strip():
        # #612: embed the merge-base (three-dot) diff verbatim so the reviewer
        # has nothing to compute — a two-dot/stale-base diff would show
        # already-merged commits as spurious deletions (#546).
        lines.append("")
        lines.append("## Diff to review (authoritative)")
        lines.append(
            "This is the merge-base (three-dot) diff — exactly the branch's own "
            "changes, nothing else. Review THIS. Do NOT compute your own diff; a "
            "two-dot or stale-base diff would show unrelated already-merged "
            "commits as spurious deletions."
        )
        lines.append("")
        lines.append("```diff")
        lines.append(diff_text.strip())
        lines.append("```")

    if sealed_paths:
        touched = _diff_touched_sealed_paths(diff_text, sealed_paths) if diff_text else []
        lines.append("")
        if touched:
            lines.append("## \U0001f6a8 SEALED ORACLE TAMPER DETECTED")
            lines.append("")
            lines.append(
                "The diff modifies a path SEALED by this repo's acceptance "
                "oracle (docs/ORACLE_LOOP.md sealing v1): "
                + ", ".join(f"`{p}`" for p in touched)
                + ". The suite under these paths is authored independently — "
                "workers may only RUN it (`coord acceptance run`), never read "
                "or edit it. **request-changes is mandatory here**, regardless "
                "of anything else in this diff."
            )
        else:
            lines.append("## Sealed paths (do not touch)")
            lines.append("")
            lines.append(
                "This repo's acceptance oracle is sealed by policy: "
                + ", ".join(f"`{p}`" for p in sealed_paths)
                + ". If the diff modifies any of them, **request-changes** — "
                "this is a hard rule, not a suggestion (docs/ORACLE_LOOP.md)."
            )

    lines.append("")
    lines.append("## What to do")
    lines.append("")
    if review_iteration > 0:
        # #476: re-review. A prior round requested changes and the worker
        # pushed fix commits. Scope to the fix delta — do NOT re-review the
        # whole PR from scratch, and do NOT raise NEW non-blocking nits on
        # already-accepted code. Only a genuine bug or an unaddressed
        # previously-requested change should block.
        lines.append(
            f"**This is re-review iteration {review_iteration}.** A previous "
            "review requested changes and the worker has pushed fix commits "
            "since then. Scope your review to those fixes — do NOT re-review "
            "the entire PR from scratch."
        )
        lines.append("")
        lines.append(
            "1. See what changed since the last review: "
            f"`git fetch origin && git log --oneline origin/{default_branch}..."
            f"origin/{branch or 'HEAD'}`. The most recent commit(s) are the fix "
            "for the last review round — concentrate there."
        )
        lines.append(
            "2. Verify the previously-requested changes were correctly made and "
            "that the fix commits introduce no regressions."
        )
        lines.append(
            "3. **Do NOT raise new non-blocking nits on unchanged, "
            "already-reviewed code.** Block (`request-changes`) ONLY for a "
            "genuine bug or a previously-requested change that was not "
            "addressed. If the fix is correct and you only have minor polish "
            "suggestions, **approve** and list them as non-blocking notes — "
            "the coordinator will not dispatch another fix round for "
            "non-blocking findings."
        )
    elif pr_number is not None:
        if diff_text and diff_text.strip():
            lines.append(
                "1. Review the diff in the '## Diff to review' section above "
                "(already fetched for you — the merge-base diff)."
            )
        else:
            lines.append(
                f"1. Get the diff: `git fetch origin && git diff origin/{default_branch}..."
                f"origin/{branch or 'HEAD'}` or ask the coordinator for the diff."
            )
        lines.append("2. Run the project's test suite.")
        lines.append("3. Review the diff against the checklist above.")
    else:
        if diff_text and diff_text.strip():
            lines.append(
                "1. Review the diff in the '## Diff to review' section above "
                "(already fetched for you — the merge-base diff)."
            )
        else:
            lines.append(
                f"1. The worker pushed branch `{branch}` but no PR was opened. "
                f"Get the diff: `git fetch origin && git diff origin/{default_branch}..."
                f"origin/{branch or '<branch>'}`. Always diff against `origin/` after "
                "fetching — a local base ref may be stale and would sweep in unrelated "
                "already-merged commits."
            )
        lines.append("2. Run the project's test suite.")
        lines.append("3. Review the diff against the checklist above.")
    lines.append("")
    lines.append(
        "4. At the END of your session, output your findings in this exact format "
        "(the coordinator will post the review to GitHub on your behalf — "
        "do NOT run any `gh` commands):"
    )
    lines.append("")
    lines.append("```")
    lines.append("REVIEW_VERDICT: approve")
    lines.append("REVIEW_BODY:")
    lines.append("<your full review text in markdown>")
    lines.append("END_REVIEW")
    lines.append("```")
    lines.append("")
    lines.append("Use `REVIEW_VERDICT: request-changes` if changes are needed.")

    return "\n".join(lines)


# ── Dispatch ────────────────────────────────────────────────────────────────

def _find_or_open_pr(
    repo_github: str,
    *,
    branch: str,
    default_branch: str,
    issue_number: int,
    issue_title: str,
    assignment_type: str = "work",
) -> dict | None:
    """Return {number, url, existed} for a PR on `branch`, opening one if needed.

    Returns None when neither lookup nor open works — caller continues without
    a PR-targeted review (falls back to branch-diff review).

    *assignment_type* decides the PR-body keyword (#1077): for types in
    :data:`coord.models.CLOSES_ISSUE_TYPES` (``"work"``), ``issue_number`` is
    the issue this PR resolves, so the body carries the closing keyword
    ``Closes #N`` and GitHub auto-closes it on merge. For any other
    WORK_LIKE type — notably ``"mock-author"`` (Gate A), whose
    ``issue_number`` is the milestone's *tracking* issue, not something the
    PR resolves — the body uses the non-closing ``Refs #N`` so the tracking
    issue still gets a discoverable backlink but does not flip to closed
    when the contract PR merges.
    """
    try:
        existing = github_ops.find_pr_for_branch(repo_github, branch)
    except RuntimeError:
        existing = None
    if existing is not None:
        return {
            "number": existing["number"],
            "url": existing.get("url"),
            "existed": True,
        }
    keyword = "Closes" if assignment_type in CLOSES_ISSUE_TYPES else "Refs"
    try:
        return github_ops.create_pr(
            repo_github,
            base=default_branch,
            head=branch,
            title=f"#{issue_number}: {issue_title}",
            body=(
                f"{keyword} #{issue_number}\n\n"
                f"Automated PR opened by coordinator for review of issue #{issue_number}."
            ),
        )
    except RuntimeError:
        return None


def _fetch_agent_advertised_repos(
    host: str,
    port: int = AGENT_PORT,
    *,
    timeout: float = 2.0,
) -> list[str] | None:
    """Query an agent's ``/health`` endpoint and return the repos it handles.

    Returns a list of repo names (strings) when the agent is reachable and
    returns well-formed JSON; returns ``None`` on *any* failure so callers
    can **fail-open** — never exclude a machine solely because its health probe
    hiccuped or timed out.

    The short *timeout* (default 2 s) is intentional: this is a preventative
    pre-filter, not a blocking gate.  If the agent is slow to respond, skip
    the filter and rely on the fall-through loop in ``dispatch_review`` to
    surface a definitive rejection.
    """
    url = f"http://{host}:{port}/health"
    try:
        resp = httpx.get(url, timeout=timeout)
        if resp.status_code == 200:
            data = resp.json()
            repos = data.get("repos")
            if isinstance(repos, list):
                return [str(r) for r in repos]
    except Exception:  # noqa: BLE001 — fail-open: any network or parse error
        pass
    return None


def dispatch_review(
    completed: Assignment,
    board: Board,
    config: Config,
    *,
    http_client: httpx.Client | None = None,
    pr_lookup=_find_or_open_pr,
    claude_md_reader=_read_repo_claude_md,
    issue_body_fetcher=None,
    now: float | None = None,
    terminal_cache: dict | None = None,
    remote_branch_checker=None,
    branch_sha_fetcher=None,
    health_checker=None,
) -> Assignment | None:
    """Open a PR for `completed` and dispatch a review assignment.

    Returns the new review Assignment, or None if review couldn't be dispatched
    (no machine handles the repo, no branch on the completed assignment, etc.).
    The caller is responsible for persisting the board.

    *health_checker* is an optional ``(host: str) -> list[str] | None`` callable
    that returns the repo names a given agent advertises, or ``None`` to
    fail-open.  When not provided, ``_fetch_agent_advertised_repos`` is called
    directly.  Inject a stub in tests to avoid real network probes.
    """
    if not config.reviews.enabled or not config.reviews.auto_dispatch:
        return None
    if completed.type not in WORK_LIKE_TYPES:
        return None
    if completed.status != "done":
        return None
    if not completed.branch:
        # Without a branch we can't open a PR or diff. Skip silently — this
        # usually means the worker forgot to switch off main, which the
        # branch-capture code in agent._reap will have left as None.
        return None

    # Dedupe: don't fire a second review if one's already in flight for this
    # completed work assignment.
    from coord.claim import has_active_followup, has_active_work_followup

    if has_active_followup(
        board, of_assignment_id=completed.assignment_id, assignment_type="review"
    ):
        return None

    # #459: skip review if a work or conflict-fix is actively rewriting the
    # branch for this issue (e.g. a coord-bounce fix iteration). Reviewing
    # stale code now would produce a verdict on code that's about to change.
    # Leave the caller's review_state as "pending" so the next reconcile pass
    # retries once the active fix finishes.
    if has_active_work_followup(
        board,
        repo_name=completed.repo_name,
        issue_number=completed.issue_number,
    ):
        return None

    repo = config.repo(completed.repo_name)
    if repo is None:
        return None

    # #522: the review chokepoint. Never (re)dispatch a review for work that
    # is already done on GitHub — issue closed OR PR merged. This is the second
    # flood vector (reviews of already-merged #349/#194) that the auto-loop
    # fix-dispatch guard alone didn't cover. Mark the row done so the pending-
    # review loop stops treating it as eligible. Fail-open inside
    # work_is_terminal, so a transient gh error never blocks a real review.
    if github_ops.work_is_terminal(
        repo.github, completed.issue_number, completed.branch, cache=terminal_cache
    ):
        completed.review_state = "done"
        return None

    # #437: STRUCTURAL TOS-COMPLIANCE GATE — auto-dispatched reviews are
    # an unattended path, so refuse to route them through a provider
    # whose capabilities mark it ``human_attended_only``.  Deferred import
    # keeps the review module free of a module-level cycle with the
    # provider registry.  On refusal we return None (same as "auto_dispatch
    # off" / "machine unreachable") so callers leave review_state as
    # 'pending' and retry on the next notify call — consistent with how
    # _reassign handles the same guard in reconcile.py.
    from coord.providers import guard_unattended_dispatch  # noqa: PLC0415
    try:
        guard_unattended_dispatch(
            spec_provider=None,
            repo_provider=repo.provider,
            providers_cfg=config.providers,
            models_cfg=config.models,
            where="auto-dispatch review",
        )
    except ValueError as exc:
        print(f"[review] skipping auto-dispatch review: {exc}")
        return None

    pr = pr_lookup(
        repo.github,
        branch=completed.branch,
        default_branch=repo.default_branch,
        issue_number=completed.issue_number,
        issue_title=completed.issue_title,
        assignment_type=completed.type,
    )

    # #904 (fix #1): build a ranked list of ALL eligible reviewer machines so
    # we can fall through to the next if one rejects the dispatch.  This
    # replaces the previous single-pick → silent-return-None path that could
    # park a work row at the merge gate forever when config drift caused a
    # "does not handle repo" 400 from the first (and only tried) machine.
    candidates = _ranked_reviewer_candidates(
        completed.machine_name, completed.repo_name, board, config
    )
    if not candidates:
        return None

    # #586: if the branch isn't on the remote, only the original worker machine
    # has it locally — any cross-machine reviewer would crash on git-fetch.
    # Narrow the candidate list to just that machine; if it's unavailable too,
    # stall visibly with "branch_not_on_remote".
    any_cross_machine = any(not same for _, same in candidates)
    if any_cross_machine and completed.branch:
        _check_remote = remote_branch_checker or github_ops.branch_exists_on_remote
        if not _check_remote(repo.github, completed.branch):
            log.warning(
                "[review] branch %r not on remote for %s — routing review back "
                "to original worker machine %s to avoid cross-machine fetch failure",
                completed.branch, completed.assignment_id, completed.machine_name,
            )
            from coord.machine_pause import paused_set  # noqa: PLC0415
            paused = paused_set()
            worker_machine = next(
                (m for m in config.machines if m.name == completed.machine_name),
                None,
            )
            if (
                worker_machine is not None
                and worker_machine.can_work_on(completed.repo_name)
                and worker_machine.name not in paused
            ):
                # Restrict to just the worker machine — it has the branch locally.
                candidates = [(worker_machine, True)]
            else:
                # Original machine also unavailable — stall visibly.
                log.error(
                    "[review] branch %r not on remote for %s and original machine "
                    "%s is unavailable (paused or not configured) — "
                    "review BLOCKED until branch is pushed to origin",
                    completed.branch, completed.assignment_id, completed.machine_name,
                )
                completed.review_state = "branch_not_on_remote"
                return None

    # Compute the parts that are constant across all candidate machines.

    # #612: merge-base diff — embedded verbatim so the reviewer reviews exactly
    # the branch's own changes (a stale-base diff sweeps in already-merged
    # commits as spurious deletions, #546).  Best-effort: None keeps the
    # fallback three-dot git-diff instructions in the briefing.
    diff_text = github_ops.pr_diff(repo.github, pr["number"]) if pr else None

    fetch_body = issue_body_fetcher or _fetch_issue_body
    issue_body = fetch_body(repo.github, completed.issue_number)

    # Pin the reviewer's model to avoid the agent defaulting to Opus (#911).
    review_model_alias = config.models.default
    review_model_wire = config.models.resolve(review_model_alias)

    # #821: capture branch HEAD SHA once; staleness detected post-review.
    _get_sha = branch_sha_fetcher or github_ops.get_branch_sha
    review_head_sha: str | None = None
    try:
        review_head_sha = _get_sha(repo.github, completed.branch)
    except Exception:  # noqa: BLE001 — fail-safe: missing SHA is not blocking
        pass

    # #603: per-issue context digest (cross-repo deps / prior findings).
    from coord.state import issue_context_block  # noqa: PLC0415
    context_prefix = issue_context_block(completed.repo_name, completed.issue_number)

    # #944 sealing v1: flag tests/acceptance/ as sealed when this repo has an
    # oracle-loop acceptance driver configured — the reviewer must reject any
    # diff that touches it (docs/ORACLE_LOOP.md).
    sealed_paths: list[str] = []
    if config.acceptance.has_driver(completed.repo_name):
        sealed_paths.append("tests/acceptance/")

    client = http_client or httpx

    # Iterate candidates in priority order.  On agent rejection (4xx from a
    # misconfigured agent, health-check filter on a drifted config, etc.) we
    # log a warning and try the next candidate instead of giving up silently.
    # Only definitive rejections (4xx responses or health-check exclusions) set
    # had_rejection=True; transient network failures leave the row as "pending"
    # so the next reconcile/notify pass retries automatically.
    had_rejection = False
    for machine, same_as_worker in candidates:
        # Fix #2 (PREVENTATIVE): pre-filter against the agent's /health
        # ``repos`` list so a drifted local config can't pick a machine that
        # will 400.  Fail-open: None means "probe failed, include anyway".
        _hc = health_checker if health_checker is not None else _fetch_agent_advertised_repos
        advertised = _hc(machine.host)
        if advertised is not None and completed.repo_name not in advertised:
            log.warning(
                "[review] skipping candidate %s: /health advertises repos %r "
                "but repo %r is not listed — possible config drift",
                machine.name, advertised, completed.repo_name,
            )
            had_rejection = True
            continue

        repo_path = machine.repo_path(completed.repo_name)
        if repo_path is None:
            log.warning(
                "[review] skipping candidate %s: no repo_path for %r",
                machine.name, completed.repo_name,
            )
            continue

        claude_md = claude_md_reader(Path(repo_path).expanduser())

        # #476 / #612: briefing is rebuilt per candidate because same_as_worker
        # (warning note in the briefing) and claude_md path can differ between
        # machines.
        briefing = context_prefix + build_review_briefing(
            pr_number=pr["number"] if pr else None,
            pr_url=pr["url"] if pr else None,
            repo_github=repo.github,
            repo_name=repo.name,
            issue_number=completed.issue_number,
            issue_title=completed.issue_title,
            issue_body=issue_body,
            branch=completed.branch,
            worker_machine=completed.machine_name,
            same_as_worker=same_as_worker,
            reviews_cfg=config.reviews,
            repo_claude_md=claude_md,
            default_branch=repo.default_branch,
            # #476: a fix worker carries review_iteration > 0; its re-review is
            # scoped to the fix delta rather than re-reviewing the whole PR.
            review_iteration=getattr(completed, "review_iteration", 0) or 0,
            diff_text=diff_text,
            sealed_paths=sealed_paths,
        )

        payload = {
            "repo_name": completed.repo_name,
            "repo_path": repo_path,
            "issue_number": completed.issue_number,
            "issue_title": f"[review] {completed.issue_title}",
            "briefing": briefing,
            "files_allowed": [],
            "files_forbidden": [],
            "pull_repos": [],
            "type": "review",
            "model": review_model_wire,
            "system_prompt": REVIEWER_SYSTEM_PROMPT,
            "review_target": str(pr["number"]) if pr else completed.branch,
            # #255: review checkout uses the PR branch, but the agent's worktree
            # setup still consults `branch` as the integration base when no PR
            # branch exists locally yet.  Match the work-dispatch path.
            "branch": repo.default_branch or "main",
        }

        url = f"http://{machine.host}:{AGENT_PORT}/assign"
        try:
            resp = client.post(url, json=payload, timeout=15)
            resp.raise_for_status()
            agent_response = resp.json()
        except httpx.HTTPStatusError as exc:
            # Fix #1 (PRIMARY): the agent definitively rejected the dispatch
            # (e.g. 400 "does not handle repo 'x'").  Try the next candidate
            # instead of silently returning None and leaving review_state as
            # 'pending' (#904).
            #
            # #904 (fix #2): only a 4xx is a *definitive* rejection — it means
            # the agent looked at the request and refused it (bad repo, bad
            # payload, etc.), which is a config-drift signal.  A 5xx means the
            # agent's own handler blew up (mid-restart, disk full, unhandled
            # exception) and says nothing about whether this agent/repo pairing
            # is valid — treat it like the transient network branch below so
            # the row stays "pending" and retries next pass instead of
            # permanently stalling as "no_eligible_reviewer".
            if exc.response.is_client_error:
                log.warning(
                    "[review] agent %s rejected dispatch with HTTP %d — "
                    "trying next reviewer candidate",
                    machine.name, exc.response.status_code,
                )
                had_rejection = True
            else:
                log.warning(
                    "[review] agent %s returned server error HTTP %d (transient) — "
                    "trying next reviewer candidate",
                    machine.name, exc.response.status_code,
                )
            continue
        except (httpx.HTTPError, httpx.TimeoutException) as exc:
            # Transient network failure — try next candidate, and if all
            # fail transiently, leave review_state unchanged so the next
            # reconcile/notify pass retries automatically.
            log.warning(
                "[review] agent %s unreachable (%s) — trying next reviewer candidate",
                machine.name, exc,
            )
            continue

        # Dispatch accepted — record the review assignment and return.
        review_assignment = Assignment(
            machine_name=machine.name,
            repo_name=completed.repo_name,
            issue_number=completed.issue_number,
            issue_title=f"[review] {completed.issue_title}",
            files_allowed=[],
            files_forbidden=[],
            briefing=briefing,
            assignment_id=agent_response.get("id") or uuid.uuid4().hex[:12],
            status="running",
            branch=completed.branch,
            pr_url=pr.get("url") if pr else None,
            dispatched_at=now if now is not None else time.time(),
            type="review",
            review_target=str(pr["number"]) if pr else completed.branch,
            review_of_assignment_id=completed.assignment_id,
            model=review_model_alias,
            review_head_sha=review_head_sha,
        )
        board.active.append(review_assignment)

        from coord.state import record_dispatched_assignment  # noqa: PLC0415
        record_dispatched_assignment(
            assignment=review_assignment,
            repo_github=repo.github,
        )

        return review_assignment

    # All candidates exhausted.  Distinguish definitive rejection (config
    # drift, drifted agent config) from transient network failures.
    if had_rejection:
        # At least one agent definitively rejected the repo — stall visibly
        # with a named state so `coord status` can surface an actionable error
        # and the pending-review loop stops silently retrying (#904).
        log.error(
            "[review] all reviewer candidates rejected dispatch for %s "
            "(repo=%r, branch=%r) — setting review_state='no_eligible_reviewer'. "
            "Check that every agent's repos list includes %r.",
            completed.assignment_id, completed.repo_name, completed.branch,
            completed.repo_name,
        )
        completed.review_state = "no_eligible_reviewer"
    else:
        # Only transient failures — leave review_state unchanged so the next
        # reconcile/notify pass retries automatically.
        log.warning(
            "[review] all reviewer candidates unreachable for %s "
            "(repo=%r) — will retry on next reconcile/notify pass",
            completed.assignment_id, completed.repo_name,
        )
    return None


def dispatch_pending_reviews(board, config, *, test_gate_active: bool = False, now=None):
    """Bounded bulk review dispatch — the flood guard (incident 2026-06-08).

    Gather every completed-work row eligible for a review, then dispatch
    reviews subject to two limits that prevent the review-flood failure mode —
    a backlog "unmasking" firing hundreds of metered ``claude -p`` reviews in a
    single reconcile/notify pass:

    1. **Surge gate.** If the number of eligible rows exceeds
       ``reviews.flood_threshold`` (and the threshold is > 0), dispatch
       *nothing* and log loudly. A sudden surge is the unmasking signature, so
       we halt and require a human to either clear the stale backlog (mark it
       reviewed/skipped) or opt in via ``reviews.allow_review_flood: true`` /
       ``COORD_ALLOW_REVIEW_FLOOD=1``.
    2. **Per-pass cap.** Otherwise dispatch at most
       ``reviews.max_auto_dispatch_per_pass`` reviews this pass (0 = unbounded);
       the remainder stay ``"pending"`` and are picked up next pass, so even a
       moderate batch bleeds out at a bounded rate instead of all at once.

    A row is eligible when its ``review_state`` is ``None``/``"pending"``, its
    ``type`` is in :data:`coord.models.WORK_LIKE_TYPES` (``"work"`` or
    ``"mock-author"``, #930), the (optional) test gate is satisfied, and #459's
    ``has_active_work_followup`` is False (don't review code a live fix is
    rewriting). Both ``reconcile()`` and ``coord notify`` route bulk dispatch
    through here so the cap, surge gate, and #459 dedupe are enforced on every
    automatic path. Sets ``review_state="dispatched"`` on each row it
    dispatches and returns the dispatched review ``Assignment``s. The caller
    persists the board.
    """
    import logging
    import os

    from coord.claim import has_active_work_followup

    logger = logging.getLogger("coord.review")

    # Test-before-Review reorder: when the pipeline orders Test ahead of Review,
    # hold automatic review dispatch until the work carries a passed/skipped
    # test verdict, so the headless auto-loop matches the displayed
    # Work → Test → Review order (and never burns a metered review on code the
    # smoke test hasn't validated yet). Explicit callers can still force the
    # gate on via ``test_gate_active``; the explicit ``coord review``/``coord
    # pr`` paths (→ ``dispatch_review`` directly) stay ungated so a human can
    # always request a review deliberately.
    gate_test = test_gate_active or (
        getattr(config, "pipeline", None) is not None
        and config.pipeline.test_precedes_review()
    )

    # #1076/#1152: a `type="mock-author"` (Gate A contract/fixture diff) or
    # `type="test-author"` (per-issue JIT acceptance-slice authoring, #931)
    # completion is a fixture/test-only diff — it matches no
    # `smoke_tests.capability_rules` rule by construction, so nothing ever
    # produces a Test-gate verdict for it and `test_state` stays NULL forever.
    # Under an active test gate that means the row is silently and
    # permanently excluded from `eligible` below — no error, no stuck
    # indicator, just a row that never gets reviewed (the #1076 repro,
    # assignment 9960b957ff3f; the #1152 repro, assignment 2e93ee72071c).
    # There is nothing to smoke-test for either shape of completion, so
    # "skipped" is always the correct verdict, not a judgment call — backfill
    # it here, the single choke point both reconcile() and `coord notify`
    # (`_dispatch_board_pending_reviews`) route bulk review dispatch through,
    # so this also retroactively unsticks any row that went "done" before
    # this fix shipped. `type="work"` rows are untouched — the test gate
    # still applies to them exactly as before (do NOT widen this to
    # `WORK_LIKE_TYPES`, which also contains `"work"`).
    _AUTO_SKIP_TEST_GATE_TYPES = ("mock-author", "test-author")
    if gate_test:
        from coord.state import record_test_verdict

        for c in board.completed:
            if (
                c.type in _AUTO_SKIP_TEST_GATE_TYPES
                and c.review_state in (None, "pending")
                and c.test_state is None
                and c.assignment_id is not None
            ):
                record_test_verdict(
                    assignment_id=c.assignment_id,
                    test_state="skipped",
                    test_reason=(
                        f"Gate A {c.type}: contract/fixture-only diff, "
                        "nothing to smoke-test (#1076/#1152)"
                    ),
                )
                c.test_state = "skipped"

    eligible = [
        c
        for c in board.completed
        if c.review_state in (None, "pending")
        and c.type in WORK_LIKE_TYPES
        # #555: NEVER auto-dispatch a headless `claude -p` review for an
        # *interactive* (`provider_name="claude-pty"`) work completion. The
        # interactive Work→Review handoff is human-attended (TUI confirm →
        # interactive review); a metered headless review must not silently
        # follow it. This guard lives only in the automatic bulk path — the
        # explicit `coord review <id>` escape hatch (→ dispatch_review) still
        # lets a human deliberately request a headless review if they want one.
        and c.provider_name != "claude-pty"
        and (not gate_test or c.test_state in ("passed", "skipped"))
        and not has_active_work_followup(
            board, repo_name=c.repo_name, issue_number=c.issue_number
        )
    ]
    if not eligible:
        return []

    threshold = config.reviews.flood_threshold
    override = (
        config.reviews.allow_review_flood
        or os.environ.get("COORD_ALLOW_REVIEW_FLOOD") == "1"
    )
    if threshold and len(eligible) > threshold and not override:
        logger.warning(
            "review flood guard: %d work rows are pending review (> "
            "reviews.flood_threshold=%d). Refusing bulk dispatch to avoid a "
            "metered review flood. Clear the stale backlog (mark reviewed/"
            "skipped), or set reviews.allow_review_flood: true (or "
            "COORD_ALLOW_REVIEW_FLOOD=1) to override.",
            len(eligible),
            threshold,
        )
        return []

    cap = config.reviews.max_auto_dispatch_per_pass
    # #522: one terminal-state cache for this whole pass, so a backlog full of
    # already-merged rows (the #349 ×4 case) costs one gh lookup per issue, not
    # one per row revisited.
    terminal_cache: dict = {}
    dispatched: list = []
    for completed in eligible:
        if cap and len(dispatched) >= cap:
            break
        review = dispatch_review(
            completed, board, config, now=now, terminal_cache=terminal_cache
        )
        if review is not None:
            completed.review_state = "dispatched"
            dispatched.append(review)
        # On failure leave review_state as "pending" so the next pass retries.
        # Terminal rows are marked review_state="done" inside dispatch_review
        # (#522), dropping them from `eligible` on the next pass.

    held = sum(1 for c in eligible if c.review_state in (None, "pending"))
    if held:
        logger.info(
            "review dispatch cap: dispatched %d this pass, %d held for next "
            "pass (reviews.max_auto_dispatch_per_pass=%d).",
            len(dispatched),
            held,
            cap,
        )
    return dispatched


def _fetch_issue_body(repo_github: str, issue_number: int) -> str:
    """Best-effort fetch of the issue body for context. Empty on failure."""
    try:
        import json
        raw = github_ops._gh(
            "issue", "view", str(issue_number),
            "--repo", repo_github,
            "--json", "body",
        )
        return json.loads(raw).get("body", "") or ""
    except (RuntimeError, ValueError):
        return ""


# ── Headless fix dispatch (dashboard / phone API) ────────────────────────────


def dispatch_headless_fix(
    work: Assignment,
    board: Board,
    config: "Config",
    *,
    parent_type: str = "work",
    http_client=None,
) -> Assignment | None:
    """Dispatch a headless (``claude -p``) fix worker for a stalled pipeline item.

    Called from ``POST /api/pipeline/action action=dispatch_fix`` so the phone
    can unstick a test-fail or request-changes item without attending an
    interactive terminal session.

    ``work`` must be a ``type='work'`` assignment that already has a branch.
    ``parent_type`` selects which failure to address:

    * ``"work"`` — fix a test-gate failure.  The briefing is built from
      ``work.test_reason`` (recorded via ``coord test --fail --reason``).
    * ``"review"`` — fix a request-changes review verdict.  The linked review
      assignment is located on the board and its findings are loaded via the
      multi-source chain in ``_load_review_findings`` (DB cache → local log →
      agent HTTP → GitHub message bus).

    The fix worker is dispatched with ``target_branch=work.branch`` in the
    agent payload so it adds commits to the **existing** ``issue-N-*`` branch
    rather than branching fresh off main.

    Returns the new fix ``Assignment`` (already added to ``board.active``),
    or ``None`` on failure (no capable machine, branch missing, findings
    unresolvable, or iteration limit reached).
    """
    from types import SimpleNamespace as _NS  # noqa: PLC0415

    # Deferred imports to avoid a circular-import cycle:
    # review.py is imported at module level by auto_loop.py, so we cannot
    # import auto_loop at review.py's module level.
    from coord.auto_loop import (  # noqa: PLC0415
        _build_fix_briefing,
        _dispatch_fix,
        _fix_model_for_iteration,
        _load_review_findings,
        _work_is_terminal,
    )
    from coord.state import issue_context_block  # noqa: PLC0415

    if not work.branch:
        return None

    if _work_is_terminal(work, config):
        return None

    next_iteration = (work.review_iteration or 0) + 1
    max_iter = config.pipeline.max_review_iterations
    if next_iteration > max_iter:
        return None

    if parent_type == "review":
        # Find the review assignment linked to this work and load its findings.
        all_assignments = list(board.active) + list(board.completed)
        review_a: Assignment | None = next(
            (
                a for a in all_assignments
                if a.review_of_assignment_id == work.assignment_id
                and a.type == "review"
            ),
            None,
        )
        if review_a is None:
            return None

        repo = config.repo(work.repo_name)
        repo_github = repo.github if repo is not None else None
        findings = _load_review_findings(
            review_a,
            None,          # no local log path on the dashboard machine
            None,          # no remote agent host — let GitHub fallback handle it
            repo_github=repo_github,
        )
        if findings is not None:
            findings_obj = findings
        else:
            # Fallback: generic pointer so the worker can still proceed.
            verdict = getattr(review_a, "review_verdict", None) or "request-changes"
            findings_obj = _NS(body=(
                f"(No structured findings were captured for review "
                f"{review_a.assignment_id}.) "
                f"The review verdict was {verdict!r}. "
                "Read the reviewer's feedback on the PR / issue comments and "
                "address every blocking item before pushing."
            ))
    else:
        # parent_type == "work": test-gate failure.
        test_story = (getattr(work, "test_reason", None) or "").strip()
        if test_story:
            findings_obj = _NS(body=(
                "The manual smoke test FAILED.  The operator reported:\n\n"
                f"> {test_story}\n\n"
                "Reproduce the failure, fix the root cause, and re-validate "
                "before pushing."
            ))
        else:
            findings_obj = _NS(body=(
                "The manual smoke test FAILED (no reason text was recorded). "
                "Pull the branch, reproduce the failure the operator hit, "
                "and fix the root cause before pushing."
            ))

    briefing = (
        issue_context_block(work.repo_name, work.issue_number)
        + _build_fix_briefing(work, findings_obj, next_iteration, max_iter)
    )
    model = _fix_model_for_iteration(config, next_iteration)
    return _dispatch_fix(
        work, briefing, board, config, next_iteration,
        model=model, http_client=http_client,
    )
