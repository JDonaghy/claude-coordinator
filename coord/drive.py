"""``coord drive`` — drive ONE issue from dispatch to merge, unattended (#1392).

The Python port of ``scripts/drive-issue.sh`` (742 lines of bash, deleted in
the same change).  The port buys **testability and shippability**, not fewer
processes: every ``coord`` invocation is still a subprocess, deliberately.

  THE CLI IS THE CONTRACT; INTERNAL FUNCTIONS ARE NOT.  The obvious "win" of
  a Python port is to stop shelling out — call ``record_test_verdict()``
  instead of ``coord test --passed``.  Do NOT do this.  It is exactly #1384:
  the ``coord test`` CLI mirrors ``test_state`` → the legacy ``smoke_test``
  field and ``record_test_verdict()`` alone does not, so calling the function
  directly silently reintroduces the bug that makes ``coord fix`` refuse to
  dispatch.  Every board mutation this driver performs goes through the CLI.

WHAT IT IS.  The pipeline is Work → Test → Review → Merge
(``pipeline.default_gates``).  coord automates all of it (#1426): the ``coord
serve`` tick loop reconciles and enqueues, and the ``coord-notify.timer``
(5 min, on the daemon host) posts completions, auto-dispatches the Test-stage
smoke assignment (``dispatch_pending_smoke``), dispatches reviews, and runs the
review → fix → re-review auto-loop.  One thing is still missing, and this
supplies it:

  NOTHING SEQUENCES THE STAGES FOR A SINGLE ISSUE.  ``coord wait`` is
  per-assignment (and reads the LOCAL dispatched ledger, so it does not work
  from a thin client at all).  → This is a resumable state machine over the
  daemon's board: it dispatches the WORK assignment, then OBSERVES
  Test/Review/Merge — coord dispatches all three itself — nudging ``coord
  notify`` (``--notify``) when nothing has changed for ``--stall`` minutes.

A FAILING TEST IS A LOOP ITERATION, NOT A DEAD END.  On a genuine test failure
this runs ``coord fix``, which dispatches a headless follow-up worker on the
SAME branch with the model escalated (sonnet → opus, every round) and the
failure quoted in its briefing.  The loop re-tests and repeats, bounded by
``--max-fix-rounds``.  A fix round that legitimately changes nothing exits
``done``, not ``advisory`` — the zero-commit heuristic is per-branch and the
branch already carries the original work's commit — so a no-op fix does not
wedge the pipeline (observed on #1445).

Everywhere coord ALREADY has a path, this observes rather than acts — in
particular it never dispatches the Test-stage smoke assignment (coord's own
``dispatch_pending_smoke`` does) or a REVIEW fix (the notify timer's auto-loop
does) — two drivers racing to dispatch the same thing is exactly the
2026-06-07 duplicate-fix-worker incident (#476/#477).

Re-running it on the same issue is safe and resumes from wherever the board
actually is.

THE ORACLE LOOP (#1453, docs/ORACLE_LOOP.md).  When this issue's milestone
already has a merged Gate-A contract and the repo has an acceptance driver
configured, dispatching ``coord assign`` straight away would just hit the
#1138 hard gate (``coord.dispatch.enforce_oracle_readiness``) and refuse —
the issue's JIT acceptance slice hasn't been authored yet.  Rather than dead-
end there, :func:`resolve_oracle_decision` (resolved ONCE, at preflight —
mirrors ``tui/src/app/pipeline.rs``'s ``gate_a_contract_exists_for`` and
``coord.milestone_dispatch.gate_a_status``, all three keyed on
:func:`coord.acceptance.gate_a_contract_path`) puts this run into "oracle
drive" mode: :func:`_dispatch_work_stage` authors the slice first (``coord
acceptance author <repo> <tracking_issue> --issue <N>``, plus ``--for-path``
when the repo's driver is routed — resolved from the milestone's Gate-A
mock kind via the SHARED :func:`coord.acceptance.resolve_for_path`, so this
never drifts from whatever eventually resolves it for the TUI's own menu,
#1460) and :func:`_decide_acceptance_author` observes it through to a landed
merge (``status='merged'``, #609 — its own Test/Review/Merge are driven by
coord exactly like a normal work row) before ever calling ``coord assign``.
An ``advisory`` JIT-slice exit is handled exactly like the main work row's
(``--accept-advisory``, #1357) rather than waited on forever.
``--no-acceptance`` opts out back to the pre-#1453 behaviour.

STRUCTURE.  All decision logic lives in :func:`decide` and :func:`preflight`,
which are pure functions over an :class:`~coord.drive_state.IssueState` plus
injected verifiers.  :class:`Driver` is the thin I/O shell: poll, execute the
returned :class:`Action`, sleep.  Every bug the bash version shipped was in the
decision half, which is why that half is where the tests are.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence

from coord.drive_state import (
    BoardFetcher,
    DriveStateError,
    IssueState,
    project,
    scratch_dir,
)
from coord.interactive import (
    DRIVE_SESSION_PREFIX,
    TmuxHost,
    tmux_available,
    tmux_session_alive,
)
from coord.failure_class import classify_failure, plan_usage_limit_resume
from coord.usage_limits import PlanLimits, evaluate_usage_gate, get_plan_limits
# Lost in the #1584-onto-#1590 rebase: _decide_review() calls this, but the
# import lived in a hunk #1590 rewrote, so the merge came out textually clean
# and semantically broken (NameError at coord/drive.py:1162). Same symbol and
# same source as coord/notify.py:53 — deliberately NOT re-pointed at #1590's
# newer classify_failure(), which would change reviewed behaviour during a
# conflict resolution.
from coord.worker_events import is_usage_limit_reason

# ── exit codes (unchanged from drive-issue.sh) ───────────────────────────────

EXIT_OK = 0
EXIT_TERMINAL_FAILURE = 1
EXIT_USAGE = 2
EXIT_DEADLINE = 3
# #1505: distinct from EXIT_TERMINAL_FAILURE so `coord drive`'s exit code
# alone tells a wrapper/notify path "a human decision is waiting on the
# board" apart from "something actually broke" — see `_escalate_merge`.
EXIT_ESCALATED = 4


class DriveError(Exception):
    """A configuration/usage problem — reported, never polled through."""

    def __init__(self, message: str, exit_code: int = EXIT_USAGE) -> None:
        super().__init__(message)
        self.exit_code = exit_code


# ── options ──────────────────────────────────────────────────────────────────


@dataclass
class DriveOptions:
    """Resolved flags.  Field names mirror the bash variables one-for-one."""

    machine: str = ""
    model: str = ""
    briefing_file: str = ""
    do_plan: bool = False
    max_fix_rounds: int = 3
    skip_test: bool = False
    repo_path: str = ""
    poll: float = 60.0
    max_work_retries: int = 1
    deadline_mins: float = 240.0
    stall_mins: float = 20.0
    notify: bool = False
    do_merge: bool = True
    merge_method: str = "rebase"
    accept_advisory: bool = False
    force_review: bool = False
    dry_run: bool = False
    max_merge_attempts: int = 3
    # #1453: skip the oracle-loop JIT slice authoring step below even when
    # this issue's milestone has a merged Gate-A contract — an escape hatch
    # for "the contract is stale/wrong for this issue" or "I want a plain
    # run", matching the opt-out every other oracle-loop gate offers
    # (`oracle:exempt` label, `exempt:` manifest list).
    no_acceptance: bool = False
    # Threaded onto every `coord` subprocess so a `coord drive --config X` run
    # cannot dispatch against a *different* config than it is reading.  The
    # bash driver ran a bare `coord` and silently had this gap.  Empty means
    # "let each subprocess resolve the default" ($COORD_CONFIG →
    # ~/.coord/coordinator.yml → ./coordinator.yml), i.e. today's behaviour.
    config_path: str = ""

    @property
    def stall_secs(self) -> float:
        return self.stall_mins * 60.0

    @property
    def deadline_secs(self) -> float:
        return self.deadline_mins * 60.0


@dataclass
class DriveCounters:
    """Bounds on every retry loop.  Unbounded merge retries was a real bug."""

    work_retries: int = 0
    fix_rounds: int = 0
    merge_attempts: int = 0
    review_dispatches: int = 0
    # #1584: bounded retry for a review WORKER that died (transient API
    # error, network drop, ...) before producing a verdict — the review-side
    # analogue of `work_retries`, bounded the same way (`opts.max_work_retries`).
    review_retries: int = 0


# ── actions ──────────────────────────────────────────────────────────────────

WAIT = "wait"
RUN = "run"
EXIT = "exit"


@dataclass(frozen=True)
class Action:
    """What the loop should do next.  The only thing :func:`decide` returns.

    ``command`` is the ``coord`` subcommand argv **without** the ``coord``
    binary itself — the driver prepends it.  Keeping it here (rather than
    building argv inside the executor) is what lets a unit test assert the
    exact CLI contract, e.g. that a skipped Test gate really is
    ``coord test --skipped --reason ... <aid>`` and not a direct
    ``record_test_verdict()`` call (#1384).
    """

    kind: str
    label: str = ""
    message: str = ""
    exit_code: int = 0
    command: tuple[str, ...] = ()
    sleep_after: float | None = None  # None → the poll interval
    on_error: str = "die"  # "die" | "warn"
    error_message: str = ""
    serialize_merge: bool = False
    warnings: tuple[str, ...] = ()

    @property
    def is_exit(self) -> bool:
        return self.kind == EXIT


def _wait(sleep_after: float | None = None, label: str = "") -> Action:
    return Action(kind=WAIT, label=label, sleep_after=sleep_after)


def _succeed(message: str) -> Action:
    return Action(kind=EXIT, message=message, exit_code=EXIT_OK)


def _die(message: str, exit_code: int = EXIT_TERMINAL_FAILURE) -> Action:
    return Action(kind=EXIT, message=message, exit_code=exit_code)


# ── oracle-loop JIT slice authoring (#1453) ─────────────────────────────────


class AcceptanceGateChecker(Protocol):
    """The GitHub questions :func:`resolve_oracle_decision` and
    :func:`_decide_acceptance_author` cannot answer from the board payload
    alone: has Gate A's contract actually merged, and (for a routed repo)
    which subtree does this milestone's slice belong to?"""

    def contract_exists(self, repo_name: str, milestone_number: int) -> bool: ...

    def resolve_for_path(self, repo_name: str, milestone_number: int) -> str | None: ...


@dataclass
class GitHubAcceptanceGateChecker:
    """Real implementation: reuses ``coord.milestone_dispatch.gate_a_status``
    — the SAME check ``coord milestone dispatch``'s Gate A gate and the
    #1138 ``issue_oracle_ready`` hard gate already run — rather than
    re-deriving the ``tests/acceptance/ms-NN/contract.md`` path here. That
    function returns ``None`` for two different reasons ("no driver
    configured" or "contract exists"); callers of this checker have already
    confirmed ``config.acceptance.has_driver(repo_name)`` themselves
    (:func:`resolve_oracle_decision` does), so ``None`` is unambiguous here.
    """

    config: Any

    def contract_exists(self, repo_name: str, milestone_number: int) -> bool:
        from coord.milestone_dispatch import gate_a_status  # noqa: PLC0415

        repo_cfg = self.config.repo(repo_name)
        if repo_cfg is None:
            return False
        return gate_a_status(repo_cfg, self.config, milestone_number) is None

    def resolve_for_path(self, repo_name: str, milestone_number: int) -> str | None:
        """#1453 review finding 1: the ``--for-path`` a routed repo's JIT
        acceptance-author dispatch needs. Delegates to
        :func:`coord.acceptance.resolve_for_path` (the SHARED derivation —
        see its docstring); raises :class:`coord.acceptance.
        ForPathResolutionError` unchanged so callers report it verbatim.
        """
        from coord.acceptance import resolve_for_path  # noqa: PLC0415

        repo_cfg = self.config.repo(repo_name)
        if repo_cfg is None:
            return None
        return resolve_for_path(self.config, repo_cfg, milestone_number)


@dataclass(frozen=True)
class OracleDecision:
    """Resolved ONCE per run (at preflight time, alongside machine
    resolution) — never recomputed per poll, since *gate_checker* costs a
    GitHub fetch and a milestone's Gate-A status does not change mid-run.

    ``active`` gates the JIT-authoring branch in :func:`_dispatch_work_stage`;
    ``reason`` is what the preflight banner prints so an operator never has
    to guess which mode a run is in. ``tracking_issue`` is set iff ``active``
    — the argument :func:`_decide_acceptance_author` needs to build ``coord
    acceptance author <repo> <tracking_issue> --issue <N>``.
    """

    active: bool
    reason: str
    tracking_issue: int | None = None


def resolve_oracle_decision(
    state: IssueState,
    opts: DriveOptions,
    config: Any,
    gate_checker: AcceptanceGateChecker,
) -> OracleDecision:
    """The #1453 gate: does this issue's Work dispatch get preceded by an
    independent JIT acceptance-slice authoring session?

    Mirrors — and must never drift from — the same rule the TUI's
    ``gate_a_contract_exists_for`` (``tui/src/app/pipeline.rs``) and
    ``coord.milestone_dispatch.gate_a_status`` already enforce, both via
    :func:`coord.acceptance.gate_a_contract_path`: a repo with a configured
    acceptance driver, an issue that resolves to a milestone with a tracking
    issue, and a Gate-A contract already merged for that milestone. This
    complements (does not replace) the #1138 hard gate
    (``coord.dispatch.enforce_oracle_readiness``), which would otherwise
    just refuse the eventual ``coord assign``/``coord approve-plan`` with no
    explanation once an oracle-opted-in milestone's issue reaches it — this
    proactively drives the authoring + merge to completion FIRST so a plain
    ``coord drive`` doesn't dead-end on that refusal.
    """
    if opts.no_acceptance:
        return OracleDecision(False, "--no-acceptance set — normal drive")
    if not config.acceptance.has_driver(state.repo):
        return OracleDecision(
            False, f"{state.repo!r} has no acceptance.drivers entry — normal drive"
        )
    if state.milestone_number is None:
        return OracleDecision(
            False, f"#{state.issue} has no GitHub milestone — normal drive"
        )
    if state.milestone_tracking_issue is None:
        return OracleDecision(
            False,
            f"#{state.issue} isn't a member of a tracked milestone work order — "
            "normal drive",
        )
    if not gate_checker.contract_exists(state.repo, state.milestone_number):
        from coord.acceptance import gate_a_contract_path  # noqa: PLC0415

        path = gate_a_contract_path(state.milestone_number)
        return OracleDecision(
            False,
            f"Gate A contract {path!r} not merged yet on "
            f"{state.repo_default_branch!r} — normal drive (run `coord "
            f"acceptance mock {state.repo} {state.milestone_tracking_issue}` "
            "first for the oracle loop, docs/ORACLE_LOOP.md)",
        )
    return OracleDecision(
        True,
        f"ORACLE DRIVE — ms-{state.milestone_number}'s Gate-A contract is "
        f"merged: authoring the sealed JIT slice for #{state.issue} "
        f"(`coord acceptance author {state.repo} "
        f"{state.milestone_tracking_issue} --issue {state.issue}`) before "
        "dispatching work",
        tracking_issue=state.milestone_tracking_issue,
    )


def _decide_acceptance_author(
    state: IssueState,
    oracle: OracleDecision,
    opts: DriveOptions,
    machine: str,
    gate_checker: AcceptanceGateChecker,
    verifier: MergeVerifier,
) -> Action | None:
    """The #1453 JIT-slice gate itself. ``None`` means "landed — fall
    through to dispatching work normally" (only ever called when
    ``oracle.active``).

    Observes a `type="test-author"` assignment scoped to THIS issue
    (``for_issue_number == state.issue`` — #1171/#1138 key the JIT slice's
    row on the milestone's TRACKING issue via `issue_number`, so it never
    shows up as this issue's own ``work_aid``; see ``IssueState``'s
    docstring). That assignment is itself `WORK_LIKE`
    (``coord.models.WORK_LIKE_TYPES``), so coord drives its OWN Test → Review
    → Merge exactly like a normal work row (dispatch_pending_smoke /
    dispatch_pending_reviews / the merge queue) with zero help from this
    driver — this only waits for its board row to reach ``status='merged'``
    (#609), the identical terminal signal :func:`decide`'s own merged check
    uses for the real work row.
    """
    aid = state.acceptance_author_aid
    status = state.acceptance_author_status

    if not aid:
        command = [
            "acceptance", "author", state.repo, str(oracle.tracking_issue),
            "--issue", str(state.issue),
        ]
        # #1453 review finding 1: a ROUTED repo's `coord acceptance author`
        # hard-refuses with no --for-path (coord.test_author.
        # dispatch_test_author's "no route matched" RuntimeError) — resolve
        # it from the milestone's Gate-A mock kind (the SHARED
        # coord.acceptance.resolve_for_path helper) before ever dispatching,
        # so a routed repo's very first JIT-authoring attempt doesn't die.
        from coord.acceptance import ForPathResolutionError  # noqa: PLC0415

        try:
            for_path = gate_checker.resolve_for_path(state.repo, state.milestone_number)
        except ForPathResolutionError as exc:
            return _die(
                f"could not resolve --for-path for {state.repo}'s JIT "
                f"acceptance slice on #{state.issue}: {exc}"
            )
        if for_path:
            command += ["--for-path", for_path]

        return Action(
            kind=RUN,
            label=(
                "ACCEPTANCE: authoring sealed JIT slice → coord acceptance "
                f"author {state.repo} {oracle.tracking_issue} --issue "
                f"{state.issue}"
                + (f" --for-path {for_path}" if for_path else "")
            ),
            command=tuple(command),
            error_message=(
                f"coord acceptance author failed to dispatch for #{state.issue}. "
                "Check coordinator.yml's acceptance.drivers entry for "
                f"{state.repo!r}, or re-run coord drive with --no-acceptance "
                "to skip JIT authoring."
            ),
        )

    if status == "merged":
        return None

    if status == "failed":
        return _die(
            f"acceptance author {aid} failed — inspect: coord log {aid} "
            f"--machine {state.acceptance_author_machine or machine}\n"
            "   Continue by hand, or re-run coord drive with "
            "--no-acceptance to skip JIT authoring."
        )

    if status == "cancelled":
        return _die(
            f"acceptance author {aid} was cancelled — re-dispatch by hand: "
            f"coord acceptance author {state.repo} {oracle.tracking_issue} "
            f"--issue {state.issue}\n"
            "   or re-run coord drive with --no-acceptance."
        )

    if status == "advisory":
        # #1453 review finding 2: this is the #1386 bug class reborn — an
        # ``advisory`` row is TERMINAL (drive_state.TERMINAL_STATUSES) and
        # is explicitly excluded from coord's Test/Review/Merge auto-loop
        # (coord.reconcile's "review_state = 'advisory'" skip), so it will
        # NEVER transition to 'merged' on its own — treating it as
        # "still landing" below would spin forever. Mirror `_decide_advisory`
        # exactly: a real 0-commit exit is terminal outright; a #1357-style
        # false positive (commits present) needs the same
        # `--accept-advisory` opt-in the main work row uses, not a silent
        # pass-through.
        branch = state.acceptance_author_branch
        probe = replace(state, work_branch=branch) if branch else state
        if not branch or not verifier.branch_has_commits(probe):
            return _die(
                f"acceptance author {aid} exited ADVISORY with no commits on "
                "its branch — nothing was authored, so there is no slice to "
                "land.\n"
                f"   inspect: coord log {aid} --machine "
                f"{state.acceptance_author_machine or machine}\n"
                "   Continue by hand, or re-run coord drive with "
                "--no-acceptance to skip JIT authoring."
            )
        if not opts.accept_advisory:
            return _die(
                f"acceptance author {aid} is ADVISORY, but its branch carries "
                "real commits (the #1357 signature — see _decide_advisory).\n"
                "   Proceed anyway with --accept-advisory, or re-run coord "
                "drive with --no-acceptance."
            )
        return Action(
            kind=WAIT,
            warnings=(
                f"ACCEPTANCE: JIT slice {aid} is ADVISORY with commits present "
                "— proceeding per --accept-advisory (#1357)",
            ),
        )

    if status == "done":
        # #1535: `done` is TERMINAL (drive_state.TERMINAL_STATUSES) exactly
        # like `advisory` — it will never transition to `merged` on its own
        # if nothing was ever pushed (a #1534-style false "done", a reap, or
        # a worker that forgot to push, the recurring shape). Re-polling
        # can't help a terminal status, so waiting to `--deadline` with no
        # diagnosis is the #1526 merge-gate defect reborn here. Mirror the
        # `advisory` branch's probe exactly.
        branch = state.acceptance_author_branch
        probe = replace(state, work_branch=branch) if branch else state
        if not branch or not verifier.branch_has_commits(probe):
            branch_display = repr(branch) if branch else "(none)"
            return _die(
                f"acceptance author {aid} exited DONE, but its branch "
                f"{branch_display} carries no commits — nothing was "
                "authored, so there is no slice to land, and DONE is "
                "terminal: it will never change on its own.\n"
                f"   inspect: coord log {aid} --machine "
                f"{state.acceptance_author_machine or machine}\n"
                "   Re-author by hand: coord acceptance author "
                f"{state.repo} {oracle.tracking_issue} --issue "
                f"{state.issue}\n"
                "   or re-run coord drive with --no-acceptance to skip JIT "
                "authoring."
            )

    # "" / running: still authoring. done (with commits, checked above):
    # authoring finished, now landing through Test → Review → Merge — coord's
    # own tick loop drives that, exactly like a normal work row; this only
    # observes (same posture as every other gate in this module).
    verb = "authoring" if status in ("", "running") else "landing (Test → Review → Merge)"
    return _wait(
        label=f"ACCEPTANCE: JIT slice {aid} status={status or '(none)'} — {verb}"
    )


# ── merge verification ───────────────────────────────────────────────────────


class MergeVerifier(Protocol):
    """The two git/GitHub questions the state machine cannot answer itself."""

    def branch_has_commits(self, state: IssueState) -> bool: ...

    def verify_merged(self, state: IssueState) -> bool: ...


@dataclass
class GitMergeVerifier:
    """Real implementation: ``git`` for commits, ``gh`` for merge state.

    ``repo_path`` is the local checkout used for fetches; defaults to
    ``~/src/<repo>``.
    """

    repo_path: str = ""
    warn: Callable[[str], None] = lambda msg: None

    def _base(self, state: IssueState) -> Path | None:
        base = Path(self.repo_path).expanduser() if self.repo_path else (
            Path.home() / "src" / state.repo
        )
        return base if (base / ".git").exists() else None

    @staticmethod
    def _git(base: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(base), *args],
            capture_output=True,
            text=True,
            check=False,
        )

    def branch_has_commits(self, state: IssueState) -> bool:
        """True when *branch* exists on the remote and carries a commit the
        default branch does not.

        Used to tell a REAL zero-commit advisory apart from the #1357 false
        positive, where the agent downgrades a good DONE over an artifact glob
        that matched nothing.
        """
        branch = state.work_branch
        if not branch:
            return False
        base = self._base(state)
        if base is None:
            return False
        target = state.repo_default_branch or "main"
        if self._git(base, "fetch", "--quiet", "origin", target).returncode != 0:
            return False
        if self._git(base, "fetch", "--quiet", "origin", branch).returncode != 0:
            return False
        proc = self._git(base, "rev-list", "--count", f"origin/{target}..FETCH_HEAD")
        if proc.returncode != 0:
            return False
        try:
            return int((proc.stdout or "0").strip() or 0) > 0
        except ValueError:
            return False

    def verify_merged(self, state: IssueState) -> bool:
        """Confirm the branch actually landed on the target.

        NOTE: ``merge-base --is-ancestor`` is the WRONG test here.  ``coord
        merge`` defaults to ``--method rebase`` (and supports squash), both of
        which rewrite the commits — so a fully-merged branch's tip SHA is never
        an ancestor of the target.  Verified against #1344: merged via PR
        #1355, two commits on main, and ``--is-ancestor`` still says no.
        """
        branch = state.work_branch
        target = state.repo_default_branch or "main"
        if not branch:
            return False

        # Primary: ask GitHub.  Authoritative for every merge method, and still
        # correct after the merged branch has been deleted from the remote.
        # Routed through the github_ops seam (#1483) rather than shelling out
        # to `gh` directly here — no `shutil.which` probe, so behaviour never
        # silently varies with whether `gh` happens to be on this host's PATH.
        if state.repo_github:
            from coord import github_ops  # noqa: PLC0415

            pr_state = github_ops.get_pr_state_for_branch(state.repo_github, branch) or ""
            if pr_state == "MERGED":
                return True
            if pr_state:
                self.warn(f"PR for {branch} is {pr_state}, not MERGED")
                return False

        # Fallback: patch-equivalence.  Every commit of a landed branch has an
        # equivalent upstream, which is exactly what `git cherry` marks with
        # '-'; a '+' means that commit is genuinely not on the target yet.
        base = self._base(state)
        if base is None:
            return False
        vref = f"refs/remotes/coord-verify/{branch}"
        if self._git(base, "fetch", "--quiet", "origin", target).returncode != 0:
            return False
        fetched = self._git(
            base, "fetch", "--quiet", "origin", f"refs/heads/{branch}:{vref}"
        )
        if fetched.returncode != 0:
            return False
        try:
            proc = self._git(base, "cherry", f"origin/{target}", f"coord-verify/{branch}")
            if proc.returncode != 0:
                return False
            unmerged = [
                line for line in (proc.stdout or "").splitlines() if line.startswith("+")
            ]
            return not unmerged
        finally:
            self._git(base, "update-ref", "-d", vref)


