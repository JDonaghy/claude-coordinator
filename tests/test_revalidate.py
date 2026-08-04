"""#1769: `coord merge --revalidate` — the merge lane's stale-verdict arm.

#1738 gave `coord drive` a re-test arm for a STALE-but-`passed` smoke verdict.
It covered 1 of the 3 real stalls measured on 2026-08-03, because the other two
branches were parked in the merge queue with no live drive. These tests cover
the second lane: the resolution reachable from `coord merge` itself.

Three things are asserted here, in order of how much they matter:

1. **`--revalidate` off ⇒ nothing changes.** Plain `coord merge` must be
   byte-identical to before: no re-test, no verdict write, no merge.
2. **A failing re-test never merges.** This must never become a laundering
   path for a verdict that would not pass against the current base.
3. **Only the stale case is eligible.** Review / CI / conflict / genuinely-
   missing-verdict blocks are untouched, even under `--revalidate`.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from coord import merge_queue as mq
from coord import revalidate as rv
from coord.cli import main
from coord.models import Assignment, Board


# ── shared fixtures ──────────────────────────────────────────────────────────

CONFIG_YAML = """\
repos:
  - name: api
    github: acme/api
    default_branch: main
    test_command: "true"
machines:
  - name: laptop
    host: laptop.tailnet
    repos: [api]
    repo_paths:
      api: {repo_path}
reviews:
  enabled: false
pipeline:
  default_gates: [test, merge]
ci_store:
  type: none
