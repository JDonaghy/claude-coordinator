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