# ── preflight (pure) ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Preflight:
    """The resolved machine plus anything worth warning about before looping."""

    machine: str
    warnings: tuple[str, ...] = ()


def preflight(
    state: IssueState,
    opts: DriveOptions,
    config: Any = None,
    *,
    usage_limits: PlanLimits | None = None,
) -> Preflight:
    """Resolve the machine and refuse the runs that can never win.

    Raises :class:`DriveError` for a configuration problem or for interactive
    work with no review (see below).

    *usage_limits* (#1466) is the ALREADY-PROBED Max-plan 5h/weekly usage
    snapshot — this function stays pure and never shells out itself, mirroring
    the *verifier*/*gate_checker* injection pattern used elsewhere in this
    module. ``None`` (every pre-#1466 caller, and any caller that skips the
    probe) is treated exactly like an unavailable probe: the gate silently
    lets the run proceed. *config* is likewise optional — ``None`` skips the
    usage gate entirely (no ``usage_gate`` section to consult), which is what
    every pre-#1466 test in this file's suite still passes.
    """
    machine = opts.machine or state.picked_machine
    if not machine:
        raise DriveError(
            f"no unpaused machine hosts {state.repo} — pass --machine",
            EXIT_USAGE,
        )

    warnings: list[str] = []

    if config is not None:
        gate_cfg = config.usage_gate
        limits = usage_limits if usage_limits is not None else PlanLimits(status="unknown")
        gate_result = evaluate_usage_gate(limits, gate_cfg)
        if gate_result.action == "block":
            raise DriveError(
                f"{gate_result.message} (usage_gate.mode: block) — refusing to "
                "dispatch. Wait for the window to reset, or lower urgency by "
                "raising the threshold / setting usage_gate.mode: warn in "
                "coordinator.yml.",
                EXIT_USAGE,
            )
        if gate_result.action == "warn":
            warnings.append(f"{gate_result.message} (usage_gate.mode: warn — proceeding anyway)")

    if not state.auto_loop:
        warnings.append(
            "pipeline.auto_loop is OFF — a request-changes review will NOT "
            "auto-dispatch a fix."
        )
        warnings.append(
            "This run will report the verdict and stop rather than dispatch one "
            "itself."
        )

    # INTERACTIVE WORK NEVER GETS AN AUTOMATIC REVIEW.
    #
    # `dispatch_pending_reviews` carries `and c.provider_name != "claude-pty"`
    # (#555): a metered headless review must never silently follow a
    # human-attended session.  So for work done interactively, the review is
    # not "late" — it is never coming, and waiting for it is an infinite stall.
    #
    # Checked HERE, at preflight, rather than at the review gate: otherwise a
    # run burns the full test suite (~6 min) before parking on a wait it can
    # never win.  That is exactly what happened driving #1357 — test gate
    # passed at 4642/4642, then 90 minutes of nothing.
    if state.work_aid and state.work_provider == "claude-pty" and not state.review_aid:
        if opts.force_review:
            warnings.append(
                f"work {state.work_aid} is INTERACTIVE (claude-pty) — no "
                "automatic review (#555)."
            )
            warnings.append(
                "--force-review set: this run will request the review explicitly."
            )
        else:
            raise DriveError(
                f"work {state.work_aid} was completed INTERACTIVELY "
                "(provider=claude-pty).\n"
                "   coord's #555 guard permanently excludes interactive work from "
                "automatic\n"
                "   review dispatch, so waiting for one would stall forever.\n\n"
                "   Either drive it unattended:      re-run with --force-review\n"
                "   or review it human-attended:     coord assign --interactive "
                f"--review-of {state.work_aid}",
                EXIT_USAGE,
            )

    return Preflight(machine=machine, warnings=tuple(warnings))


