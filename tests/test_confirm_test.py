"""#2464: a Test-stage PASS must be an observation, not the worker's self-report.

Both of the Test stage's verdict channels were the worker grading its own work:
the `SMOKE: pass` line it chose to print (#2244 elevated that line ABOVE the
exit code, correctly — `claude -p` exits 0 no matter what the suite did), and
the worker calling `coord test --passed <parent>` on itself (#2217, which
`_record_smoke_verdict` then treated as authoritative). #2096 calls this shape 1,
*unconfirmed success*: the pipeline records the outcome of a claim.

It has already fired for real. Assignment `8de33c80fcd0` ran the suite, hit 5
real failures, printed `SMOKE: fail`, and was recorded `test_state=passed`; CI
found the identical five and blocked the merge (#2230).

The headline test here is the one #2464 names as its "done" criterion, and it
fails against the pre-fix code:

    test_smoke_pass_marker_with_failing_real_run_is_not_recorded_passed

Everything else exists to pin the *fail direction*, which is the part that could
do damage if it were wrong. This gate may only ever strengthen: it can turn an
unearned `passed` into `failed`, but a machine that merely *cannot run* the
suite — no checkout, no toolchain, a timeout — must fall back to the old
behaviour rather than fail every branch in the fleet.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import pytest

from coord import confirm_test as ct
from coord.revalidate import (
    KIND_BASELINE_RED,
    KIND_BUILD,
    KIND_INFRA,
    KIND_OK,
    KIND_SETUP,
    KIND_SUITE,
    KIND_TIMEOUT,
)

BRANCH = "issue-42-fix-thing"


# ── stubs ────────────────────────────────────────────────────────────────────


@dataclass
class _StubRepo:
    name: str = "api"
    github: str = "acme/api"
    test_command: str | None = "run-the-suite"
    build_command: str | None = None
    ci_command: str | None = None


class _StubMachine:
    def __init__(self, path: str | None) -> None:
        self.name = "testbox"
        self.host = "testbox.tailnet"
        self._path = path

    def repo_path(self, repo_name: str) -> str | None:
        return self._path


class _StubPipeline:
    def __init__(self, confirm_test_verdict: bool = True) -> None:
        self.confirm_test_verdict = confirm_test_verdict


class _StubConfig:
    def __init__(
        self,
        repo: _StubRepo | None = None,
        repo_path: str | None = None,
        confirm_test_verdict: bool = True,
    ) -> None:
        self._repo = repo if repo is not None else _StubRepo()
        self.machines = [_StubMachine(repo_path)]
        self.pipeline = _StubPipeline(confirm_test_verdict)

    def repo(self, name: str):
        return self._repo if self._repo and name == self._repo.name else None


@dataclass
class _FakeProc:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


class _ScriptedRunner:
    """Stands in for `revalidate._shell_runner`: `(command, cwd, timeout)`.

    Lets a test say "the build passes but the suite exits 1" without actually
    owning a red suite, and records every call so a test can assert a
    confirmation did NOT run.
    """

    def __init__(self, results: dict | None = None, default: object = None) -> None:
        self.results = results or {}
        self.default = default if default is not None else _FakeProc(0)
        self.calls: list[tuple[str, Path, int]] = []
        #: What was actually on disk when the command ran. Captured here
        #: because a green confirmation deletes its worktree on the way out,
        #: so a test cannot inspect it afterwards.
        self.trees: list[set[str]] = []

    def __call__(self, command: str, cwd, timeout: int):
        self.calls.append((command, Path(cwd), timeout))
        self.trees.append({p.name for p in Path(cwd).iterdir()})
        outcome = self.results.get(command, self.default)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


# ── real-git fixtures ────────────────────────────────────────────────────────


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
    )


@pytest.fixture(autouse=True)
def isolated_coord_dir(tmp_path: Path, monkeypatch) -> Path:
    """`confirm_branch` builds a throwaway worktree under ``COORD_DIR``.

    Pin it into the test's tmp dir so a test run never writes into the real
    ``~/.coord/`` — same discipline as `tests/test_revalidate.py`.
    """
    d = tmp_path / "coord-state"
    d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("coord.state.COORD_DIR", d)
    return d


@pytest.fixture(autouse=True)
def _no_env_override(monkeypatch):
    """The escape hatch must not leak in from the developer's shell."""
    monkeypatch.delenv(ct.DISABLE_ENV_VAR, raising=False)


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """A real clone with a real `origin` carrying a real branch.

    `confirm_branch` genuinely fetches and genuinely creates a git worktree —
    only the build/test command itself is faked (via *runner*). Stubbing git
    too would leave the part most likely to break untested.
    """
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(origin)],
        capture_output=True, text=True, check=True,
    )

    seed = tmp_path / "seed"
    subprocess.run(
        ["git", "clone", str(origin), str(seed)],
        capture_output=True, text=True, check=True,
    )
    _git(seed, "config", "user.email", "test@example.com")
    _git(seed, "config", "user.name", "Test")
    # The Test stage runs this suite on a capability-matched machine whose
    # global gitconfig we do not control, and #2269's arm deliberately runs it
    # against a POPULATED $HOME. A developer's `commit.gpgsign = true` (or this
    # repo's own `core.hooksPath = .githooks`, which does not exist in a temp
    # clone) would otherwise fail these commits for reasons having nothing to
    # do with what is under test.
    _git(seed, "config", "commit.gpgsign", "false")
    _git(seed, "config", "core.hooksPath", str(tmp_path / "no-hooks"))
    (seed / "README.md").write_text("base\n", encoding="utf-8")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", "base")
    _git(seed, "push", "origin", "main")
    _git(seed, "checkout", "-b", BRANCH)
    (seed / "feature.txt").write_text("the change under test\n", encoding="utf-8")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", "feature")
    _git(seed, "push", "origin", BRANCH)

    base = tmp_path / "base"
    subprocess.run(
        ["git", "clone", str(origin), str(base)],
        capture_output=True, text=True, check=True,
    )
    # `confirm_branch` runs `git worktree add` here; same hook isolation.
    _git(base, "config", "core.hooksPath", str(tmp_path / "no-hooks"))
    return base


