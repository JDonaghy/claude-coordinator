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

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from coord import interactive

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
