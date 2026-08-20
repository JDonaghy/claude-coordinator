"""Independent, out-of-band confirmation of a Test-stage PASS claim (#2464).

THE DEFECT
----------
The Test stage's verdict was, end to end, *the worker's own report about its
own work*. Both channels are self-reports:

* the printed marker line — `SMOKE: pass` — which :mod:`coord.smoke`'s system
  prompt explicitly elevates above the exit code ("THE VERDICT IS THE LINE YOU
  PRINT, NOT YOUR EXIT CODE", #2244), because for a `claude -p` worker the exit
  code genuinely is not a signal: the session exits 0 whenever it ends normally
  no matter what the suite did;
* the worker calling ``coord test --passed <parent>`` itself (#2217), which
  :func:`coord.notify._record_smoke_verdict` treats as *authoritative* and
  refuses to clobber.

Neither involves anybody observing a test run. #2096 calls this **shape 1,
unconfirmed success**: the pipeline records the outcome of a *claim*, not of an
observation. It has already fired for real — assignment ``8de33c80fcd0`` ran the
full suite, hit 5 real failures, printed ``SMOKE: fail``, and was recorded
``test_state=passed`` anyway; CI found the identical five and blocked the merge
(#2230). #2244 fixed that *specific* accident by parsing the marker, but the
mechanism underneath stayed "trust text the LLM chose to print" — and the
recurring warnings in :mod:`coord.smoke` about partial and backgrounded runs
(#2244/#2272/#2301) are that same shape recurring with no adversarial intent at
all.

THE FIX
-------
:func:`confirm_branch` re-runs the repo's own ``build_command`` /
``test_command`` in a throwaway worktree at ``origin/<branch>`` and reads the
**real exit code**. No LLM, no worker, no tokens — the same mechanical
primitive :mod:`coord.revalidate` already uses for ``coord merge
--revalidate``, which is why this module reuses that module's helpers wholesale
rather than growing a second copy of them. The difference is *where it is
wired*: ``--revalidate`` is opt-in and only ever re-confirms an
**already-passed** verdict at merge time, so it never guarded the initial Test
verdict. This runs before ``test_state`` is recorded at all.

PROVIDER-AGNOSTIC BY CONSTRUCTION
---------------------------------
This runs in the reap path, *outside* whichever provider's process produced the
claim. That is deliberate and is the whole reason it is wired here rather than
in a provider: neither the Claude session's completion signal nor opencode's own
structured verdict (:mod:`coord.providers.opencode` — a stronger signal than a
free-text marker line, but still fundamentally the worker grading itself) is
independently trustworthy. One check placed after both covers both, and covers
any provider added later, with no per-backend logic.

WHAT IT CONFIRMS, AND WHAT IT DELIBERATELY DOES NOT
---------------------------------------------------
**Only PASS claims.** A ``fail`` verdict is already fail-closed, and ``blocked``
/ mute-leg rows already park without merging; spending a full suite run to
confirm bad news costs minutes and changes no gate. The laundering direction —
a *pass* that was never earned — is the one that reaches `main`.

**Not baseline-red.** A ``SMOKE: baseline-red`` claim records ``skipped``, which
does satisfy the merge gate, so it is the same shape in principle. Confirming it
requires running the suite on the merge-base too (what
``scripts/coord-test-runner.sh`` does), which is a second run and a larger
change than #2464 scopes. Called out here so it is a known, deliberate gap
rather than an oversight.

FAIL DIRECTION — THE PART THAT MATTERS
--------------------------------------
This may only ever *strengthen* the gate. It can turn an unearned ``passed``
into ``failed``; it must never turn a machine that simply cannot run the suite
into a wall of false failures. So a refutation requires the strongest possible
evidence — **a build/test command that ran to completion and returned nonzero**
(:data:`REFUTING_KINDS`).

Everything else is *inconclusive*, and inconclusive falls back to the worker's
claim, exactly reproducing pre-#2464 behaviour with a note in ``test_reason``
saying so:

* no local checkout of that repo on this machine, no ``test_command``
  configured, a failed fetch, a branch that is not on the remote
  (:data:`~coord.revalidate.KIND_SETUP`) — the check never started;
* a missing toolchain (:data:`~coord.revalidate.KIND_INFRA`) — #1814's case,
  where `cargo` was absent from the daemon's PATH and a green branch read as a
  red suite. Reusing :func:`~coord.revalidate.is_infrastructure_failure` means
  that lesson is not re-learned here;
* a timeout (:data:`~coord.revalidate.KIND_TIMEOUT`) — a hung or merely slow
  suite says nothing about the branch. Classifying a timeout as a refutation
  would let a too-tight ceiling fail every branch in the fleet, so it does not.

That asymmetry is the safety property: the worst case of a broken confirmation
environment is that the Test stage behaves exactly as it did before this module
existed.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

# Deliberate intra-package reuse of `revalidate`'s mechanical helpers, including
# the private ones. #2464's whole premise is that the out-of-band primitive
# already exists and is merely wired to the wrong place — copying `_run` /
# `_shell_runner` / `_remove_worktree` / `_tail` into a second module would fork
# the very behaviour (the #1924 daemon-env stripping, the #561 worktree
# discipline, the #1814 infra classification) that makes it trustworthy.
from coord.revalidate import (
    KIND_BASELINE_RED,
    KIND_BUILD,
    KIND_INFRA,
    KIND_OK,
    KIND_SETUP,
    KIND_SUITE,
    KIND_TIMEOUT,
    _Echo,
    _remove_worktree,
    _run,
    _shell_runner,
    _tail,
    is_baseline_red_failure,
    is_infrastructure_failure,
    local_repo_dir,
)

#: Ceiling on one confirmation run. Deliberately tighter than
#: :data:`coord.revalidate.DEFAULT_TIMEOUT_SECONDS` (30 min): that one bounds an
#: operator-initiated merge, this one runs in the reap path where a wedged suite
#: would stall every subsequent notification behind it. Safe to keep tight
#: precisely because a timeout is INCONCLUSIVE, not a refutation — the cost of
#: it firing early is one wasted run and a fall back to the worker's claim, not
#: a falsely-failed branch.
CONFIRM_DEFAULT_TIMEOUT_SECONDS = 60 * 20

#: Operator escape hatch. Set to a falsey value to restore exactly the
#: pre-#2464 behaviour (trust the worker's claim). Exists because this runs
#: unconditionally in the reap path and an operator on a machine that cannot run
#: a repo's suite needs an off-switch that does not require a config edit —
#: though they should not usually need one, since that machine's confirmations
#: come back :data:`~coord.revalidate.KIND_SETUP` (inconclusive) anyway.
DISABLE_ENV_VAR = "COORD_CONFIRM_TEST_VERDICT"

_FALSEY = frozenset({"0", "false", "no", "off", ""})
_TRUTHY = frozenset({"1", "true", "yes", "on"})

#: The only kinds that may overturn a PASS claim: a build or test command that
#: ran to completion and returned nonzero. Nothing weaker. See the module
#: docstring's fail-direction section — this frozenset IS that safety property.
REFUTING_KINDS = frozenset({KIND_BUILD, KIND_SUITE})

#: Kinds meaning "the check could not reach a verdict". The caller falls back to
#: the worker's own claim on any of these, which is pre-#2464 behaviour.
INCONCLUSIVE_KINDS = frozenset({KIND_SETUP, KIND_INFRA, KIND_TIMEOUT})


@dataclass
class ConfirmationResult:
    """Outcome of one out-of-band confirmation run.

    Exactly one of :attr:`confirmed` / :attr:`refuted` / :attr:`baseline_red` /
    :attr:`inconclusive` is true, so a caller can branch on them exhaustively
    without an else-fallthrough that silently means "passed".
    """

    kind: str
    reason: str = ""
    output: str = ""
    command: str = ""
    returncode: int | None = None
    worktree: Path | None = None

    @property
    def confirmed(self) -> bool:
        """The repo's own build+test really did pass at this branch."""
        return self.kind == KIND_OK

    @property
    def refuted(self) -> bool:
        """A command ran to completion and returned nonzero — the claim is wrong."""
        return self.kind in REFUTING_KINDS

    @property
    def baseline_red(self) -> bool:
        """The suite failed identically on the merge-base (#2170)."""
        return self.kind == KIND_BASELINE_RED

    @property
    def inconclusive(self) -> bool:
        """The check could not reach a verdict — fall back to the claim."""
        return self.kind in INCONCLUSIVE_KINDS