# ── the state machine (pure) ─────────────────────────────────────────────────


def decide(
    state: IssueState,
    opts: DriveOptions,
    counters: DriveCounters,
    verifier: MergeVerifier,
    *,
    machine: str = "",
    oracle: OracleDecision | None = None,
    gate_checker: AcceptanceGateChecker | None = None,
) -> Action:
    """One step of the state machine: given the board, what next?

    Pure apart from the injected *verifier* (git/GitHub) and the bounded
    counters it increments.  Every branch here was a bash ``case`` arm; the
    ordering is identical, and — critically — **no terminal status falls
    through to a bare wait**.  An ``advisory`` work row doing exactly that was
    a silent 240-minute spin (fixed in PR #1386, and now unit-tested).

    *oracle* (#1453) is resolved ONCE per run by :func:`resolve_oracle_decision`
    and threaded through unchanged on every call — ``None`` (the default,
    every pre-#1453 caller) behaves exactly as before: no JIT slice, straight
    to ``coord assign``. *gate_checker* is only consulted when *oracle* is
    active (to resolve a routed repo's ``--for-path``, #1453 review finding
    1) — unused, like *oracle*, on every pre-#1453 call site.
    """
    machine = machine or opts.machine or state.picked_machine

    # ---- terminal: merged ---------------------------------------------------
    if state.work_status == "merged" or state.merge_status == "MERGED":
        target = state.repo_default_branch or "main"
        if state.work_branch and verifier.verify_merged(state):
            return _succeed(f"✓ MERGED — {state.work_branch} has landed on {target}")
        base = opts.repo_path or f"~/src/{state.repo}"
        return _die(
            f"board says merged but {state.work_branch} has NOT landed on {target}\n"
            f"   verify by hand: git -C {base} log --oneline origin/{target}"
        )

    # ---- something is running: just wait -----------------------------------
    if state.active_count > 0:
        return _wait()

    # ---- no work yet: plan and/or dispatch ---------------------------------
    if not state.work_aid:
        return _dispatch_work_stage(state, opts, machine, oracle, gate_checker, verifier)

    # ---- work died from hitting the account's usage limit: wait -----------
    #
    # #1461: a usage-limit kill is NOT a defect and NOT the #1357 zero-commit
    # advisory signature — it is the ONE terminal state known safe to
    # re-dispatch unchanged, once the reset time passes. Falling into the
    # bounded-retry branch below (or `coord fix`'s model escalation, on the
    # advisory side) would just re-dispatch straight into the same exhausted
    # budget and fail again for no diagnostic reason — exactly the confusion
    # the issue is about. Detected via the `usage limit — resets ...` prefix
    # that `coord.worker_events.format_usage_limit_reason` stamps onto
    # `failure_reason` regardless of whether the agent's own reap landed on
    # FAILED or ADVISORY (#1461's own worked example hit both in one
    # session). Deliberately does NOT auto-retry here — retrying before the
    # reset only produces more of the same; a human (or a future reset-aware
    # auto-retry) re-runs `coord retry` once the window reopens.
    #
    # #1590: routed through `coord.failure_class` so this branch and the
    # sequencer's budget agree on what "environmental" means, and the surfaced
    # warning now names *when* the node could resume — the `reset_at_raw` the
    # detector has always parsed and nobody ever used.
    if state.work_status in ("failed", "advisory"):
        classification = classify_failure(
            failure_reason=state.work_failure_reason or None
        )
        if classification.is_usage_limit:
            resume = plan_usage_limit_resume(
                reset_at_raw=classification.reset_at_raw
            )
            when = (
                resume.resume_at.isoformat(timespec="minutes")
                if resume.from_reset_time
                else "unknown (reset time not parseable)"
            )
            return Action(
                kind=WAIT,
                label=(
                    f"WORK: {state.work_aid} killed by the usage limit — waiting "
                    "for the reset, not retrying"
                ),
                warnings=(
                    f"usage-limit kill detected on {state.work_aid}: "
                    f"{state.work_failure_reason} — waiting for the reset instead "
                    "of retrying (#1461)",
                    f"{classification.reason}; earliest resume {when} (#1590)",
                ),
            )

    # ---- work failed: bounded retry ----------------------------------------
    if state.work_status == "failed":
        if counters.work_retries >= opts.max_work_retries:
            # #1590 part 6: name the actual cause. "failed 3 retries in: <prose>"
            # sent the morning triage looking at the work even when the provider
            # was the problem; the class is now stated up front.
            classification = classify_failure(
                failure_reason=state.work_failure_reason or None
            )
            return _die(
                f"work {state.work_aid} failed {counters.work_retries} retr(ies) in: "
                f"{state.work_failure_reason or 'no reason recorded'}\n"
                f"   cause: {classification.reason}\n"
                f"   inspect: coord log {state.work_aid} --machine "
                f"{state.work_machine or machine}"
            )
        counters.work_retries += 1
        return Action(
            kind=RUN,
            label=(
                f"WORK: failed → coord retry {state.work_aid} "
                f"(attempt {counters.work_retries}/{opts.max_work_retries})"
            ),
            command=("retry", state.work_aid),
            error_message=f"coord retry failed for {state.work_aid}",
        )

    # ---- work reached a terminal state that is not 'done' ------------------
    #
    # Every status here is TERMINAL (a non-terminal row would have been caught
    # by the active_count wait above), so none of them may fall through to a
    # bare wait — that spins silently until the deadline instead of reporting
    # anything.
    warnings: tuple[str, ...] = ()
    if state.work_status == "done":
        pass
    elif state.work_status == "advisory":
        advisory = _decide_advisory(state, opts, machine, verifier)
        if advisory.is_exit:
            return advisory
        warnings = advisory.warnings
    elif state.work_status == "cancelled":
        return _die(
            f"work {state.work_aid} was cancelled — re-dispatch with: "
            f"coord assign {machine} {state.repo} {state.issue} --force"
        )
    else:
        return _die(
            f"unexpected terminal work status '{state.work_status}' for "
            f"{state.work_aid} —\n"
            f"   refusing to guess. Inspect: coord log {state.work_aid} --machine "
            f"{state.work_machine or machine}"
        )

    # A 'done' row with no branch never pushed anything either.
    if not state.work_branch:
        return _die(
            f"work {state.work_aid} finished with no branch — nothing was pushed "
            "(0-commit advisory).\n"
            f"   inspect: coord log {state.work_aid} --machine "
            f"{state.work_machine or machine}"
        )

    test = _decide_test(state, opts, counters, machine)
    if test is not None:
        return replace(test, warnings=warnings + test.warnings)

    review = _decide_review(state, opts, counters, machine)
    if review is not None:
        return replace(review, warnings=warnings + review.warnings)

    if not opts.do_merge:
        return replace(
            _succeed(
                "✓ review approved — stopping here (--no-merge)\n"
                f"  merge with: coord merge --only {state.work_aid}"
            ),
            warnings=warnings,
        )

    merge = _decide_merge(state, opts, counters)
    return replace(merge, warnings=warnings + merge.warnings)


