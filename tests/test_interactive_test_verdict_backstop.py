"""#923: interactive Test-stage verdict backstop.

Acceptance scenarios:
(a) Smoke session exits, agent did NOT run `coord test` → operator is
    prompted (TTY) → work row test_state=passed on the daemon /board.
(b) Idempotency: agent DID run `coord test --passed` before exiting →
    `_prompt_and_relay_test_verdict` detects the already-recorded verdict
    and returns True WITHOUT prompting the operator again.
(c) Non-TTY (headless) + no pre-recorded verdict → hint printed, False returned,
    no record_test_verdict call made.
(d) TTY + operator chooses [f]ailed + supplies a reason → reason propagated.
(e) TTY + operator skips [s] → False returned, no record_test_verdict call.
"""

from __future__ import annotations

import pytest


# ── shared helpers ─────────────────────────────────────────────────────────────


def _make_work_stub(test_state: str | None):
    """Return a minimal assignment stub with the given test_state."""

    class _Stub:
        pass

    s = _Stub()
    s.test_state = test_state
    return s


class _Board:
    """Minimal Board stub with find_by_id."""

    def __init__(self, stubs: dict):
        self._stubs = stubs

    def find_by_id(self, aid: str):
        return self._stubs.get(aid)


def _call_relay(
    monkeypatch,
    *,
    work_assignment_id: str = "work-923",
    smoke_assignment_id: str = "smoke-923",
    issue_number: int = 923,
    board_work_stub=None,
    **extra,
):
    """Invoke _prompt_and_relay_test_verdict with sensible defaults.

    Patches:
    - board_service.read_board → returns a Board with board_work_stub at
      work_assignment_id (or an empty board if None)
    - record_test_verdict → captures the call
    Returns (captured_record_call_kwargs | None, return_value).
    """
    from coord.commands.review import _prompt_and_relay_test_verdict

    # Patch the board so idempotency check sees the stub.
    _board = _Board({work_assignment_id: board_work_stub} if board_work_stub else {})
    monkeypatch.setattr(
        "coord.commands.review._read_board_tv",
        lambda: _board,
        raising=False,
    )
    # Use the real import path inside the function (lazy import inside try).
    # Patch via the module path the function resolves to.
    monkeypatch.setattr(
        "coord.board_service.read_board",
        lambda: _board,
        raising=False,
    )

    captured: dict = {}

    def _fake_record_tv(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(
        "coord.state.record_test_verdict",
        _fake_record_tv,
        raising=False,
    )

    ok = _prompt_and_relay_test_verdict(
        work_assignment_id=work_assignment_id,
        smoke_assignment_id=smoke_assignment_id,
        repo_name="claude-coordinator",
        repo_github="JDonaghy/claude-coordinator",
        issue_number=issue_number,
        machine_name="precision",
        verdict_cmd_hint="coord test --passed work-923",
        **extra,
    )
    return captured or None, ok


# ── (a) TTY, no prior verdict → operator prompted → passed recorded ────────────


class TestTtyNoVerdictThenPassed:
    """(a) Main scenario from the issue: session exits without coord test,
    operator chooses [p]assed at the backstop prompt."""

    def test_passed_verdict_recorded(self, monkeypatch) -> None:
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)

        # Board shows no prior test_state on the WORK row.
        work_stub = _make_work_stub(test_state=None)

        # Operator presses [p].
        monkeypatch.setattr("click.prompt", lambda *a, **kw: "p")

        captured, ok = _call_relay(monkeypatch, board_work_stub=work_stub)

        assert ok is True
        assert captured is not None
        assert captured["assignment_id"] == "work-923"
        assert captured["test_state"] == "passed"
        assert captured["test_reason"] is None
        # Legacy mirror columns.
        assert captured["smoke_test"] == "pass"

    def test_work_row_not_found_still_prompts(self, monkeypatch) -> None:
        """No work row on the board → board returns None → still prompts."""
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)

        # No stub registered → find_by_id returns None.
        monkeypatch.setattr("click.prompt", lambda *a, **kw: "p")

        captured, ok = _call_relay(monkeypatch, board_work_stub=None)

        assert ok is True
        assert captured is not None
        assert captured["test_state"] == "passed"


