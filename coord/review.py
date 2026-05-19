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

import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import httpx

from coord import github_ops
from coord.config import Config, ReviewsConfig
from coord.dispatch import AGENT_PORT
from coord.models import Assignment, Board, Machine


REVIEWER_SYSTEM_PROMPT = """\
You are an independent code reviewer dispatched by the coordinator. \
Your job is to find problems — do NOT rubber-stamp.

Rules:
- You have a fresh session. You have NO context from the worker who wrote \
this code. Treat the diff as if you're reading it for the first time.
- You ARE allowed to run `gh pr review` to post your final review (this is \
the one gh command reviewers may use).
- You ARE allowed to run the project's test suite and read project files.
- You are NOT allowed to push commits or modify the PR's code. You only \
review.

How to review:
1. Read the project's CLAUDE.md for project conventions.
2. Read the PR diff (`gh pr diff <number>`).
3. Run the test suite. Note any failures or regressions.
4. Check the diff against the review checklist in your briefing.
5. For each finding, cite the specific file:line and the rule it violates.
6. Post your review using `gh pr review <number>` with --approve, \
--request-changes, or --comment.

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
        lines.append(f"1. Run `gh pr diff {pr_number} --repo {repo_github}` to see the changes.")
        lines.append("2. Run the project's test suite.")
        lines.append("3. Review the diff against the checklist above.")
        lines.append(
            f"4. Post your review with `gh pr review {pr_number} --repo {repo_github}` "
            "and one of `--approve`, `--request-changes`, or `--comment`. "
            "Include findings inline in `--body`."
        )
    else:
        lines.append(
            f"1. The worker pushed branch `{branch}` but no PR was opened. "
            "Inspect the diff with `git diff main..." + (branch or "<branch>") + "`."
        )
        lines.append("2. Run the project's test suite.")
        lines.append("3. Report findings via `gh issue comment " + str(issue_number) + "`.")

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
            body=f"Automated PR opened by coordinator for review of issue #{issue_number}.",
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
        "system_prompt": REVIEWER_SYSTEM_PROMPT,
        "review_target": str(pr["number"]) if pr else completed.branch,
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
    )
    board.active.append(review_assignment)
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
