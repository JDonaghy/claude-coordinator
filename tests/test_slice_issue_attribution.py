"""#1553: oracle-loop slice work must be attributed to the CHILD issue.

``coord acceptance author <repo> <tracking> --issue N`` dispatches against
``<tracking>`` because every JIT slice of a milestone shares one branch and
one PR — so ``issue_number`` cannot be the child. The child lives in
``for_issue_number``, and *that* is the attribution field: it is what
"is this issue being worked on?", "what did this issue cost?", and the
work-followup guard must key on.

Two invariants this module pins:

1. **Every row in a slice's chain carries the child.** Not just the
   originating ``test-author`` dispatch — its review, its ``[fix-N]``
   bounces, its smoke, and a retry all inherit it, either explicitly at the
   dispatch site or via the parent lookup in
   ``coord.state._record_dispatched_assignment_local``.
2. **The tracking issue keeps its parent link.** ``issue_number`` still
   points at the epic, so the epic can still roll up its children and the
   shared branch/PR bookkeeping is untouched.
"""

from __future__ import annotations

from coord.models import Assignment, effective_issue_number
from coord.state import record_dispatched_assignment


# ── effective_issue_number ─────────────────────────────────────────────────


def _slice_assignment(**kw) -> Assignment:
    base = dict(
        machine_name="laptop",
        repo_name="api",
        issue_number=1120,
        issue_title="[test-author] ms-38 slice #1124",
        type="test-author",
        for_issue_number=1124,
    )
    base.update(kw)
    return Assignment(**base)


def test_effective_issue_number_prefers_the_child() -> None:
    assert effective_issue_number(_slice_assignment()) == 1124


def test_effective_issue_number_falls_back_to_issue_number() -> None:
    """Ordinary work (no slice correlation) is completely unchanged."""
    a = Assignment(
        machine_name="laptop", repo_name="api", issue_number=42,
        issue_title="Fix auth", type="work",
    )
    assert effective_issue_number(a) == 42


def test_effective_issue_number_accepts_a_wire_dict() -> None:
    """The board-JSON/DB row shape shares one definition with the dataclass."""
    assert effective_issue_number({"issue_number": 1120, "for_issue_number": 1124}) == 1124
    assert effective_issue_number({"issue_number": 42}) == 42
    assert effective_issue_number({"issue_number": 42, "for_issue_number": None}) == 42


def test_effective_issue_number_tolerates_a_junk_row() -> None:
    """Display/aggregation paths must degrade, never raise."""
    assert effective_issue_number({}) == 0
    assert effective_issue_number({"issue_number": "", "for_issue_number": ""}) == 0
    assert effective_issue_number({"issue_number": "42"}) == 42


def test_tracking_issue_relationship_is_preserved() -> None:
    """The parent link must survive: issue_number STAYS the tracking issue.

    The epic rolls its children up through this field (and the shared
    branch/PR bookkeeping depends on it), so re-attributing the slice must
    not orphan the row from its epic.
    """
    a = _slice_assignment()
    assert a.issue_number == 1120
    assert effective_issue_number(a) == 1124


# ── dispatch-site propagation ──────────────────────────────────────────────


def _record(aid: str, **kw) -> None:
    base = dict(
        assignment_id=aid,
        machine_name="laptop",
        repo_name="api",
        issue_number=1120,
        issue_title="t",
        type="work",
    )
    base.update(kw)
    record_dispatched_assignment(assignment=Assignment(**base), repo_github="acme/api")


def _for_issue(coord_db, aid: str):
    row = coord_db.execute(
        "SELECT for_issue_number FROM assignments WHERE assignment_id = ?", (aid,)
    ).fetchone()
    return row["for_issue_number"] if row is not None else None


def test_followup_inherits_slice_attribution_from_its_parent(coord_db) -> None:
    """A smoke/review/pr-helper dispatched off a slice books to the child.

    The derived dispatchers pass the parent's assignment id as
    ``review_of_assignment_id``; the write path resolves the parent's
    ``for_issue_number`` from it. This is what keeps
    ``coord/smoke.py``'s dispatcher correct without it having to know
    about slices at all.
    """
    _record("author-1", type="test-author", for_issue_number=1124)
    _record("smoke-1", type="smoke", review_of_assignment_id="author-1")
    assert _for_issue(coord_db, "smoke-1") == 1124
    # ...and the parent link is untouched.
    row = coord_db.execute(
        "SELECT issue_number FROM assignments WHERE assignment_id = 'smoke-1'"
    ).fetchone()
    assert row["issue_number"] == 1120