def confirmation_enabled(config=None) -> bool:
    """Whether Test-stage PASS claims get independently confirmed.

    Default **on** — #2464 specifies the promoted check runs unconditionally,
    and a gate that has to be switched on is the posture that let the original
    defect ship. :data:`DISABLE_ENV_VAR` wins over config so an operator can
    turn it off on one host without editing the shared ``coordinator.yml``.

    *config* is duck-typed (``getattr`` throughout) for the same reason
    :func:`coord.revalidate.revalidate` is: the daemon and the tests pass
    lighter stand-ins than the real ``Config``, and a hard attribute read would
    turn a missing shim into an ``AttributeError`` mid-reap.
    """
    raw = os.environ.get(DISABLE_ENV_VAR)
    if raw is not None:
        value = raw.strip().lower()
        if value in _FALSEY:
            return False
        if value in _TRUTHY:
            return True
    pipeline = getattr(config, "pipeline", None)
    flag = getattr(pipeline, "confirm_test_verdict", None)
    if flag is None:
        return True
    return bool(flag)


def confirm_worktree_path(repo_name: str, branch: str) -> Path:
    """Throwaway worktree for one confirmation run.

    Under ``~/.coord/confirm-worktrees/`` — OUTSIDE the base checkout, for the
    #561 reason :func:`coord.revalidate.revalidation_worktree_path` documents:
    on the daemon host the base checkout doubles as the live editable
    coordinator source, so moving its branch silently downgrades the running
    `coord`. Keyed by (repo, branch) so a re-run reuses and overwrites the same
    path rather than accumulating trees per assignment.
    """
    from coord.state import COORD_DIR  # noqa: PLC0415

    safe = f"{repo_name}-{branch}".replace("/", "-")
    return COORD_DIR / "confirm-worktrees" / safe


