"""#770 (Phase 2 of #767): dispatch a `type="milestone-chat"` session.

Lets the operator chat about a milestone's issues and — once they confirm —
write the `## Work order` block that ``coord/milestone_order.py`` (#768)
parses into a DAG/frontier. This is a deliberately narrow slice of #645's
broader "milestone-steward" chat (create milestone, create/edit issues,
assess relevance): it exists only to close the #767 Phase 2 loop — propose
an order, confirm with the operator, write it. Full #645 scope stays a
separate, later piece of work; nothing here forecloses it.

Used by `coord milestone chat <repo> <tracking_issue>` (the CLI command in
``coord/commands/milestone.py``).

The session itself runs as a `type="milestone-chat"` assignment on an agent
server. Tools are `Read,Bash` (see `coord/agent.py`); a deny list blocks raw
`gh` mutations and unrelated `coord` write commands — the one write action
the worker may take is `coord milestone write-order`, and only after the
operator has explicitly confirmed the proposed block in conversation.
"""
from __future__ import annotations

import time
import uuid

from coord import github_ops
from coord.config import Config
from coord.models import Machine, Proposal
from coord.refine_chat import pick_refinement_machine

# Soft caps so the seed briefing stays bounded on milestones with many/large
# issues — mirrors the caps `new_issue_chat.py` / `refine_chat.py` use.
MAX_ISSUES = 50
MAX_ISSUE_BODY_CHARS = 1500


def pick_milestone_chat_machine(cfg: Config, repo: str) -> Machine | None:
    """Pick a machine to run the milestone-chat session on.

    Milestone-chat is read-only w.r.t. git (no worktree, no branch — its one
    write action goes through `coord milestone write-order` against the
    GitHub API, not the local checkout) and short-lived, so any qualified,
    unpaused machine works — same selection `refine_chat`/`new_issue_chat`
    use. Reused directly rather than re-implemented (identical logic).
    """
    return pick_refinement_machine(cfg, repo)


def _fetch_milestone_issues(slug: str, milestone_number: int) -> list[dict]:
    """Best-effort fetch of open issues under *milestone_number*.

    Returns a list of ``{"number", "title", "body"}`` dicts (bodies
    truncated to ``MAX_ISSUE_BODY_CHARS`` so cohort/dependency inference has
    signal without blowing the prompt budget), capped at ``MAX_ISSUES``.
    Returns an empty list on any failure so the seed briefing still works
    without issue context — the operator can still chat, just with less to
    go on.
    """
    try:
        issues = github_ops.get_open_issues(slug)
    except RuntimeError:
        return []
    under_milestone = [
        i for i in issues
        if (i.get("milestone") or {}).get("number") == milestone_number
    ]
    out: list[dict] = []
    for issue in under_milestone[:MAX_ISSUES]:
        body = (issue.get("body") or "").strip()
        if len(body) > MAX_ISSUE_BODY_CHARS:
            body = body[:MAX_ISSUE_BODY_CHARS] + "\n...(truncated)"
        out.append({
            "number": issue.get("number"),
            "title": issue.get("title") or "",
            "body": body,
        })
    return out


def build_milestone_chat_briefing(
    *,
    repo_name: str,
    repo_slug: str,
    milestone_title: str,
    tracking_issue_number: int,
    tracking_issue_body: str,
    issues: list[dict],
) -> str:
    """Compose the seed briefing the worker sees as its first user message.

    The agent's ``MILESTONE_CHAT_SYSTEM_PROMPT`` already tells it how to
    behave; this packs the milestone context into the first user turn.
    """
    parts: list[str] = []
    parts.append(
        f"=== Milestone chat context for {repo_slug} "
        f"— milestone {milestone_title!r} ===\n"
    )

    parts.append(f"TRACKING ISSUE: #{tracking_issue_number}")
    parts.append(
        "CURRENT TRACKING ISSUE BODY (may already carry a `## Work order` "
        "block from a prior run — treat re-runs as idempotent updates, not "
        "duplicates):"
    )
    parts.append(tracking_issue_body.strip() if tracking_issue_body.strip() else "(empty)")
    parts.append("")

    parts.append(f"OPEN ISSUES UNDER THIS MILESTONE ({len(issues)}):")
    if issues:
        for issue in issues:
            parts.append(f"--- #{issue['number']}: {issue['title']} ---")
            parts.append(issue["body"] if issue["body"] else "(no body)")
            parts.append("")
    else:
        parts.append("  (none fetched)")

    parts.append("---")
    parts.append(
        "Discuss this milestone with the operator. When asked to propose a "
        "work order — or when it's clearly useful — infer parallel cohorts "
        "(`group: <label>`) vs. hard dependencies (`after: #N[,#M...]`) from "
        "the issue bodies above, present the proposed `## Work order` block, "
        "and only after the operator explicitly confirms it, write it with:\n\n"
        f"  coord milestone write-order {repo_name} {tracking_issue_number} "
        "<<'EOF'\n"
        "  - [ ] #762  {group: A}\n"
        "  ...\n"
        "  EOF\n"
    )
    return "\n".join(parts)


