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

# ── exit codes (unchanged from drive-issue.sh) ───────────────────────────────

EXIT_OK = 0
EXIT_TERMINAL_FAILURE = 1
EXIT_USAGE = 2
EXIT_DEADLINE = 3


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

    # "" / running / done: still landing through Test → Review → Merge —
    # coord's own tick loop drives that, exactly like a normal work row;
    # this only observes (same posture as every other gate in this module).
    return _wait(label=f"ACCEPTANCE: JIT slice {aid} authoring/merging in progress")


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
        if state.repo_github and shutil.which("gh"):
            proc = subprocess.run(
                [
                    "gh", "pr", "view", branch,
                    "--repo", state.repo_github,
                    "--json", "state", "-q", ".state",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            pr_state = (proc.stdout or "").strip() if proc.returncode == 0 else ""
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


def preflight(state: IssueState, opts: DriveOptions) -> Preflight:
    """Resolve the machine and refuse the runs that can never win.

    Raises :class:`DriveError` for a configuration problem or for interactive
    work with no review (see below).
    """
    machine = opts.machine or state.picked_machine
    if not machine:
        raise DriveError(
            f"no unpaused machine hosts {state.repo} — pass --machine",
            EXIT_USAGE,
        )

    warnings: list[str] = []
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

    # ---- work failed: bounded retry ----------------------------------------
    if state.work_status == "failed":
        if counters.work_retries >= opts.max_work_retries:
            return _die(
                f"work {state.work_aid} failed {counters.work_retries} retr(ies) in: "
                f"{state.work_failure_reason or 'no reason recorded'}\n"
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

    if opts.do_plan:
        if not state.plan_aid:
            args = ["assign", "--plan-only", machine, state.repo, str(state.issue)]
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

    args = ["assign", machine, state.repo, str(state.issue)]
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


def _decide_merge(
    state: IssueState, opts: DriveOptions, counters: DriveCounters
) -> Action:
    """The MERGE stage."""
    status = state.merge_status
    if status.upper() == "HUMAN_REQUIRED":
        return _die(
            f"merge entry is HUMAN_REQUIRED: {state.merge_reason or 'no reason recorded'}\n"
            "   An automated conflict-fix already gave up. Resolve by hand, or "
            "override:\n"
            f"     coord merge --only {state.merge_aid or state.work_aid} "
            "--override-human-required '<reason>'"
        )
    if status.upper() == "CONFLICT":
        # coord auto-dispatches a conflict-fix worker and re-enqueues on
        # success; give it room rather than fighting it.
        return _wait(label="MERGE: conflict — waiting for coord's conflict-fix worker")
    if status.upper() == "BLOCKED":
        return _wait(
            label=(
                "MERGE: blocked — "
                f"{state.merge_reason or 'gate not satisfied'}; re-checking"
            )
        )

    # Cap the attempts: without this, a merge that fails for a reason the board
    # never reflects (so merge_status stays empty) would re-run `coord merge`
    # on every poll until the deadline.
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
    # the attempt cap above is what bounds it.
    return Action(
        kind=RUN,
        label=(
            f"MERGE: attempt {counters.merge_attempts}/{opts.max_merge_attempts} "
            f"(coord merge --only {aid} --method {opts.merge_method})"
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

    _run_log: Path | None = field(default=None, init=False, repr=False)

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
        try:
            return self._loop()
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

        pre = preflight(state, self.opts)
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
        nudged = False

        while True:
            now = self.clock()
            if now > deadline:
                self.warn(f"deadline of {self.opts.deadline_mins:g}m exceeded")
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
                nudged = False
                self.log(
                    f"state: work={state.work_status or '-'} "
                    f"test={state.work_test_state or '-'} "
                    f"review={state.review_status or '-'}/"
                    f"{state.review_verdict or '-'} "
                    f"iter={state.work_review_iter} "
                    f"merge={state.merge_status or '-'} "
                    f"active={state.active_count}"
                )
            elif now - last_change > self.opts.stall_secs and not nudged:
                self.warn(
                    f"no state change in {self.opts.stall_mins:g}m "
                    f"({','.join(state.active_types) or 'nothing'} active)"
                )
                self.run_notify()
                nudged = True
                last_change = now

            action = decide(
                state, self.opts, counters, self.verifier,
                machine=machine, oracle=oracle, gate_checker=self.oracle_gate,
            )
            for warning in action.warnings:
                self.warn(warning)

            if action.is_exit:
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