"""


@pytest.fixture(autouse=True)
def isolated_coord_dir(tmp_path: Path, monkeypatch):
    """`revalidate` builds its throwaway worktree under ``COORD_DIR`` — pin
    that to the test's tmp dir so a test run never writes into the real
    ``~/.coord/``."""
    d = tmp_path / "coord-state"
    d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("coord.state.COORD_DIR", d)
    return d


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(CONFIG_YAML.format(repo_path=str(tmp_path / "checkout")))
    return p


def _stamp_anchors(aid: str, *, head_sha: str, base_sha: str, patch_id: str) -> None:
    """Persist the #1479 freshness anchors on a board row.

    ``save_board`` does not carry them (they're written by
    ``coord.state._stamp_test_staleness_anchor``, three live `gh` reads that
    ride along with a real verdict write), so a seeded board has them NULL —
    which makes every staleness check a no-op and the entry look fresh.
    """
    from coord.db import get_connection

    conn = get_connection()
    conn.execute(
        "UPDATE assignments SET test_head_sha=?, test_base_sha=?, test_patch_id=? "
        "WHERE assignment_id=?",
        (head_sha, base_sha, patch_id, aid),
    )
    conn.commit()


def _entry(
    aid: str,
    *,
    issue: int,
    state: str = mq.PENDING,
    target: str = "main",
) -> mq.QueuedMerge:
    return mq.QueuedMerge(
        assignment_id=aid,
        repo_name="api",
        repo_github="acme/api",
        branch=f"issue-{issue}-{aid}",
        target_branch=target,
        issue_number=issue,
        issue_title=f"issue {issue}",
        state=state,
    )


def _tested_work(
    aid: str,
    *,
    issue: int,
    test_state: str | None = "passed",
    base_sha: str = "base-old",
) -> Assignment:
    """A done work row carrying a terminal verdict anchored (per #1479) to the
    branch/base it was actually tested against."""
    return Assignment(
        machine_name="laptop", repo_name="api", issue_number=issue,
        issue_title=f"issue {issue}", assignment_id=aid, type="work",
        status="done", branch=f"issue-{issue}-{aid}",
        test_state=test_state,
        test_head_sha=f"branch-sha-{issue}",
        test_base_sha=base_sha,
        test_patch_id=f"patch-{issue}",
    )


def _config(*, gates: list[str] | None = None, reviews: bool = False):
    from dataclasses import dataclass as _dc, field as _f

    @_dc
    class _Reviews:
        enabled: bool = False

    @_dc
    class _Pipeline:
        default_gates: list[str] | None = None

    @_dc
    class _Cfg:
        reviews: _Reviews = _f(default_factory=_Reviews)
        pipeline: _Pipeline = _f(default_factory=_Pipeline)

    cfg = _Cfg()
    cfg.reviews.enabled = reviews
    cfg.pipeline.default_gates = (
        gates if gates is not None else ["test", "review", "merge"]
    )
    return cfg


# ══════════════════════════════════════════════════════════════════════════════
# 1. Stale-vs-missing has exactly ONE implementation
# ══════════════════════════════════════════════════════════════════════════════

class TestSingleStaleDetector:
    """#1769 acceptance: "Stale-vs-missing detection has exactly one
    implementation, imported by both `coord/drive.py` and the merge path —
    assert this with a test that would fail if the logic were duplicated."

    #1738 put the predicate in `coord/drive.py`. #1769 moved it to
    `coord/merge_queue.py`, next to the `SmokeVerdictStatus` code that emits
    both of the wordings it matches, and made drive an alias. A future copy —
    the #1141 failure mode — breaks these.
    """

    def test_drive_and_merge_queue_share_the_same_function_object(self) -> None:
        from coord import drive as drive_mod

        assert drive_mod._is_stale_smoke_reason is mq.is_stale_smoke_reason, (
            "coord.drive must ALIAS coord.merge_queue.is_stale_smoke_reason, "
            "not define its own copy — a second string-matching implementation "
            "in a second module is how #1141 went stale"
        )
        assert drive_mod._STALE_SMOKE_MARKERS is mq.STALE_SMOKE_MARKERS

    def test_revalidate_path_uses_the_same_module(self) -> None:
        """The merge lane consumes the SAME module-level detector, via
        `merge_queue.revalidation_candidates` (structured) — not a third copy
        of the string matching."""
        import inspect

        src = inspect.getsource(rv)
        assert "smoke test verdict is stale" not in src, (
            "coord.revalidate must not carry its own copy of the stale-verdict "
            "marker strings — it consumes merge_queue's classification"
        )
        assert "test verdict stale" not in src

    def test_no_module_outside_merge_queue_defines_the_markers(self) -> None:
        """The literal marker tuple is defined once, in merge_queue.py."""
        coord_dir = Path(mq.__file__).parent
        definers = []
        for path in sorted(coord_dir.rglob("*.py")):
            text = path.read_text(encoding="utf-8", errors="ignore")
            # A *definition* looks like `... = ("smoke test verdict is stale"`
            for line in text.splitlines():
                stripped = line.strip()
                if (
                    "= (" in stripped
                    and "smoke test verdict is stale" in stripped
                ):
                    definers.append(str(path.relative_to(coord_dir.parent)))
        assert sorted(set(definers)) == ["coord/merge_queue.py"], definers

    def test_distinguishes_stale_from_missing(self) -> None:
        """The behaviour itself, unchanged from #1738's coverage in
        tests/test_drive.py — asserted here too so the lifted home is
        independently pinned."""
        assert mq.is_stale_smoke_reason(
            "smoke test verdict is stale: recorded against base abc1234, "
            "base is now def5678 — re-verify"
        )
        assert mq.is_stale_smoke_reason("test verdict stale (base moved)")
        assert not mq.is_stale_smoke_reason(
            "smoke test required but no verdict recorded"
        )
        assert not mq.is_stale_smoke_reason("test verdict missing")
        assert not mq.is_stale_smoke_reason("review required but not approved")
        assert not mq.is_stale_smoke_reason("")
        assert not mq.is_stale_smoke_reason(None)


# ══════════════════════════════════════════════════════════════════════════════
# 2. Eligibility — only the stale case, only when nothing else blocks
# ══════════════════════════════════════════════════════════════════════════════

class TestRevalidationCandidates:
    @staticmethod
    def _stale_setup():
        """One PENDING entry whose only problem is a base that moved."""
        entry = _entry("w1", issue=101)
        entry.target_branch_head_sha = "base-new"
        board = Board(active=[], completed=[_tested_work("w1", issue=101)])
        return entry, board

    def test_stale_verdict_is_a_candidate(self) -> None:
        entry, board = self._stale_setup()
        cands = mq.revalidation_candidates([entry], board, _config(gates=["test", "merge"]))
        assert [c.entry.assignment_id for c in cands] == ["w1"]
        assert cands[0].work_assignment_id == "w1"
        assert cands[0].smoke.kind == mq.SMOKE_STALE

    def test_missing_verdict_is_not_a_candidate(self) -> None:
        """#1769 acceptance: a genuinely-missing verdict is never revalidated
        — it's the #1640 lost-write shape, which a re-test cannot safely paper
        over."""
        entry = _entry("w1", issue=101)
        board = Board(active=[], completed=[
            _tested_work("w1", issue=101, test_state=None),
        ])
        assert mq.revalidation_candidates(
            [entry], board, _config(gates=["test", "merge"])
        ) == []

    def test_fresh_verdict_is_not_a_candidate(self) -> None:
        entry = _entry("w1", issue=101)
        entry.target_branch_head_sha = "base-old"  # unmoved
        board = Board(active=[], completed=[_tested_work("w1", issue=101)])
        assert mq.revalidation_candidates(
            [entry], board, _config(gates=["test", "merge"])
        ) == []

    def test_review_block_is_not_a_candidate(self) -> None:
        """#1769 acceptance: an entry blocked on review is untouched even under
        --revalidate — a re-test gives it nothing it's waiting for."""
        entry, board = self._stale_setup()
        cfg = _config(gates=["test", "review", "merge"], reviews=True)
        assert mq.revalidation_candidates([entry], board, cfg) == []

    def test_conflict_entry_is_not_a_candidate(self) -> None:
        entry, board = self._stale_setup()
        entry.state = mq.CONFLICT
        assert mq.revalidation_candidates(
            [entry], board, _config(gates=["test", "merge"])
        ) == []

    def test_human_required_entry_is_not_a_candidate(self) -> None:
        entry, board = self._stale_setup()
        entry.state = mq.HUMAN_REQUIRED
        assert mq.revalidation_candidates(
            [entry], board, _config(gates=["test", "merge"])
        ) == []

    def test_smoke_gate_disabled_yields_no_candidates(self) -> None:
        entry, board = self._stale_setup()
        assert mq.revalidation_candidates(
            [entry], board, _config(gates=["merge"])
        ) == []

    def test_is_pure_no_mutation(self) -> None:
        """Safe to call from --dry-run: nothing is written."""
        entry, board = self._stale_setup()
        before = (entry.state, entry.error)
        mq.revalidation_candidates([entry], board, _config(gates=["test", "merge"]))
        assert (entry.state, entry.error) == before


# ══════════════════════════════════════════════════════════════════════════════
# 3. The composite re-test itself
# ══════════════════════════════════════════════════════════════════════════════

def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True,
    )