# ── confirm_branch: the mechanical check ─────────────────────────────────────


class TestConfirmBranch:
    def test_green_suite_confirms_the_claim(self, checkout: Path) -> None:
        runner = _ScriptedRunner(default=_FakeProc(0))
        result = ct.confirm_branch(
            "api", BRANCH, _StubConfig(repo_path=str(checkout)), runner=runner,
        )

        assert result.kind == KIND_OK, result.reason
        assert result.confirmed is True
        assert result.refuted is False
        assert [c[0] for c in runner.calls] == ["run-the-suite"], (
            "the repo's own test_command should have been re-run out-of-band"
        )
        # It ran against a real checkout of the BRANCH, not of the base — the
        # whole point is to re-run what the worker claimed to have run.
        assert "feature.txt" in runner.trees[0], (
            "the confirmation worktree must contain the branch's own commit, "
            f"got {sorted(runner.trees[0])}"
        )

    def test_red_suite_refutes_the_claim(self, checkout: Path) -> None:
        """The core of #2464: a real nonzero exit overturns a pass claim."""
        runner = _ScriptedRunner(default=_FakeProc(1, stdout="5 failed"))
        result = ct.confirm_branch(
            "api", BRANCH, _StubConfig(repo_path=str(checkout)), runner=runner,
        )

        assert result.kind == KIND_SUITE, result.reason
        assert result.refuted is True
        assert result.confirmed is False
        assert result.returncode == 1

    def test_red_build_refutes_before_the_suite_runs(self, checkout: Path) -> None:
        repo = _StubRepo(build_command="build-it")
        runner = _ScriptedRunner(results={"build-it": _FakeProc(2, stderr="boom")})
        result = ct.confirm_branch(
            "api", BRANCH, _StubConfig(repo, repo_path=str(checkout)), runner=runner,
        )

        assert result.kind == KIND_BUILD, result.reason
        assert result.refuted is True
        assert [c[0] for c in runner.calls] == ["build-it"], (
            "a failing build must short-circuit before the suite is attempted"
        )

    def test_missing_toolchain_is_inconclusive_not_a_refutation(
        self, checkout: Path
    ) -> None:
        """#1814's lesson, re-pinned here.

        `cargo: command not found` inside the daemon read as a red suite for a
        branch CI had proven green. If that misclassification happened HERE it
        would mark real branches failed, so it must stay inconclusive.
        """
        runner = _ScriptedRunner(
            default=_FakeProc(127, stderr="run-the-suite: command not found")
        )
        result = ct.confirm_branch(
            "api", BRANCH, _StubConfig(repo_path=str(checkout)), runner=runner,
        )

        assert result.kind == KIND_INFRA, result.reason
        assert result.refuted is False, (
            "a missing toolchain says nothing about the branch — refuting on it "
            "would fail every branch on a misconfigured machine"
        )
        assert result.inconclusive is True

    def test_baseline_red_marker_is_not_a_refutation(self, checkout: Path) -> None:
        """#2170: red on the merge-base too ⇒ the branch made nothing worse."""
        runner = _ScriptedRunner(
            default=_FakeProc(4, stdout="RESULT: BASELINE-RED\nsame 3 failures")
        )
        result = ct.confirm_branch(
            "api", BRANCH, _StubConfig(repo_path=str(checkout)), runner=runner,
        )

        assert result.kind == KIND_BASELINE_RED, result.reason
        assert result.baseline_red is True
        assert result.refuted is False

    def test_timeout_is_inconclusive(self, checkout: Path) -> None:
        """A suite that did not finish says nothing about the branch.

        Classifying a timeout as a refutation would let one too-tight ceiling
        fail every branch in the fleet.
        """
        runner = _ScriptedRunner(
            default=subprocess.TimeoutExpired(cmd="run-the-suite", timeout=1)
        )
        result = ct.confirm_branch(
            "api", BRANCH, _StubConfig(repo_path=str(checkout)), runner=runner,
        )

        assert result.kind == KIND_TIMEOUT, result.reason
        assert result.refuted is False
        assert result.inconclusive is True

    def test_ci_command_wins_over_test_command(self, checkout: Path) -> None:
        """#2091: when a repo declares what CI runs, confirm with THAT."""
        repo = _StubRepo(ci_command="the-ci-suite")
        runner = _ScriptedRunner(default=_FakeProc(0))
        ct.confirm_branch(
            "api", BRANCH, _StubConfig(repo, repo_path=str(checkout)), runner=runner,
        )

        assert [c[0] for c in runner.calls] == ["the-ci-suite"]

    @pytest.mark.parametrize(
        "kwargs, why",
        [
            ({"branch": None}, "no branch recorded"),
            ({"repo": _StubRepo(test_command=None)}, "no test_command configured"),
            ({"repo_path": None}, "no local checkout on this machine"),
            ({"branch": "no-such-branch"}, "branch is not on the remote"),
        ],
    )
    def test_unrunnable_checks_are_inconclusive(
        self, checkout: Path, kwargs: dict, why: str
    ) -> None:
        """Every "could not even start" path is SETUP, never a refutation.

        This is the no-regression guarantee: on a machine where the check
        cannot apply, the Test stage must behave exactly as it did pre-#2464.
        """
        repo = kwargs.get("repo", _StubRepo())
        repo_path = kwargs.get("repo_path", str(checkout))
        if "repo_path" in kwargs and kwargs["repo_path"] is None:
            repo_path = None
        branch = kwargs.get("branch", BRANCH)

        result = ct.confirm_branch(
            "api", branch, _StubConfig(repo, repo_path=repo_path),
            runner=_ScriptedRunner(default=_FakeProc(0)),
        )

        assert result.kind == KIND_SETUP, f"{why}: got {result.kind} / {result.reason}"
        assert result.refuted is False
        assert result.inconclusive is True

    def test_unknown_repo_is_inconclusive(self, checkout: Path) -> None:
        result = ct.confirm_branch(
            "not-a-repo", BRANCH, _StubConfig(repo_path=str(checkout)),
        )
        assert result.kind == KIND_SETUP
        assert result.refuted is False

    def test_green_run_cleans_up_its_worktree(self, checkout: Path) -> None:
        runner = _ScriptedRunner(default=_FakeProc(0))
        ct.confirm_branch(
            "api", BRANCH, _StubConfig(repo_path=str(checkout)), runner=runner,
        )
        assert not ct.confirm_worktree_path("api", BRANCH).exists(), (
            "a green confirmation has nothing to inspect — it must not leave "
            "worktrees piling up in the reap path"
        )

    def test_refuted_run_keeps_its_worktree_for_inspection(
        self, checkout: Path
    ) -> None:
        result = ct.confirm_branch(
            "api", BRANCH, _StubConfig(repo_path=str(checkout)),
            runner=_ScriptedRunner(default=_FakeProc(1)),
        )
        assert result.worktree is not None
        assert result.worktree.exists()


