"""`coord sync` plus the `issue`/`context` groups and `track`/`untrack`/
`backlog`. Extracted from coord/cli.py (#747)."""

from __future__ import annotations

import sys
from pathlib import Path

import click

from coord.commands._common import _apply_label_change, _CONFIG_OPTION, _load_config


@click.command(help="Sync open issues from GitHub into the local SQLite cache.")
@_CONFIG_OPTION
@click.option("--quiet", "-q", is_flag=True, help="Suppress per-repo output.")
def sync(config_path: Path, quiet: bool) -> None:
    """Fetch open issues for every configured repo and write them to the local
    ``issues`` table in ``~/.coord/coord.db``.

    The TUI board reads from this table to show the full backlog under
    Pending.  Run this manually, call it from a cron job, or press 'r' in
    the TUI which triggers it automatically alongside the data refresh.
    """
    from coord import github_ops
    from coord.state import upsert_open_issues

    cfg = _load_config(config_path)
    total = 0
    for repo in cfg.repos:
        try:
            issues = github_ops.get_open_issues(repo.github)
            upsert_open_issues(repo.name, issues)
            if not quiet:
                click.echo(f"  {repo.name}: {len(issues)} open issue(s)")
            total += len(issues)
        except Exception as e:  # noqa: BLE001
            click.echo(f"  {repo.name}: sync failed — {e}", err=True)
    if not quiet:
        click.echo(f"synced {total} open issue(s) across {len(cfg.repos)} repo(s)")


@click.group("issue")
def issue_group() -> None:
    """Issue-tracker operations through the backend-agnostic seam.

    The write routes through the daemon (GitHub via `gh` today; GitLab /
    bare-DB later) so callers — notably the chat-about-issue session — never
    touch `gh` directly.
    """


@issue_group.command(
    "edit",
    help=(
        "Edit an issue's title and/or body. REPO is the local repo name from "
        "coordinator.yml; ISSUE is the GH issue number. Provide --title and/or "
        "--body / --body-file. Routes through the issue-tracker seam."
    ),
)


@click.argument("repo")
@click.argument("issue", type=int)
@click.option("--title", default=None, help="New issue title.")
@click.option("--body", default=None, help="New issue body (markdown).")
@click.option(
    "--body-file",
    type=click.Path(path_type=Path),
    default=None,
    help="Read the new body from a file (preferred for long markdown). '-' = stdin.",
)


@_CONFIG_OPTION
def issue_edit_cmd(
    repo: str,
    issue: int,
    title: str | None,
    body: str | None,
    body_file: Path | None,
    config_path: Path,
) -> None:
    cfg = _load_config(config_path)
    repo_entry = cfg.repo(repo)
    slug = repo_entry.github if repo_entry else repo
    if body_file is not None:
        body = sys.stdin.read() if str(body_file) == "-" else Path(body_file).read_text()
    if title is None and body is None:
        click.echo("error: provide --title and/or --body / --body-file", err=True)
        sys.exit(2)
    from coord.state import edit_issue_content  # noqa: PLC0415

    try:
        updated = edit_issue_content(
            repo, issue, title=title, body=body, repo_github=slug
        )
    except Exception as e:  # noqa: BLE001
        click.echo(f"error: issue edit failed: {e}", err=True)
        sys.exit(1)
    click.echo(f"#{issue} ({slug}) updated" if updated else f"#{issue} ({slug}): no change")


@click.group("context")
def context_group() -> None:
    """The per-issue rolling context digest (#603).

    Short, curated notes (cross-repo deps, approaches already tried, hard
    constraints) injected at the TOP of every agent briefing for the issue so
    findings don't evaporate between attempts.  DB-only, dropped when the issue
    closes.  Pinned entries stay on top and never age out.
    """


@context_group.command("show")
@click.argument("repo")
@click.argument("issue", type=int)
@_CONFIG_OPTION
def context_show(repo: str, issue: int, config_path: Path) -> None:
    """Print the rendered digest plus raw entries (with ids for pin/clear)."""
    from coord.state import list_issue_context, render_issue_context_entries

    entries = list_issue_context(repo, issue)
    if not entries:
        click.echo(f"(no context for {repo} #{issue})")
        return
    click.echo(render_issue_context_entries(entries))
    click.echo("\nentries (id · source · pinned):")
    for e in entries:
        pin = "📌" if e["pinned"] else "  "
        src = f" [{e['source']}]" if e.get("source") else ""
        click.echo(f"  {pin} #{e['id']}{src}: {e['body']}")