@pytest.fixture
def git_fleet(tmp_path: Path):
    """A bare 'origin' with `main` plus three feature branches that compose
    cleanly, and a local checkout wired to it — the real shape `revalidate()`
    operates on (throwaway worktree off the base checkout, `origin/<branch>`
    refs).

    Three branches, not two: #1715's headline acceptance is stated over a
    THREE-entry queue ("assert the run count is 1, not 3"). Tests that only
    need two simply queue two — an unused branch in the fleet costs nothing.
    """
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    checkout = tmp_path / "checkout"

    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    _git(seed, "config", "user.email", "t@example.com")
    _git(seed, "config", "user.name", "t")
    (seed / "base.txt").write_text("base\n")
    _git(seed, "add", ".")
    _git(seed, "commit", "-q", "-m", "base")

    for issue, aid in ((101, "w1"), (102, "w2"), (103, "w3")):
        _git(seed, "checkout", "-q", "-b", f"issue-{issue}-{aid}", "main")
        (seed / f"f{issue}.txt").write_text(f"issue {issue}\n")
        _git(seed, "add", ".")
        _git(seed, "commit", "-q", "-m", f"issue {issue}")
    _git(seed, "checkout", "-q", "main")

    subprocess.run(
        ["git", "clone", "-q", "--bare", str(seed), str(origin)],
        check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "clone", "-q", str(origin), str(checkout)],
        check=True, capture_output=True, text=True,
    )
    _git(checkout, "config", "user.email", "t@example.com")
    _git(checkout, "config", "user.name", "t")
    return checkout


@dataclass
class _Run:
    returncode: int
    stdout: str = ""
    stderr: str = ""


def _live_config(checkout: Path, *, test_command: str = "true"):
    """A config object shaped like the real one, pointing at *checkout*."""
    @dataclass
    class _Repo:
        name: str = "api"
        github: str = "acme/api"
        default_branch: str = "main"
        test_command: str | None = None
        build_command: str | None = None

    @dataclass
    class _Machine:
        name: str = "laptop"
        host: str = "laptop.tailnet"
        path: str = ""

        def repo_path(self, repo_name: str):
            return self.path if repo_name == "api" else None

    class _Cfg:
        def __init__(self) -> None:
            self._repo = _Repo(test_command=test_command)
            self.machines = [_Machine(path=str(checkout))]

        def repo(self, name):
            return self._repo if name == "api" else None

    return _Cfg()