# ── (b) Idempotency: agent already ran coord test ─────────────────────────────


class TestIdempotent:
    """(b) When the WORK row already has test_state set the backstop must NOT
    prompt or call record_test_verdict again."""

    def test_already_passed_no_prompt(self, monkeypatch) -> None:
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)

        work_stub = _make_work_stub(test_state="passed")

        # click.prompt must NOT be called — raise if it is.
        monkeypatch.setattr(
            "click.prompt",
            lambda *a, **kw: pytest.fail(
                "click.prompt called despite test_state already recorded"
            ),
        )

        captured, ok = _call_relay(monkeypatch, board_work_stub=work_stub)

        assert ok is True
        assert captured is None  # record_test_verdict NOT called

    def test_already_failed_no_prompt(self, monkeypatch) -> None:
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)

        work_stub = _make_work_stub(test_state="failed")

        monkeypatch.setattr(
            "click.prompt",
            lambda *a, **kw: pytest.fail(
                "click.prompt called despite test_state already recorded"
            ),
        )

        captured, ok = _call_relay(monkeypatch, board_work_stub=work_stub)

        assert ok is True
        assert captured is None

    def test_already_skipped_no_prompt(self, monkeypatch) -> None:
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)

        work_stub = _make_work_stub(test_state="skipped")

        monkeypatch.setattr(
            "click.prompt",
            lambda *a, **kw: pytest.fail(
                "click.prompt called despite test_state already recorded"
            ),
        )

        captured, ok = _call_relay(monkeypatch, board_work_stub=work_stub)

        assert ok is True
        assert captured is None

    def test_empty_test_state_still_prompts(self, monkeypatch) -> None:
        """An empty string test_state is treated as 'not set' → prompt fires."""
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)

        work_stub = _make_work_stub(test_state="")

        answers = iter(["p"])
        monkeypatch.setattr("click.prompt", lambda *a, **kw: next(answers))

        captured, ok = _call_relay(monkeypatch, board_work_stub=work_stub)

        assert ok is True
        assert captured is not None
        assert captured["test_state"] == "passed"


# ── (c) Non-TTY, no prior verdict ─────────────────────────────────────────────


class TestNonTtyNoVerdict:
    """(c) Headless caller + no recorded verdict → hint only, no record call."""

    def test_headless_no_record_call(self, monkeypatch) -> None:
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)

        work_stub = _make_work_stub(test_state=None)

        # prompt must NOT be called.
        monkeypatch.setattr(
            "click.prompt",
            lambda *a, **kw: pytest.fail(
                "click.prompt must not be called in non-TTY mode"
            ),
        )

        captured, ok = _call_relay(monkeypatch, board_work_stub=work_stub)

        assert ok is False
        assert captured is None  # record_test_verdict NOT called


# ── (d) TTY + failed + reason propagated ──────────────────────────────────────


class TestTtyFailedWithReason:
    """(d) Operator chooses [f]ailed and supplies a failure reason."""

    def test_failed_reason_propagated(self, monkeypatch) -> None:
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)

        work_stub = _make_work_stub(test_state=None)

        _reason = "build produced wrong binary; expected coord-tui to show 3 panels"
        answers = iter(["f", _reason])
        monkeypatch.setattr("click.prompt", lambda *a, **kw: next(answers))

        captured, ok = _call_relay(monkeypatch, board_work_stub=work_stub)

        assert ok is True
        assert captured is not None
        assert captured["test_state"] == "failed"
        assert captured["test_reason"] == _reason
        assert captured["smoke_test"] == "fail"
        assert captured["smoke_test_reason"] == _reason

    def test_failed_empty_reason_stored_as_none(self, monkeypatch) -> None:
        """Operator presses Enter (empty reason) → test_reason=None."""
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)

        work_stub = _make_work_stub(test_state=None)

        # fail + empty reason
        answers = iter(["f", ""])
        monkeypatch.setattr("click.prompt", lambda *a, **kw: next(answers))

        captured, ok = _call_relay(monkeypatch, board_work_stub=work_stub)

        assert ok is True
        assert captured is not None
        assert captured["test_state"] == "failed"
        assert captured["test_reason"] is None