def _dispatch_work_stage(
    state: IssueState,
    opts: DriveOptions,
    machine: str,
    oracle: OracleDecision | None = None,
    gate_checker: AcceptanceGateChecker | None = None,
    verifier: MergeVerifier | None = None,
) -> Action:
    """No work row yet: run the optional plan stage, then dispatch the work.

    #1453: when *oracle* is active, the sealed JIT acceptance slice for this
    issue is authored — and observed through to a landed merge — BEFORE
    either the plan or the direct-assign path below. Otherwise the #1138
    hard gate (``coord.dispatch.enforce_oracle_readiness``) would simply
    refuse the eventual ``coord assign``/``coord approve-plan`` once an
    oracle-opted-in milestone's issue reaches it, with this driver never
    having explained why.
    """
    if oracle is not None and oracle.active:
        assert gate_checker is not None and verifier is not None, (
            "oracle.active implies resolve_oracle_decision ran with a real "
            "gate_checker; decide()/Driver always thread one through"
        )
        gate = _decide_acceptance_author(state, oracle, opts, machine, gate_checker, verifier)
        if gate is not None:
            return gate

    # #1499: durable provenance stamped on every assignment this driver
    # dispatches via `coord assign` — the piece that survives the driver
    # process exiting (see coord.models.Assignment.driven_by / Proposal.driven_by).
    driven_by = f"drive:{state.repo}#{state.issue}"

    if opts.do_plan:
        if not state.plan_aid:
            args = [
                "assign", "--plan-only", machine, state.repo, str(state.issue),
                "--driven-by", driven_by,
            ]
            if opts.model:
                args += ["--model", opts.model]
            return Action(
                kind=RUN,
                label=(
                    f"PLAN: coord assign --plan-only {machine} {state.repo} "
                    f"{state.issue}"
                ),
                command=tuple(args),
            )
        if state.plan_status == "done":
            return Action(
                kind=RUN,
                label=f"PLAN: approved → coord approve-plan {state.plan_aid}",
                command=("approve-plan", state.plan_aid),
            )
        if state.plan_status == "failed":
            return _die(
                f"plan assignment {state.plan_aid} failed — inspect: "
                f"coord log {state.plan_aid} --machine {machine}"
            )
        return _wait()

    args = ["assign", machine, state.repo, str(state.issue), "--driven-by", driven_by]
    if opts.model:
        args += ["--model", opts.model]
    if opts.briefing_file:
        args += ["--briefing-file", opts.briefing_file]
    return Action(
        kind=RUN,
        label=f"WORK: coord assign {machine} {state.repo} {state.issue}",
        command=tuple(args),
    )


def _decide_advisory(
    state: IssueState,
    opts: DriveOptions,
    machine: str,
    verifier: MergeVerifier,
) -> Action:
    """The #448 downgrade: the agent flagged a zero-commit / stash-miss exit.

    #1357 makes this a FALSE POSITIVE for every Python-only headless assignment
    in claude-coordinator — its only artifact glob is
    ``tui/target/debug/coord-tui``, which a Python diff never produces, so
    #1323's stash-miss check downgrades a perfectly good DONE.  Ask git which
    case this actually is rather than trusting the status.
    """
    if not state.work_branch or not verifier.branch_has_commits(state):
        return _die(
            f"work {state.work_aid} exited ADVISORY with no commits on its branch —\n"
            "   nothing was pushed, so there is nothing to test, review, or merge.\n"
            f"   inspect: coord log {state.work_aid} --machine "
            f"{state.work_machine or machine}"
        )
    if not opts.accept_advisory:
        return _die(
            f"work {state.work_aid} is ADVISORY, but its branch carries real "
            "commits.\n"
            "   This is the #1357 signature: since v0.4.75 every Python-only "
            "headless\n"
            "   assignment in this repo is downgraded DONE→ADVISORY by an "
            "artifact glob\n"
            "   that a Python diff can never match.\n"
            "   Proceed anyway with --accept-advisory (and fix #1357 to stop "
            "needing it)."
        )
    return Action(
        kind=WAIT,
        warnings=(
            "ADVISORY with commits present — proceeding per --accept-advisory (#1357)",
        ),
    )


def _decide_test(
    state: IssueState,
    opts: DriveOptions,
    counters: DriveCounters,
    machine: str,
) -> Action | None:
    """The TEST gate.  ``None`` means "passed/skipped, fall through".

    #1426: coord dispatches this stage itself (``dispatch_smoke`` via the
    ``coord serve`` tick loop or ``coord notify``) onto a capability-matched
    machine; this only OBSERVES ``test_state``, exactly like the review gate
    below.  ``--skip-test`` is the one Test-stage action taken here, and it is
    a ``coord test --skipped`` CLI call — never a direct
    ``record_test_verdict()`` (#1384).
    """
    test_state = state.work_test_state
    if test_state in ("passed", "skipped"):
        return None

    # #1605: the Test-stage CHILD assignment (`type="smoke"`) itself reached
    # a terminal FAILED/cancelled status — a dead agent, a killed process
    # group, a terminal API error, anything short of the worker actually
    # printing `SMOKE: pass`/`SMOKE: fail` — without ever producing a
    # verdict. Before this, `test_state` could be left at `"running"`
    # (`dispatch_smoke`'s own marker, #1426) forever: every gate treats
    # `"running"` as "no verdict yet" (#1395), so nothing downstream ever
    # resolves it and this function prints "TEST: in progress" every poll,
    # unbounded — the exact #1598 incident this closes (2.5 hours against
    # three idle machines). `reconcile_completed_assignments` /
    # `coord diagnose --stage test` normally resolve this from the daemon's
    # own tick, but a live `coord drive` loop must not depend on that
    # timing — detect the contradiction directly from the child's own board
    # fields and stop with an actionable message rather than poll forever.
    # Scoped to smoke FAILED/cancelled (not "done"): a fresh `done` smoke
    # completion has an expected, bounded propagation lag before `coord
    # notify` records its verdict — that is NOT this bug.
    if state.smoke_status in ("failed", "cancelled") and test_state == "running":
        return _die(
            "test stage is stuck: work.test_state='running' but its "
            f"Test-stage worker {state.smoke_aid} already finished "
            f"(status={state.smoke_status!r}, reason="
            f"{state.smoke_failure_reason or 'none recorded'!r}) — the "
            "parent verdict was never resolved (#1605).\n"
            f"   Recover: coord diagnose {state.repo} {state.issue} --stage "
            "test\n"
            "   (add --reset if the diagnosis alone doesn't clear it)"
        )

    if test_state == "":
        if opts.skip_test:
            return Action(
                kind=RUN,
                label="TEST: --skip-test → recording 'skipped'",
                command=(
                    "test",
                    "--skipped",
                    "--reason",
                    "coord drive --skip-test",
                    state.work_aid,
                ),
                sleep_after=5.0,
            )
        # Waiting for coord to dispatch the Test stage itself.  The stall
        # detector nudges `coord notify` (--notify) after --stall minutes of no
        # state change — no need to force it here on every poll.
        return _wait()

    if test_state == "running":
        return _wait(label="TEST: in progress on a capability-matched machine")

    if test_state == "failed":
        if counters.fix_rounds >= opts.max_fix_rounds:
            return _die(
                f"test still failing after {counters.fix_rounds} fix round(s) — "
                "stopping.\n"
                f"   Reason: {state.work_test_reason or 'none recorded'}\n"
                f"   Inspect: coord log {state.work_aid} --machine "
                f"{state.work_machine or machine}\n"
                f"   Continue by hand: coord assign --interactive --fix-of "
                f"{state.work_aid}"
            )
        counters.fix_rounds += 1
        # `coord fix` gates on the assignment's legacy `smoke_test == "fail"`
        # field — which `coord test --fail` mirrors from `test_state` — and
        # dispatches a follow-up worker with `inherit_branch=True`, so the fix
        # continues the SAME branch rather than orphaning it on a fresh one. It
        # also escalates the model (sonnet → opus) and quotes the stored test
        # output in the briefing.  This is why a test failure is a loop
        # iteration and not a dead end.  (The interactive `--fix-of` and `coord
        # bounce` paths are NOT usable here: `--fix-of` requires --interactive,
        # and `bounce` needs a request-changes REVIEW id, not a failed test.)
        return Action(
            kind=RUN,
            label=(
                f"TEST: failed → fix round {counters.fix_rounds}/"
                f"{opts.max_fix_rounds} (coord fix {state.work_aid})"
            ),
            command=("fix", state.work_aid),
            error_message=(
                f"coord fix {state.work_aid} failed to dispatch.\n"
                "   Most likely the assignment's legacy smoke_test field is not "
                "'fail' — that is\n"
                "   what `coord fix` gates on, and only `coord test --fail` sets "
                "it.\n"
                f"   Check: coord log {state.work_aid}   /   continue by hand: "
                f"coord assign --interactive --fix-of {state.work_aid}"
            ),
        )

    return Action(kind=WAIT, warnings=(f"unexpected test_state '{test_state}'",))


