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


def get_closed_epics(repo: str, *, label: str = "epic") -> list[dict]:
    """Return closed issues in *repo* carrying *label* (default ``"epic"``).

    Used by ``coord plans`` (#974) so a milestone's tracking epic is still
    found once it has been closed while the milestone itself stays open
    (e.g. all work-order nodes finished and someone tidied up the epic
    before remembering to close the milestone) — see
    :func:`coord.plans.find_tracking_issue`. A small, label-filtered,
    closed-only lookup rather than a full ``--state all`` issue fetch, since
    only closed *epics* are of interest here.
    """
    raw = _gh(
        "issue", "list", "--repo", repo, "--state", "closed", "--label", label,
        "--json", "number,title,labels,milestone,body,assignees",
        "--limit", "500",
    )
    return json.loads(raw)


def get_issue(repo: str, issue_number: int) -> dict:
    """Fetch a single issue by number.

    Returns ``{number, title, body, state, milestone, labels, ...}``.
    ``milestone`` is ``None`` when the issue has none, else ``{"number":
    ..., "title": ...}`` — used by ``coord milestone order`` (#768) to
    resolve a tracking issue's milestone and validate node membership
    without a second call. ``labels`` is a list of ``{"name": ..., ...}``
    dicts — #1138's ``enforce_oracle_readiness`` reads issue labels (e.g.
    ``oracle:exempt``) off this same call, so it must be requested here
    too, not just on the list endpoints (``get_open_issues``,
    ``get_closed_epics``).
    """
    raw = _gh(
        "issue", "view", str(issue_number), "--repo", repo,
        "--json", "number,title,body,state,milestone,labels",
    )
    return json.loads(raw)


# ── Sub-issues (#1195) ───────────────────────────────────────────────────────
#
# The REST sub-issues API is live on GitHub today but used nowhere in this
# repo before #1195 — every epic->child relation so far is the `## Work
# order` / `## Sub-issues` markdown checklist `coord.milestone_order` parses.
# These wrap the raw endpoints; `coord.parentage_github.GitHubParentage` is
# the adapter that turns them into the backend-agnostic `coord.parentage`
# seam shape (`Child`/`ParentRef`).
#
# Gotcha verified while filing #1195: the write endpoints (POST/DELETE) take
# the child's internal database `id`, NOT its issue `number` — resolve via
# `get_issue`'s `--jq .id` before writing (see `_resolve_issue_id`).


def get_sub_issues(repo: str, issue_number: int) -> list[dict]:
    """The live sub-issues of *issue_number* (``GET .../sub_issues``).

    Returns ``[]`` for an issue with no sub-issues (confirmed live: this is
    the API's normal response, not a 404/410 — see #1195's filing notes).
    Each item is a full issue object; callers only need ``number``/``state``.
    """
    raw = _gh("api", f"repos/{repo}/issues/{issue_number}/sub_issues")
    return json.loads(raw)


def get_issue_parent(repo: str, issue_number: int) -> dict | None:
    """The parent of *issue_number*, or ``None`` when it has none.

    Reads the ``parent`` field GitHub already includes on ``GET
    /issues/{n}`` (confirmed live while filing #1195 — no preview header
    needed). ``None`` covers both "field absent" and the documented
    ``parent: null`` shape.
    """
    raw = _gh("api", f"repos/{repo}/issues/{issue_number}", "--jq", ".parent")
    stripped = raw.strip()
    if not stripped or stripped == "null":
        return None
    return json.loads(stripped)


def _resolve_issue_id(repo: str, issue_number: int) -> int:
    """Issue `number` -> internal database `id` (#1195's write-path gotcha:
    the sub-issues POST/DELETE endpoints want the latter, not the former)."""
    raw = _gh("api", f"repos/{repo}/issues/{issue_number}", "--jq", ".id")
    return int(raw.strip())


