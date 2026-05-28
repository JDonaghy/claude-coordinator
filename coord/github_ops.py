"""GitHub operations via gh CLI."""

from __future__ import annotations

import json
import subprocess


def _gh(*args: str) -> str:
    result = subprocess.run(
        ["gh", *args],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def get_open_issues(repo: str) -> list[dict]:
    raw = _gh(
        "issue", "list", "--repo", repo, "--state", "open",
        "--json", "number,title,labels,milestone,body,assignees",
        "--limit", "100",
    )
    return json.loads(raw)


def get_issue(repo: str, issue_number: int) -> dict:
    """Fetch a single issue by number. Returns {number, title, body, state, ...}."""
    raw = _gh(
        "issue", "view", str(issue_number), "--repo", repo,
        "--json", "number,title,body,state",
    )
    return json.loads(raw)


def post_issue_comment(repo: str, issue_number: int, body: str):
    _gh("issue", "comment", str(issue_number), "--repo", repo, "--body", body)


def add_issue_labels(repo: str, issue_number: int, labels: list[str]) -> None:
    """Add labels to an issue. Idempotent — `gh issue edit --add-label`
    silently no-ops when the label is already present.  Raises RuntimeError
    on `gh` failure; callers should wrap in try/except when labeling is
    best-effort (e.g. post-dispatch auto-tagging)."""
    if not labels:
        return
    args = ["issue", "edit", str(issue_number), "--repo", repo]
    for lbl in labels:
        args.extend(["--add-label", lbl])
    _gh(*args)


def get_repo_file(repo: str, path: str, branch: str = "develop") -> str:
    import base64
    raw = _gh("api", f"repos/{repo}/contents/{path}?ref={branch}")
    data = json.loads(raw)
    return base64.b64decode(data["content"]).decode()


def check_branch_exists(repo: str, branch: str) -> bool:
    try:
        _gh("api", f"repos/{repo}/branches/{branch}")
        return True
    except RuntimeError:
        return False


def delete_remote_branch(repo: str, branch: str) -> bool:
    """Delete a remote branch. Returns True on success, False on failure."""
    try:
        _gh("api", "-X", "DELETE", f"repos/{repo}/git/refs/heads/{branch}")
        return True
    except RuntimeError:
        return False


def get_default_branch_head(repo: str, branch: str) -> str:
    """Return the full commit SHA at the tip of `branch` on `repo` (owner/name)."""
    raw = _gh("api", f"repos/{repo}/branches/{branch}")
    data = json.loads(raw)
    return data["commit"]["sha"]


# ── PR operations (used by the merge queue) ──────────────────────────────

def find_pr_for_branch(repo: str, branch: str) -> dict | None:
    """Return the first open PR whose head ref matches `branch`, or None."""
    raw = _gh(
        "pr", "list", "--repo", repo, "--state", "open",
        "--head", branch,
        "--json", "number,title,url,headRefName,baseRefName,additions,deletions,mergeable",
        "--limit", "1",
    )
    items = json.loads(raw)
    return items[0] if items else None


def create_pr(
    repo: str,
    *,
    base: str,
    head: str,
    title: str,
    body: str,
) -> dict:
    """Open a PR. Returns {number, url}. If one already exists for `head`, returns it."""
    existing = find_pr_for_branch(repo, head)
    if existing is not None:
        return {"number": existing["number"], "url": existing["url"], "existed": True}
    url = _gh(
        "pr", "create", "--repo", repo,
        "--base", base, "--head", head,
        "--title", title, "--body", body,
    )
    # gh pr create returns the URL on the last line of stdout.
    pr_url = url.strip().splitlines()[-1] if url.strip() else ""
    number = int(pr_url.rsplit("/", 1)[-1]) if pr_url else 0
    return {"number": number, "url": pr_url, "existed": False}


def get_pr_size(repo: str, number: int) -> int:
    """Return additions+deletions for sequencing. 0 on lookup failure."""
    try:
        raw = _gh(
            "pr", "view", str(number), "--repo", repo,
            "--json", "additions,deletions",
        )
    except RuntimeError:
        return 0
    data = json.loads(raw)
    return int(data.get("additions", 0)) + int(data.get("deletions", 0))


def merge_pr(repo: str, number: int, method: str = "rebase") -> tuple[bool, str]:
    """Merge a PR. Returns (success, message).

    Conflict / not-rebaseable cases come back as (False, <gh stderr>). Caller
    decides whether to retry or surface to the user — we never resolve conflicts
    here.
    """
    flag = {"rebase": "--rebase", "squash": "--squash", "merge": "--merge"}.get(method, "--rebase")
    try:
        out = _gh("pr", "merge", str(number), "--repo", repo, flag, "--delete-branch=false")
    except RuntimeError as e:
        return False, str(e)
    return True, out


def list_open_prs(repo: str) -> list[dict]:
    raw = _gh(
        "pr", "list", "--repo", repo, "--state", "open",
        "--json", "number,title,headRefName",
    )
    return json.loads(raw)


def get_recent_develop_commits(repo: str, count: int = 10) -> list[dict]:
    raw = _gh(
        "api", f"repos/{repo}/commits?sha=develop&per_page={count}",
    )
    commits = json.loads(raw)
    return [
        {"sha": c["sha"][:7], "message": c["commit"]["message"].split("\n")[0]}
        for c in commits
    ]


def create_issue(
    repo: str,
    title: str,
    body: str,
    labels: list[str] | None = None,
    milestone: str | None = None,
) -> dict:
    args = ["issue", "create", "--repo", repo, "--title", title, "--body", body]
    if labels:
        for label in labels:
            args.extend(["--label", label])
    if milestone:
        args.extend(["--milestone", milestone])
    raw = _gh(*args)
    url = raw.strip()
    number = int(url.rstrip("/").rsplit("/", 1)[-1])
    return {"number": number, "url": url}


def update_issue_body(repo: str, issue_number: int, body: str) -> None:
    _gh(
        "api", "-X", "PATCH",
        f"repos/{repo}/issues/{issue_number}",
        "-f", f"body={body}",
    )


def post_pr_review(repo: str, number: int, verdict: str, body: str) -> None:
    """Post a PR review via the gh CLI.

    *verdict* must be ``"approve"`` or ``"request-changes"``.  Any other value
    raises :class:`ValueError` before invoking gh.
    """
    if verdict == "approve":
        flag = "--approve"
    elif verdict == "request-changes":
        flag = "--request-changes"
    else:
        raise ValueError(f"Invalid review verdict: {verdict!r} (must be 'approve' or 'request-changes')")
    _gh("pr", "review", str(number), "--repo", repo, flag, "--body", body)