# ── locking: two overlapping notify passes must not race on one worktree ──────
#
# #2464-review: `/notify`'s own lock (`coord/serve_app.py`'s `post_notify`)
# gives up after 120s and runs the whole drain UNLOCKED, and a confirmation can
# now legitimately run far longer than that. So two confirmations for the same
# (repo, branch) really can overlap, and `git worktree add --force --detach`
# does not protect against a second process reusing the identical path.
# `confirm_branch` must serialize on a per-(repo, branch) lock instead.


class TestConfirmBranchLocking:
    def test_lock_path_is_distinct_from_the_worktree_path(self) -> None:
        # Taking `flock` on a path `git worktree add` is about to create (and
        # a failed run may leave behind) is not a stable thing to lock
        # against.
        assert ct.confirm_lock_path("api", BRANCH) != ct.confirm_worktree_path(
            "api", BRANCH,
        )

    def test_a_held_lock_makes_a_second_confirmation_inconclusive_not_a_race(
        self, checkout: Path,
    ) -> None:
        """Simulate a second notify pass finding the confirm-worktree lock
        already held by an in-flight confirmation for the same branch. It
        must step aside — INCONCLUSIVE, never a refutation — and it must
        never touch the worktree lifecycle at all (no fetch, no checkout, no
        build/test command), which is what proves there is no race."""
        lock = ct.FileLock(ct.confirm_lock_path("api", BRANCH))
        lock.acquire(timeout=None)
        try:
            runner = _ScriptedRunner(default=_FakeProc(0))
            # timeout=0: nothing left in the shared deadline to wait for the
            # lock, so this must fail fast rather than block the test.
            result = ct.confirm_branch(
                "api", BRANCH, _StubConfig(repo_path=str(checkout)),
                runner=runner, timeout=0,
            )
        finally:
            lock.release()

        assert result.kind == KIND_SETUP
        assert result.inconclusive is True
        assert result.refuted is False
        assert "already in progress" in result.reason
        assert runner.calls == [], (
            "a confirmation that could not take the lock must never run the "
            "build/test command — that would be the exact race this lock "
            "exists to prevent"
        )

    def test_a_lock_held_for_a_different_branch_does_not_block_this_one(
        self, checkout: Path,
    ) -> None:
        other_lock = ct.FileLock(ct.confirm_lock_path("api", "some-other-branch"))
        other_lock.acquire(timeout=None)
        try:
            runner = _ScriptedRunner(default=_FakeProc(0))
            result = ct.confirm_branch(
                "api", BRANCH, _StubConfig(repo_path=str(checkout)), runner=runner,
            )
        finally:
            other_lock.release()

        assert result.confirmed

    def test_the_lock_is_released_after_a_run_so_a_later_confirmation_can_proceed(
        self, checkout: Path,
    ) -> None:
        """Guards against a leaked lock: every return path inside
        `confirm_branch` (success, refutation, inconclusive) must release the
        lock, or the very next confirmation of the same branch would hang."""
        config = _StubConfig(repo_path=str(checkout))

        first = ct.confirm_branch(
            "api", BRANCH, config, runner=_ScriptedRunner(default=_FakeProc(1)),
        )
        assert first.refuted

        second = ct.confirm_branch(
            "api", BRANCH, config, runner=_ScriptedRunner(default=_FakeProc(0)),
        )
        assert second.confirmed


# ── the switch ───────────────────────────────────────────────────────────────


class TestConfirmationEnabled:
    def test_defaults_on(self) -> None:
        """#2464 specifies unconditional. A gate you must switch on is the
        posture that let the defect ship."""
        assert ct.confirmation_enabled(None) is True
        assert ct.confirmation_enabled(_StubConfig()) is True

    def test_config_can_disable(self) -> None:
        assert ct.confirmation_enabled(
            _StubConfig(confirm_test_verdict=False)
        ) is False

    @pytest.mark.parametrize("raw", ["0", "false", "no", "off", ""])
    def test_env_can_disable(self, monkeypatch, raw: str) -> None:
        monkeypatch.setenv(ct.DISABLE_ENV_VAR, raw)
        assert ct.confirmation_enabled(_StubConfig()) is False

    @pytest.mark.parametrize("raw", ["1", "true", "yes", "on"])
    def test_env_overrides_a_disabling_config(self, monkeypatch, raw: str) -> None:
        monkeypatch.setenv(ct.DISABLE_ENV_VAR, raw)
        assert ct.confirmation_enabled(
            _StubConfig(confirm_test_verdict=False)
        ) is True

    def test_missing_pipeline_shim_defaults_on(self) -> None:
        """*config* is duck-typed — a lighter stand-in must not crash the reap."""
        assert ct.confirmation_enabled(object()) is True