def test_followup_inheritance_is_transitive_down_the_chain(coord_db) -> None:
    """review → fix-1 → smoke-of-fix-1 all land on the child.

    #1553 observed six rows for one child (author, review, fix-1, fix-2,
    review-of-fix-2, smoke) and every one of them booked to the epic.
    """
    _record("author-1", type="test-author", for_issue_number=1124)
    _record("review-1", type="review", review_of_assignment_id="author-1")
    _record("fix-1", type="test-author", review_of_assignment_id="review-1")
    _record("smoke-1", type="smoke", review_of_assignment_id="fix-1")
    for aid in ("author-1", "review-1", "fix-1", "smoke-1"):
        assert _for_issue(coord_db, aid) == 1124, aid


def test_explicit_for_issue_number_wins_over_the_parent_lookup(coord_db) -> None:
    _record("author-1", type="test-author", for_issue_number=1124)
    _record(
        "other-1", type="review", review_of_assignment_id="author-1", for_issue_number=1125
    )
    assert _for_issue(coord_db, "other-1") == 1125


def test_ordinary_followup_gets_no_slice_attribution(coord_db) -> None:
    """A review of plain work stays NULL — non-slice work is unchanged."""
    _record("work-1", type="work")
    _record("review-1", type="review", review_of_assignment_id="work-1")
    assert _for_issue(coord_db, "review-1") is None


def test_followup_of_a_missing_parent_is_not_an_error(coord_db) -> None:
    """A dangling review_of_assignment_id resolves to NULL, not a crash."""
    _record("review-1", type="review", review_of_assignment_id="does-not-exist")
    assert _for_issue(coord_db, "review-1") is None


def test_redispatch_does_not_clear_the_slice_attribution(coord_db) -> None:
    """The ON CONFLICT COALESCE still protects a recorded correlation."""
    _record("author-1", type="test-author", for_issue_number=1124)
    _record("author-1", type="test-author")  # re-dispatch, no for_issue_number
    assert _for_issue(coord_db, "author-1") == 1124


def _review_config():
    from coord.config import Config, ReviewsConfig
    from coord.models import Machine, Repo

    return Config(
        repos=[Repo(name="api", github="acme/api", depends_on=[], default_branch="main")],
        machines=[
            Machine(name="laptop", host="laptop.tail", repos=["api"],
                    repo_paths={"api": "/w"}, capabilities=[]),
            Machine(name="server", host="server.tail", repos=["api"],
                    repo_paths={"api": "/s"}, capabilities=[]),
        ],
        reviews=ReviewsConfig(enabled=True, auto_dispatch=True),
    )


class _OkClient:
    def post(self, url, *, json, timeout):
        class _Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"assignment_id": "rev-new", "id": "rev-new"}

        return _Resp()


def _dispatch_review_for(completed):
    from coord.models import Board
    from coord.review import dispatch_review

    board = Board()
    result = dispatch_review(
        completed, board, _review_config(),
        http_client=_OkClient(),
        pr_lookup=lambda repo_github, **kw: {"number": 1, "url": "u", "existed": True},
        claude_md_reader=lambda p: None,
        issue_body_fetcher=lambda repo, num: "",
    )
    assert result is not None
    return result


def test_dispatch_review_of_a_slice_books_the_review_to_the_child() -> None:
    """The in-memory board object carries it too, not just the DB row.

    ``reconcile``/``notify`` read ``board.active`` directly, so the guard
    and the Pipeline both need the live object to be right — the SQL
    inheritance alone would leave them a stale view until the next reload.
    """
    completed = _slice_assignment(
        assignment_id="author-1", status="done", branch="ms-38-acceptance",
    )
    review = _dispatch_review_for(completed)
    assert review.for_issue_number == 1124
    assert review.issue_number == 1120  # parent link preserved
    assert effective_issue_number(review) == 1124


