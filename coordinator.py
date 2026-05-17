#!/usr/bin/env python3
"""Multi-agent coordinator for Claude Code workers.

Interactive CLI that plans work, posts briefings as issue comments,
and fires Managed Agent sessions to execute assignments in parallel.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from anthropic import Anthropic

from board import Board, WorkerConfig, Assignment, save_board, load_board
from github_ops import (
    get_open_issues,
    get_repo_file,
    get_recent_develop_commits,
    post_issue_comment,
    list_open_prs,
)
from workers import (
    get_client,
    create_agent,
    create_environment,
    AgentSetup,
    fire_worker,
    wait_for_any_completion,
    get_worker_summary,
)

SETUP_FILE = "agent_setup.json"

COORDINATOR_SYSTEM = """You are a coordinator for a multi-agent coding session.
You do NOT write code. You plan, track, and route work across multiple workers
to maximize throughput without file conflicts.

## Conflict Rules
- Two workers must NEVER touch the same file concurrently.
- src/core/engine/ sub-modules can be parallelized (keys.rs vs motions.rs etc.)
- src/render.rs is a single large file — only one worker at a time.
- src/gtk/mod.rs, src/gtk/click.rs, src/gtk/draw.rs — treat as one unit.
- src/tui_main/mod.rs, src/tui_main/mouse.rs — treat as one unit.

## Priority Order
1. Regressions from recently-landed work
2. Newly unblocked issues (prereq just closed)
3. Milestone items by dependency order
4. Non-milestone bugs
5. Enhancements/research

## Output Format
For each idle worker, propose ONE assignment as JSON:
```json
[
  {
    "worker": "worker-name",
    "issue": 123,
    "issue_title": "Short title",
    "files_allowed": ["src/core/engine/keys.rs", "src/core/engine/motions.rs"],
    "files_forbidden": ["src/gtk/", "src/render.rs"],
    "briefing": "Work on #123 — description of what to do, which files to touch, approach hints."
  }
]
```

