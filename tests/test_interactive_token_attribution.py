"""#2129: interactive-leg token attribution must not fabricate ~30x physically
impossible token counts.

Two bugs in ``coord.interactive._tokens_from_transcript`` combined to inflate
interactive legs' ``coord usage`` ``est(~)`` figures far beyond what's
physically possible:

1. **Whole-session attribution.** A worktree's Claude Code transcript file is
   append-only and keeps growing across every leg that runs in that same
   interactive session (smoke, review, ...). The old code only checked the
   *file's* mtime against the leg's ``started_at`` — once the file had been
   touched after that cutoff, every line in it (including earlier and later
   legs' turns) was summed into this leg's total.
2. **Per-content-block double counting.** Claude Code writes one JSONL line
   per *content block* of an assistant turn (e.g. a ``thinking`` block and a
   ``tool_use`` block from the same API response land as two separate
   lines), and every line for that turn repeats the SAME ``usage`` totals.
   Summing every line multiplies a single turn's tokens by however many
   content blocks it had.

This file covers the fix (per-line time bounding + message-id dedup) and the
physical-plausibility backstop (``_output_tokens_physically_plausible``) added
as defense in depth.
"""

from __future__ import annotations

import functools
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner

from coord import interactive
from coord.cli import main

_BASE = datetime(2026, 8, 9, 12, 0, 0, tzinfo=timezone.utc)


def _ts(offset_secs: float) -> str:
    dt = _BASE + timedelta(seconds=offset_secs)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{int(dt.microsecond / 1000):03d}Z"


def _epoch(offset_secs: float) -> float:
    return (_BASE + timedelta(seconds=offset_secs)).timestamp()


def _assistant_line(
    *,
    msg_id: str,
    offset_secs: float,
    input_tokens: int,
    output_tokens: int,
    cache_creation: int = 0,
    cache_read: int = 0,
    content_type: str = "text",
) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "timestamp": _ts(offset_secs),
            "message": {
                "id": msg_id,
                "role": "assistant",
                "content": [{"type": content_type, "text": "hi"}],
                "usage": {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "cache_creation_input_tokens": cache_creation,
                    "cache_read_input_tokens": cache_read,
                },
            },
        }
    )


class TestDedupByMessageId:
    def test_two_content_blocks_same_turn_counted_once(self, tmp_path: Path) -> None:
        # A single API response emits a `thinking` block and a `tool_use`
        # block as two JSONL lines, same message id, same usage totals —
        # exactly what a real Claude Code transcript does per turn.
        proj = tmp_path / "-home-john--coord-worktrees-abc"
        proj.mkdir()
        lines = [
            _assistant_line(
                msg_id="msg_1", offset_secs=10, input_tokens=2, output_tokens=140,
                cache_creation=6991, cache_read=22715, content_type="thinking",
            ),
            _assistant_line(
                msg_id="msg_1", offset_secs=10.25, input_tokens=2, output_tokens=140,
                cache_creation=6991, cache_read=22715, content_type="tool_use",
            ),
        ]
        (proj / "sess.jsonl").write_text("\n".join(lines) + "\n")

        inp, out, cc, cr = interactive._tokens_from_transcript(
            _epoch(0), worktree_path="/home/john/.coord/worktrees/abc",
            projects_dir=tmp_path, ended_at=_epoch(60),
        )
        # Not doubled: one turn's worth of tokens, not two.
        assert (inp, out, cc, cr) == (2, 140, 6991, 22715)

    def test_distinct_turns_both_counted(self, tmp_path: Path) -> None:
        proj = tmp_path / "-home-john--coord-worktrees-abc"
        proj.mkdir()
        lines = [
            _assistant_line(msg_id="msg_1", offset_secs=5, input_tokens=2, output_tokens=100),
            _assistant_line(msg_id="msg_2", offset_secs=15, input_tokens=3, output_tokens=200),
        ]
        (proj / "sess.jsonl").write_text("\n".join(lines) + "\n")

        inp, out, _cc, _cr = interactive._tokens_from_transcript(
            _epoch(0), worktree_path="/home/john/.coord/worktrees/abc",
            projects_dir=tmp_path, ended_at=_epoch(60),
        )
        assert (inp, out) == (5, 300)