def add_sub_issue(repo: str, parent_number: int, child_number: int) -> None:
    """Make *child_number* a sub-issue of *parent_number* (``POST
    .../sub_issues``). Resolves *child_number* to its database id first —
    the endpoint wants ``sub_issue_id`` (a database id), not the issue
    number, and 422s if the body doesn't shape up."""
    child_id = _resolve_issue_id(repo, child_number)
    _gh(
        "api", f"repos/{repo}/issues/{parent_number}/sub_issues",
        "--method", "POST",
        "-F", f"sub_issue_id={child_id}",
    )


def remove_sub_issue(repo: str, parent_number: int, child_number: int) -> None:
    """Detach *child_number* from *parent_number* (``DELETE
    .../sub_issue`` — singular, unlike the GET/POST plural; a real GitHub
    API asymmetry, not a typo here)."""
    child_id = _resolve_issue_id(repo, child_number)
    _gh(
        "api", f"repos/{repo}/issues/{parent_number}/sub_issue",
        "--method", "DELETE",
        "-F", f"sub_issue_id={child_id}",
    )


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


def create_milestone(
    repo: str,
    title: str,
    *,
    description: str | None = None,
    due_on: str | None = None,
) -> dict:
    """Create a GitHub milestone via ``gh api POST .../milestones`` (#645 seam).

    The GitHub backend of the milestone-tracker seam
    (``coord.state.write_milestone``) — GitLab / bare-DB adapters slot in
    alongside this later, same as ``edit_issue``. ``due_on`` is an ISO 8601
    timestamp (e.g. ``"2026-08-01T00:00:00Z"``) per the GitHub API; this
    layer does not validate the format, it just forwards it. Returns the
    created milestone's JSON (``number``, ``title``, ``description``,
    ``due_on``, ``html_url``, ...).
    """
    args = ["api", f"repos/{repo}/milestones", "-f", f"title={title}"]
    if description is not None:
        args += ["-f", f"description={description}"]
    if due_on is not None:
        args += ["-f", f"due_on={due_on}"]
    raw = _gh(*args)
    return json.loads(raw)


def edit_milestone(
    repo: str,
    number: int,
    *,
    title: str | None = None,
    description: str | None = None,
    due_on: str | None = None,
) -> dict:
    """Edit a GitHub milestone's title/description/due date via
    ``gh api -X PATCH .../milestones/{number}`` (#645 seam, mirrors
    ``edit_issue``). A no-op (all three fields ``None``) returns ``{}``
    without shelling out. Returns the updated milestone's JSON."""
    if title is None and description is None and due_on is None:
        return {}
    args = ["api", "-X", "PATCH", f"repos/{repo}/milestones/{number}"]
    if title is not None:
        args += ["-f", f"title={title}"]
    if description is not None:
        args += ["-f", f"description={description}"]
    if due_on is not None:
        args += ["-f", f"due_on={due_on}"]
    raw = _gh(*args)
    return json.loads(raw)