class TestCompositeRevalidation:
    @staticmethod
    def _candidates():
        out = []
        for aid, issue in (("w1", 101), ("w2", 102)):
            entry = _entry(aid, issue=issue)
            entry.target_branch_head_sha = "base-new"
            out.append(mq.RevalidationCandidate(
                entry=entry,
                work_assignment_id=aid,
                smoke=mq.SmokeVerdictStatus(
                    ok=False, kind=mq.SMOKE_STALE, assignment_id=aid,
                    anchor="base", recorded_sha="base-old", current_sha="base-new",
                ),
            ))
        return out

    def test_batch_composes_all_branches_and_runs_the_suite_once(
        self, git_fleet: Path, coord_db,
    ) -> None:
        """#1715 option 3: N entries → 1 suite run, on a composite of all N."""
        runs: list[tuple[str, Path]] = []

        def runner(command, cwd, timeout):
            runs.append((command, cwd))
            # Every branch's file must be present in the composite tree.
            assert (Path(cwd) / "f101.txt").exists()
            assert (Path(cwd) / "f102.txt").exists()
            return _Run(0)

        recorded: list[tuple] = []
        with patch(
            "coord.state.record_test_verdict",
            side_effect=lambda **kw: recorded.append(kw),
        ):
            result = rv.revalidate(
                self._candidates(), _live_config(git_fleet), runner=runner,
            )

        assert result.ok, result.reason
        assert len(runs) == 1, "the whole point is ONE suite run for N entries"
        assert result.composed == ["issue-101-w1", "issue-102-w2"]
        assert sorted(result.recorded) == ["w1", "w2"]
        assert [r["assignment_id"] for r in recorded] == ["w1", "w2"]
        assert {r["test_state"] for r in recorded} == {"passed"}

    def test_failing_suite_records_nothing_and_reports_the_failure(
        self, git_fleet: Path, coord_db,
    ) -> None:
        """#1769 acceptance: "A revalidation whose re-test FAILS leaves the
        entry blocked and does not merge. ... it must never become a laundering
        path for a verdict that would not pass against the current base."
        """
        def runner(command, cwd, timeout):
            return _Run(1, stdout="E   assert 1 == 2\n", stderr="1 failed")

        with patch("coord.state.record_test_verdict") as record:
            result = rv.revalidate(
                self._candidates(), _live_config(git_fleet), runner=runner,
            )

        assert result.ok is False
        record.assert_not_called()
        assert "SUITE FAILED" in result.reason
        assert "assert 1 == 2" in result.output
        # The failure is quoted back to the operator, not swallowed.
        rendered = "\n".join(rv.format_failure(result))
        assert "assert 1 == 2" in rendered
        assert "worktree kept for inspection" in rendered

    def test_failing_build_records_nothing(self, git_fleet: Path, coord_db) -> None:
        cfg = _live_config(git_fleet)
        cfg._repo.build_command = "exit 3"

        def runner(command, cwd, timeout):
            return _Run(3 if command == "exit 3" else 0, stderr="boom")

        with patch("coord.state.record_test_verdict") as record:
            result = rv.revalidate(self._candidates(), cfg, runner=runner)

        assert result.ok is False
        assert "BUILD FAILED" in result.reason
        record.assert_not_called()

    def test_timeout_records_nothing(self, git_fleet: Path, coord_db) -> None:
        def runner(command, cwd, timeout):
            raise subprocess.TimeoutExpired(cmd=command, timeout=timeout)

        with patch("coord.state.record_test_verdict") as record:
            result = rv.revalidate(
                self._candidates(), _live_config(git_fleet), runner=runner,
                timeout=5,
            )

        assert result.ok is False
        assert "timed out" in result.reason
        record.assert_not_called()

    def test_unconfigured_test_command_refuses(
        self, git_fleet: Path, coord_db,
    ) -> None:
        """Recording `passed` for a suite that does not exist is never
        correct — refuse instead."""
        with patch("coord.state.record_test_verdict") as record:
            result = rv.revalidate(
                self._candidates(),
                _live_config(git_fleet, test_command=None),
                runner=lambda *a: _Run(0),
            )
        assert result.ok is False
        assert "no test_command" in result.reason
        record.assert_not_called()

    def test_missing_local_checkout_refuses(self, tmp_path: Path, coord_db) -> None:
        with patch("coord.state.record_test_verdict") as record:
            result = rv.revalidate(
                self._candidates(),
                _live_config(tmp_path / "does-not-exist"),
                runner=lambda *a: _Run(0),
            )
        assert result.ok is False
        assert "no local checkout" in result.reason
        record.assert_not_called()

    def test_mixed_bases_are_refused(self, git_fleet: Path, coord_db) -> None:
        """A composite that spans two bases validates nothing meaningful."""
        cands = self._candidates()
        cands[1].entry.target_branch = "develop"
        with patch("coord.state.record_test_verdict") as record:
            result = rv.revalidate(
                cands, _live_config(git_fleet), runner=lambda *a: _Run(0),
            )
        assert result.ok is False
        assert "more than one" in result.reason
        record.assert_not_called()

    def test_empty_candidate_list_is_a_no_op(self, coord_db) -> None:
        result = rv.revalidate([], _live_config(Path("/nonexistent")))
        assert result.ok is True
        assert result.recorded == []


# ══════════════════════════════════════════════════════════════════════════════
# 4. Black-box: the CLI, end to end
# ══════════════════════════════════════════════════════════════════════════════

def _live_sha(checkout: Path):
    """Answer `get_branch_sha` from the REAL git refs in *checkout*.

    The staleness the black-box exercises is seeded on the board side (the
    recorded anchors name commits that no longer describe anything), while the
    *live* side reads the actual repo — so after `--revalidate` re-anchors a
    verdict to the commits it really validated, the gate genuinely agrees. A
    fake on both sides would prove nothing about the round trip.
    """
    def _get(repo, branch):
        res = subprocess.run(
            ["git", "rev-parse", f"origin/{branch}"],
            cwd=str(checkout), capture_output=True, text=True,
        )
        return res.stdout.strip() or None
    return _get