def _decide_review(
    state: IssueState,
    opts: DriveOptions,
    counters: DriveCounters,
    machine: str,
) -> Action | None:
    """The REVIEW gate.  ``None`` means "approved, fall through to merge".

    coord dispatches the review itself once the test verdict lands (the notify
    timer's ``dispatch_pending_reviews``).  We only observe — except for the
    #555 interactive case, which needs one explicit request.
    """
    verdict = state.review_verdict
    if verdict == "approve":
        return None

    # #1584: the review WORKER itself died (transient API error, network
    # drop, ...) before ever producing a verdict. Before #1584 that worker
    # was mislabelled `done` with `review_verdict == ""`, which fell through
    # to the `verdict == ""` branch below and either waited for a dispatch
    # that would never come or (if `state.work_review_state == "done"`)
    # died with a "REVIEW_VERDICT block failed to parse" message that no
    # longer applies now that the review is correctly `failed`. Checked
    # BEFORE `verdict == ""` so it can never fall through to that stale
    # message or to a silent `_wait()` — the exact regression this issue's
    # own evidence (#1563) was filed over.
    #
    # Re-dispatch via ``coord review <work_aid>`` — NOT ``coord retry
    # <review_aid>``. ``coord retry``'s underlying `_reassign` (coord/
    # reconcile.py) hardcodes `type="work"` on every re-dispatch regardless
    # of the failed assignment's own type (it exists solely to retry WORK
    # rows); pointing it at a review assignment id would silently create a
    # bogus fresh `type="work"` assignment on this issue instead of a
    # review. ``coord review`` is the existing #555 escape-hatch command
    # (already used a few lines below for the interactive case) — a thin,
    # type-correct wrapper over `coord.review.dispatch_review` keyed on the
    # WORK row, which is still `status="done"` (only the review it spawned
    # failed). Bounded the same way as the WORK failed-retry loop in
    # `decide()` (usage-limit-aware wait, then a bounded re-dispatch).
    if state.review_status == "failed":
        if is_usage_limit_reason(state.review_failure_reason):
            return Action(
                kind=WAIT,
                label=(
                    f"REVIEW: {state.review_aid} killed by the usage limit — "
                    "waiting for the reset, not retrying"
                ),
                warnings=(
                    f"usage-limit kill detected on {state.review_aid}: "
                    f"{state.review_failure_reason} — waiting for the reset "
                    "instead of retrying (#1461/#1584)",
                ),
            )
        if counters.review_retries >= opts.max_work_retries:
            return _die(
                f"review {state.review_aid} failed "
                f"{counters.review_retries} retr(ies) in: "
                f"{state.review_failure_reason or 'no reason recorded'}\n"
                f"   inspect: coord log {state.review_aid}"
            )
        counters.review_retries += 1
        return Action(
            kind=RUN,
            label=(
                f"REVIEW: failed → coord review {state.work_aid} "
                f"(attempt {counters.review_retries}/{opts.max_work_retries})"
            ),
            command=("review", state.work_aid),
            error_message=f"coord review failed for {state.work_aid}",
        )

    if verdict == "request-changes":
        if state.work_review_iter >= state.max_review_iterations:
            return _die(
                "review requested changes and the fix loop is exhausted\n"
                f"   ({state.work_review_iter} rounds, cap "
                f"{state.max_review_iterations}).\n"
                f"   Findings: coord log {state.review_aid}\n"
                f"   Continue by hand: coord assign --interactive --fix-of "
                f"{state.review_aid}"
            )
        # The auto-loop dispatches the fix; wait for it to appear.
        return _wait()

    if verdict == "":
        if state.work_review_state == "done":
            return _die(
                f"review {state.review_aid} finished but recorded NO verdict — the\n"
                "   REVIEW_VERDICT block failed to parse (#1346/#1348 class).\n"
                "   Recover: coord post-pending-reviews, or read the transcript "
                "directly."
            )
        # No review row at all.  For interactive work that is terminal, not
        # transient (#555) — request one explicitly, once, rather than waiting
        # on a dispatch that will never happen.  Preflight already refused this
        # case unless --force-review was given.
        if not state.review_aid and state.work_provider == "claude-pty":
            if not opts.force_review:
                return _die(
                    f"no review for interactive work {state.work_aid} and "
                    "--force-review not set (#555)."
                )
            if counters.review_dispatches >= 1:
                return _die(
                    f"requested a review for {state.work_aid} but none appeared "
                    "on the board.\n"
                    "   Check for an eligible reviewer machine: coord status"
                )
            counters.review_dispatches += 1
            return Action(
                kind=RUN,
                label="REVIEW: requesting explicitly (interactive work, #555)",
                command=("review", state.work_aid),
                error_message=(
                    f"explicit review dispatch failed for {state.work_aid}"
                ),
            )
        return _wait()

    return Action(kind=WAIT, warnings=(f"unexpected review verdict '{verdict}'",))


# #1505: merge statuses a bounded `coord merge --only` retry can actually
# change. "" / PENDING / READY / MERGING are normal in-flight states — the
# next `coord merge` tick is expected to move them forward. CONFLICT is
# retried too, deliberately: that's what runs `classify_conflict` +
# `dispatch_conflict_fix` (#1474, see this function's docstring). Everything
# else — most commonly NEEDS_ATTENTION, or any status this driver has never
# seen before — cannot be resolved by retrying, so it escalates instead of
# spinning the attempt cap down to zero on a no-op.
#
# #1505 review fix: `merge_queue.plan()`'s `_state_to_plan_status` collapses
# CONFLICT, HUMAN_REQUIRED, and SKIPPED into a single "NEEDS_ATTENTION"
# status for operator display, and `merge_plan` (not the raw `merge_queue`
# table) is what a normal daemon-backed `/board` build actually populates —
# so a literal `status == "CONFLICT"` almost never reaches this function
# without help. `drive_state._merge_entry` is where that help lives: it
# cross-checks the raw `merge_queue` row and reports its un-collapsed state
# whenever the plan says NEEDS_ATTENTION, so a fresh, still-auto-fixable
# conflict lands here as "CONFLICT" (retried) rather than "NEEDS_ATTENTION"
# (escalated on sight). See `_merge_entry`'s docstring for the full story.
_RETRYABLE_MERGE_STATUSES = frozenset({"", "PENDING", "READY", "MERGING", "CONFLICT"})

_PR_NUMBER_RE = re.compile(r"/pull/(\d+)")


def _extract_pr_number(pr_url: str) -> int | None:
    """Best-effort PR number out of a GitHub PR URL, or ``None``."""
    if not pr_url:
        return None
    m = _PR_NUMBER_RE.search(pr_url)
    return int(m.group(1)) if m else None


# ── #1526: driver/gate divergence ───────────────────────────────────────────
#
# `coord drive` decides `test=`/`review=` are satisfied from
# `work_test_state`/`review_verdict` — the same fields `_decide_test`/
# `_decide_review` above already let through (that is WHY `decide()` ever
# reaches `_decide_merge` at all). `coord merge` enforces a DIFFERENT, fresher
# check (`merge_queue.has_smoke_verdict`/`has_approved_review` — SHA/patch-id
# -anchored freshness for smoke, patch-id voiding for review) and can refuse
# for a reason this driver's own view never sees coming. When it does, the
# refusal text is left on the board as `merge_reason` — persisted on the raw
# queue row's `.error` by `merge_queue.process()` — even while `merge_status`
# itself often still reads a RETRYABLE value like READY, because the
# board-render gate check in `merge_queue.plan()`'s `_entry_gate_status`
# doesn't have the live SHA data a real merge attempt fetches (see that
# function's docstring). Retrying `coord merge` unchanged cannot resolve two
# READS of the board disagreeing with each other; only a human (or a fresh
# verdict) can — see `_merge_gate_divergence`.
_SMOKE_GATE_MARKERS = ("smoke test required", "test verdict missing")
_REVIEW_GATE_MARKERS = ("review required", "review not approved")


def _merge_gate_kind(reason: str) -> str | None:
    """Classify a merge-queue block *reason* as the gate it names, or
    ``None`` when it isn't one of the two this module knows a corrective
    action for.

    Matches both `merge_queue.process()`'s live-attempt wording ("smoke test
    required but no verdict recorded" / "review required but not approved")
    and `merge_queue.plan()`'s board-render wording ("test verdict missing" /
    "review not approved") — the two functions describe the identical gates
    in different words.
    """
    r = (reason or "").lower()
    if any(marker in r for marker in _SMOKE_GATE_MARKERS):
        return "smoke"
    if any(marker in r for marker in _REVIEW_GATE_MARKERS):
        return "review"
    return None


def _merge_gate_divergence(state: IssueState) -> str | None:
    """``"smoke"``/``"review"`` when *state* shows the #1526 divergence,
    else ``None``.

    The divergence: `state.merge_reason` names a smoke/review block while
    this SAME state's `work_test_state`/`review_verdict` says the opposite.
    That contradiction can only come from `coord merge` checking something
    this driver's view does not (freshness against the CURRENT branch/base,
    not just the terminal verdict) — never from a retry, since neither
    input changes by running `coord merge` again unchanged.
    """
    kind = _merge_gate_kind(state.merge_reason)
    if kind == "smoke" and state.work_test_state in ("passed", "skipped"):
        return "smoke"
    if kind == "review" and state.review_verdict == "approve":
        return "review"
    return None


