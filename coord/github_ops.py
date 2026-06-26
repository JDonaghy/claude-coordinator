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
    # #658: raised from 100 → 500 so repos with many open issues don't silently
    # skip old issue numbers during coord sync.  GitHub paginates the REST list
    # endpoint at 100 items internally, so this costs ~5 API calls for a large
    # repo — acceptable for a background sync.
    raw = _gh(
        "issue", "list", "--repo", repo, "--state", "open",
        "--json", "number,title,labels,milestone,body,assignees",
        "--limit", "500",
    )
    return json.loads(raw)


def get_issue(repo: str, issue_number: int) -> dict:
    """Fetch a single issue by number. Returns {number, title, body, state, ...}."""
    raw = _gh(
        "issue", "view", str(issue_number), "--repo", repo,
        "--json", "number,title,body,state",
    )
    return json.loads(raw)


def edit_issue(
    repo: str,
    issue_number: int,
    *,
    title: str | None = None,
    body: str | None = None,
) -> None:
    """Edit an issue's title and/or body. The GitHub backend of the
    issue-tracker seam (`state.edit_issue_content`) — GitLab / bare-DB adapters
    slot in alongside this later. The body is piped via stdin (`--body-file -`)
    to avoid arg-length and shell-quoting issues on long markdown bodies."""
    if title is None and body is None:
        return
    args = ["issue", "edit", str(issue_number), "--repo", repo]
    if title is not None:
        args += ["--title", title]
    if body is not None:
        args += ["--body-file", "-"]
    result = subprocess.run(
        ["gh", *args],
        input=body if body is not None else None,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"gh issue edit #{issue_number} failed: {result.stderr.strip()}"
        )


def issue_is_closed(repo: str, issue_number: int) -> bool:
    """True when issue ``issue_number`` is closed on GitHub.

    Best-effort and **fail-open**: any ``gh`` error returns ``False`` so a
    transient GitHub/CLI failure never silently blocks a legitimate dispatch.
    """
    try:
        return get_issue(repo, issue_number).get("state", "").upper() == "CLOSED"
    except (RuntimeError, json.JSONDecodeError):
        return False


def pr_is_merged(repo: str, branch: str) -> bool:
    """True when a PR whose head is ``branch`` has been merged on ``repo``.

    Uses ``gh pr list --head <branch> --state all`` rather than ``pr view`` so
    the result survives **branch deletion after merge** and the quadraui case
    where a PR merged into ``develop`` leaves its linked issue OPEN (so
    :func:`issue_is_closed` would miss it).  Best-effort and **fail-open**:
    returns ``False`` when there is no PR, the PR is still open, or ``gh``
    errors — never blocks a legitimate dispatch on a transient failure.
    """
    if not branch:
        return False
    try:
        raw = _gh(
            "pr", "list", "--repo", repo, "--head", branch,
            "--state", "all", "--json", "number,state,mergedAt", "--limit", "10",
        )
    except RuntimeError:
        return False
    try:
        prs = json.loads(raw)
    except json.JSONDecodeError:
        return False
    return any(
        p.get("mergedAt") or p.get("state", "").upper() == "MERGED" for p in prs
    )


def work_is_terminal(
    repo_github: str,
    issue_number: int | None,
    branch: str | None,
    *,
    cache: dict | None = None,
) -> bool:
    """True when work is already done on GitHub: **issue closed OR PR merged**.

    The single chokepoint guard (#522) consulted before any fix/review
    dispatch, so already-merged/closed work can never re-enter the loop (the
    root cause of the 2026-06-09 launch flood: #349 ×4, #194).

    Best-effort and **fail-open**: any error resolves to ``False`` so a
    transient GitHub/CLI failure never blocks a legitimate dispatch.

    *cache* — optional ``dict`` shared across a single ``notify`` run, keyed by
    ``(repo_github, issue_number, branch)``, so a burst of transitions for the
    same merged issue costs **one** ``gh`` round-trip, not one per call.
    """
    if not repo_github:
        return False

    key = (repo_github, issue_number, branch)
    if cache is not None and key in cache:
        return cache[key]

    terminal = False
    try:
        if issue_number and issue_is_closed(repo_github, issue_number):
            terminal = True
        elif branch and pr_is_merged(repo_github, branch):
            terminal = True
    except Exception:  # noqa: BLE001 — fail-open: never block a dispatch
        terminal = False

    if cache is not None:
        cache[key] = terminal
    return terminal


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


def remove_issue_label(repo: str, issue_number: int, label: str) -> None:
    """Remove a label from an issue via ``gh issue edit --remove-label``.

    Idempotent — ``gh`` silently no-ops if the label is not present.
    Raises RuntimeError on ``gh`` failure.
    """
    _gh("issue", "edit", str(issue_number), "--repo", repo, "--remove-label", label)


_TEST_MODE_LABELS = ("test-mode:smoke", "test-mode:auto")


def set_test_mode_label(
    repo_github: str,
    repo_name: str,
    issue_number: int,
    mode: str,
) -> None:
    """Persist the per-issue test-mode policy as a GitHub label.

    Removes any existing ``test-mode:*`` label then adds ``test-mode:{mode}``.
    Also updates the local issues cache so the TUI pipeline reflects the change
    without waiting for the next ``coord sync``.

    ``repo_github`` — ``owner/name`` slug for the ``gh`` CLI.
    ``repo_name``   — coordinator-local repo name for the DB cache.
    ``mode``        — ``"smoke"`` or ``"auto"``.
    """
    from coord import state as _state  # noqa: PLC0415

    if mode not in ("smoke", "auto"):
        raise ValueError(f"mode must be 'smoke' or 'auto', got {mode!r}")

    # Step 1: remove any stale test-mode:* labels.
    for old_label in _TEST_MODE_LABELS:
        try:
            remove_issue_label(repo_github, issue_number, old_label)
        except RuntimeError:
            pass  # already absent — not an error

    # Step 2: add the new label (idempotent).
    new_label = f"test-mode:{mode}"
    add_issue_labels(repo_github, issue_number, [new_label])

    # Step 3: refresh the local cache so the TUI sees the update.
    try:
        issue_data = get_issue(repo_github, issue_number)
        current_labels = [lbl.get("name", "") for lbl in issue_data.get("labels", [])]
        _state.update_issue_labels(repo_name, issue_number, current_labels)
    except Exception:
        pass  # cache update is best-effort


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