@context_group.command("add")
@click.argument("repo")
@click.argument("issue", type=int)
@click.argument("body")
@click.option(
    "--pin", "pinned", is_flag=True,
    help="Pin as a critical (always on top, never aged out by the budget).",
)


@click.option(
    "--source", default="operator",
    help="Who recorded this: work|fix|review|test|operator (default operator).",
)


@_CONFIG_OPTION
def context_add(
    repo: str, issue: int, body: str, pinned: bool, source: str, config_path: Path
) -> None:
    """Append a context entry for REPO #ISSUE (BODY is one short finding)."""
    from coord.state import add_issue_context_entry

    eid = add_issue_context_entry(repo, issue, body, pinned=pinned, source=source)
    tag = " (pinned)" if pinned else ""
    suffix = f" (id {eid})" if eid else ""
    click.echo(f"added{tag} to {repo} #{issue}{suffix}")


@context_group.command("pin")
@click.argument("repo")
@click.argument("issue", type=int)
@click.argument("entry_id", type=int)
@_CONFIG_OPTION
def context_pin(repo: str, issue: int, entry_id: int, config_path: Path) -> None:
    """Pin entry ENTRY_ID so it stays on top and never ages out."""
    from coord.state import set_issue_context_pin

    click.echo("pinned" if set_issue_context_pin(repo, issue, entry_id, True) else "no such entry")


@context_group.command("unpin")
@click.argument("repo")
@click.argument("issue", type=int)
@click.argument("entry_id", type=int)
@_CONFIG_OPTION
def context_unpin(repo: str, issue: int, entry_id: int, config_path: Path) -> None:
    """Unpin entry ENTRY_ID (it becomes a normal aged-out note)."""
    from coord.state import set_issue_context_pin

    click.echo("unpinned" if set_issue_context_pin(repo, issue, entry_id, False) else "no such entry")


@context_group.command("clear")
@click.argument("repo")
@click.argument("issue", type=int)
@_CONFIG_OPTION
def context_clear(repo: str, issue: int, config_path: Path) -> None:
    """Delete ALL context entries for REPO #ISSUE."""
    from coord.state import clear_issue_context

    n = clear_issue_context(repo, issue)
    click.echo(f"cleared {n} entr{'y' if n == 1 else 'ies'} for {repo} #{issue}")


@context_group.command("curate")
@click.argument("repo")
@click.argument("issue", type=int)
@click.option(
    "--model", default="haiku",
    help="claude -p model for the compress (default haiku — cheap).",
)


@_CONFIG_OPTION
def context_curate(repo: str, issue: int, model: str, config_path: Path) -> None:
    """LLM-compress the digest: merge duplicates, drop resolved notes, keep
    pinned criticals.  On-demand (one metered `claude -p` call) — the everyday
    cap+pins curation is automatic and free."""
    import json as _json
    import re as _re

    from coord.state import list_issue_context, replace_issue_context
    from coord.test_orchestrator import _call_claude

    entries = list_issue_context(repo, issue)
    if len(entries) <= 3:
        click.echo(f"{repo} #{issue}: {len(entries)} entries — nothing to curate.")
        return
    payload = _json.dumps(
        [{"body": e["body"], "pinned": e["pinned"], "source": e.get("source")}
         for e in entries],
        indent=2,
    )
    system = (
        "You compress a SHORT per-issue engineering context digest injected at "
        "the top of an AI agent's briefing. Rules: merge duplicates; drop "
        "resolved / obsolete / now-irrelevant notes; KEEP every cross-repo "
        "dependency, hard constraint, and failed-approach lesson; never invent "
        "facts. Preserve pinned=true for criticals (deps/constraints). Aim for "
        "<= 8 entries, each one tight line. Output ONLY a JSON array of "
        '{"body": str, "pinned": bool} — no prose, no code fences.'
    )
    try:
        raw = _call_claude(system, payload, model=model)
    except Exception as exc:  # noqa: BLE001
        click.echo(f"error: curate failed: {exc}", err=True)
        sys.exit(1)
    match = _re.search(r"\[.*\]", raw, _re.DOTALL)
    try:
        parsed = _json.loads(match.group(0)) if match else None
        assert isinstance(parsed, list)
    except Exception:  # noqa: BLE001
        click.echo(
            "error: curate returned unparseable output; context left unchanged.",
            err=True,
        )
        sys.exit(1)
    cleaned = [
        {"body": str(e.get("body", "")).strip(),
         "pinned": bool(e.get("pinned")), "source": "curated"}
        for e in parsed
        if str(e.get("body", "")).strip()
    ]
    if not cleaned:
        click.echo(
            "error: curate produced no entries; context left unchanged.", err=True
        )
        sys.exit(1)
    replace_issue_context(repo, issue, cleaned)
    click.echo(f"curated {repo} #{issue}: {len(entries)} → {len(cleaned)} entries")