def _escalate_merge(
    state: IssueState, status: str, *, gate_kind: str | None = None
) -> Action:
    """Build the EXIT action for a merge status retrying cannot fix (#1505).

    Escalates on the FIRST encounter rather than after exhausting
    ``max_merge_attempts`` — a merge attempt is expensive (a whole
    ``coord merge`` run) and, for these statuses, guaranteed to be a no-op;
    an escalation record is cheap and actionable instead.

    This function stays pure like every other decision in this module (see
    the "STRUCTURE" section of the module docstring) — it only *describes*
    the escalation via the returned :class:`Action`'s ``command``.  The
    actual write happens in :meth:`Driver.run`'s exit handling, which runs
    that command through the CLI exactly like any other board mutation this
    driver performs (``coord escalate record ...``, never a direct
    ``coord.state`` call).

    *gate_kind* (#1526) is set by :func:`_decide_merge` when
    :func:`_merge_gate_divergence` fired — a smoke/review gate refusal this
    driver's own ``work_test_state``/``review_verdict`` view contradicts. The
    proposed command in that case names the specific, safe corrective action
    (re-confirm the test verdict, or a scoped/full re-review) rather than the
    generic "inspect the plan" fallback below.

    Otherwise, the proposed command mirrors the #1477 resolution this issue
    was opened over: when a PR is known, ``gh pr merge --rebase`` + ``coord
    reconcile-merges`` is the sanctioned escape hatch (also documented in
    docs/OPERATING_GOTCHAS.md). Without a known PR number there is nothing
    concrete to propose beyond pointing at the plan for a human to read.
    """
    pr_number = _extract_pr_number(state.merge_pr_url)
    if gate_kind == "smoke":
        proposed = (
            f"coord test {state.work_aid} --passed   # ONLY if the suite "
            "genuinely still passes against the CURRENT base — otherwise "
            f"dispatch a fresh smoke test for {state.work_aid}"
        )
    elif gate_kind == "review":
        proposed = (
            f"coord review-reaffirm {state.work_aid} --reason '<why this "
            f"delta is safe>'   # or a full re-review: coord review "
            f"{state.work_aid}"
        )
    elif pr_number is not None:
        proposed = f"gh pr merge {pr_number} --rebase && coord reconcile-merges"
    else:
        proposed = (
            f"coord merge --plan --repo {state.repo}   "
            "# inspect the gates, then decide"
        )

    if gate_kind is not None:
        driver_view = (
            f"test_state={state.work_test_state!r}"
            if gate_kind == "smoke"
            else f"review_verdict={state.review_verdict!r}"
        )
        reason = (
            f"{gate_kind}_required — coord merge's own gate reports "
            f"{state.merge_reason!r}, but this driver's OWN view already "
            f"shows {driver_view} — the two cannot converge by retrying the "
            "identical `coord merge` command (#1526); a human must "
            "reconcile them"
        )
    else:
        reason = (
            f"merge_status={status or '(empty)'} — no number of retries changes "
            "this; escalating on first encounter instead of burning the "
            "merge-attempt budget (#1505)"
        )
    gate_pairs = (
        ("merge_status", status or "(empty)"),
        ("merge_reason", state.merge_reason or "(none)"),
        ("review_verdict", state.review_verdict or "(none)"),
        ("test_state", state.work_test_state or "(none)"),
        ("pr_url", state.merge_pr_url or "(none)"),
    )
    gates_summary = " | ".join(f"{k}={v}" for k, v in gate_pairs)
    aid = state.merge_aid or state.work_aid

    command: list[str] = [
        "escalate", "record", state.repo, str(state.issue),
        "--stage", "merge",
        "--reason", reason,
    ]
    for k, v in gate_pairs:
        command += ["--gate", f"{k}={v}"]
    command += ["--command", proposed]
    if aid:
        command += ["--assignment", aid]

    return Action(
        kind=EXIT,
        exit_code=EXIT_ESCALATED,
        message=(
            f"merge escalated: {reason}\n"
            f"   gates: {gates_summary}\n"
            f"   proposed: {proposed}\n"
            f"   Recorded on the board — see: coord escalate list --repo {state.repo}"
        ),
        command=tuple(command),
        error_message=(
            "failed to record the escalation on the board (exiting anyway — "
            f"resolve by hand: coord escalate record {state.repo} {state.issue} "
            "--reason ... --command ...)"
        ),
    )


def _decide_merge(
    state: IssueState, opts: DriveOptions, counters: DriveCounters
) -> Action:
    """The MERGE stage.

    #1474: a CONFLICT status must NOT be a bare wait.  ``dispatch_conflict_fix``
    (coord.conflict_fix) has exactly two sanctioned callers — inside an actual
    ``coord merge`` run, and the semantic-escalation variant behind
    ``pipeline.escalate_semantic_conflicts`` that only ``coord resume``
    (human-invoked) reaches — so nothing ever dispatches the fix worker while
    this function just parks on ``_wait()``.  That was the exact deadlock that
    stalled #1453/#1461 for ~14 hours despite ``classify_conflict()`` correctly
    saying ``rebaseable`` and a capable machine being idle: the coordinator's
    own #241 auto-rebase machinery was never invoked.

    The fix is to fall through to the same bounded ``coord merge --only <aid>``
    retry below every other non-terminal status already uses — that call is
    what runs ``classify_conflict`` + ``dispatch_conflict_fix`` (or discovers
    one is already in flight / already failed and escalates to
    ``HUMAN_REQUIRED``, which stays terminal via the check above). Once a
    conflict-fix is actually dispatched, it shows up as a `type="conflict-fix"`
    row for this same issue, so the very first check in :func:`decide`
    (``state.active_count > 0`` → wait) parks the run there on the next poll —
    :func:`decide` never even reaches this function while it is running. That
    is what keeps this from double-dispatching or fighting an in-flight fix;
    :func:`coord.conflict_fix.has_prior_conflict_fix` /
    :func:`~coord.conflict_fix._has_active_conflict_fix` are the belt-and-
    braces guard inside ``dispatch_conflict_fix`` itself.

    #1526: the driver/gate divergence (:func:`_merge_gate_divergence`) is
    checked FIRST, before the status switch below — it can hide behind
    EITHER a nominally-blocking status (BLOCKED, if ``merge_queue.plan()``'s
    own render-time gate check caught the same disagreement) or a
    nominally-retryable one (READY/PENDING/"", if only a live ``coord
    merge`` attempt caught it and left its reason on the board — see that
    function's docstring for why the two checks can disagree). Escalating
    here, before either branch runs, is what stops the driver from spending
    its whole ``--max-merge-attempts`` budget retrying a merge that cannot
    succeed until a human — or a fresh verdict — reconciles the two
    readings; retrying the identical ``coord merge`` command changes neither
    side of the disagreement.
    """
    divergence = _merge_gate_divergence(state)
    if divergence is not None:
        return _escalate_merge(state, state.merge_status, gate_kind=divergence)

    status = state.merge_status
    if status.upper() == "HUMAN_REQUIRED":
        return _die(
            f"merge entry is HUMAN_REQUIRED: {state.merge_reason or 'no reason recorded'}\n"
            "   An automated conflict-fix already gave up. Resolve by hand, or "
            "override:\n"
            f"     coord merge --only {state.merge_aid or state.work_aid} "
            "--override-human-required '<reason>'"
        )
    if status.upper() == "BLOCKED":
        return _wait(
            label=(
                "MERGE: blocked — "
                f"{state.merge_reason or 'gate not satisfied'}; re-checking"
            )
        )

    # #1505: a status no retry can fix (most commonly NEEDS_ATTENTION)
    # escalates immediately rather than falling into the bounded retry below
    # — see `_RETRYABLE_MERGE_STATUSES` and `_escalate_merge`.
    if status.upper() not in _RETRYABLE_MERGE_STATUSES:
        return _escalate_merge(state, status)

    # Cap the attempts: without this, a merge that fails for a reason the board
    # never reflects (so merge_status stays empty) would re-run `coord merge`
    # on every poll until the deadline. The same cap bounds the CONFLICT case
    # (#1474) — a `coord merge --only` that keeps landing back on CONFLICT
    # (e.g. a fresh conflict on every rebase attempt) must still terminate
    # rather than spin forever.
    if counters.merge_attempts >= opts.max_merge_attempts:
        return _die(
            f"merge attempted {counters.merge_attempts} times without landing.\n"
            f"   Last board state: status='{status or 'none'}' "
            f"reason='{state.merge_reason or 'none'}'\n"
            f"   Inspect the gates: coord merge --plan --repo {state.repo}"
        )
    counters.merge_attempts += 1
    aid = state.merge_aid or state.work_aid
    # Tolerant on purpose: the first attempt often lands before the daemon's
    # tick has run `enqueue_approved_work`, so `--only <aid>` finds no queue
    # entry.  That is a "try again next poll", not a reason to abort the run —
    # the attempt cap above is what bounds it. A CONFLICT entry that is
    # already CONFLICT (not PENDING) similarly errors out of `--only`
    # (`coord merge --only` refuses a non-PENDING entry) rather than
    # reclassifying it — that message still counts against the same cap, so
    # a genuinely stuck entry dies with a clear pointer instead of spinning.
    conflict_note = (
        " — retrying via coord merge's own conflict-fix dispatch (#241)"
        if status.upper() == "CONFLICT"
        else ""
    )
    return Action(
        kind=RUN,
        label=(
            f"MERGE: attempt {counters.merge_attempts}/{opts.max_merge_attempts} "
            f"(coord merge --only {aid} --method {opts.merge_method})"
            f"{conflict_note}"
        ),
        command=("merge", "--only", aid, "--method", opts.merge_method),
        on_error="warn",
        error_message=(
            "coord merge returned non-zero (or the merge lock timed out) — "
            "re-checking next poll"
        ),
        serialize_merge=True,
    )


# ── locking ──────────────────────────────────────────────────────────────────


class LockBusy(Exception):
    """Someone else holds the lock."""