def test_dispatch_review_defers_when_a_sibling_round_has_an_active_retry() -> None:
    """#1553 regression: the #459 guard must key on the effective issue.

    A different round for the SAME child (#1124) is actively being retried
    (``type="work"``, the generic ``coord retry`` path — see
    ``coord.reconcile._reassign``): it carries ``for_issue_number=1124`` but
    keeps ``issue_number`` as the shared tracking issue, exactly like
    ``author-1`` below. Before the call-site fix, ``dispatch_review`` passed
    the raw (tracking) ``issue_number`` to ``has_active_work_followup``,
    which internally compares by the effective issue — an effective-vs-raw
    mismatch that let the guard silently stop firing. The review must be
    deferred (``dispatch_review`` returns ``None``) rather than dispatched
    against code that's mid-rewrite.
    """
    from coord.models import Assignment, Board
    from coord.review import dispatch_review

    active_retry = Assignment(
        assignment_id="retry-1124",
        machine_name="laptop",
        repo_name="api",
        issue_number=1120,
        issue_title="[fix] retry of slice #1124",
        type="work",
        status="running",
        for_issue_number=1124,
    )
    board = Board(active=[active_retry])
    completed = _slice_assignment(
        assignment_id="author-1", status="done", branch="ms-38-acceptance",
    )
    result = dispatch_review(
        completed, board, _review_config(),
        http_client=_OkClient(),
        pr_lookup=lambda repo_github, **kw: {"number": 1, "url": "u", "existed": True},
        claude_md_reader=lambda p: None,
        issue_body_fetcher=lambda repo, num: "",
    )
    assert result is None


def test_dispatch_review_does_not_defer_for_an_unrelated_sibling_child() -> None:
    """The guard must not over-match: an active retry for a DIFFERENT child
    under the same tracking issue must not block this child's review."""
    from coord.models import Assignment, Board
    from coord.review import dispatch_review

    active_retry = Assignment(
        assignment_id="retry-1125",
        machine_name="laptop",
        repo_name="api",
        issue_number=1120,
        issue_title="[fix] retry of slice #1125",
        type="work",
        status="running",
        for_issue_number=1125,  # different child from `completed` below
    )
    board = Board(active=[active_retry])
    completed = _slice_assignment(
        assignment_id="author-1", status="done", branch="ms-38-acceptance",
    )
    result = dispatch_review(
        completed, board, _review_config(),
        http_client=_OkClient(),
        pr_lookup=lambda repo_github, **kw: {"number": 1, "url": "u", "existed": True},
        claude_md_reader=lambda p: None,
        issue_body_fetcher=lambda repo, num: "",
    )
    assert result is not None
    assert result.for_issue_number == 1124


def test_dispatch_review_of_ordinary_work_is_unchanged() -> None:
    a = Assignment(
        machine_name="laptop", repo_name="api", issue_number=16,
        issue_title="X", status="done", branch="issue-16-fix",
        assignment_id="work-1", type="work",
    )
    review = _dispatch_review_for(a)
    assert review.for_issue_number is None
    assert effective_issue_number(review) == 16


# ── cost attribution (#1117 / milestone #37) ───────────────────────────────


def test_usage_rows_roll_up_cost_to_the_child() -> None:
    """`coord usage --by issue` groups a slice leg under the child issue."""
    from coord.usage_rollup import row_issue_number

    assert row_issue_number({"issue_number": 1120, "for_issue_number": 1124}) == 1124
    assert row_issue_number({"issue_number": 1120}) == 1120
    assert row_issue_number({"issue_number": 1120, "for_issue_number": None}) == 1120


def test_usage_aggregate_books_slice_spend_to_the_child() -> None:
    """End-to-end through the aggregator: $7.90 of #1124 must not land on #1120."""
    from coord.usage_rollup import TimeWindow, aggregate

    rows = [
        {
            "repo_name": "api", "issue_number": 1120, "for_issue_number": 1124,
            "type": "test-author", "model": "sonnet", "status": "done",
            "cost_usd": 7.90, "dispatched_at": 1000.0, "finished_at": 2000.0,
        },
        {
            "repo_name": "api", "issue_number": 1120,
            "type": "mock-author", "model": "sonnet", "status": "done",
            "cost_usd": 1.10, "dispatched_at": 1000.0, "finished_at": 2000.0,
        },
    ]
    result = aggregate(rows, by="issue", window=TimeWindow(), pricing={})
    by_key = {g["key"]: g for g in result["groups"]}
    assert by_key[1124]["cost_total"] == 7.90
    assert by_key[1120]["cost_total"] == 1.10


def test_assignment_usage_is_keyed_on_the_child(tmp_path) -> None:
    """`coord.usage._assignment_to_usage` seeds the per-issue usage row."""
    from coord.usage import _assignment_to_usage

    usage = _assignment_to_usage(_slice_assignment(assignment_id="a1"), logs_dir=tmp_path)
    assert usage.issue_number == 1124


def test_assignment_usage_unchanged_for_ordinary_work(tmp_path) -> None:
    from coord.usage import _assignment_to_usage

    a = Assignment(
        machine_name="laptop", repo_name="api", issue_number=42,
        issue_title="Fix auth", type="work", assignment_id="a2",
    )
    assert _assignment_to_usage(a, logs_dir=tmp_path).issue_number == 42