def close_issue(repo: str, issue_number: int, *, comment: str | None = None) -> None:
    """Close a GitHub issue, optionally posting *comment* first.

    The deterministic counterpart to a ``Closes #N`` keyword in a PR body:
    ``coord merge`` calls this after a successful merge so an issue is never
    stranded open when a worker-created PR forgot the keyword (and
    conventional-commit ``fix(#N):`` subjects are *not* GitHub closing
    keywords).  Idempotent — closing an already-closed issue is a no-op.
    Raises RuntimeError on any other ``gh`` failure.  Part of the
    issue-tracker seam (GitHub backend); GitLab / bare-DB adapters slot in
    alongside this later (#806).
    """
    if comment:
        post_issue_comment(repo, issue_number, comment)
    result = subprocess.run(
        ["gh", "issue", "close", str(issue_number), "--repo", repo],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0 and "already closed" not in result.stderr.lower():
        raise RuntimeError(
            f"gh issue close #{issue_number} failed: {result.stderr.strip()}"
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
    """True when ``branch``'s *current* tip is a commit that actually merged on ``repo``.

    Uses ``gh pr list --head <branch> --state all`` rather than ``pr view`` so
    the result survives **branch deletion after merge** and the quadraui case
    where a PR merged into ``develop`` leaves its linked issue OPEN (so
    :func:`issue_is_closed` would miss it).  Best-effort and **fail-open**:
    returns ``False`` when there is no PR, the PR is still open, or ``gh``
    errors — never blocks a legitimate dispatch on a transient failure.

    #1150: branch reuse across merge cycles is a designed pattern
    (``--fix-of``/``--rework-of`` continue on the same branch; ``--force`` can
    re-target a branch name with prior history) — so "a PR with this head ref
    name merged *at some point*" is not proof that the branch's *current*
    commits are merged. To distinguish those cases, once a merged PR is found
    we resolve the branch's current tip via :func:`get_branch_sha` (the same
    GitHub-API SHA lookup #821 uses for stale-review detection) and require it
    to match the merged PR's ``headRefOid`` — the exact commit that landed.

    When the tip can't be resolved via ``get_branch_sha`` (it fails closed to
    ``None`` on *any* error, transient or not — see its docstring), we do
    **not** blindly trust the historical merge, because that would reintroduce
    this same issue's bug class under a transient-failure trigger: a rate
    limit or network blip at the wrong moment would read as "already merged"
    and callers (``reconcile``'s merge sweep, ``prune_stale_queue_entries``)
    would permanently mark live, unmerged work as done or delete its queue
    entry. Trusting history is only actually safe in the one case where it's
    *structurally* impossible for new commits to exist: the branch was
    positively confirmed deleted (a 404, via :func:`branch_exists_on_remote`,
    which distinguishes "GitHub said not found" from any other failure).
    Every other unresolved case — auth hiccup, timeout, rate limit — fails
    open toward ``False`` ("not yet merged"), matching this function's and
    ``prune_stale_queue_entries``'s documented fail-open convention.
    """
    if not branch:
        return False
    try:
        raw = _gh(
            "pr", "list", "--repo", repo, "--head", branch,
            "--state", "all", "--json", "number,state,mergedAt,headRefOid",
            "--limit", "10",
        )
    except RuntimeError:
        return False
    try:
        prs = json.loads(raw)
    except json.JSONDecodeError:
        return False
    merged = [
        p for p in prs
        if p.get("mergedAt") or p.get("state", "").upper() == "MERGED"
    ]
    if not merged:
        return False

    current_sha = get_branch_sha(repo, branch)
    if current_sha is not None:
        return any(p.get("headRefOid") == current_sha for p in merged)
    # SHA lookup failed. Only trust the historical merge if we can positively
    # confirm the branch is gone (a 404 means no further commits could have
    # been pushed to it). Any other failure (transient network/auth/rate
    # limit) fails open toward False — see docstring.
    if not branch_exists_on_remote(repo, branch):
        return True  # confirmed deleted — no new commits possible; trust history
    return False


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


def change_issue_labels(
    repo: str,
    issue_number: int,
    *,
    add: set[str],
    remove: set[str],
) -> tuple[list[str], bool]:
    """Atomically add and/or remove arbitrary labels on an issue (#802).

    Fetches the current label set first, computes the minimal delta, and
    runs a single ``gh issue edit`` call only when something actually
    changes — tolerates already-present ``add`` labels and already-absent
    ``remove`` labels (idempotent, matches ``_apply_label_change``'s
    pre-#802 behavior).

    Returns ``(new_labels, changed)`` where ``new_labels`` is the final
    label list (sorted) and ``changed`` is ``True`` when any labels were
    added or removed. Raises ``RuntimeError`` on ``gh`` failure.
    """
    view_raw = _gh(
        "issue", "view", str(issue_number), "--repo", repo, "--json", "labels",
    )
    current: set[str] = {
        lbl.get("name", "")
        for lbl in json.loads(view_raw).get("labels", [])
    }

    to_add = add - current
    to_remove = remove & current
    changed = bool(to_add or to_remove)

    if changed:
        args = ["issue", "edit", str(issue_number), "--repo", repo]
        for lbl in sorted(to_add):
            args.extend(["--add-label", lbl])
        for lbl in sorted(to_remove):
            args.extend(["--remove-label", lbl])
        _gh(*args)

    new_labels = sorted((current - to_remove) | to_add)
    return new_labels, changed


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


def get_branch_sha(repo: str, branch: str) -> str | None:
    """Return the current HEAD SHA for *branch* on *repo*, or ``None`` on failure.

    Best-effort wrapper around the GitHub branches API.  Returns ``None`` when
    GitHub is unavailable, ``gh`` is not authenticated, or the branch does not
    exist — callers treat ``None`` as "SHA tracking unavailable" and skip the
    commit-bound staleness check introduced in #821.
    """
    try:
        raw = _gh("api", f"repos/{repo}/branches/{branch}")
        data = json.loads(raw)
        return data["commit"]["sha"]
    except Exception:  # noqa: BLE001 — fail-safe: unknown SHA is not blocking
        return None


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


def get_repo_milestones(repo: str, *, state: str = "open") -> list[dict]:
    """Return milestones for *repo* (open ones by default).

    Each item has at least ``number`` and ``title`` keys, matching the
    shape returned by the GitHub milestones REST endpoint. Used to resolve a
    milestone title → number (``coord milestone assign``) without a separate
    call.
    """
    raw = _gh(
        "api", "--paginate",
        f"repos/{repo}/milestones?state={state}",
        "--jq", ".[] | {number: .number, title: .title}",
    )
    # --jq emits one JSON object per line when applied to an array.
    results = []
    for line in raw.splitlines():
        line = line.strip()
        if line:
            results.append(json.loads(line))
    return results


def get_milestone(repo: str, milestone_number: int) -> dict:
    """Fetch a single milestone by number; returns ``{number, title, ...}``.

    Used to resolve a milestone number → title so the local issues cache
    ``milestone_title`` column can be populated without listing all milestones.
    Raises RuntimeError (propagated from ``_gh``) when the milestone does not
    exist.
    """
    raw = _gh("api", f"repos/{repo}/milestones/{milestone_number}")
    return json.loads(raw)


def get_milestone_issues(
    repo: str, milestone_title: str, *, state: str = "all"
) -> list[dict]:
    """Return every issue under *milestone_title* in *repo* (open+closed by default).

    Each item has ``number``, ``title``, ``state`` ("OPEN"/"CLOSED"), and
    ``labels`` (list of ``{"name": ...}``). ``gh issue list --milestone`` takes
    the milestone TITLE, not its number (unlike most other milestone-related
    calls in this module) — matches the existing ``--milestone`` usage in
    :func:`create_issue`. Used by ``--audit-of`` (#885) to enumerate a
    milestone's issue states for the audit briefing without a separate call
    per issue.
    """
    raw = _gh(
        "issue", "list", "--repo", repo, "--milestone", milestone_title,
        "--state", state, "--json", "number,title,state,labels",
        "--limit", "200",
    )
    return json.loads(raw)


def assign_issue_milestone(
    repo: str, issue_number: int, milestone_number: int
) -> None:
    """Assign *milestone_number* to *issue_number* on *repo* via the GitHub API.

    Uses ``gh api -X PATCH`` with ``-F milestone=<int>`` (capital -F so the
    value is sent as a JSON integer, as GitHub's REST API requires). Raises
    RuntimeError on any ``gh`` failure.
    """
    _gh(
        "api", "-X", "PATCH",
        f"repos/{repo}/issues/{issue_number}",
        "-F", f"milestone={milestone_number}",
    )


def unassign_issue_milestone(repo: str, issue_number: int) -> None:
    """Clear *issue_number*'s milestone on *repo* via the GitHub API (#1003).

    The counterpart to :func:`assign_issue_milestone` — ``-F milestone=null``
    sends a JSON ``null`` (per ``gh api``'s typed-field convention: literal
    ``null``/``true``/``false``/numbers are sent as their JSON type, not a
    string), which GitHub's REST API treats as "remove the milestone".
    Idempotent — clearing an issue that has no milestone is a no-op on
    GitHub's side. Raises RuntimeError on any ``gh`` failure.
    """
    _gh(
        "api", "-X", "PATCH",
        f"repos/{repo}/issues/{issue_number}",
        "-F", "milestone=null",
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