# ── (e) TTY + skip → no record ────────────────────────────────────────────────


class TestTtySkip:
    """(e) Operator presses [s]kip → no verdict recorded."""

    def test_skip_no_record_call(self, monkeypatch) -> None:
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)

        work_stub = _make_work_stub(test_state=None)

        monkeypatch.setattr("click.prompt", lambda *a, **kw: "s")

        captured, ok = _call_relay(monkeypatch, board_work_stub=work_stub)

        assert ok is False
        assert captured is None


# ── DB integration: real test_state written and read back ────────────────────


class TestDbRoundTrip:
    """Verify that record_test_verdict writes to the real (in-memory) DB and
    the idempotency gate subsequently suppresses re-prompting."""

    def test_verdict_written_and_idempotency_suppresses_reprompt(
        self, monkeypatch, coord_db
    ) -> None:
        """Full round-trip through the real DB (in-memory from conftest).

        Step 1: insert a WORK row with no test_state.
        Step 2: call the backstop (TTY, [p]assed).
        Step 3: call the backstop AGAIN → idempotency gate fires, no re-prompt.
        """
        import time as _time

        from coord.state import _record_test_verdict_local

        # Insert a minimal work assignment into the in-memory DB.
        coord_db.execute(
            """INSERT INTO assignments
               (assignment_id, machine_name, repo_name, issue_number,
                issue_title, status, type, dispatched_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                "work-db-923",
                "precision",
                "claude-coordinator",
                923,
                "test issue",
                "done",
                "work",
                _time.time(),
            ),
        )
        coord_db.commit()

        # Step 2: call backstop — operator presses [p].
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)

        prompt_calls: list = []

        def _spy_prompt(*a, **kw):
            prompt_calls.append(kw.get("default", "?"))
            return "p"

        monkeypatch.setattr("click.prompt", _spy_prompt)

        # Use the real local record_test_verdict path (board_service=None → local).
        from coord.commands.review import _prompt_and_relay_test_verdict

        # Patch board so idempotency check reads from real DB via our stub.
        class _RealBoardStub:
            """Reads test_state from the real in-memory DB."""

            def find_by_id(self, aid: str):
                row = coord_db.execute(
                    "SELECT test_state FROM assignments WHERE assignment_id=?",
                    (aid,),
                ).fetchone()
                if row is None:
                    return None

                class _S:
                    pass

                s = _S()
                s.test_state = row["test_state"]
                return s

        monkeypatch.setattr(
            "coord.board_service.read_board",
            lambda: _RealBoardStub(),
        )
        # Patch record_test_verdict to use the local writer (no daemon).
        monkeypatch.setattr(
            "coord.state.record_test_verdict",
            lambda **kw: _record_test_verdict_local(**kw),
        )

        ok1 = _prompt_and_relay_test_verdict(
            work_assignment_id="work-db-923",
            smoke_assignment_id="smoke-db-923",
            repo_name="claude-coordinator",
            repo_github="JDonaghy/claude-coordinator",
            issue_number=923,
            machine_name="precision",
            verdict_cmd_hint="coord test --passed work-db-923",
        )
        assert ok1 is True
        assert len(prompt_calls) == 1  # prompt fired exactly once

        # Verify DB was written.
        row = coord_db.execute(
            "SELECT test_state FROM assignments WHERE assignment_id='work-db-923'"
        ).fetchone()
        assert row["test_state"] == "passed"

        # Step 3: call again — idempotency gate should suppress re-prompt.
        prompt_calls.clear()
        ok2 = _prompt_and_relay_test_verdict(
            work_assignment_id="work-db-923",
            smoke_assignment_id="smoke-db-923",
            repo_name="claude-coordinator",
            repo_github="JDonaghy/claude-coordinator",
            issue_number=923,
            machine_name="precision",
            verdict_cmd_hint="coord test --passed work-db-923",
        )
        assert ok2 is True
        assert len(prompt_calls) == 0  # no second prompt