class TestPerLineTimeBounding:
    def test_lines_outside_leg_window_excluded(self, tmp_path: Path) -> None:
        # Simulates a continuous interactive session where three legs
        # (smoke, review, smoke) run one after another and share the SAME
        # transcript file. Only the middle leg's own turn should be counted
        # when we ask for its window — not the whole file.
        proj = tmp_path / "-home-john--coord-worktrees-abc"
        proj.mkdir()
        lines = [
            # Previous leg's turn — before this leg started.
            _assistant_line(msg_id="prev", offset_secs=0, input_tokens=1, output_tokens=1_000_000),
            # This leg's own turn.
            _assistant_line(msg_id="mine", offset_secs=100, input_tokens=2, output_tokens=140),
            # Next leg's turn — after this leg ended.
            _assistant_line(msg_id="next", offset_secs=500, input_tokens=1, output_tokens=1_000_000),
        ]
        (proj / "sess.jsonl").write_text("\n".join(lines) + "\n")

        inp, out, _cc, _cr = interactive._tokens_from_transcript(
            _epoch(90), worktree_path="/home/john/.coord/worktrees/abc",
            projects_dir=tmp_path, ended_at=_epoch(110),
        )
        assert (inp, out) == (2, 140)

    def test_default_ended_at_is_now(self, tmp_path: Path, monkeypatch) -> None:
        proj = tmp_path / "-home-john--coord-worktrees-abc"
        proj.mkdir()
        # A "future" turn that hasn't happened yet from this leg's perspective.
        lines = [
            _assistant_line(msg_id="mine", offset_secs=10, input_tokens=2, output_tokens=140),
            _assistant_line(msg_id="future", offset_secs=1000, input_tokens=1, output_tokens=1_000_000),
        ]
        (proj / "sess.jsonl").write_text("\n".join(lines) + "\n")

        monkeypatch.setattr(interactive.time, "time", lambda: _epoch(20))
        inp, out, _cc, _cr = interactive._tokens_from_transcript(
            _epoch(0), worktree_path="/home/john/.coord/worktrees/abc",
            projects_dir=tmp_path,
        )
        assert (inp, out) == (2, 140)

    def test_no_tokens_returns_zero(self, tmp_path: Path) -> None:
        assert interactive._tokens_from_transcript(
            _epoch(0), worktree_path="/nonexistent", projects_dir=tmp_path,
            ended_at=_epoch(60),
        ) == (0, 0, 0, 0)


class TestPhysicalPlausibility:
    def test_realistic_rate_is_plausible(self) -> None:
        # ~2.3 tok/s — comfortably realistic.
        assert interactive._output_tokens_physically_plausible(140, 60.0) is True

    def test_generous_ceiling_boundary(self) -> None:
        ceiling = interactive.MAX_PLAUSIBLE_OUTPUT_TOKENS_PER_SECOND
        assert interactive._output_tokens_physically_plausible(int(ceiling * 60), 60.0) is True
        assert interactive._output_tokens_physically_plausible(int(ceiling * 60) + 1, 60.0) is False

    def test_issue_2129_reported_figures_are_implausible(self) -> None:
        # From the issue: a 22m45s (1365s) leg credited with 5.0M output tokens.
        assert interactive._output_tokens_physically_plausible(5_000_000, 1365) is False

    def test_zero_duration_only_plausible_for_zero_tokens(self) -> None:
        assert interactive._output_tokens_physically_plausible(0, 0.0) is True
        assert interactive._output_tokens_physically_plausible(1, 0.0) is False