Only propose issues that match each worker's constraints (e.g. no GTK issues for no-GTK workers).
Include enough context in the briefing that the worker can start immediately after reading the issue.
"""


def load_setup() -> AgentSetup | None:
    if not os.path.exists(SETUP_FILE):
        return None
    with open(SETUP_FILE) as f:
        data = json.load(f)
    return AgentSetup(**data)


def save_setup(setup: AgentSetup):
    with open(SETUP_FILE, "w") as f:
        json.dump({"agent_id": setup.agent_id, "agent_version": setup.agent_version,
                    "environment_id": setup.environment_id}, f, indent=2)


def ensure_setup(client: Anthropic) -> AgentSetup:
    setup = load_setup()
    if setup:
        print(f"Using existing agent {setup.agent_id}")
        return setup

    print("Creating worker agent and environment (one-time setup)...")
    worker_system = (
        "You are a coding agent working on a software project. "
        "Read the repo's CLAUDE.md for conventions. "
        "Read the issue and its latest comment for your assignment. "
        "Do the work, commit to a feature branch, and push. "
        "Do NOT open a PR unless explicitly told to. "
        "Report what you did when finished."
    )
    agent_id, agent_version = create_agent(client, worker_system)
    env_id = create_environment(client)
    setup = AgentSetup(agent_id=agent_id, agent_version=agent_version, environment_id=env_id)
    save_setup(setup)
    print(f"Agent: {agent_id}, Environment: {env_id}")
    return setup


def fetch_context(repo: str) -> str:
    """Fetch repo context for the coordinator brain."""
    parts = []
    try:
        claude_md = get_repo_file(repo, "CLAUDE.md")
        parts.append(f"## CLAUDE.md (abbreviated)\n{claude_md[:3000]}")
    except Exception:
        parts.append("(CLAUDE.md not found)")

    try:
        project_state = get_repo_file(repo, "PROJECT_STATE.md")
        parts.append(f"## PROJECT_STATE.md (abbreviated)\n{project_state[:2000]}")
    except Exception:
        parts.append("(PROJECT_STATE.md not found)")

    issues = get_open_issues(repo)
    issue_list = "\n".join(
        f"- #{i['number']} — {i['title']} [{','.join(l['name'] for l in i.get('labels', []))}]"
        for i in sorted(issues, key=lambda x: x["number"])
    )
    parts.append(f"## Open Issues\n{issue_list}")

    commits = get_recent_develop_commits(repo, 10)
    commit_list = "\n".join(f"- {c['sha']} {c['message']}" for c in commits)
    parts.append(f"## Recent develop commits\n{commit_list}")

    prs = list_open_prs(repo)
    if prs:
        pr_list = "\n".join(f"- PR #{p['number']} ({p['headRefName']}): {p['title']}" for p in prs)
        parts.append(f"## Open PRs\n{pr_list}")

    return "\n\n".join(parts)


def ask_coordinator(client: Anthropic, board: Board, context: str) -> list[dict]:
    """Ask the coordinator brain to propose assignments for idle workers."""
    idle = board.idle_workers()
    if not idle:
        return []

    worker_desc = "\n".join(
        f"- {w.name}: GTK={'yes' if w.can_gtk else 'NO'}{f' ({w.notes})' if w.notes else ''}"
        for w in idle
    )

    active_desc = ""
    if board.active:
        active_desc = "Currently active:\n" + "\n".join(
            f"- {a.worker_name} → #{a.issue_number} {a.issue_title} (files: {', '.join(a.files_allowed[:3])})"
            for a in board.active if a.status == "running"
        )

    user_msg = (
        f"{context}\n\n"
        f"## Current Board\n{active_desc or 'No active assignments.'}\n\n"
        f"## Idle Workers\n{worker_desc}\n\n"
        f"Propose one assignment per idle worker. Output ONLY the JSON array."
    )

    response = client.messages.create(
        model="claude-opus-4-7",
        max_tokens=4096,
        system=COORDINATOR_SYSTEM,
        messages=[{"role": "user", "content": user_msg}],
    )

    text = response.content[0].text
    start = text.find("[")
    end = text.rfind("]") + 1
    if start == -1 or end == 0:
        print(f"Could not parse coordinator response:\n{text}")
        return []
    return json.loads(text[start:end])


def present_for_approval(proposals: list[dict]) -> list[dict]:
    """Show proposals and get user approval."""
    if not proposals:
        print("No proposals from coordinator.")
        return []

    approved = []
    for p in proposals:
        print(f"\n{'='*60}")
        print(f"Worker:  {p['worker']}")
        print(f"Issue:   #{p['issue']} — {p['issue_title']}")
        print(f"Files:   {', '.join(p.get('files_allowed', []))}")
        print(f"Blocked: {', '.join(p.get('files_forbidden', []))}")
        print(f"Briefing:\n  {p['briefing'][:200]}...")
        print(f"{'='*60}")

        choice = input("Approve? [Y/n/edit/skip] ").strip().lower()
        if choice in ("", "y", "yes"):
            approved.append(p)
        elif choice == "edit":
            new_briefing = input("New briefing (or Enter to keep): ").strip()
            if new_briefing:
                p["briefing"] = new_briefing
            approved.append(p)
        elif choice == "skip":
            print("Skipped.")
        else:
            print("Skipped.")

    return approved


def fire_assignments(
    client: Anthropic,
    setup: AgentSetup,
    board: Board,
    approved: list[dict],
):
    """Post briefings and fire worker sessions."""
    for p in approved:
        assignment = Assignment(
            worker_name=p["worker"],
            issue_number=p["issue"],
            issue_title=p["issue_title"],
            files_allowed=p.get("files_allowed", []),
            files_forbidden=p.get("files_forbidden", []),
            briefing=p["briefing"],
            status="running",
        )

        print(f"\nPosting briefing on #{assignment.issue_number}...")
        post_issue_comment(
            board.repo, assignment.issue_number,
            f"## Worker briefing (automated coordinator)\n\n{assignment.briefing}",
        )

        print(f"Firing worker {assignment.worker_name} on #{assignment.issue_number}...")
        session_id = fire_worker(client, setup, board.repo, assignment)
        assignment.session_id = session_id
        board.active.append(assignment)
        print(f"Session started: {session_id}")

    save_board(board)


def monitor_and_loop(client: Anthropic, setup: AgentSetup, board: Board, context: str):
    """Poll active sessions and re-assign when workers complete."""
    while board.active:
        active_sessions = {
            a.worker_name: a.session_id
            for a in board.active
            if a.status == "running" and a.session_id
        }

        if not active_sessions:
            break

        print(f"\nMonitoring {len(active_sessions)} active worker(s)... (Ctrl-C to pause)")
        try:
            completed_names = wait_for_any_completion(client, active_sessions, poll_interval=30)
        except KeyboardInterrupt:
            print("\nPaused. Board state saved.")
            save_board(board)
            return

        for name in completed_names:
            assignment = next(a for a in board.active if a.worker_name == name)
            summary = get_worker_summary(client, assignment.session_id)
            print(f"\n{'='*60}")
            print(f"Worker {name} finished #{assignment.issue_number}:")
            print(summary[:500])
            print(f"{'='*60}")

            board.mark_done(name)
            save_board(board)

        idle = board.idle_workers()
        if idle:
            print(f"\n{len(idle)} worker(s) idle. Proposing next assignments...")
            proposals = ask_coordinator(client, board, context)
            approved = present_for_approval(proposals)
            if approved:
                fire_assignments(client, setup, board, approved)


def parse_workers(worker_args: list[str]) -> list[WorkerConfig]:
    """Parse --worker 'name:gtk=yes:note=...' args."""
    workers = []
    for w in worker_args:
        parts = w.split(":")
        name = parts[0]
        can_gtk = True
        notes = ""
        for part in parts[1:]:
            if part.startswith("gtk="):
                can_gtk = part.split("=")[1].lower() in ("yes", "true", "1")
            elif part.startswith("note="):
                notes = part.split("=", 1)[1]
        workers.append(WorkerConfig(name=name, can_gtk=can_gtk, notes=notes))
    return workers


def main():
    parser = argparse.ArgumentParser(description="Multi-agent coordinator for Claude Code")
    parser.add_argument("--repo", required=True, help="GitHub repo (owner/name)")
    parser.add_argument("--worker", action="append", default=[],
                        help="Worker config: 'name:gtk=yes:note=...'")
    parser.add_argument("--resume", action="store_true",
                        help="Resume from saved board state")
    args = parser.parse_args()

    client = get_client()
    setup = ensure_setup(client)

    if args.resume:
        board = load_board()
        if not board:
            print("No saved board state found.")
            sys.exit(1)
        print(f"Resumed board — round {board.round_number}")
    else:
        workers = parse_workers(args.worker)
        if not workers:
            print("No workers specified. Use --worker 'name:gtk=yes'")
            sys.exit(1)
        board = Board(repo=args.repo, workers=workers)

    print(f"\nCoordinator for {board.repo}")
    print(board.summary())

    print("\nFetching repo context...")
    context = fetch_context(board.repo)
    print(f"Context loaded ({len(context)} chars)")

    board.round_number += 1
    proposals = ask_coordinator(client, board, context)
    approved = present_for_approval(proposals)

    if approved:
        fire_assignments(client, setup, board, approved)
        monitor_and_loop(client, setup, board, context)
    else:
        print("No assignments approved. Exiting.")

    print(f"\nFinal board state:")
    print(board.summary())
    save_board(board)


if __name__ == "__main__":
    main()