def list_remote_branch_names(repo: str) -> set[str]:
    """Return the set of branch names that currently exist on `repo` (owner/name).

    One paginated ``gh api`` call.  Used by ``coord merge`` to skip re-enqueuing
    done-work whose branch was already merged-and-deleted (the dominant
    merge-queue clog source).  Returns an empty set on error so callers can
    fail OPEN (treat "couldn't determine" as "don't skip").
    """
    try:
        raw = _gh(
            "api", "--paginate",
            f"repos/{repo}/git/refs/heads",
            "--jq", ".[].ref",
        )
    except RuntimeError:
        return set()
    prefix = "refs/heads/"
    return {
        line.strip()[len(prefix):]
        for line in raw.splitlines()
        if line.strip().startswith(prefix)
    }


def branch_exists_on_remote(repo: str, branch: str) -> bool:
    """Return True if `branch` currently exists on `repo` (owner/name) at GitHub.

    Uses a targeted ``gh api`` call rather than listing all branches.  Fails
    OPEN (returns True) on any infrastructure problem — an unresponsive ``gh``,
    a network glitch, or an authentication issue must never prevent a legitimate
    dispatch.  Only returns False when we receive a clear "not found" signal
    from GitHub (HTTP 4xx in the error output).

    Called by ``dispatch_review`` and ``_dispatch_fix`` (#586) to avoid
    routing a follow-on assignment to a machine that can't fetch the branch.
    """
    try:
        _gh("api", f"repos/{repo}/git/refs/heads/{branch}")
        return True
    except RuntimeError as exc:
        err = str(exc).lower()
        # Only return False when GitHub explicitly told us the ref doesn't
        # exist (HTTP 4xx response).  Any other failure (gh not installed,
        # not authenticated, network timeout) is treated as "unknown" and we
        # fail OPEN so the guard doesn't block legitimate dispatch.
        if "http 4" in err or "could not resolve" in err or "not found" in err:
            return False
        return True


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


def pr_diff(repo_github: str, pr_number: int, *, max_chars: int = 60000) -> str | None:
    """Return the merge-base (three-dot) diff for PR ``pr_number``, or None.

    ``gh pr diff`` is three-dot / merge-base by GitHub semantics, so the output
    is exactly the branch's own changes (#612) — code merged to the base after
    the branch was cut never appears as spurious deletions. Truncated to
    *max_chars* with a trailing note so a huge diff can't blow the briefing
    size. Best-effort: returns None on any ``gh`` error so the caller falls
    back to the in-briefing three-dot diff instructions.
    """
    try:
        diff = _gh("pr", "diff", str(pr_number), "--repo", repo_github)
    except RuntimeError:
        return None
    if len(diff) > max_chars:
        diff = diff[:max_chars] + f"\n... [diff truncated at {max_chars} chars] ..."
    return diff


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


def get_branch_diff_size(repo: str, base: str, branch: str) -> int:
    """Return total diff size (additions+deletions) for *branch* relative to *base*.

    Uses the GitHub three-dot compare API — no PR required.  Sums
    ``additions + deletions`` across all changed files.  Returns ``0`` on any
    failure so callers can treat size as unknown-but-not-blocking.

    Prefer this over :func:`get_pr_size` at enqueue time so size is populated
    before a PR is opened and the ordering shown to the user matches the
    ordering used at merge time (#776 size unification).
    """
    try:
        raw = _gh("api", f"repos/{repo}/compare/{base}...{branch}")
        data = json.loads(raw)
        return sum(
            int(f.get("additions", 0)) + int(f.get("deletions", 0))
            for f in data.get("files", [])
        )
    except Exception:  # noqa: BLE001 — fail-open: unknown size is not blocking
        return 0


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


def close_pr(repo: str, number: int, *, comment: str | None = None) -> None:
    """Close an open PR, optionally posting a comment first.

    Posts *comment* (if given) via ``gh issue comment`` — PRs share the GitHub
    issue comment stream — then closes the PR via ``gh pr close``.  Raises
    RuntimeError on ``gh`` failure.
    """
    if comment:
        post_issue_comment(repo, number, comment)
    _gh("pr", "close", str(number), "--repo", repo)


def branch_is_fully_merged(
    repo: str,
    branch: str,
    default_branch: str = "main",
) -> bool:
    """Return True when *branch* has 0 commits ahead of *default_branch*.

    Uses the GitHub three-dot compare API.  Returns False on any error —
    fail-safe so we never accidentally close a live PR.

    Note: only detects **fast-forward** merges.  After a squash or rebase
    merge the branch's original commits remain "ahead" (different SHAs) even
    though the work has landed.  The ``issue_is_closed`` check is the primary
    stale-PR signal for those cases.
    """
    if not branch or not default_branch or branch == default_branch:
        return False
    try:
        raw = _gh("api", f"repos/{repo}/compare/{default_branch}...{branch}")
        cmp = json.loads(raw)
        return isinstance(cmp, dict) and cmp.get("ahead_by") == 0
    except Exception:  # noqa: BLE001 — fail-safe: keep the PR open on any error
        return False


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
