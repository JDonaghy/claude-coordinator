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
from coord.models import Assignment, Board, Machine


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
_REVIEW_BLOCK_RE = re.compile(
    r"REVIEW_VERDICT:\s*(approve|request-changes|pass|fail)\s*[\r\n]+"
    r"REVIEW_BODY:\s*[\r\n]+(.*?)[\r\n]*END_REVIEW",
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


REVIEWER_SYSTEM_PROMPT = """\
You are an independent code reviewer dispatched by the coordinator. \
Your job is to find problems — do NOT rubber-stamp.

Rules:
- You have a fresh session. You have NO context from the worker who wrote \
this code. Treat the diff as if you're reading it for the first time.
- You are NOT allowed to run any `gh` commands. The coordinator posts the \
review on your behalf after your session ends.
- You ARE allowed to run the project's test suite and read project files.
- You are NOT allowed to push commits or modify the PR's code. You only \
review.

How to review:
1. Read the project's CLAUDE.md for project conventions.
2. Read the PR diff using `git diff` or the briefing instructions.
3. Run the test suite. Note any failures or regressions.
4. Check the diff against the review checklist in your briefing.
5. For each finding, cite the specific file:line and the rule it violates.
6. At the END of your session, output your verdict in this exact format:

REVIEW_VERDICT: approve
REVIEW_BODY:
<your full review text in markdown>
END_REVIEW

Or for requesting changes:

REVIEW_VERDICT: request-changes
REVIEW_BODY:
<your full review text in markdown>
END_REVIEW

If the diff is clean and tests pass, approve — but be thorough first.\
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
    candidates = [
        m for m in config.machines if m.can_work_on(repo_name)
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
) -> str:
    """Assemble the reviewer's prompt. Pure function — easy to test."""

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

    lines.append("")
    lines.append("## What to do")
    lines.append("")
    if pr_number is not None:
        lines.append(
            f"1. Get the diff: `git fetch origin && git diff origin/{default_branch}..."
            f"origin/{branch or 'HEAD'}` or ask the coordinator for the diff."
        )
        lines.append("2. Run the project's test suite.")
        lines.append("3. Review the diff against the checklist above.")
    else:
        lines.append(
            f"1. The worker pushed branch `{branch}` but no PR was opened. "
            "Inspect the diff with `git diff main..." + (branch or "<branch>") + "`."
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
) -> dict | None:
    """Return {number, url, existed} for a PR on `branch`, opening one if needed.

    Returns None when neither lookup nor open works — caller continues without
    a PR-targeted review (falls back to branch-diff review).
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
    try:
        return github_ops.create_pr(
            repo_github,
            base=default_branch,
            head=branch,
            title=f"#{issue_number}: {issue_title}",
            body=(
                f"Closes #{issue_number}\n\n"
                f"Automated PR opened by coordinator for review of issue #{issue_number}."
            ),
        )
    except RuntimeError:
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
) -> Assignment | None:
    """Open a PR for `completed` and dispatch a review assignment.

    Returns the new review Assignment, or None if review couldn't be dispatched
    (no machine handles the repo, no branch on the completed assignment, etc.).
    The caller is responsible for persisting the board.
    """
    if not config.reviews.enabled or not config.reviews.auto_dispatch:
        return None
    if completed.type != "work":
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
    from coord.claim import has_active_followup

    if has_active_followup(
        board, of_assignment_id=completed.assignment_id, assignment_type="review"
    ):
        return None

    repo = config.repo(completed.repo_name)
    if repo is None:
        return None

    pr = pr_lookup(
        repo.github,
        branch=completed.branch,
        default_branch=repo.default_branch,
        issue_number=completed.issue_number,
        issue_title=completed.issue_title,
    )

    choice = pick_reviewer_machine(
        completed.machine_name, completed.repo_name, board, config
    )
    if choice is None:
        return None

    repo_path = choice.machine.repo_path(completed.repo_name)
    if repo_path is None:
        return None
    claude_md = claude_md_reader(Path(repo_path).expanduser())

    fetch_body = issue_body_fetcher or _fetch_issue_body
    briefing = build_review_briefing(
        pr_number=pr["number"] if pr else None,
        pr_url=pr["url"] if pr else None,
        repo_github=repo.github,
        repo_name=repo.name,
        issue_number=completed.issue_number,
        issue_title=completed.issue_title,
        issue_body=fetch_body(repo.github, completed.issue_number),
        branch=completed.branch,
        worker_machine=completed.machine_name,
        same_as_worker=choice.same_as_worker,
        reviews_cfg=config.reviews,
        repo_claude_md=claude_md,
        default_branch=repo.default_branch,
    )

    # Pin the reviewer's model.  Without this the payload omits `model`
    # and the agent lets `claude -p` pick its CLI default, which became
    # Opus 4.8 in claude-code 2.1.x — silently making every review the
    # most expensive model available.  Use the configured default
    # (typically sonnet) and resolve through models.versions so the wire
    # carries an exact id when one is pinned.
    review_model_alias = config.models.default
    review_model_wire = config.models.resolve(review_model_alias)

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

    url = f"http://{choice.machine.host}:{AGENT_PORT}/assign"
    client = http_client or httpx
    try:
        resp = client.post(url, json=payload, timeout=15)
        resp.raise_for_status()
        agent_response = resp.json()
    except (httpx.HTTPError, httpx.TimeoutException):
        return None

    review_assignment = Assignment(
        machine_name=choice.machine.name,
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
    )
    board.active.append(review_assignment)

    from coord.state import record_dispatched_assignment
    record_dispatched_assignment(
        assignment=review_assignment,
        repo_github=repo.github,
    )

    return review_assignment


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