def dispatch_milestone_chat(
    repo_name: str,
    tracking_issue_number: int,
    config: Config,
    *,
    machine_override: str | None = None,
) -> tuple[str, str]:
    """End-to-end: resolve the milestone, pick a machine, seed the
    briefing, dispatch a ``type="milestone-chat"`` assignment.

    Returns ``(assignment_id, machine_name)``. Raises ``RuntimeError`` when
    the repo is unknown, the tracking issue can't be fetched or has no
    milestone, no machine claims the repo, or the agent rejects the
    dispatch.
    """
    repo_cfg = config.repo(repo_name)
    if repo_cfg is None:
        raise RuntimeError(f"repo {repo_name!r} not in coordinator.yml")

    try:
        issue_data = github_ops.get_issue(repo_cfg.github, tracking_issue_number)
    except RuntimeError as e:
        raise RuntimeError(f"could not fetch #{tracking_issue_number}: {e}") from e

    milestone = issue_data.get("milestone") or {}
    milestone_number = milestone.get("number")
    if milestone_number is None:
        raise RuntimeError(f"#{tracking_issue_number} has no milestone")
    milestone_title = milestone.get("title") or f"#{milestone_number}"

    # Pick the machine.
    if machine_override:
        machine = next(
            (m for m in config.machines if m.name == machine_override),
            None,
        )
        if machine is None:
            raise RuntimeError(f"machine {machine_override!r} not in coordinator.yml")
        if not machine.can_work_on(repo_name):
            raise RuntimeError(
                f"machine {machine_override!r} does not list repo {repo_name!r}"
            )
    else:
        picked = pick_milestone_chat_machine(config, repo_name)
        if picked is None:
            raise RuntimeError(
                f"no machine claims repo {repo_name!r} — milestone-chat needs a "
                "machine that has the repo cloned"
            )
        machine = picked

    issues = _fetch_milestone_issues(repo_cfg.github, milestone_number)

    briefing = build_milestone_chat_briefing(
        repo_name=repo_name,
        repo_slug=repo_cfg.github,
        milestone_title=milestone_title,
        tracking_issue_number=tracking_issue_number,
        tracking_issue_body=issue_data.get("body") or "",
        issues=issues,
    )

    tracking_title = issue_data.get("title") or f"Milestone chat #{tracking_issue_number}"
    resolved_model = config.models.default
    proposal = Proposal(
        id=0,
        machine_name=machine.name,
        repo_name=repo_name,
        issue_number=tracking_issue_number,
        issue_title=tracking_title,
        rationale="milestone-chat",
        briefing=briefing,
        model=resolved_model,
        type="milestone-chat",
        required_gates=[],
    )

    from coord.dispatch import dispatch_with_retry
    from coord.models import Assignment
    from coord.state import record_dispatched_assignment

    response = dispatch_with_retry(
        proposal,
        config,
        max_retries=config.concurrency.max_retries,
        backoff_base=config.concurrency.backoff_base,
    )

    assignment_id = response.get("id") or uuid.uuid4().hex[:12]

    asg = Assignment(
        machine_name=machine.name,
        repo_name=repo_name,
        issue_number=tracking_issue_number,
        issue_title=tracking_title,
        files_allowed=[],
        files_forbidden=[],
        briefing=briefing,
        assignment_id=assignment_id,
        status="running",
        dispatched_at=time.time(),
        type="milestone-chat",
        model=resolved_model,
    )
    record_dispatched_assignment(
        assignment=asg,
        repo_github=repo_cfg.github,
    )

    return assignment_id, machine.name