def _compare_files(repo, base, head):
    # The base move touched real source → never inert (#1738), so a verdict
    # anchored to the old base is genuinely stale.
    return ["coord/merge_queue.py"]


_FLEET = (("w1", 101), ("w2", 102), ("w3", 103))


def _seed_stale_entries(pairs=_FLEET[:2]):
    """N approved, tested entries whose verdicts were all staled by a base
    move — the #1769/#1715 headline scenario, with no live drive."""
    from coord.state import save_board

    entries = []
    works = []
    for aid, issue in pairs:
        e = _entry(aid, issue=issue)
        e.branch_head_sha = f"branch-sha-{issue}"
        entries.append(e)
        works.append(_tested_work(aid, issue=issue))
    mq.save_queue(entries)
    save_board(Board(active=[], completed=works))
    for aid, issue in pairs:
        _stamp_anchors(
            aid,
            head_sha=f"branch-sha-{issue}",
            base_sha="base-old",          # main has since moved to base-new
            patch_id=f"patch-{issue}",
        )
    return entries


def _seed_two_stale_entries(git_fleet: Path):
    return _seed_stale_entries(_FLEET[:2])


@pytest.fixture
def blackbox(git_fleet: Path, tmp_path: Path, coord_db):
    """Config + checkout for the headline scenario: two approved, tested
    entries, base moved so both verdicts are stale, no live drive."""
    cfg = tmp_path / "coordinator.yml"
    cfg.write_text(CONFIG_YAML.format(repo_path=str(git_fleet)))
    _seed_two_stale_entries(git_fleet)
    return cfg, git_fleet


def _gh_patches(checkout: Path):
    """Patch the `gh` surface `coord merge` touches so nothing hits the network."""
    next_pr = [900]

    def fake_create_pr(repo, *, base, head, title, body):
        n = next_pr[0]
        next_pr[0] += 1
        return {"number": n, "url": f"u/{n}", "existed": False}

    return [
        patch("coord.github_ops.create_pr", side_effect=fake_create_pr),
        patch("coord.github_ops.get_pr_size", return_value=10),
        patch("coord.github_ops.merge_pr", return_value=(True, "ok")),
        patch("coord.github_ops.get_branch_sha", side_effect=_live_sha(checkout)),
        patch("coord.github_ops.get_branch_patch_id", return_value=None),
        patch("coord.github_ops.get_compare_files", side_effect=_compare_files),
        patch("coord.github_ops.list_remote_branch_names", return_value=set()),
    ]


def _invoke(args: list[str], checkout: Path):
    stack = _gh_patches(checkout)
    for p in stack:
        p.start()
    try:
        return CliRunner().invoke(main, args)
    finally:
        for p in reversed(stack):
            p.stop()


def _states() -> dict[str, str]:
    return {x.assignment_id: x.state for x in mq.load_queue()}