# ── the wiring: end to end through post_transition ───────────────────────────


class TestSmokeVerdictIsConfirmed:
    """Drives the real reap path (`notify.post_transition`) and asserts on the
    persisted row, the same shape `tests/test_notify.py` uses for this surface.
    """

    def _record_work(self, assignment_id: str = "work-1") -> None:
        from coord.models import Assignment
        from coord.state import _record_dispatched_assignment_local

        work = Assignment(
            assignment_id=assignment_id, machine_name="laptop", repo_name="api",
            issue_number=42, issue_title="Fix thing", type="work",
            status="done", branch=BRANCH,
        )
        _record_dispatched_assignment_local(assignment=work, repo_github="acme/api")

    def _record_smoke(self, smoke_id: str = "smoke-1", *, parent_id: str = "work-1") -> None:
        from coord.models import Assignment
        from coord.state import _record_dispatched_assignment_local

        smoke = Assignment(
            assignment_id=smoke_id, machine_name="laptop", repo_name="api",
            issue_number=42, issue_title="[smoke] Fix thing", type="smoke",
            status="running", review_of_assignment_id=parent_id,
            branch=BRANCH,
        )
        _record_dispatched_assignment_local(assignment=smoke, repo_github="acme/api")

    def _transition(self, tmp_path: Path, marker: str | None):
        from coord.notify import EVENT_COMPLETION, Transition

        transition = Transition(
            assignment_id="smoke-1", machine_name="laptop", repo_name="api",
            issue_number=42, event=EVENT_COMPLETION, exit_code=0,
        )
        record = {"repo_github": "acme/api", "type": "smoke",
                  "review_of_assignment_id": "work-1"}
        entry = {"started_at": 1000.0, "finished_at": 1010.0,
                 "branch": BRANCH, "log_path": None}
        if marker is not None:
            log_path = tmp_path / "smoke-1.log"
            log_path.write_text(
                f"9911 passed, 18 skipped in 662.70s\n{marker}\n", encoding="utf-8",
            )
            entry["log_path"] = str(log_path)
        return transition, record, entry

    def _reap(self, transition, record, entry, confirmation):
        """Run the transition with the confirmation scripted to *confirmation*.

        Returns the mock so a test can assert whether it was consulted at all.
        """
        from coord.notify import post_transition

        with (
            patch("coord.notify.post_completion"),
            patch("coord.notify.mark_notified"),
            patch("coord.notify._capture_cost"),
            patch("coord.notify._capture_smoke_tests"),
            patch("coord.notify._capture_completion_summary"),
            patch("coord.notify._capture_claude_session_id"),
            patch("coord.notify._agent_host", return_value=None),
            patch("coord.config.load", return_value=_StubConfig()),
            patch(
                "coord.confirm_test.confirm_branch", return_value=confirmation,
            ) as confirm,
        ):
            post_transition(transition, record, entry)
        return confirm

    def _row(self) -> dict:
        from coord.state import get_connection

        row = get_connection().execute(
            "SELECT test_state, smoke_test, test_reason FROM assignments "
            "WHERE assignment_id=?",
            ("work-1",),
        ).fetchone()
        assert row is not None, "the work assignment must exist"
        return row

    # ── the headline: #2464's stated "done" criterion ────────────────────────

    def test_smoke_pass_marker_with_failing_real_run_is_not_recorded_passed(
        self, coord_db, tmp_path: Path
    ) -> None:
        """`SMOKE: pass` + an independent run that exits nonzero ⇒ NOT passed.

        This is the exact replay #2464 asks for, and it fails against the
        pre-fix code, which recorded `passed` on the strength of the printed
        line alone.
        """
        self._record_work()
        self._record_smoke()
        transition, record, entry = self._transition(tmp_path, "SMOKE: pass")

        confirm = self._reap(
            transition, record, entry,
            ct.ConfirmationResult(
                kind=KIND_SUITE,
                reason="the independently-run suite command FAILED (exit 1)",
                returncode=1,
            ),
        )

        confirm.assert_called_once()
        row = self._row()
        assert row["test_state"] != "passed", (
            "a pass claim contradicted by a real run must never be recorded as "
            "passed — that is the laundering path #2464 closes"
        )
        assert row["test_state"] == "failed", (
            f"expected test_state='failed', got {row['test_state']!r}"
        )
        assert "REFUTED" in (row["test_reason"] or ""), (
            "the row must say WHY it was overturned, so an operator is not left "
            f"guessing: {row['test_reason']!r}"
        )

    def test_self_recorded_pass_is_overturned_by_a_failing_real_run(
        self, coord_db, tmp_path: Path
    ) -> None:
        """The #2217 channel is the same defect, and the more common one.

        `build_smoke_briefing` tells every smoke worker to call
        `coord test --passed <parent>` itself. If only the marker path were
        guarded, the ordinary case would sail straight through unchecked.
        """
        from coord.state import record_test_verdict

        self._record_work()
        self._record_smoke()
        record_test_verdict(assignment_id="work-1", test_state="passed")

        transition, record, entry = self._transition(tmp_path, None)
        confirm = self._reap(
            transition, record, entry,
            ct.ConfirmationResult(kind=KIND_SUITE, reason="suite exited 1", returncode=1),
        )

        confirm.assert_called_once()
        row = self._row()
        assert row["test_state"] == "failed", (
            "a worker's self-recorded pass is authoritative against everything "
            "EXCEPT a contradicting run; got "
            f"{row['test_state']!r}"
        )

    def test_self_recorded_pass_confirmed_by_a_real_run_is_annotated(
        self, coord_db, tmp_path: Path
    ) -> None:
        """#2464-review: a self-recorded pass genuinely confirmed by a real
        run must have that outcome PERSISTED, not silently discarded because
        `test_state` didn't change. Before the fix this fell through to the
        generic "already authoritative — leaving it untouched" branch and
        `record_test_verdict` was never even called, so the confirmation's own
        reason never reached the row — indistinguishable from a confirmation
        that never ran at all.
        """
        from coord.state import record_test_verdict

        self._record_work()
        self._record_smoke()
        record_test_verdict(assignment_id="work-1", test_state="passed")

        transition, record, entry = self._transition(tmp_path, None)
        confirm = self._reap(
            transition, record, entry,
            ct.ConfirmationResult(kind=KIND_OK, reason="re-ran the suite and it passed"),
        )

        confirm.assert_called_once()
        row = self._row()
        assert row["test_state"] == "passed"
        assert "confirmed" in (row["test_reason"] or "").lower(), (
            "a confirmed self-recorded pass must say so in test_reason rather "
            f"than leaving whatever text (or nothing) was there before: "
            f"{row['test_reason']!r}"
        )

    def test_self_recorded_pass_inconclusive_confirmation_is_annotated(
        self, coord_db, tmp_path: Path
    ) -> None:
        """Same gap as above, on the INCONCLUSIVE arm: the row must say
        UNCONFIRMED, not silently keep stale (or no) reason text."""
        from coord.state import record_test_verdict

        self._record_work()
        self._record_smoke()
        record_test_verdict(assignment_id="work-1", test_state="passed")

        transition, record, entry = self._transition(tmp_path, None)
        confirm = self._reap(
            transition, record, entry,
            ct.ConfirmationResult(
                kind=KIND_SETUP, reason="no local checkout for 'api' on this machine",
            ),
        )

        confirm.assert_called_once()
        row = self._row()
        assert row["test_state"] == "passed"
        assert "UNCONFIRMED" in (row["test_reason"] or ""), (
            "an inconclusive confirmation of a self-recorded pass must still "
            f"say nobody checked, not silently discard the attempt: "
            f"{row['test_reason']!r}"
        )

    def test_confirmed_pass_is_recorded_passed(
        self, coord_db, tmp_path: Path
    ) -> None:
        self._record_work()
        self._record_smoke()
        transition, record, entry = self._transition(tmp_path, "SMOKE: pass")

        self._reap(
            transition, record, entry,
            ct.ConfirmationResult(kind=KIND_OK, reason="re-ran the suite and it passed"),
        )

        row = self._row()
        assert row["test_state"] == "passed"
        assert row["smoke_test"] == "pass", "#1384's legacy mirror still derives"
        assert "confirmed" in (row["test_reason"] or "").lower()

    def test_inconclusive_confirmation_leaves_the_pass_intact(
        self, coord_db, tmp_path: Path
    ) -> None:
        """No-regression guarantee.

        On a machine that cannot run the repo's suite the stage must behave
        exactly as it did before #2464 — a wall of false failures here would be
        far worse than the defect being fixed.
        """
        self._record_work()
        self._record_smoke()
        transition, record, entry = self._transition(tmp_path, "SMOKE: pass")

        self._reap(
            transition, record, entry,
            ct.ConfirmationResult(
                kind=KIND_SETUP, reason="no local checkout for 'api' on this machine",
            ),
        )

        row = self._row()
        assert row["test_state"] == "passed", (
            "an inconclusive confirmation must fall back to the worker's claim"
        )
        assert "UNCONFIRMED" in (row["test_reason"] or ""), (
            "and the row must SAY it is unconfirmed rather than implying a "
            f"verdict nobody checked: {row['test_reason']!r}"
        )

    def test_baseline_red_confirmation_records_skipped(
        self, coord_db, tmp_path: Path
    ) -> None:
        """#2170's convention: not the branch's fault, so no fix round burns."""
        self._record_work()
        self._record_smoke()
        transition, record, entry = self._transition(tmp_path, "SMOKE: pass")

        self._reap(
            transition, record, entry,
            ct.ConfirmationResult(
                kind=KIND_BASELINE_RED, reason="every failure reproduces on the base",
            ),
        )

        row = self._row()
        assert row["test_state"] == "skipped", (
            f"expected 'skipped' for a red baseline, got {row['test_state']!r}"
        )

    def test_a_fail_marker_does_not_spend_a_confirmation_run(
        self, coord_db, tmp_path: Path
    ) -> None:
        """Only PASS claims are confirmed.

        `fail` is already fail-closed; re-running the suite to confirm bad news
        costs minutes of wall-clock in the reap loop and changes no gate.
        """
        self._record_work()
        self._record_smoke()
        transition, record, entry = self._transition(tmp_path, "SMOKE: fail 5 failures")

        confirm = self._reap(
            transition, record, entry,
            ct.ConfirmationResult(kind=KIND_OK, reason="unused"),
        )

        confirm.assert_not_called()
        assert self._row()["test_state"] == "failed"

    def test_confirmation_failure_does_not_break_the_reap(
        self, coord_db, tmp_path: Path
    ) -> None:
        """An exception in the confirmation must not strand the assignment.

        Raising here would abandon the transition mid-flight and leave the
        parent's `test_state` at "running" forever — the #1598 stranding shape,
        which is worse than the defect being fixed.
        """
        from coord.notify import post_transition

        self._record_work()
        self._record_smoke()
        transition, record, entry = self._transition(tmp_path, "SMOKE: pass")

        with (
            patch("coord.notify.post_completion"),
            patch("coord.notify.mark_notified"),
            patch("coord.notify._capture_cost"),
            patch("coord.notify._capture_smoke_tests"),
            patch("coord.notify._capture_completion_summary"),
            patch("coord.notify._capture_claude_session_id"),
            patch("coord.notify._agent_host", return_value=None),
            patch("coord.config.load", return_value=_StubConfig()),
            patch(
                "coord.confirm_test.confirm_branch",
                side_effect=RuntimeError("git exploded"),
            ),
        ):
            post_transition(transition, record, entry)

        row = self._row()
        assert row["test_state"] == "passed", (
            "a broken confirmation degrades to pre-#2464 behaviour"
        )
        assert "UNCONFIRMED" in (row["test_reason"] or "")

    def test_disabled_confirmation_restores_pre_fix_behaviour(
        self, coord_db, tmp_path: Path, monkeypatch
    ) -> None:
        """The operator escape hatch actually reaches the reap path."""
        from coord.notify import post_transition

        monkeypatch.setenv(ct.DISABLE_ENV_VAR, "0")
        self._record_work()
        self._record_smoke()
        transition, record, entry = self._transition(tmp_path, "SMOKE: pass")

        with (
            patch("coord.notify.post_completion"),
            patch("coord.notify.mark_notified"),
            patch("coord.notify._capture_cost"),
            patch("coord.notify._capture_smoke_tests"),
            patch("coord.notify._capture_completion_summary"),
            patch("coord.notify._capture_claude_session_id"),
            patch("coord.notify._agent_host", return_value=None),
            patch("coord.config.load", return_value=_StubConfig()),
            patch("coord.confirm_test.confirm_branch") as confirm,
        ):
            post_transition(transition, record, entry)

        confirm.assert_not_called()
        assert self._row()["test_state"] == "passed"