def _classify_failure(
    stage: str, returncode: int, output: str, worktree: Path, command: str,
) -> ConfirmationResult:
    """Turn a nonzero build/test exit into the right kind.

    Order matters and mirrors :func:`coord.revalidate.revalidate`: infra first
    (the suite never ran), then baseline-red (it ran, but the branch is not at
    fault), and only what is left over is a genuine refutation.
    """
    infra = is_infrastructure_failure(returncode, output)
    baseline = not infra and is_baseline_red_failure(returncode, output)
    if infra:
        return ConfirmationResult(
            kind=KIND_INFRA,
            reason=(
                f"confirmation could not run the {stage} command (exit "
                f"{returncode}): the toolchain is missing on this machine, so "
                "the suite never executed and NOTHING was learned about the "
                "branch — falling back to the worker's own claim (#1814)"
            ),
            output=_tail(output),
            command=command,
            returncode=returncode,
            worktree=worktree,
        )
    if baseline:
        return ConfirmationResult(
            kind=KIND_BASELINE_RED,
            reason=(
                f"confirmation ran the {stage} command and it failed (exit "
                f"{returncode}), but every failure reproduces on the merge-base "
                "— the baseline is red, so the branch made nothing worse "
                "(#2170)"
            ),
            output=_tail(output),
            command=command,
            returncode=returncode,
            worktree=worktree,
        )
    return ConfirmationResult(
        kind=KIND_BUILD if stage == "build" else KIND_SUITE,
        reason=(
            f"the independently-run {stage} command FAILED (exit {returncode}) "
            "at this branch — the Test-stage worker's pass claim is not "
            "supported by an actual run (#2464)"
        ),
        output=_tail(output),
        command=command,
        returncode=returncode,
        worktree=worktree,
    )