class TestMergeRevalidateBlackBox:
    """#1769 acceptance: "Black-box test: seeded board, two approved entries,
    base moved so both verdicts are stale; assert `--revalidate` drains both
    and that plain `coord merge` drains neither."
    """

    def test_plain_merge_drains_neither_and_never_re_tests(self, blackbox) -> None:
        cfg, checkout = blackbox
        with patch("coord.revalidate.revalidate") as reval:
            result = _invoke(["merge", "--config", str(cfg)], checkout)

        assert result.exit_code == 0, result.output
        reval.assert_not_called()  # plain `coord merge` must never re-test
        assert _states() == {"w1": mq.PENDING, "w2": mq.PENDING}
        assert "stale" in result.output.lower()

    def test_dry_run_names_both_as_candidates_without_running_anything(
        self, blackbox,
    ) -> None:
        """#1715: "--dry-run names the batch members and states plainly that
        one composed run will validate all of them"."""
        cfg, checkout = blackbox
        with patch("coord.revalidate.revalidate") as reval:
            result = _invoke(
                ["merge", "--config", str(cfg), "--revalidate", "--dry-run"],
                checkout,
            )

        assert result.exit_code == 0, result.output
        reval.assert_not_called()
        # Every member is named...
        assert "revalidate: api #101 (issue-101-w1" in result.output
        assert "revalidate: api #102 (issue-102-w2" in result.output
        # ...as ONE batch, costing ONE run.
        assert "BATCH of 2" in result.output
        assert "ONE composed suite run (not 2)" in result.output
        assert "2 entry(ies) in 1 batch(es) — 1 suite run(s)" in result.output
        # The trade is stated where the operator decides, not just in --help.
        assert "validates the COMPOSITE, not each branch alone" in result.output
        assert _states() == {"w1": mq.PENDING, "w2": mq.PENDING}

    def test_revalidate_drains_both(self, blackbox) -> None:
        """The headline criterion: two approved, tested branches queued with no
        live drive — `coord merge --revalidate` merges both, no human action."""
        cfg, checkout = blackbox
        result = _invoke(["merge", "--config", str(cfg), "--revalidate"], checkout)

        assert result.exit_code == 0, result.output
        assert "--revalidate: PASSED" in result.output
        assert _states() == {"w1": mq.MERGED, "w2": mq.MERGED}, result.output

    def test_failing_revalidation_merges_nothing(self, blackbox) -> None:
        """A composite that fails against the current base leaves BOTH entries
        blocked. Never a laundering path."""
        cfg, checkout = blackbox
        cfg.write_text(
            cfg.read_text().replace('test_command: "true"', 'test_command: "exit 1"')
        )

        result = _invoke(["merge", "--config", str(cfg), "--revalidate"], checkout)

        assert "SUITE FAILED" in result.output
        assert _states() == {"w1": mq.PENDING, "w2": mq.PENDING}
        assert mq.MERGED not in _states().values()

    def test_revalidate_with_nothing_stale_says_so(
        self, git_fleet: Path, tmp_path: Path, coord_db,
    ) -> None:
        """--revalidate on a queue with no stale entry is inert and explicit."""
        from coord.state import save_board

        cfg = tmp_path / "coordinator.yml"
        cfg.write_text(CONFIG_YAML.format(repo_path=str(git_fleet)))
        mq.save_queue([_entry("w1", issue=101)])
        # No verdict at all → SMOKE_MISSING, which --revalidate never touches.
        save_board(Board(active=[], completed=[
            _tested_work("w1", issue=101, test_state=None),
        ]))

        with patch("coord.revalidate.revalidate") as reval:
            result = _invoke(
                ["merge", "--config", str(cfg), "--revalidate"], git_fleet,
            )

        reval.assert_not_called()
        assert "no entry is blocked solely on a stale test verdict" in result.output
        assert _states() == {"w1": mq.PENDING}