class TestPipelineConfigFlag:
    def test_confirm_test_verdict_parses_and_defaults_on(self, tmp_path: Path) -> None:
        from coord.config import load

        base = (
            "repos:\n"
            "  - name: api\n"
            "    github: acme/api\n"
            "machines:\n"
            "  - name: laptop\n"
            "    host: laptop.tailnet\n"
            "    repos: [api]\n"
        )
        default_cfg = tmp_path / "default.yml"
        default_cfg.write_text(base)
        assert load(default_cfg).pipeline.confirm_test_verdict is True

        off_cfg = tmp_path / "off.yml"
        off_cfg.write_text(base + "pipeline:\n  confirm_test_verdict: false\n")
        assert load(off_cfg).pipeline.confirm_test_verdict is False

    def test_non_boolean_is_rejected(self, tmp_path: Path) -> None:
        from coord.config import ConfigError, load

        p = tmp_path / "bad.yml"
        p.write_text(
            "repos:\n  - name: api\n    github: acme/api\n"
            "machines:\n  - name: laptop\n    host: laptop.tailnet\n    repos: [api]\n"
            "pipeline:\n  confirm_test_verdict: maybe\n"
        )
        with pytest.raises(ConfigError, match="confirm_test_verdict"):
            load(p)


# ── #2464-review: the confirmation must not hold the fleet, or judge on the
#    wrong hardware ────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _fresh_pass_budget():
    """No pass budget open unless a test opens one.

    `begin_confirmation_pass` writes thread-local state and pytest runs every
    test on one thread, so a test that opens a pass would otherwise leave a
    partially-spent budget behind for whatever ran next.
    """
    ct._pass_state.__dict__.pop("remaining", None)
    yield
    ct._pass_state.__dict__.pop("remaining", None)