@click.command(
    help=(
        "Send an issue to the Pipeline as DISPATCHABLE by tagging it with "
        "both the `coord` and `status:ready` labels on GitHub.\n\n"
        "A dispatchable Pipeline:New card needs BOTH labels.  Coordinator "
        "issues are often *created* with `coord` already, so adding only "
        "`coord` was a no-op that left them stuck without `status:ready` "
        "(#486 Leg 4 bug).  This now ensures both — idempotent: in the normal "
        "Refining → Refined (`coord ready`) → Send flow the issue already has "
        "`status:ready`, so only `coord` is added.  Any pre-Pipeline "
        "`status:refining` / `status:backlog` label is cleared, mirroring "
        "`coord ready`.\n\n"
        "REPO is the local repo name from coordinator.yml; ISSUE is the "
        "GH issue number."
    )
)


@click.argument("repo")
@click.argument("issue", type=int)
@_CONFIG_OPTION
def track(repo: str, issue: int, config_path: Path) -> None:
    """#261/#486: TUI right-click 'Send to Pipeline' fires this command to
    make the issue a dispatchable Pipeline:New card (`coord` + `status:ready`)."""
    cfg = _load_config(config_path)
    repo_entry = cfg.repo(repo)
    slug = repo_entry.github if repo_entry else repo
    _apply_label_change(
        repo, issue, config_path,
        add={"coord", "status:ready"},
        remove_if_present={"status:refining", "status:backlog"},
        success_message=(
            f"#{issue} ({slug}) sent to Pipeline (coord + status:ready)"
        ),
        no_op_message=(
            f"#{issue} ({slug}) already dispatchable "
            "(coord + status:ready present)"
        ),
    )


@click.command(
    help=(
        "Remove an issue from the Pipeline, returning it to the Board's "
        "Backlog.  Strips the `coord` label (Pipeline membership is the "
        "`coord` label, so this is the only way to evict a card) plus any "
        "`status:*` label, so the issue lands in Backlog rather than "
        "Refined/Refining.\n\n"
        "Inverse of `coord track` (Send to Pipeline).  The TUI right-click "
        "'Drop to backlog' on a Pipeline row fires this.\n\n"
        "REPO is the local repo name from coordinator.yml; ISSUE is the "
        "GH issue number."
    )
)


@click.argument("repo")
@click.argument("issue", type=int)
@_CONFIG_OPTION
def untrack(repo: str, issue: int, config_path: Path) -> None:
    """#266: TUI right-click 'Drop to backlog' on a Pipeline row fires this to
    evict the issue from the coord Pipeline (removes `coord` + any `status:*`)."""
    cfg = _load_config(config_path)
    repo_entry = cfg.repo(repo)
    slug = repo_entry.github if repo_entry else repo
    _apply_label_change(
        repo, issue, config_path,
        add=set(),
        remove_if_present={
            "coord", "status:ready", "status:refining", "status:backlog",
        },
        success_message=f"#{issue} ({slug}) dropped to Backlog (removed from Pipeline)",
        no_op_message=f"#{issue} ({slug}) not in the Pipeline (no coord label)",
    )


@click.command(
    help=(
        "Drop an issue back to Backlog by removing its `status:*` label.\n\n"
        "Symmetric with `coord refine` / `coord ready` — strips both "
        "`status:refining` and `status:ready` if present, returning the "
        "issue to the unscoped Backlog state.\n\n"
        "REPO is the local repo name from coordinator.yml; ISSUE is the "
        "GH issue number."
    )
)


@click.argument("repo")
@click.argument("issue", type=int)
@_CONFIG_OPTION
def backlog(repo: str, issue: int, config_path: Path) -> None:
    """#266: TUI right-click 'Drop to Backlog' fires this command to
    walk a Refining/Refined row back to the unscoped Backlog state."""
    cfg = _load_config(config_path)
    repo_entry = cfg.repo(repo)
    slug = repo_entry.github if repo_entry else repo
    _apply_label_change(
        repo, issue, config_path,
        add=set(),
        remove_if_present={"status:refining", "status:ready"},
        success_message=f"#{issue} ({slug}) dropped to Backlog",
        no_op_message=f"#{issue} ({slug}) already in Backlog (no status:* label)",
    )