class TestBatchAcceptance:
    """#1715: the cascade half. N approved branches on one base used to cost
    N−1 full suite runs, because the first merge staled everything behind it.

    These are the issue's stated acceptance criteria, asserted on the run
    COUNT directly — "this is the whole point and it must be asserted
    directly, not implied by wall-clock".
    """

    @staticmethod
    def _cfg_with_counter(tmp_path: Path, git_fleet: Path, tail: str = "") -> tuple:
        """Config whose test_command appends one byte per invocation.

        Counting invocations of the repo's real `test_command`, through the
        real CLI, is the only measurement that actually answers "how many
        suite runs did that cost" — a mock of `revalidate()` would move the
        assertion above the thing under test.
        """
        counter = tmp_path / "suite-runs"
        cmd = f"printf x >> {counter}" + tail
        cfg = tmp_path / "coordinator.yml"
        cfg.write_text(
            CONFIG_YAML.format(repo_path=str(git_fleet)).replace(
                'test_command: "true"', f'test_command: "{cmd}"',
            )
        )
        return cfg, counter

    @staticmethod
    def _runs(counter: Path) -> int:
        return len(counter.read_text()) if counter.exists() else 0

    def test_three_stale_entries_cost_one_suite_run_and_all_merge(
        self, git_fleet: Path, tmp_path: Path, coord_db,
    ) -> None:
        """THE headline criterion: "Three approved entries queued, all
        stale-but-`passed`, base moved: `coord merge --revalidate` performs ONE
        suite run and merges all three. Assert the run count is 1, not 3."
        """
        _seed_stale_entries(_FLEET)
        cfg, counter = self._cfg_with_counter(tmp_path, git_fleet)

        result = _invoke(["merge", "--config", str(cfg), "--revalidate"], git_fleet)

        assert result.exit_code == 0, result.output
        assert self._runs(counter) == 1, (
            f"expected ONE composed suite run for three entries, got "
            f"{self._runs(counter)} — the cascade is back\n{result.output}"
        )
        assert _states() == {
            "w1": mq.MERGED, "w2": mq.MERGED, "w3": mq.MERGED,
        }, result.output
        assert "--revalidate: PASSED" in result.output
        assert "1 suite run(s) for 3 entry(ies)" in result.output

    def test_red_composite_merges_nothing_then_narrows_to_the_culprit(
        self, git_fleet: Path, tmp_path: Path, coord_db,
    ) -> None:
        """#1715: "A red composite merges nothing, and the follow-up per-entry
        pass identifies the actual culprit and merges the others."

        The suite here fails iff issue 103's file is in the tree, so the
        composite (which contains it) is red while 101 and 102 are green on
        their own — one culprit, two innocents, decided by the real suite
        rather than by a stubbed verdict.
        """
        _seed_stale_entries(_FLEET)
        cfg, counter = self._cfg_with_counter(
            tmp_path, git_fleet, tail="; ! test -f f103.txt",
        )

        result = _invoke(["merge", "--config", str(cfg), "--revalidate"], git_fleet)

        assert result.exit_code == 0, result.output
        # The composite failed → it merged nothing on its own result.
        assert "SUITE FAILED" in result.output
        # ...and the innocents still merged, off their own solo runs.
        assert _states() == {
            "w1": mq.MERGED, "w2": mq.MERGED, "w3": mq.PENDING,
        }, result.output
        # The culprit is NAMED, not just left blocked.
        assert "api #103 (issue-103-w3)" in result.output
        assert "culprit(s): api #103 (issue-103-w3)" in result.output
        # Worst case is 1 composite + N solo, and no more.
        assert self._runs(counter) == 4, result.output
        # #1715 is explicit that a red composite marks nothing failed: the
        # culprit is left PENDING and retryable, not parked in a terminal
        # state that needs a human to unwind.
        assert not ({mq.CONFLICT, mq.HUMAN_REQUIRED, mq.SKIPPED}
                    & set(_states().values()))

    def test_culprit_alone_blocks_only_itself_and_costs_nothing_extra(
        self, git_fleet: Path, tmp_path: Path, coord_db,
    ) -> None:
        """N=1 must stay byte-identical to #1769: one run, no fallback.

        With a single candidate the "composite" already IS that branch, so
        re-running it solo would be the same run twice.
        """
        _seed_stale_entries((("w3", 103),))
        cfg, counter = self._cfg_with_counter(
            tmp_path, git_fleet, tail="; ! test -f f103.txt",
        )

        result = _invoke(["merge", "--config", str(cfg), "--revalidate"], git_fleet)

        assert _states() == {"w3": mq.PENDING}, result.output
        assert self._runs(counter) == 1, "N=1 must not re-run itself"
        assert "re-running each branch on its own" not in result.output

    def test_setup_failure_never_fans_out_into_n_identical_failures(
        self, git_fleet: Path, tmp_path: Path, coord_db,
    ) -> None:
        """A common-mode failure (here: no local checkout) must not trigger the
        per-entry pass — every solo run would hit the identical wall, turning
        one clear error into N copies of it."""
        _seed_stale_entries(_FLEET)
        cfg = tmp_path / "coordinator.yml"
        cfg.write_text(
            CONFIG_YAML.format(repo_path=str(tmp_path / "gone"))
        )

        calls: list[int] = []
        real = rv.revalidate

        def counting(cands, *a, **kw):
            calls.append(len(cands))
            return real(cands, *a, **kw)

        with patch("coord.revalidate.revalidate", side_effect=counting):
            result = _invoke(
                ["merge", "--config", str(cfg), "--revalidate"], git_fleet,
            )

        assert calls == [3], f"expected one composite attempt only, got {calls}"
        assert "no local checkout" in result.output
        assert _states() == {
            "w1": mq.PENDING, "w2": mq.PENDING, "w3": mq.PENDING,
        }

    def test_dry_run_names_the_three_batch_members_and_the_single_run(
        self, git_fleet: Path, tmp_path: Path, coord_db,
    ) -> None:
        """#1715: "--dry-run names the batch members and states plainly that
        one composed run will validate all of them"."""
        _seed_stale_entries(_FLEET)
        cfg, counter = self._cfg_with_counter(tmp_path, git_fleet)

        result = _invoke(
            ["merge", "--config", str(cfg), "--revalidate", "--dry-run"],
            git_fleet,
        )

        assert "revalidate: api #101 (issue-101-w1" in result.output
        assert "revalidate: api #102 (issue-102-w2" in result.output
        assert "revalidate: api #103 (issue-103-w3" in result.output
        assert "BATCH of 3" in result.output
        assert "ONE composed suite run (not 3)" in result.output
        assert self._runs(counter) == 0, "--dry-run must run no suite at all"
        assert _states() == {
            "w1": mq.PENDING, "w2": mq.PENDING, "w3": mq.PENDING,
        }


class TestSkipSmokeUnchanged:
    """#1769 acceptance: "--skip-smoke keeps working unchanged as the manual
    override." It waives the gate; --revalidate satisfies it. They are not the
    same thing, and --revalidate must not have altered the waiver."""

    def test_skip_smoke_still_merges_a_stale_entry_without_re_testing(
        self, blackbox,
    ) -> None:
        cfg, checkout = blackbox
        with patch("coord.revalidate.revalidate") as reval:
            result = _invoke(
                ["merge", "--config", str(cfg), "--skip-smoke"], checkout,
            )

        assert result.exit_code == 0, result.output
        reval.assert_not_called()
        assert "--skip-smoke: interactive smoke-test gate bypassed" in result.output
        assert _states() == {"w1": mq.MERGED, "w2": mq.MERGED}


