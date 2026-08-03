"""``coord merge --revalidate`` — the merge lane's stale-verdict resolution (#1769).

#1738 gave ``coord drive`` an arm for a STALE-but-``passed`` smoke verdict: it
re-dispatches the Test stage against the current base instead of escalating to
a human. That arm lives in :mod:`coord.drive` and only ever fires while a live
drive is watching the issue. Every *other* merge path — ``coord merge``, its
``--only`` form, the auto-drain, the TUI merge action, the daemon ``/merge``
route — still had only escalate-or-block, so a branch that finishes and sits in
the merge queue with no live drive stays stuck: the next merge moves the base,
stales its verdict, and nobody is watching. Measured on 2026-08-03: three
stale-verdict stalls in one session, #1738's arm could fire on exactly one.

**This module is the resolution for that lane**, and it is deliberately
*opt-in*. ``coord merge`` with no flag is byte-identical to before — nothing
here runs unless the operator typed ``--revalidate``. An unattended dispatcher
firing test runs from inside the merge path is the shape that was gated off
after the 2026-06-07 auto-loop token-burn incident (it is why
``merge.auto_drain`` defaults to ``false``), so the auto-drain and the daemon's
own periodic drain pass ``revalidate=False`` and always will.

STRATEGY — batch composite (#1715 Option 3)
-------------------------------------------
What the operator does by hand — three times in the session above — is: make a
worktree at the current base, compose every stale branch onto it, run the suite
**once**, and let them all through on that single result. That is what
:func:`revalidate` does. It validates the *composite* rather than each branch
individually, so a failure does not identify its culprit; #1715 calls that
trade out explicitly and accepts it for a drain the operator asked to
revalidate, because it turns O(N) suite runs into 1. When the composite fails,
every candidate stays blocked (see :func:`revalidate`'s contract) — a failure is
never laundered into a merge.

Nothing here dispatches a worker or spends tokens: it is `git` + the repo's own
``build_command``/``test_command``, run locally, exactly like ``coord test``
(#561's throwaway-worktree discipline included — the base checkout is never
moved, because on the daemon host it doubles as the live editable coordinator
source).

BOUNDED
-------
One composite run per ``coord merge --revalidate`` invocation. There is no
retry loop: a composite that fails leaves every candidate blocked with the
failure quoted, and the operator (or the next invocation) decides. That is the
merge lane's analogue of #1738's ``fix_rounds`` budget — a branch that cannot
pass terminates with a clear reason rather than spinning.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from coord.merge_queue import RevalidationCandidate

# Hard ceiling on the composite suite run. A hung test binary must not wedge a
# `coord merge` invocation forever — and on the daemon route this runs inside a
# request handler that holds `_merge_lock`, so every other merge in the fleet
# waits behind it. Generous but bounded: the claude-coordinator suite is ~6 min
# serial and the tui `cargo test` leg is slower still, so 30 min covers a cold
# composite with room to spare while still terminating a wedged run.
# `_merge_via_daemon` sizes the thin client's HTTP timeout off this value.
DEFAULT_TIMEOUT_SECONDS = 60 * 30

# How much of a failing run's output to quote back. The whole point of the
# "a failed re-test leaves the entry blocked with the failure quoted" rule is
# that the operator can act on it without going hunting, but a full pytest
# log has no business being echoed into a merge summary.
_OUTPUT_TAIL_CHARS = 4000


def _tail(text: str, limit: int = _OUTPUT_TAIL_CHARS) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return "…(truncated)…\n" + text[-limit:]


@dataclass
class RevalidationResult:
    """Outcome of one composite revalidation run.

    ``ok`` is the only thing the merge path branches on: ``True`` means fresh
    ``passed`` verdicts were recorded for every candidate and ``process()`` may
    now find their smoke gate satisfied; ``False`` means every candidate is
    left exactly as it was — still blocked, never merged.
    """

    ok: bool
    reason: str = ""
    output: str = ""
    composed: list[str] = field(default_factory=list)
    recorded: list[str] = field(default_factory=list)
    worktree: Path | None = None

    def __bool__(self) -> bool:  # pragma: no cover — convenience only
        return self.ok


class _Echo:
    """Null echo so the library is usable without a Click context."""

    def __call__(self, msg: str = "") -> None:  # pragma: no cover
        return None


def local_repo_dir(config, repo_name: str) -> Path | None:
    """Resolve the base checkout for *repo_name*.

    Same resolution ``coord test`` uses (``coord.commands.test_gate.
    _local_repo_dir``): this machine's ``repo_paths`` first, then any machine
    in the config that knows the repo. Returns an expanded :class:`Path`, or
    ``None`` when no path is configured.
    """
    import socket

    hostname = socket.gethostname().split(".")[0]
    local_machine = next(
        (
            m for m in getattr(config, "machines", [])
            if m.name == hostname or m.host.split(".")[0] == hostname
        ),
        None,
    )
    repo_path = None
    if local_machine is not None:
        repo_path = local_machine.repo_path(repo_name)
    if repo_path is None:
        for m in getattr(config, "machines", []):
            repo_path = m.repo_path(repo_name)
            if repo_path:
                break
    return Path(repo_path).expanduser() if repo_path else None


def revalidation_worktree_path(repo_name: str, target_branch: str) -> Path:
    """Throwaway worktree for a composite revalidation run.

    Under ``~/.coord/revalidate-worktrees/`` — OUTSIDE the base checkout, for
    the #561 reason: the base checkout doubles as the live editable coordinator
    source on the daemon host, so moving its branch silently downgrades the
    running ``coord`` until somebody restores it.
    """
    from coord.state import COORD_DIR

    slug = target_branch.replace("/", "-")
    return COORD_DIR / "revalidate-worktrees" / f"{repo_name}-{slug}"


def _run(
    args: list[str], *, cwd: Path, timeout: int | None = 300
) -> subprocess.CompletedProcess:
    return subprocess.run(
        args, cwd=str(cwd), capture_output=True, text=True, timeout=timeout,
    )


def _rev_parse(repo_dir: Path, ref: str) -> str | None:
    """Resolve *ref* to a full SHA in *repo_dir*, or ``None``."""
    try:
        res = _run(["git", "rev-parse", ref], cwd=repo_dir, timeout=60)
    except (subprocess.SubprocessError, OSError):
        return None
    if res.returncode != 0:
        return None
    sha = res.stdout.strip()
    return sha or None


def _remove_worktree(repo_dir: Path, wt_path: Path) -> None:
    """Best-effort removal (+ prune of the admin refs), mirroring ``coord test``.

    Falls back to a plain directory delete: the path can survive as an
    orphaned tree when the worktree was registered against a *different* base
    checkout (a re-cloned repo, a moved ``repo_path``), and ``git worktree
    add`` refuses a path that already exists — which would wedge every future
    revalidation for that (repo, target) pair.
    """
    for args in (
        ["git", "worktree", "remove", "--force", str(wt_path)],
        ["git", "worktree", "prune"],
    ):
        try:
            _run(args, cwd=repo_dir, timeout=60)
        except (subprocess.SubprocessError, OSError):
            pass
    if wt_path.exists():
        shutil.rmtree(wt_path, ignore_errors=True)


def describe_candidates(candidates: list[RevalidationCandidate]) -> list[str]:
    """One operator-readable line per candidate, for ``--dry-run`` output."""
    lines: list[str] = []
    for c in candidates:
        e = c.entry
        lines.append(
            f"  revalidate: {e.repo_name} #{e.issue_number} ({e.branch} → "
            f"{e.target_branch}) — "
            f"{c.smoke.short_reason or 'verdict no longer covers the base'}"
        )
    return lines


def revalidate(
    candidates: list[RevalidationCandidate],
    config,
    *,
    echo=None,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    runner=None,
) -> RevalidationResult:
    """Compose every candidate branch onto the current base, run the suite once.

    On success, records a fresh ``passed`` Test-gate verdict for each
    candidate's work assignment. ``coord.state.record_test_verdict`` re-stamps
    the #1479 freshness anchors (``test_base_sha``/``test_head_sha``/
    ``test_patch_id``) as part of that write, so the verdict is anchored to the
    base the composite was actually validated against — which is the whole
    point, and is why this cannot be done by hand-editing ``test_state``.

    On **any** failure — a branch that will not compose, a build failure, a
    test failure, a timeout, a missing local checkout or an unconfigured test
    command — **no verdict is written at all** and every candidate is left
    blocked. There is no partial credit: this must never become a laundering
    path for a verdict that would not pass against the current base.

    All candidates must share one ``(repo_name, target_branch)`` pair — the
    caller groups them (``coord merge`` already processes the queue in exactly
    those groups). A mixed list is refused rather than silently validating a
    composite that means nothing.

    *runner* (testing seam) replaces the ``build``/``test`` command execution:
    ``runner(command: str, cwd: Path) -> subprocess.CompletedProcess``-alike
    with ``returncode`` and ``stdout``/``stderr``. Defaults to a real
    ``subprocess.run(shell=True)``.
    """
    echo = echo or _Echo()
    if not candidates:
        return RevalidationResult(ok=True, reason="no revalidation candidates")

    repos = {c.entry.repo_name for c in candidates}
    targets = {c.entry.target_branch for c in candidates}
    if len(repos) != 1 or len(targets) != 1:
        return RevalidationResult(
            ok=False,
            reason=(
                "revalidation candidates span more than one "
                f"(repo, target_branch): repos={sorted(repos)} "
                f"targets={sorted(targets)} — refusing to validate a "
                "composite that spans bases"
            ),
        )
    repo_name = repos.pop()
    target_branch = targets.pop()

    repo_cfg = config.repo(repo_name) if config is not None else None
    if repo_cfg is None:
        return RevalidationResult(
            ok=False, reason=f"no repo config for {repo_name!r}",
        )
    test_command = repo_cfg.test_command
    if not test_command:
        # Refusing here is the safe direction: with nothing to run, "passed"
        # would be a claim about a suite that never executed — the exact lie
        # #1738's escalation wording goes out of its way not to invite.
        return RevalidationResult(
            ok=False,
            reason=(
                f"no test_command configured for {repo_name!r} — cannot "
                "revalidate (recording a verdict for a suite that never ran "
                "is never correct)"
            ),
        )

    repo_dir = local_repo_dir(config, repo_name)
    if repo_dir is None or not repo_dir.exists():
        return RevalidationResult(
            ok=False,
            reason=(
                f"no local checkout for {repo_name!r} on this machine "
                f"({repo_dir or 'no repo_path configured'}) — revalidation "
                "runs the suite locally, so it must run where the repo lives "
                "(the daemon host, via `coord merge --revalidate`)"
            ),
        )

    branches = [c.entry.branch for c in candidates]
    echo(
        f"  --revalidate: composing {len(branches)} branch(es) onto "
        f"origin/{target_branch} and running the suite once (#1715 option 3)"
    )

    wt_path = revalidation_worktree_path(repo_name, target_branch)
    _remove_worktree(repo_dir, wt_path)
    wt_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        fetched = _run(["git", "fetch", "origin", "--prune"], cwd=repo_dir)
    except (subprocess.SubprocessError, OSError) as e:
        return RevalidationResult(ok=False, reason=f"git fetch failed: {e}")
    if fetched.returncode != 0:
        return RevalidationResult(
            ok=False, reason=f"git fetch failed: {fetched.stderr.strip()}",
        )

    added = _run(
        ["git", "worktree", "add", "--force", "--detach",
         str(wt_path), f"origin/{target_branch}"],
        cwd=repo_dir,
    )
    if added.returncode != 0:
        return RevalidationResult(
            ok=False,
            reason=(
                f"could not create the revalidation worktree at "
                f"origin/{target_branch}: {added.stderr.strip()}"
            ),
        )

    composed: list[str] = []
    # `git merge` (not rebase) with an explicit commit: we only need a tree
    # that contains every candidate's content on top of the current base.
    # Nothing here is ever pushed — the worktree is thrown away below and
    # `coord merge` still does the real merge through `gh` afterwards.
    for branch in branches:
        merged = _run(
            ["git", "merge", "--no-ff", "--no-edit", f"origin/{branch}"],
            cwd=wt_path,
        )
        if merged.returncode != 0:
            _run(["git", "merge", "--abort"], cwd=wt_path)
            return RevalidationResult(
                ok=False,
                reason=(
                    f"branch {branch!r} does not compose onto "
                    f"origin/{target_branch} (conflict) — resolve the "
                    "conflict before revalidating"
                ),
                output=_tail(merged.stdout + "\n" + merged.stderr),
                composed=list(composed),
                worktree=wt_path,
            )
        composed.append(branch)
        echo(f"    composed {branch}")

    run_cmd = runner or _shell_runner
    build_command = getattr(repo_cfg, "build_command", None)
    if build_command:
        echo(f"    running build: {build_command}")
        try:
            built = run_cmd(build_command, wt_path, timeout)
        except subprocess.TimeoutExpired:
            return RevalidationResult(
                ok=False,
                reason=f"revalidation build timed out after {timeout}s",
                composed=list(composed),
                worktree=wt_path,
            )
        if built.returncode != 0:
            return RevalidationResult(
                ok=False,
                reason=(
                    "revalidation BUILD FAILED against the current base "
                    f"(exit {built.returncode}) — every candidate stays "
                    "blocked"
                ),
                output=_tail((built.stdout or "") + "\n" + (built.stderr or "")),
                composed=list(composed),
                worktree=wt_path,
            )

    echo(f"    running tests: {test_command}")
    try:
        tested = run_cmd(test_command, wt_path, timeout)
    except subprocess.TimeoutExpired:
        return RevalidationResult(
            ok=False,
            reason=f"revalidation suite timed out after {timeout}s",
            composed=list(composed),
            worktree=wt_path,
        )
    if tested.returncode != 0:
        return RevalidationResult(
            ok=False,
            reason=(
                "revalidation SUITE FAILED against the current base "
                f"(exit {tested.returncode}) — every candidate stays "
                "blocked, nothing merged"
            ),
            output=_tail((tested.stdout or "") + "\n" + (tested.stderr or "")),
            composed=list(composed),
            worktree=wt_path,
        )

    # ── Suite green: record the fresh verdicts ──────────────────────────────
    from coord.state import record_test_staleness_anchor, record_test_verdict

    # The commits this run ACTUALLY validated, read from the local refs the
    # worktree was built from — not re-discovered from GitHub afterwards. See
    # `record_test_staleness_anchor`'s docstring for why that distinction is
    # load-bearing rather than an optimisation.
    validated_base_sha = _rev_parse(repo_dir, f"origin/{target_branch}")

    recorded: list[str] = []
    composite_note = (
        "revalidated by `coord merge --revalidate` — composite of "
        + ", ".join(composed)
        + f" onto origin/{target_branch}"
    )
    for c in candidates:
        aid = c.work_assignment_id
        if not aid:
            # Should not happen: SMOKE_STALE always names the row that carries
            # the stale verdict. Fail loudly rather than merging an entry whose
            # verdict we could not actually refresh.
            return RevalidationResult(
                ok=False,
                reason=(
                    f"{c.entry.repo_name} #{c.entry.issue_number}: the stale "
                    "verdict names no work assignment, so no fresh verdict "
                    "can be recorded — entry stays blocked"
                ),
                composed=list(composed),
                recorded=recorded,
                worktree=wt_path,
            )
        record_test_verdict(assignment_id=aid, test_state="passed")
        record_test_staleness_anchor(
            assignment_id=aid,
            test_head_sha=_rev_parse(repo_dir, f"origin/{c.entry.branch}"),
            test_base_sha=validated_base_sha,
            # #1475's patch-id is GitHub's compare diff hashed — reproducing it
            # from a local `git diff` is not guaranteed byte-identical, and a
            # WRONG patch-id would read as "content unchanged" for content that
            # did change. NULL is the fail-closed value the gate already
            # understands ("cannot confirm identical content"), so a branch that
            # moves after this run re-blocks on SHA alone, exactly as a
            # pre-#1475 row does.
            test_patch_id=None,
        )
        recorded.append(aid)
        echo(
            f"    recorded fresh Test verdict for {c.entry.repo_name} "
            f"#{c.entry.issue_number} ({aid})"
        )

    _remove_worktree(repo_dir, wt_path)
    return RevalidationResult(
        ok=True,
        reason=composite_note,
        composed=composed,
        recorded=recorded,
        worktree=None,
    )


def _shell_runner(command: str, cwd: Path, timeout: int):
    """Run *command* through the shell in *cwd*, capturing output.

    Same shape as ``coord test``'s build/test step (``subprocess.run(cmd,
    shell=True, cwd=worktree)``) — the repo's own configured command, run in
    the composite worktree, inheriting the environment.
    """
    return subprocess.run(
        command, shell=True, cwd=str(cwd), capture_output=True, text=True,
        timeout=timeout,
    )


def format_failure(result: RevalidationResult) -> list[str]:
    """Operator-facing lines for a failed revalidation (blocked, not merged)."""
    lines = [f"  --revalidate: {result.reason}"]
    if result.output:
        lines.append("  --revalidate: output tail:")
        lines.extend(
            "      " + ln for ln in result.output.splitlines()
        )
    if result.worktree is not None:
        lines.append(
            f"  --revalidate: worktree kept for inspection: {result.worktree}"
        )
    return lines


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "RevalidationResult",
    "describe_candidates",
    "format_failure",
    "local_repo_dir",
    "revalidate",
    "revalidation_worktree_path",
]