def confirm_branch(
    repo_name: str,
    branch: str | None,
    config,
    *,
    timeout: int = CONFIRM_DEFAULT_TIMEOUT_SECONDS,
    runner=None,
    echo=None,
) -> ConfirmationResult:
    """Run the repo's real build+test at ``origin/<branch>`` and report the truth.

    Writes **nothing** — no verdict, no board state, no GitHub. It answers one
    question ("does this branch actually build and pass?") and hands the answer
    back for the caller to act on. That separation is what makes it safe to call
    from the reap path and trivial to test.

    Which command: ``ci_command`` when the repo declares one, else
    ``test_command`` — identical to :func:`coord.revalidate.revalidate`'s #2091
    choice, and for the same reason. A verdict is only worth what the suite
    behind it is worth, so when a repo has said what CI runs, confirm with that.

    *runner* is the testing seam, same shape as
    :func:`coord.revalidate._shell_runner`: ``runner(command, cwd, timeout)``
    returning something with ``returncode`` / ``stdout`` / ``stderr``.
    """
    echo = echo or _Echo()

    if not branch:
        return ConfirmationResult(
            kind=KIND_SETUP,
            reason=(
                "no branch recorded for the Test-stage assignment, so there is "
                "nothing to check out and confirm"
            ),
        )

    repo_cfg = config.repo(repo_name) if config is not None else None
    if repo_cfg is None:
        return ConfirmationResult(
            kind=KIND_SETUP, reason=f"no repo config for {repo_name!r}",
        )

    test_command = (
        getattr(repo_cfg, "ci_command", None) or ""
    ).strip() or getattr(repo_cfg, "test_command", None)
    if not test_command:
        return ConfirmationResult(
            kind=KIND_SETUP,
            reason=(
                f"no test_command configured for {repo_name!r} — there is no "
                "suite to confirm against"
            ),
        )

    repo_dir = local_repo_dir(config, repo_name)
    if repo_dir is None or not repo_dir.exists():
        return ConfirmationResult(
            kind=KIND_SETUP,
            reason=(
                f"no local checkout for {repo_name!r} on this machine "
                f"({repo_dir or 'no repo_path configured'}) — confirmation runs "
                "the suite locally, so it can only run where the repo lives"
            ),
        )

    wt_path = confirm_worktree_path(repo_name, branch)
    _remove_worktree(repo_dir, wt_path)
    wt_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        fetched = _run(["git", "fetch", "origin", "--prune"], cwd=repo_dir)
    except (subprocess.SubprocessError, OSError) as exc:
        return ConfirmationResult(
            kind=KIND_SETUP, reason=f"git fetch failed: {exc}",
        )
    if fetched.returncode != 0:
        return ConfirmationResult(
            kind=KIND_SETUP,
            reason=f"git fetch failed: {(fetched.stderr or '').strip()}",
        )

    added = _run(
        ["git", "worktree", "add", "--force", "--detach",
         str(wt_path), f"origin/{branch}"],
        cwd=repo_dir,
    )
    if added.returncode != 0:
        # Most often: the worker never pushed, so `origin/<branch>` does not
        # exist. Inconclusive rather than a refutation — "we could not find the
        # branch" is not "the branch is red". The pushed-nothing case has its
        # own detector (`push_failure_reason`, #1797).
        return ConfirmationResult(
            kind=KIND_SETUP,
            reason=(
                f"could not check out origin/{branch} for confirmation: "
                f"{(added.stderr or '').strip()}"
            ),
        )

    run_cmd = runner or _shell_runner

    build_command = getattr(repo_cfg, "build_command", None)
    if build_command:
        echo(f"    confirming build: {build_command}")
        try:
            built = run_cmd(build_command, wt_path, timeout)
        except subprocess.TimeoutExpired:
            return ConfirmationResult(
                kind=KIND_TIMEOUT,
                reason=(
                    f"confirmation build timed out after {timeout}s — a suite "
                    "that did not finish says nothing about the branch"
                ),
                command=build_command,
                worktree=wt_path,
            )
        if built.returncode != 0:
            return _classify_failure(
                "build",
                built.returncode,
                (built.stdout or "") + "\n" + (built.stderr or ""),
                wt_path,
                build_command,
            )

    echo(f"    confirming tests: {test_command}")
    try:
        tested = run_cmd(test_command, wt_path, timeout)
    except subprocess.TimeoutExpired:
        return ConfirmationResult(
            kind=KIND_TIMEOUT,
            reason=(
                f"confirmation suite timed out after {timeout}s — a suite that "
                "did not finish says nothing about the branch"
            ),
            command=test_command,
            worktree=wt_path,
        )
    if tested.returncode != 0:
        return _classify_failure(
            "suite",
            tested.returncode,
            (tested.stdout or "") + "\n" + (tested.stderr or ""),
            wt_path,
            test_command,
        )

    # Green, and observed rather than reported. Clean up: nothing to inspect.
    _remove_worktree(repo_dir, wt_path)
    return ConfirmationResult(
        kind=KIND_OK,
        reason=(
            f"independently re-ran `{test_command}` at origin/{branch} and it "
            "passed"
        ),
        command=test_command,
        returncode=0,
    )


__all__ = [
    "CONFIRM_DEFAULT_TIMEOUT_SECONDS",
    "DISABLE_ENV_VAR",
    "INCONCLUSIVE_KINDS",
    "REFUTING_KINDS",
    "ConfirmationResult",
    "confirm_branch",
    "confirm_worktree_path",
    "confirmation_enabled",
]