class TestOnlyPathRevalidates:
    """`--only` is the surgical single-entry lane. #1769 covers it too — it is
    the form an operator reaches for when one specific branch has gone stale
    (which is exactly what happened on #1732 and #1703)."""

    def test_only_revalidate_merges_the_one_stale_entry(self, blackbox) -> None:
        cfg, checkout = blackbox
        result = _invoke(
            ["merge", "--config", str(cfg), "--only", "w1", "--revalidate"],
            checkout,
        )

        assert result.exit_code == 0, result.output
        assert "--revalidate: PASSED" in result.output
        states = _states()
        assert states["w1"] == mq.MERGED, result.output
        # The sibling was never in scope for a --only run.
        assert states["w2"] == mq.PENDING

    def test_only_without_revalidate_leaves_it_blocked(self, blackbox) -> None:
        cfg, checkout = blackbox
        with patch("coord.revalidate.revalidate") as reval:
            result = _invoke(
                ["merge", "--config", str(cfg), "--only", "w1"], checkout,
            )

        reval.assert_not_called()
        assert result.exit_code == 0, result.output
        assert _states() == {"w1": mq.PENDING, "w2": mq.PENDING}


class TestDaemonRoute:
    """The daemon `/merge` route is the lane a thin client (and the TUI 'Go'
    button) reaches. It must forward `--revalidate` — the suite has to run
    where the repo is checked out, which is the daemon host — and the
    unattended auto-drain must never set it."""

    def test_post_merge_forwards_revalidate(
        self, valid_config_path: Path, tmp_path: Path,
    ) -> None:
        from starlette.testclient import TestClient

        from coord.config import load as load_config
        from coord.dao import SqliteStore
        from coord.serve_app import build_app

        seen: dict = {}

        def _fake_callback(**kwargs):
            seen.update(kwargs)

        cfg = load_config(valid_config_path)
        app = build_app(SqliteStore(tmp_path / "daemon.db"), cfg)
        with patch("coord.cli.merge") as merge_cmd:
            merge_cmd.callback = _fake_callback
            with TestClient(app) as cli:
                resp = cli.post(
                    "/merge",
                    json={"dry_run": True, "repo_filter": "no-such", "revalidate": True},
                )
        assert resp.status_code == 200, resp.text
        assert seen.get("revalidate") is True, seen

    def test_post_merge_defaults_revalidate_off(
        self, valid_config_path: Path, tmp_path: Path,
    ) -> None:
        from starlette.testclient import TestClient

        from coord.config import load as load_config
        from coord.dao import SqliteStore
        from coord.serve_app import build_app

        seen: dict = {}

        def _fake_callback(**kwargs):
            seen.update(kwargs)

        cfg = load_config(valid_config_path)
        app = build_app(SqliteStore(tmp_path / "daemon.db"), cfg)
        with patch("coord.cli.merge") as merge_cmd:
            merge_cmd.callback = _fake_callback
            with TestClient(app) as cli:
                resp = cli.post(
                    "/merge", json={"dry_run": True, "repo_filter": "no-such"},
                )
        assert resp.status_code == 200, resp.text
        assert seen.get("revalidate") is False, seen

    def test_auto_drain_never_revalidates(self) -> None:
        """The unattended tick must not start suite runs on its own schedule —
        the 2026-06-07 token-burn shape. `_auto_drain_tick` calls
        `merge_queue.process` directly and never goes near `revalidate`."""
        import inspect

        from coord import serve_app

        src = inspect.getsource(serve_app._auto_drain_tick)
        assert "coord.revalidate" not in src
        assert "revalidate(" not in src


class TestThinClientTimeout:
    """A `--revalidate` run executes the whole suite on the daemon host. The
    thin client's HTTP timeout has to outlast that, or the operator sees a
    timeout error for a merge that actually succeeded."""

    def test_revalidate_gets_a_longer_daemon_timeout(self) -> None:
        from coord.commands import merge as merge_mod

        seen: dict = {}

        def fake_post_record(svc, path, params, timeout):
            seen["timeout"] = timeout
            return {"output": "", "exit_code": 0}

        with patch("coord.client.post_record", side_effect=fake_post_record):
            merge_mod._merge_via_daemon(object(), {"revalidate": True})
        assert seen["timeout"] > rv.DEFAULT_TIMEOUT_SECONDS

        with patch("coord.client.post_record", side_effect=fake_post_record):
            merge_mod._merge_via_daemon(object(), {"revalidate": False})
        assert seen["timeout"] == 900.0