@dataclass
class FileLock:
    """``flock``-based advisory lock, the Python twin of the bash ``flock -n``."""

    path: Path
    _fd: int | None = field(default=None, init=False, repr=False)

    def acquire(self, timeout: float | None = 0.0) -> None:
        """Take the lock.  ``timeout=0`` is non-blocking; ``None`` blocks forever."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o644)
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._fd = fd
                return
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    os.close(fd)
                    raise
                if deadline is not None and time.monotonic() >= deadline:
                    os.close(fd)
                    raise LockBusy(str(self.path)) from exc
                time.sleep(0.25)

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> FileLock:
        self.acquire(timeout=None)
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


# ── the driver (I/O) ─────────────────────────────────────────────────────────


def coord_argv() -> list[str]:
    """The ``coord`` invocation prefix.

    Prefers the installed console script (the same thing a human types).  Falls
    back to ``python -m coord.cli`` when it is not on PATH — which happens under
    a venv whose ``bin`` is not exported, e.g. a worker with the agent venv
    stripped (#402).  Overridable with ``$COORD_DRIVE_COORD_BIN`` for tests.
    """
    override = os.environ.get("COORD_DRIVE_COORD_BIN")
    if override:
        return override.split()
    found = shutil.which("coord")
    if found:
        return [found]
    return [sys.executable, "-m", "coord.cli"]


# ── tmux launch (`coord drive --tmux`, #1398) ─────────────────────────────────
#
# A drive runs 60-90 minutes. `--tmux` launches it DETACHED in a
# `coord-drive-<repo>-<issue>` tmux session instead of running inline, so the
# run survives the launching terminal closing, a TUI restart, or an ssh drop
# — the same rationale, and the same `TmuxHost`/`tmux_available`/
# `tmux_session_alive` seam, as the `coord-<assignment_id>` interactive
# sessions in `coord/interactive.py` and the free-floating `coord-term-*`
# terminals in `coord/commands/terminal.py`. Unlike both of those, a drive
# session is LOCAL ONLY — the driver runs on the operator's machine, reading
# the daemon's board over the network, so there is no remote/ssh variant
# here (see the class docstring's "Out of scope" note in #1398).
#
# Killing the tmux session IS Stop: the per-issue `flock` in `Driver.run()`
# is released when the process's file descriptor closes (the OS does this on
# any process exit, including SIGHUP from a killed tmux pane) — no separate
# cleanup code is needed for cancellation to be correct.


def drive_session_name(repo: str, issue: int) -> str:
    """Return the canonical tmux session name for a ``coord drive --tmux`` run."""
    return f"{DRIVE_SESSION_PREFIX}{repo}-{issue}"


def parse_drive_session_name(session_name: str) -> tuple[str, int] | None:
    """Parse a ``coord-drive-<repo>-<issue>`` session name back to ``(repo, issue)``.

    Returns ``None`` when *session_name* doesn't carry the drive prefix, or
    the segment after the LAST hyphen isn't a bare issue number (repo names
    may themselves contain hyphens, so the issue number — always numeric —
    is what anchors the split).
    """
    if not session_name.startswith(DRIVE_SESSION_PREFIX):
        return None
    rest = session_name[len(DRIVE_SESSION_PREFIX):]
    repo, sep, issue_str = rest.rpartition("-")
    if not sep or not repo or not issue_str.isdigit():
        return None
    return repo, int(issue_str)


def list_drive_sessions(*, host: TmuxHost = TmuxHost(None)) -> list[dict[str, Any]]:
    """Return live ``coord-drive-*`` tmux sessions on *host* as parsed dicts.

    Each entry: ``{"repo": str, "issue": int, "session_name": str, "attached": bool}``.
    Mirrors :func:`coord.commands.terminal.list_tmux_terminal_sessions` — a
    single ``tmux list-sessions`` call; returns ``[]`` when tmux is
    unavailable, has no server running, or has no matching sessions.
    """
    try:
        result = subprocess.run(
            host.cmd([
                "list-sessions", "-F",
                "#{session_name}\t#{session_attached}",
            ]),
            capture_output=True,
            text=True,
            timeout=5.0,
        )
    except (subprocess.SubprocessError, OSError):
        return []
    if result.returncode != 0:
        return []

    sessions: list[dict[str, Any]] = []
    for raw_line in result.stdout.splitlines():
        parts = raw_line.split("\t")
        if len(parts) < 2:
            continue
        name, attached_raw = parts[0].strip(), parts[1].strip()
        parsed = parse_drive_session_name(name)
        if parsed is None:
            continue
        repo, issue = parsed
        sessions.append({
            "repo": repo,
            "issue": issue,
            "session_name": name,
            "attached": attached_raw not in ("", "0"),
        })
    return sessions


def launch_drive_in_tmux(
    cmd: Sequence[str],
    *,
    repo: str,
    issue: int,
    host: TmuxHost = TmuxHost(None),
) -> str:
    """Create a detached tmux session named for *(repo, issue)* running *cmd*.

    *cmd* is a full argv (e.g. ``coord_argv() + ["drive", repo, str(issue),
    ...]``) — each element is passed to tmux as a SEPARATE argument, which
    tmux hands to ``execve`` unmodified (no shell re-splitting), so a path
    containing spaces (``--briefing-file``, ``--config``) survives intact.

    Returns the session name.  Raises :class:`DriveError` when tmux is
    unavailable or a session for this *(repo, issue)* is already alive — the
    CLI checks aliveness first for a friendlier message, but this guards
    direct/test callers too.
    """
    if not tmux_available():
        raise DriveError("tmux is not available on this machine.", EXIT_USAGE)
    session = drive_session_name(repo, issue)
    if tmux_session_alive(session, host=host):
        raise DriveError(
            f"already driving {repo} #{issue} (tmux session {session!r} is live).\n"
            f"   attach with: coord drive-attach {repo} {issue}",
            EXIT_USAGE,
        )
    try:
        result = subprocess.run(
            host.cmd(["new-session", "-d", "-s", session, *cmd]),
            capture_output=True,
            text=True,
            timeout=15.0,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        raise DriveError(f"failed to create tmux session: {exc}", EXIT_USAGE) from exc
    if result.returncode != 0:
        raise DriveError(
            f"tmux new-session failed: {(result.stderr or '').strip()}", EXIT_USAGE
        )
    return session


@dataclass
class Driver:
    """The resumable state machine's I/O shell: poll → decide → execute → sleep."""

    repo: str
    issue: int
    opts: DriveOptions
    config: Any
    fetcher: BoardFetcher = field(default_factory=BoardFetcher)
    verifier: MergeVerifier | None = None
    oracle_gate: AcceptanceGateChecker | None = None
    out: Any = None
    err: Any = None
    sleeper: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    # #1466: injected so tests can stub the Max-plan usage probe without a
    # real `claude -p "/usage"` subprocess — mirrors *verifier*/*oracle_gate*
    # above. Defaults to the real (cached, ~60s) probe.
    usage_prober: Callable[[], PlanLimits] = get_plan_limits

    _run_log: Path | None = field(default=None, init=False, repr=False)
    # #1499: the terminating Action's own message, captured by `_loop()` right
    # before it returns — `run()` folds this into the `drive_exited` audit
    # summary/details so a non-exceptional terminal exit (e.g. `decide()`
    # returning a `_die(...)` Action for a genuinely failed work assignment,
    # as opposed to a raised DriveError) still narrates WHY, not just the
    # bare exit code.
    _last_exit_message: str = field(default="", init=False, repr=False)

    def __post_init__(self) -> None:
        self.out = self.out or sys.stdout
        self.err = self.err or sys.stderr
        if self.verifier is None:
            self.verifier = GitMergeVerifier(
                repo_path=self.opts.repo_path, warn=self.warn
            )
        if self.oracle_gate is None:
            self.oracle_gate = GitHubAcceptanceGateChecker(config=self.config)

    # ── logging ─────────────────────────────────────────────────────────
    @staticmethod
    def _stamp() -> str:
        return time.strftime("%H:%M:%S")

    def log(self, message: str) -> None:
        print(f"{self._stamp()}  {message}", file=self.out, flush=True)

    def warn(self, message: str) -> None:
        print(f"{self._stamp()}  !! {message}", file=self.err, flush=True)

    def _append_run_log(self, text: str) -> None:
        if self._run_log is None or not text:
            return
        try:
            with self._run_log.open("a") as fh:
                fh.write(text)
        except OSError:
            pass

    # ── state ───────────────────────────────────────────────────────────
    def read_state(self) -> IssueState | None:
        """Project the current board, or ``None`` on a transport blip.

        A blip must never be a traceback: the loop just retries next poll.
        """
        try:
            payload = self.fetcher.fetch()
        except Exception as exc:  # noqa: BLE001 — transport, not logic
            self.warn(f"state read failed: {exc}")
            return None
        try:
            return project(payload, self.repo, self.issue, self.config)
        except DriveStateError as exc:
            raise DriveError(str(exc), EXIT_USAGE) from exc

    # ── execution ───────────────────────────────────────────────────────
    def run_coord(self, args: tuple[str, ...], *, serialize_merge: bool = False) -> int:
        """Run a ``coord`` subcommand, echoing its output and appending to the log.

        Output is captured and echoed after the process exits rather than
        streamed through a pipe.  The bash version used ``tee``, whose exit
        code masks the command's (one of the sharp edges #1392 set out to
        remove) — here the return code is unambiguous, and the run log still
        gets every byte.
        """
        argv = [*coord_argv(), *args]
        if self.opts.config_path:
            # Click parses options interspersed with arguments, so appending is
            # safe for every subcommand this driver invokes (all of which carry
            # the shared --config option).
            argv += ["--config", self.opts.config_path]
        if serialize_merge:
            # Merges are serialized on THIS HOST even when the runs themselves
            # are parallel.  #1400 fixed the daemon-side cross-talk (the
            # process-global `redirect_stdout` in POST /merge), so this is now
            # belt-and-braces for same-host callers rather than the only thing
            # preventing fleet-wide cross-talk; it still earns its keep by
            # keeping this host's own queue submissions ordered (two branches
            # rebasing onto a moving main at once is how pile-ups start) and by
            # failing fast locally instead of piling up blocked daemon requests.
            lock = FileLock(scratch_dir() / "merge.lock")
            try:
                lock.acquire(timeout=1800.0)
            except LockBusy:
                self.warn("merge lock timed out after 30m — re-checking next poll")
                return 1
            try:
                return self._spawn(argv)
            finally:
                lock.release()
        return self._spawn(argv)

    def _spawn(self, argv: list[str]) -> int:
        proc = subprocess.run(argv, capture_output=True, text=True, check=False)
        combined = (proc.stdout or "") + (proc.stderr or "")
        if combined:
            print(combined.rstrip("\n"), file=self.out, flush=True)
        self._append_run_log(combined)
        return proc.returncode

    def run_notify(self) -> None:
        """Nudge ``coord notify`` under the shared lock, to cut timer latency."""
        if not self.opts.notify:
            return
        self.log("nudging: coord notify (flock ~/.coord/notify.lock)")
        lock = FileLock(Path.home() / ".coord" / "notify.lock")
        try:
            lock.acquire(timeout=300.0)
        except LockBusy:
            self.warn("could not take ~/.coord/notify.lock within 5m — skipping nudge")
            return
        try:
            if self.run_coord(("notify",)) != 0:
                self.warn("coord notify returned non-zero")
        finally:
            lock.release()

    def _post_escalation_comment(self, state: IssueState, message: str) -> None:
        """#1526: durably surface an escalation's reason onto the issue
        itself, not just this run's tmux pane and the ``coord escalate``
        board row.

        Best-effort and never raises: a failed post must not mask the
        escalation that already happened via the exit code (``EXIT_
        ESCALATED``) and the board record `action.command` just wrote — the
        two durable channels this already has. This is a THIRD channel, not
        a replacement for either.
        """
        if not state.repo_github:
            return
        try:
            from coord import github_ops  # noqa: PLC0415

            github_ops.post_issue_comment(
                state.repo_github,
                state.issue,
                "🚧 **`coord drive` escalated — a human decision is needed.**\n\n"
                f"{message}\n\n"
                f"Run it: `coord escalate run {state.repo} {state.issue}`\n"
                f"Dismiss it: `coord escalate dismiss {state.repo} {state.issue}`",
            )
        except Exception as exc:  # noqa: BLE001 — best-effort, never mask the exit
            self.warn(f"could not post the escalation comment to GitHub: {exc}")

    # ── audit boundaries (#1499) ────────────────────────────────────────
    def _record_drive_audit(
        self,
        event_type: str,
        summary: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Emit a ``category="drive"``, ``actor="drive"`` audit row.

        ``coord.audit.record_audit`` is itself best-effort (never raises into
        the caller — disk-full/locked-DB/schema-drift are swallowed there),
        so this needs no try/except of its own: a broken audit_log must never
        take down the drive loop.
        """
        from coord.audit import record_audit  # noqa: PLC0415

        record_audit(
            tier="business",
            category="drive",
            event_type=event_type,
            actor="drive",
            summary=summary,
            repo=self.repo,
            issue=self.issue,
            details=details,
        )

    def _drive_exit_summary(
        self, exit_code: int | None, exc: BaseException | None
    ) -> tuple[str, dict[str, Any]]:
        """Human summary + machine details for the terminating ``drive_exited``
        row — the piece that answers "what did the driver do, and why did it
        stop?" retroactively, after the tmux session (if any) is long gone."""
        ident = f"{self.repo}#{self.issue}"
        if exc is not None:
            if isinstance(exc, DriveError):
                summary = f"drive exited for {ident}: {exc} (exit_code={exc.exit_code})"
                return summary, {"exit_code": exc.exit_code, "error": str(exc)}
            summary = f"drive exited for {ident}: unexpected error ({exc!r})"
            return summary, {"exit_code": None, "error": repr(exc)}
        # Non-exceptional terminal exit — decide() returned a `_die(...)` (or
        # `_succeed(...)`) Action directly (`_loop`'s `action.is_exit` branch
        # returns the code without raising). `_last_exit_message` carries that
        # Action's own `message` — the same text this run's log already
        # printed via self.log/self.warn — so the audit row narrates WHY,
        # not just the bare exit code.
        reason = self._last_exit_message.strip()
        if not reason:
            reason = {EXIT_OK: "ok", EXIT_DEADLINE: "deadline exceeded"}.get(
                exit_code, f"exit_code={exit_code}"
            )
        summary = f"drive exited for {ident} (exit_code={exit_code}): {reason}"
        return summary, {"exit_code": exit_code, "reason": reason}

    # ── the loop ────────────────────────────────────────────────────────
    def run(self) -> int:
        scratch = scratch_dir()
        self._run_log = scratch / f"{self.repo}-{self.issue}.log"

        # PER-ISSUE lock.  Two drivers on DIFFERENT issues are fine; two on the
        # SAME issue are not — they would double-dispatch work and
        # double-record verdicts.
        lock = FileLock(scratch / f"lock-{self.repo}-{self.issue}")
        holder = scratch / f"holder-{self.repo}-{self.issue}"
        try:
            lock.acquire(timeout=0.0)
        except LockBusy:
            try:
                who = holder.read_text().strip()
            except OSError:
                who = "another run"
            # No `drive_started`/`drive_exited` pair here — this run never
            # actually started (another driver already holds the per-issue
            # lock), so there is nothing new to narrate in the audit log.
            raise DriveError(
                f"already driving {self.repo} #{self.issue} ({who}).\n"
                "   A second driver on the SAME issue would double-dispatch work.\n"
                "   Other issues can be driven concurrently.\n"
                f"   Lock file: {lock.path}",
                EXIT_USAGE,
            ) from None
        try:
            holder.write_text(f"{self.repo} #{self.issue} (pid {os.getpid()})\n")
        except OSError:
            pass
        self._record_drive_audit(
            "drive_started", f"drive started for {self.repo}#{self.issue}"
        )
        try:
            exit_code = self._loop()
        except BaseException as exc:  # noqa: BLE001 — narrate every exit, then re-raise unchanged
            summary, details = self._drive_exit_summary(None, exc)
            self._record_drive_audit("drive_exited", summary, details=details)
            raise
        else:
            summary, details = self._drive_exit_summary(exit_code, None)
            self._record_drive_audit("drive_exited", summary, details=details)
            return exit_code
        finally:
            try:
                holder.unlink()
            except OSError:
                pass
            lock.release()

    def _loop(self) -> int:
        state = self.read_state()
        if state is None:
            raise DriveError("could not read board state", EXIT_USAGE)

        # #1466: probe ONCE here (not per-poll) — the underlying `claude -p
        # "/usage"` call is itself cached ~60s (coord.usage_limits), but
        # there's no reason to re-shell-out every loop iteration for a
        # decision only made at the top of the run. Skipped entirely when
        # the gate is off, so a `disabled` config never pays the subprocess
        # cost.
        usage_limits = (
            self.usage_prober() if self.config.usage_gate.mode != "disabled" else None
        )
        pre = preflight(state, self.opts, self.config, usage_limits=usage_limits)
        machine = pre.machine

        # #1453: resolved ONCE here (not per-poll) — the gate_checker inside
        # costs a GitHub fetch and a milestone's Gate-A status can't change
        # mid-run. Threaded unchanged into every decide() call below.
        oracle = resolve_oracle_decision(state, self.opts, self.config, self.oracle_gate)

        self.log(f"driving {self.repo} #{self.issue}")
        self.log(f"  machine        : {machine}")
        self.log(f"  acceptance     : {oracle.reason}")
        self.log(
            f"  test command   : {state.repo_test_command or '<none configured>'} "
            "(coord dispatches this itself — #1426; this observes)"
        )
        self.log(
            "  merge          : "
            + (f"yes ({self.opts.merge_method})" if self.opts.do_merge else "no")
        )
        self.log(
            "  auto-loop      : "
            + (
                "on (coord dispatches review fixes; this observes)"
                if state.auto_loop
                else "off"
            )
        )
        self.log(f"  test fix rounds: {self.opts.max_fix_rounds} (via coord fix)")
        self.log(
            f"  review fix cap : {state.max_review_iterations} (coord's auto-loop)"
        )
        self.log(
            "  notify nudge   : "
            + (
                "on"
                if self.opts.notify
                else "off (relying on the 5-min coord-notify.timer)"
            )
        )
        self.log(f"  log            : {self._run_log}")
        for warning in pre.warnings:
            self.warn(warning)

        if self.opts.dry_run:
            self.log("current state:")
            print(
                json.dumps(state.as_flat_dict(), indent=2, default=str),
                file=self.out,
                flush=True,
            )
            return EXIT_OK

        counters = DriveCounters()
        start = self.clock()
        deadline = start + self.opts.deadline_secs
        last_fingerprint = ""
        last_change = start
        # #1593: the nudge cadence is tracked SEPARATELY from `last_change`.
        # The one-shot latch (`nudged = False`/`True`, cleared only on a
        # fingerprint change) let a stage that stalls for 30-40 real minutes
        # get exactly one nudge near the start, then go completely silent —
        # `coord notify` correctly finds nothing to settle while the worker
        # is still running, and nothing ever re-checks after that. Re-nudging
        # every `stall_secs` while the fingerprint stays put, without
        # resetting `last_change`, keeps the staleness clock honest (so
        # `--stall` measures real elapsed idle time, not time-since-last-
        # nudge) while guaranteeing a stalled stage is never more than one
        # `stall_secs` window away from a fresh check.
        last_nudge: float | None = None

        while True:
            now = self.clock()
            if now > deadline:
                self._last_exit_message = (
                    f"deadline of {self.opts.deadline_mins:g}m exceeded"
                )
                self.warn(self._last_exit_message)
                if state is not None:
                    print(
                        json.dumps(state.as_flat_dict(), indent=2, default=str),
                        file=self.err,
                        flush=True,
                    )
                return EXIT_DEADLINE

            state = self.read_state()
            if state is None:
                self.sleeper(self.opts.poll)
                continue

            fingerprint = state.fingerprint
            if fingerprint != last_fingerprint:
                last_fingerprint = fingerprint
                last_change = now
                last_nudge = None
                self.log(
                    f"state: work={state.work_status or '-'} "
                    f"test={state.work_test_state or '-'} "
                    f"review={state.review_status or '-'}/"
                    f"{state.review_verdict or '-'} "
                    f"iter={state.work_review_iter} "
                    f"merge={state.merge_status or '-'}"
                    # #1526: print the merge gate's OWN reason right next to
                    # its status — this is the line the 2026-07-27/28 stalls
                    # never carried, so a "test=passed" operator watching the
                    # pane had no way to see `coord merge` disagreeing until
                    # it had already burned the whole retry budget.
                    + (f" ({state.merge_reason})" if state.merge_reason else "")
                    + f" active={state.active_count}"
                )
            elif now - last_change > self.opts.stall_secs and (
                last_nudge is None or now - last_nudge > self.opts.stall_secs
            ):
                # #1593: re-nudge on a `stall_secs` cadence for as long as the
                # fingerprint stays put, instead of firing once and going
                # silent. `last_change` is deliberately left untouched here —
                # only `last_nudge` advances — so the elapsed time below (and
                # `--stall`'s own monotonicity: smaller stall never means
                # FEWER nudges) keeps reflecting genuine staleness rather than
                # resetting every time this branch fires.
                self.warn(
                    f"no state change in {(now - last_change) / 60.0:g}m "
                    f"({','.join(state.active_types) or 'nothing'} active)"
                )
                self.run_notify()
                last_nudge = now

            action = decide(
                state, self.opts, counters, self.verifier,
                machine=machine, oracle=oracle, gate_checker=self.oracle_gate,
            )
            for warning in action.warnings:
                self.warn(warning)

            if action.is_exit:
                # #1499: capture the exit reason for the audit boundary before
                # anything below can fail — the escalation write is explicitly
                # best-effort, so it must not be able to cost us the reason.
                self._last_exit_message = action.message
                # #1505: an escalation exit still carries a `command` — the
                # `coord escalate record ...` write that makes the stop
                # reason board-visible after this process is gone. Run it
                # HERE (the I/O shell), not inside `decide()`, which stays a
                # pure function like every other decision in this module.
                # Best-effort: a failed write must never block the exit
                # itself (there is nothing left to retry), so this only
                # warns, never raises.
                if action.command:
                    rc = self.run_coord(action.command)
                    if rc != 0:
                        self.warn(
                            action.error_message
                            or f"coord {' '.join(action.command)} exited {rc}"
                        )
                # #1526: an escalation's reason must reach the issue itself,
                # not just this tmux pane (gone the moment the session ends)
                # and the `coord escalate` board row (invisible unless an
                # operator thinks to run `coord escalate list`). This is what
                # turned "drive died without closing the issue" into three
                # unexplained deaths during the 2026-07-27/28 overnight run.
                if action.exit_code == EXIT_ESCALATED:
                    self._post_escalation_comment(state, action.message)
                if action.exit_code == EXIT_OK:
                    for line in action.message.splitlines():
                        self.log(line)
                else:
                    for line in action.message.splitlines():
                        self.warn(line)
                return action.exit_code

            if action.label:
                self.log(action.label)

            if action.kind == RUN:
                rc = self.run_coord(
                    action.command, serialize_merge=action.serialize_merge
                )
                if rc != 0:
                    msg = action.error_message or (
                        f"coord {' '.join(action.command)} exited {rc}"
                    )
                    if action.on_error == "warn":
                        self.warn(msg)
                    else:
                        raise DriveError(msg, EXIT_TERMINAL_FAILURE)

            self.sleeper(
                self.opts.poll if action.sleep_after is None else action.sleep_after
            )
