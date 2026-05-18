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


def post_issue_comment(repo: str, issue_number: int, body: str):
    _gh("issue", "comment", str(issue_number), "--repo", repo, "--body", body)


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


def get_default_branch_head(repo: str, branch: str) -> str:
    """Return the full commit SHA at the tip of `branch` on `repo` (owner/name)."""
    raw = _gh("api", f"repos/{repo}/branches/{branch}")
    data = json.loads(raw)
    return data["commit"]["sha"]


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