class _FakeClock:
    """A monotonic clock a test drives by hand, in seconds."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class TestPassBudgetBoundsTheDrain:
    """A confirmation runs *inside* the notify drain, which holds
    `~/.coord/notify.lock` for the whole pass — so its wall clock is the whole
    fleet's. Bounding one run is not enough: a pass with several completed
    smoke rows would serialize several, and the hold would scale with board
    activity. The pass budget is what makes the worst case a fixed number, and
    a fixed number is what the thin client can be given.
    """

    def test_thin_client_notify_timeout_outlasts_a_confirming_pass(self) -> None:
        """`coord notify` routes to the daemon (#906) and the daemon finishes
        the drain under `notify.lock` no matter what the client does. The
        pre-#2464 180s would report `notify via daemon failed` for a pass that
        is still running and will finish fine — on the one command the whole
        auto-loop is driven by. Same fix as `coord merge --revalidate`'s
        `coord.revalidate.client_timeout_seconds`."""
        from coord.commands import lifecycle

        seen: dict = {}

        def fake_post_record(svc, path, params, timeout):
            seen["path"] = path
            seen["timeout"] = timeout
            return {"output": "", "exit_code": 0}

        with (
            patch(
                "coord.board_service.daemon_reroute_target", return_value=object(),
            ),
            patch("coord.client.post_record", side_effect=fake_post_record),
        ):
            lifecycle.notify.callback(config_path=None)

        assert seen["path"] == "/notify"
        assert seen["timeout"] > 180.0, (
            "the pre-#2464 timeout predates the drain ever running a suite"
        )
        assert seen["timeout"] >= ct.CONFIRM_PASS_BUDGET_SECONDS, (
            "the client has to outlast everything the daemon may spend "
            "confirming before it answers"
        )
        assert seen["timeout"] == ct.notify_client_timeout_seconds()

    def test_systemd_unit_timeout_outlasts_the_client_timeout(self) -> None:
        """`deploy/coord-notify.service` is the OTHER client-side ceiling on
        the same call — the timer is the sanctioned single pipeline driver, and
        the tighter of the two ceilings is the one that actually bites."""
        import re

        unit = Path(__file__).resolve().parents[1] / "deploy" / "coord-notify.service"
        m = re.search(r"^TimeoutStartSec=(\d+)", unit.read_text(), re.MULTILINE)
        assert m is not None, "the unit must still declare TimeoutStartSec"
        assert float(m.group(1)) >= ct.notify_client_timeout_seconds()

    def test_budget_hands_out_the_run_ceiling_then_only_the_remainder(self) -> None:
        ct.begin_confirmation_pass()
        assert ct.confirmation_timeout() == int(ct.CONFIRM_DEFAULT_TIMEOUT_SECONDS)

        # A confirmation that used most of the pass leaves only what is left —
        # never a fresh full ceiling, which is how the hold would grow.
        ct.spend_confirmation_budget(
            ct.CONFIRM_PASS_BUDGET_SECONDS - ct.CONFIRM_DEFAULT_TIMEOUT_SECONDS + 120
        )
        assert ct.confirmation_timeout() == int(
            ct.CONFIRM_DEFAULT_TIMEOUT_SECONDS - 120
        )

        ct.spend_confirmation_budget(ct.CONFIRM_PASS_BUDGET_SECONDS)
        assert ct.confirmation_timeout() is None, (
            "a spent budget must stop handing out time, not go negative"
        )

    def test_a_nearly_spent_budget_does_not_start_a_doomed_run(self) -> None:
        ct.begin_confirmation_pass(ct.CONFIRM_MIN_RUN_SECONDS - 1)
        assert ct.confirmation_timeout() is None

        ct.begin_confirmation_pass(ct.CONFIRM_MIN_RUN_SECONDS + 5)
        assert ct.confirmation_timeout() == ct.CONFIRM_MIN_RUN_SECONDS + 5

    def test_spending_outside_a_pass_is_a_no_op_not_a_crash(self) -> None:
        ct.spend_confirmation_budget(10_000)
        assert ct.confirmation_timeout() == int(ct.CONFIRM_DEFAULT_TIMEOUT_SECONDS), (
            "with no pass open, the full per-run ceiling applies"
        )

    def test_a_backwards_clock_cannot_refund_budget(self) -> None:
        ct.begin_confirmation_pass(600)
        ct.spend_confirmation_budget(-10_000)
        assert ct.confirmation_timeout() == 600

    @pytest.mark.parametrize(
        "entrypoint", ["run", "_run_drain_locked"],
    )
    def test_every_pass_entrypoint_opens_a_budget(self, entrypoint: str) -> None:
        """Both ways into a pass must arm the bound. The daemon's `/notify`
        handler invokes the `coord notify` CLI callback (so `notify.run`),
        while its pipeline clock calls `run_drain` — miss either and that route
        holds the lock unbounded."""
        import coord.notify as notify_mod

        class _Reached(Exception):
            """Raised from the budget call, so nothing after it runs."""

        cfg = _StubConfig()
        cfg.machines = []
        with (
            patch(
                "coord.confirm_test.begin_confirmation_pass", side_effect=_Reached,
            ),
            pytest.raises(_Reached),
        ):
            getattr(notify_mod, entrypoint)(cfg)


class TestExhaustedBudgetFallsBackToTheClaim:
    """When the budget runs out the row records `passed` UNCONFIRMED — exactly
    pre-#2464 behaviour — rather than the drain holding `notify.lock` longer.
    The truncation is stated in `test_reason`, never silent."""

    _record_work = TestSmokeVerdictIsConfirmed._record_work
    _record_smoke = TestSmokeVerdictIsConfirmed._record_smoke
    _transition = TestSmokeVerdictIsConfirmed._transition
    _row = TestSmokeVerdictIsConfirmed._row

    def test_spent_budget_skips_the_run_and_says_so(
        self, coord_db, tmp_path: Path
    ) -> None:
        self._record_work()
        self._record_smoke()
        transition, record, entry = self._transition(tmp_path, "SMOKE: pass")

        ct.begin_confirmation_pass(0.0)
        confirm = TestSmokeVerdictIsConfirmed._reap(
            self, transition, record, entry,
            ct.ConfirmationResult(kind=KIND_OK, reason="should not be reached"),
        )

        assert confirm.call_count == 0, (
            "a spent budget must not start another suite run"
        )
        row = self._row()
        assert row["test_state"] == "passed", (
            "falling back to the claim is pre-#2464 behaviour, not a failure"
        )
        assert "UNCONFIRMED" in row["test_reason"]

    def test_a_confirmation_charges_its_own_wall_clock_to_the_budget(
        self, coord_db, tmp_path: Path
    ) -> None:
        """One long confirmation must leave less for the next row in the same
        pass — otherwise the bound is per-run again and the hold grows with the
        number of completed smoke rows."""
        self._record_work()
        self._record_smoke()
        transition, record, entry = self._transition(tmp_path, "SMOKE: pass")

        ct.begin_confirmation_pass(ct.CONFIRM_PASS_BUDGET_SECONDS)
        clock = _FakeClock()

        def slow_confirm(*a, **k):
            clock.now += ct.CONFIRM_PASS_BUDGET_SECONDS
            return ct.ConfirmationResult(kind=KIND_OK, reason="green")

        with (
            patch("coord.notify.post_completion"),
            patch("coord.notify.mark_notified"),
            patch("coord.notify._capture_cost"),
            patch("coord.notify._capture_smoke_tests"),
            patch("coord.notify._capture_completion_summary"),
            patch("coord.notify._capture_claude_session_id"),
            patch("coord.notify._agent_host", return_value=None),
            patch("coord.config.load", return_value=_StubConfig()),
            patch("coord.notify.time.monotonic", side_effect=clock),
            patch("coord.confirm_test.confirm_branch", side_effect=slow_confirm),
        ):
            from coord.notify import post_transition

            post_transition(transition, record, entry)

        assert self._row()["test_state"] == "passed"
        assert ct.confirmation_timeout() is None, (
            "the run's wall clock must have been charged against the pass"
        )


class TestOneWindowForBuildAndSuite:
    """`CONFIRM_DEFAULT_TIMEOUT_SECONDS` documents itself as the ceiling on
    *one confirmation run*, and both the pass budget and the thin-client
    timeout are sized off it as if it were. Giving the build and the suite that
    ceiling each would make the real worst case twice every number derived from
    it — inside the drain, that factor of two is lock-hold time."""

    def test_the_suite_only_gets_what_the_build_left(self, checkout: Path) -> None:
        clock = _FakeClock()
        repo = _StubRepo(build_command="build-it")
        calls: list[tuple[str, int]] = []

        def recording(command, cwd, timeout):
            calls.append((command, timeout))
            if command == "build-it":
                clock.now += 500
            return _FakeProc(0)

        result = ct.confirm_branch(
            "api", BRANCH, _StubConfig(repo, repo_path=str(checkout)),
            timeout=1200, runner=recording, clock=clock,
        )

        assert result.kind == KIND_OK, result.reason
        assert calls[0] == ("build-it", 1200)
        assert calls[1] == ("run-the-suite", 700), (
            "the suite must inherit the remainder of the shared window, not a "
            f"fresh full one — got {calls}"
        )

    def test_a_build_that_eats_the_window_leaves_no_suite_run(
        self, checkout: Path
    ) -> None:
        clock = _FakeClock()
        repo = _StubRepo(build_command="build-it")
        calls: list[str] = []

        def recording(command, cwd, timeout):
            calls.append(command)
            clock.now += 2000
            return _FakeProc(0)

        result = ct.confirm_branch(
            "api", BRANCH, _StubConfig(repo, repo_path=str(checkout)),
            timeout=1200, runner=recording, clock=clock,
        )

        assert calls == ["build-it"], (
            "the suite must not start once the shared window is gone"
        )
        assert result.kind == KIND_TIMEOUT
        assert result.inconclusive is True
        assert result.refuted is False, (
            "running out of time says nothing about the branch"
        )


class TestConfirmationRespectsCapabilityRouting:
    """CLAUDE.md: 'Smoke tests validate on capable hardware.' The Test stage
    routes the original run through `smoke_tests.capability_rules`; the
    confirmation runs wherever the notify drain runs, which in production is
    the always-on daemon host. Re-running a GTK or browser suite there and
    watching it fail for want of a display would REFUTE a good branch — the one
    direction this module promises never to fail in."""

    @staticmethod
    def _rules(files: list[str], requires: list[str]):
        from coord.config import SmokeRule, SmokeTestsConfig

        return SmokeTestsConfig(
            capability_rules=[SmokeRule(files=files, requires=requires)]
        )

    @staticmethod
    def _config_here(checkout: Path, smoke_tests, capabilities: list[str]):
        """A config whose single machine really is *this* host, so the real
        `local_machine` hostname match runs rather than being stubbed out."""
        import socket

        cfg = _StubConfig(repo_path=str(checkout))
        machine = cfg.machines[0]
        machine.name = socket.gethostname().split(".")[0]
        machine.host = machine.name
        machine.capabilities = capabilities
        cfg.smoke_tests = smoke_tests
        return cfg

    @pytest.fixture
    def tui_checkout(self, tmp_path: Path, checkout: Path) -> Path:
        """The same fleet, with the branch also touching `tui/`."""
        seed = tmp_path / "seed"
        (seed / "tui").mkdir(exist_ok=True)
        (seed / "tui" / "app.rs").write_text("fn main() {}\n", encoding="utf-8")
        _git(seed, "add", "-A")
        _git(seed, "commit", "-m", "tui change")
        _git(seed, "push", "origin", BRANCH)
        return checkout

    def test_unmet_capability_is_inconclusive_and_runs_nothing(
        self, tui_checkout: Path
    ) -> None:
        runner = _ScriptedRunner(default=_FakeProc(1, stdout="cannot open display"))
        cfg = self._config_here(
            tui_checkout, self._rules(["tui/"], ["gtk"]), capabilities=["rust"],
        )

        result = ct.confirm_branch("api", BRANCH, cfg, runner=runner)

        assert result.kind == KIND_SETUP, result.reason
        assert result.inconclusive is True
        assert result.refuted is False
        assert runner.calls == [], (
            "a machine the Test stage would never have routed to must not get "
            "to overturn the verdict"
        )
        assert "gtk" in result.reason

    def test_a_capable_machine_still_confirms(self, tui_checkout: Path) -> None:
        runner = _ScriptedRunner(default=_FakeProc(1, stdout="5 failed"))
        cfg = self._config_here(
            tui_checkout, self._rules(["tui/"], ["gtk"]),
            capabilities=["rust", "gtk"],
        )

        result = ct.confirm_branch("api", BRANCH, cfg, runner=runner)

        assert result.kind == KIND_SUITE, result.reason
        assert result.refuted is True, (
            "the gate must not water down confirmation on capable hardware"
        )

    def test_a_diff_matching_no_rule_is_not_gated(self, checkout: Path) -> None:
        runner = _ScriptedRunner(default=_FakeProc(0))
        cfg = self._config_here(
            checkout, self._rules(["tui/"], ["gtk"]), capabilities=[],
        )

        result = ct.confirm_branch("api", BRANCH, cfg, runner=runner)

        assert result.kind == KIND_OK, result.reason
        assert [c[0] for c in runner.calls] == ["run-the-suite"]

    def test_an_unrecognized_host_gets_the_benefit_of_the_doubt(
        self, tui_checkout: Path
    ) -> None:
        """Mirrors `coord.acceptance.capability_gap` (#966): a dev box outside
        the fleet may well have everything installed, and refusing to confirm
        anywhere unrecognized would switch the gate off for it entirely."""
        runner = _ScriptedRunner(default=_FakeProc(0))
        cfg = _StubConfig(repo_path=str(tui_checkout))
        cfg.machines[0].name = "not-this-host"
        cfg.machines[0].host = "not-this-host.tailnet"
        cfg.machines[0].capabilities = []
        cfg.smoke_tests = self._rules(["tui/"], ["gtk"])

        result = ct.confirm_branch("api", BRANCH, cfg, runner=runner)

        assert result.kind == KIND_OK, result.reason

    def test_an_uncomputable_diff_does_not_silently_gate(
        self, tmp_path: Path
    ) -> None:
        """`branch_touched_files` returns [] for 'unknown', and the caller must
        read that as 'gate not applicable', never as 'nothing required'
        collapsing into a refusal."""
        assert ct.branch_touched_files(tmp_path, BRANCH, "main") == []