class TestContextTokenPlausibility:
    """#2129 review (non-blocking finding): output_tokens had an independent
    physical-plausibility backstop but input/cache_creation/cache_read did
    not — even though they're the more visually dramatic part of the bug
    report (the 1.24B cache-read figure). Covers the added
    ``_context_tokens_physically_plausible`` backstop for those three fields.
    """

    def test_realistic_rate_is_plausible(self) -> None:
        # A generous-but-real turn: ~30k combined input/cache tokens over a
        # minute.
        assert interactive._context_tokens_physically_plausible(2, 7_000, 23_000, 60.0) is True

    def test_generous_ceiling_boundary(self) -> None:
        ceiling = interactive.MAX_PLAUSIBLE_CONTEXT_TOKENS_PER_SECOND
        assert interactive._context_tokens_physically_plausible(0, 0, int(ceiling * 60), 60.0) is True
        assert interactive._context_tokens_physically_plausible(0, 0, int(ceiling * 60) + 1, 60.0) is False

    def test_issue_2129_reported_cache_figure_is_implausible(self) -> None:
        # From the issue: a 22m45s (1365s) leg credited with a 1.24B
        # cache-read figure.
        assert interactive._context_tokens_physically_plausible(0, 0, 1_242_500_000, 1365) is False

    def test_zero_duration_only_plausible_for_zero_tokens(self) -> None:
        assert interactive._context_tokens_physically_plausible(0, 0, 0, 0.0) is True
        assert interactive._context_tokens_physically_plausible(1, 0, 0, 0.0) is False


