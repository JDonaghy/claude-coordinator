"""Per-checkout git hygiene: parked branch, uncommitted changes (#1628).

Both of these are the #561/#601 failure mode.  A Build, a ``coord test``, or
an interactive agent inspecting a branch does ``git checkout`` in the *live*
base checkout and doesn't restore it.  From then on the machine builds,
tests, and (if it's the editable checkout) *runs the coordinator itself*
from that branch's code, silently — #561 disabled guards that way, #601 ran
old code against a retired local DB.  ``coord/cli.py`` already shouts about
this for the editable checkout on every command; this generalises it to
every checkout in ``coordinator.yml`` and puts it in the same report as the
rest of the fleet's headroom.

Uncommitted changes in a base checkout are the sibling signal: the merge
agent rebases and force-pushes in there, and a dirty tree makes that fail in
ways that read as a coordinator bug.  Both are WARN, never CRIT — they are
recoverable in one command and a human is often *deliberately* mid-edit.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from coord.health.models import CheckResult, HealthContext, Severity
from coord.health.registry import check

_GIT_TIMEOUT = 5.0


def _git(repo: Path, *args: str) -> tuple[int, str]:
    """``(returncode, stdout)`` — never raises for the ordinary failures.

    stdout is returned **unstripped**: ``git status --porcelain`` encodes the
    index/worktree state in the first two columns, so ``" M path"`` and
    ``"M  path"`` mean different things and a ``.strip()`` here silently ate
    the leading space (and with it one character of every path).
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, f"{type(exc).__name__}: {exc}"
    return result.returncode, result.stdout


@check(
    id="repo_branch",
    scope="checkout",
    title="branch",
    order=60,
    description="Base checkouts are on their default (or configured develop) branch.",
)
def probe_repo_branch(ctx: HealthContext) -> list[CheckResult]:
    results: list[CheckResult] = []
    for checkout in ctx.checkouts:
        code, raw = _git(checkout.path, "rev-parse", "--abbrev-ref", "HEAD")
        branch = raw.strip()
        if code != 0:
            results.append(
                CheckResult(
                    check_id="repo_branch",
                    scope="checkout",
                    subject=checkout.name,
                    severity=Severity.UNKNOWN,
                    headroom="could not read HEAD",
                    error=branch,
                    values={"path": str(checkout.path)},
                )
            )
            continue

        home = checkout.home_branches
        detached = branch == "HEAD"
        parked = detached or branch not in home
        results.append(
            CheckResult(
                check_id="repo_branch",
                scope="checkout",
                subject=checkout.name,
                severity=Severity.WARN if parked else Severity.OK,
                headroom=("detached HEAD" if detached else branch)
                + (f" (expected {home[0]})" if parked else ""),
                threshold="warn when off the default branch",
                detail=(
                    f"restore with: git -C {checkout.path} checkout {home[0]}"
                    if parked
                    else ""
                ),
                values={
                    "path": str(checkout.path),
                    "branch": branch,
                    "expected": list(home),
                    "detached": detached,
                    "parked": parked,
                },
            )
        )
    return results


@check(
    id="repo_dirty",
    scope="checkout",
    title="worktree clean",
    order=61,
    description="Base checkouts have no uncommitted changes.",
)
def probe_repo_dirty(ctx: HealthContext) -> list[CheckResult]:
    results: list[CheckResult] = []
    for checkout in ctx.checkouts:
        # --porcelain is the stable machine format; --untracked-files=normal
        # (the default) counts a stray build artifact as dirty, which is the
        # behaviour we want — the merge agent's force-push trips on those too.
        code, out = _git(checkout.path, "status", "--porcelain")
        if code != 0:
            results.append(
                CheckResult(
                    check_id="repo_dirty",
                    scope="checkout",
                    subject=checkout.name,
                    severity=Severity.UNKNOWN,
                    headroom="could not read git status",
                    error=out,
                    values={"path": str(checkout.path)},
                )
            )
            continue

        lines = [line for line in out.splitlines() if line.strip()]
        count = len(lines)
        if count:
            plural = "s" if count != 1 else ""
            # Porcelain v1: two status columns, a space, then the path.
            sample = ", ".join(line[3:].strip() for line in lines[:3])
            if count > 3:
                sample += ", …"
            headroom = f"{count} uncommitted change{plural}"
            if sample:
                headroom = f"{headroom} ({sample})"
        else:
            headroom = "clean"
        results.append(
            CheckResult(
                check_id="repo_dirty",
                scope="checkout",
                subject=checkout.name,
                severity=Severity.WARN if count else Severity.OK,
                headroom=headroom,
                threshold="warn when dirty",
                values={
                    "path": str(checkout.path),
                    "dirty_count": count,
                    "entries": lines[:20],
                },
            )
        )
    return results