class TestPersistInteractiveTokensSkipsImplausibleWrites:
    def test_implausible_result_is_not_persisted(self, tmp_path: Path, monkeypatch, caplog) -> None:
        import coord.state as state_mod

        calls = []
        monkeypatch.setattr(state_mod, "mark_assignment_interactive", lambda aid: None)
        monkeypatch.setattr(
            state_mod, "update_assignment_tokens",
            lambda *a, **kw: calls.append((a, kw)),
        )
        monkeypatch.setattr(
            interactive, "_tokens_from_transcript",
            lambda *a, **kw: (1, 5_000_000, 0, 0),
        )
        monkeypatch.setattr(interactive.time, "time", lambda: 1000.0)

        with caplog.at_level("WARNING"):
            interactive._persist_interactive_tokens("aid-1", 0.0, "/some/worktree")

        assert calls == []
        assert any("implausible" in r.message for r in caplog.records)

    def test_implausible_cache_only_result_is_not_persisted(
        self, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        """A regression that only inflates the cache/input fields (output
        stays realistic) must still be caught -- the scenario the
        ``_context_tokens_physically_plausible`` backstop exists for."""
        import coord.state as state_mod

        calls = []
        monkeypatch.setattr(state_mod, "mark_assignment_interactive", lambda aid: None)
        monkeypatch.setattr(
            state_mod, "update_assignment_tokens",
            lambda *a, **kw: calls.append((a, kw)),
        )
        monkeypatch.setattr(
            interactive, "_tokens_from_transcript",
            # Realistic output_tokens (would pass the output-only check) but
            # the reported 1.24B cache-read figure from the bug report.
            lambda *a, **kw: (2, 140, 0, 1_242_500_000),
        )
        monkeypatch.setattr(interactive.time, "time", lambda: 1365.0)

        with caplog.at_level("WARNING"):
            interactive._persist_interactive_tokens("aid-1", 0.0, "/some/worktree")

        assert calls == []
        assert any("implausible" in r.message for r in caplog.records)

    def test_plausible_result_is_persisted(self, tmp_path: Path, monkeypatch) -> None:
        import coord.state as state_mod

        calls = []
        monkeypatch.setattr(state_mod, "mark_assignment_interactive", lambda aid: None)
        monkeypatch.setattr(
            state_mod, "update_assignment_tokens",
            lambda *a, **kw: calls.append((a, kw)),
        )
        monkeypatch.setattr(
            interactive, "_tokens_from_transcript",
            lambda *a, **kw: (2, 140, 6991, 22715),
        )
        monkeypatch.setattr(interactive.time, "time", lambda: 1000.0)

        interactive._persist_interactive_tokens("aid-1", 940.0, "/some/worktree")

        assert len(calls) == 1
        args, kwargs = calls[0]
        assert args == ("aid-1",)
        assert kwargs == {
            "input_tokens": 2,
            "output_tokens": 140,
            "cache_creation_tokens": 6991,
            "cache_read_tokens": 22715,
        }


class TestBlackBoxCliRendering:
    """#2129 blocking review finding: a black-box test on the actual
    ``coord usage`` CLI output — not just the unit-level helpers above.

    Drives a synthetic multi-content-block, multi-leg transcript through the
    REAL pipeline: ``_tokens_from_transcript`` -> ``_persist_interactive_tokens``
    (real DB writes) -> ``fetch_usage_rows`` (real ``SqliteStore`` read, NOT
    mocked) -> ``CliRunner().invoke(main, ["usage", ...])``. Reproduces the
    vimcode #634 shape from the bug report (a ~22m45s interactive `smoke`
    leg sharing an append-only transcript file with neighboring legs, whose
    turns carry duplicate per-content-block JSONL lines) and asserts the
    rendered ``est(~)`` figure is now physically plausible instead of the
    ~30x-inflated ``~$505.5527`` / ``5.0M`` output-token figure the issue
    reported.
    """

    @pytest.fixture
    def real_file_db(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        """Point BOTH the state-write path (``coord.db.get_connection``,
        used by ``_persist_interactive_tokens``) and the usage-read path
        (``coord.dao.SqliteStore`` -> ``DB_PATH``, used by
        ``fetch_usage_rows``) at the SAME real sqlite file.

        The autouse ``coord_db`` fixture overrides ``get_connection`` with a
        ``:memory:`` connection, which is fine for tests that only exercise
        one side — but ``SqliteStore`` always opens its own separate
        ``mode=ro`` connection, which can never see another connection's
        ``:memory:`` database. A real file is the only way for a value
        written via the state-write path to actually be visible to the
        CLI's read path in the same test, which is the whole point of a
        black-box test here.
        """
        import coord.dao as dao_mod
        import coord.db as db_mod

        db_path = tmp_path / "usage-blackbox.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        db_mod._ensure_schema(conn)
        db_mod.override_connection(conn)
        monkeypatch.setattr(dao_mod, "DB_PATH", db_path)
        yield db_path
        db_mod.close()

    def test_interactive_leg_renders_plausible_est_not_30x_inflated(
        self,
        real_file_db: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        valid_config_yaml: str,
    ) -> None:
        from coord.db import get_connection

        assignment_id = "aid-634-smoke"
        worktree_path = "/home/john/.coord/worktrees/vimcode634"
        started_at = _epoch(0)
        duration = 1365.0  # 22m45s -- exactly the leg length from the bug report
        finished_at = started_at + duration

        conn = get_connection()
        conn.execute(
            "INSERT INTO assignments (assignment_id, machine_name, repo_name, "
            "issue_number, issue_title, status, type, model, dispatched_at, "
            "finished_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                assignment_id, "laptop", "vimcode", 634, "Fix the thing",
                "done", "smoke", "sonnet", started_at, finished_at,
            ),
        )
        conn.commit()

        # A synthetic transcript reproducing BOTH original bugs at once:
        # whole-session attribution (a previous leg's and a following leg's
        # turns sharing this worktree's one append-only transcript file) and
        # per-content-block double counting (two JSONL lines — a `thinking`
        # block and a `tool_use` block -- for the same message id).
        projects_dir = tmp_path / "claude-projects"
        proj = projects_dir / worktree_path.replace("/", "-")
        proj.mkdir(parents=True)
        lines = [
            # A previous leg's turn in the SAME file, well before this leg
            # started -- must NOT be attributed to this leg.
            _assistant_line(
                msg_id="prev-leg", offset_secs=-100,
                input_tokens=5, output_tokens=2_000_000,
                cache_creation=500_000, cache_read=600_000_000,
            ),
            # This leg's own turn -- two JSONL lines (thinking + tool_use)
            # for the SAME message id, as Claude Code actually writes them.
            _assistant_line(
                msg_id="mine-1", offset_secs=10, input_tokens=2, output_tokens=70,
                cache_creation=6991, cache_read=22715, content_type="thinking",
            ),
            _assistant_line(
                msg_id="mine-1", offset_secs=10.25, input_tokens=2, output_tokens=70,
                cache_creation=6991, cache_read=22715, content_type="tool_use",
            ),
            _assistant_line(
                msg_id="mine-2", offset_secs=800, input_tokens=3, output_tokens=90,
                cache_creation=1000, cache_read=5000,
            ),
            # A following leg's turn, well after this leg ended -- must NOT
            # be attributed to this leg either.
            _assistant_line(
                msg_id="next-leg", offset_secs=duration + 100,
                input_tokens=4, output_tokens=3_000_000,
                cache_creation=400_000, cache_read=700_000_000,
            ),
        ]
        (proj / "sess.jsonl").write_text("\n".join(lines) + "\n")

        # Run the REAL persist path (mark_assignment_interactive +
        # _tokens_from_transcript + the physical-plausibility backstop +
        # update_assignment_tokens), pointed at our synthetic transcript dir
        # via a bound `projects_dir` in place of the real `~/.claude/projects`
        # -- functools.partial binds one kwarg on the REAL function, it does
        # not replace the parsing/dedup/time-bounding logic under test.
        bound_tokens_from_transcript = functools.partial(
            interactive._tokens_from_transcript, projects_dir=projects_dir
        )
        monkeypatch.setattr(interactive, "_tokens_from_transcript", bound_tokens_from_transcript)
        monkeypatch.setattr(interactive.time, "time", lambda: finished_at)

        interactive._persist_interactive_tokens(assignment_id, started_at, worktree_path)

        # Sanity check on the DB row directly: one turn's worth of tokens
        # per unique message id, only from this leg's own window -- NOT the
        # 2M/3M neighboring-leg figures and NOT doubled by the duplicate
        # content-block line.
        row = conn.execute(
            "SELECT output_tokens, is_interactive FROM assignments WHERE assignment_id = ?",
            (assignment_id,),
        ).fetchone()
        assert row["is_interactive"] == 1
        assert row["output_tokens"] == 160  # 70 (deduped) + 90

        cfg_path = tmp_path / "coordinator.yml"
        cfg_path.write_text(valid_config_yaml)

        # ── the actual black-box assertion: real CLI output, real DB read ──
        drill_result = CliRunner().invoke(
            main, ["usage", "--config", str(cfg_path), "--issue", "634"]
        )
        assert drill_result.exit_code == 0, drill_result.output

        smoke_line = next(
            line for line in drill_result.output.splitlines() if line.startswith("smoke")
        )
        # The exact inflated figures from the #2129 bug report must be gone.
        assert "5.0M" not in smoke_line
        assert "1242" not in smoke_line
        assert "$505" not in smoke_line

        est_field = smoke_line.split()[4]
        assert est_field.startswith("~$"), smoke_line
        est_value = float(est_field.lstrip("~$"))
        # 160 output + ~8k cache-creation + ~28k cache-read tokens at sonnet
        # list pricing prices to a few cents -- nowhere near the physically
        # impossible ~$505.55/leg the bug report showed.
        assert est_value < 1.0, smoke_line

        by_issue_result = CliRunner().invoke(
            main, ["usage", "--config", str(cfg_path), "--by-issue"]
        )
        assert by_issue_result.exit_code == 0, by_issue_result.output
        assert "#634" in by_issue_result.output
        assert "$505" not in by_issue_result.output
        assert "$2822" not in by_issue_result.output
