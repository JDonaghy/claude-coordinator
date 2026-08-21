"""#1982: the portal sync bridge — pull, push, ordering, replay, heartbeat.

The tests that matter most here are the ORDERING ones. The portal accepts a
push of `awaiting-signoff` with no design round attached and immediately
emails the customer "your design is ready — approve it or tell us what to
change", landing them on an empty screen (measured in production 2026-08-14,
dogfood #835). `status` and `design_round` are separate coord-owned fields
and the portal enforces no ordering between them because it cannot. So the
ordering is ours to guarantee, and these tests are the guarantee.
"""

from __future__ import annotations

import pytest

from coord import portal_store, portal_sync
from coord.portal_bridge import PortalBridgeError, PushResult
from coord.portal_sync import (
    PortalSyncError,
    enqueue_design_round,
    enqueue_preview,
    enqueue_question,
    enqueue_status,
    sync_tick,
)

SUB = "sub-001"


class FakeClient:
    """Records every call; scripted pull pages and push outcomes."""

    def __init__(
        self,
        pages: list[dict] | None = None,
        *,
        push_outcomes: dict[str, str] | None = None,
        push_error: Exception | None = None,
        pull_error: Exception | None = None,
        heartbeat_error: Exception | None = None,
        heartbeat_ok: bool = True,
    ):
        self.pages = list(pages or [{"events": [], "cursor": None, "has_more": False}])
        self.push_outcomes = push_outcomes or {}
        self.push_error = push_error
        self.pull_error = pull_error
        self.heartbeat_error = heartbeat_error
        self.heartbeat_ok = heartbeat_ok
        self.pull_calls: list[str | None] = []
        self.pushes: list[dict] = []
        self.heartbeats = 0

    def pull(self, cursor=None, limit=None):
        self.pull_calls.append(cursor)
        if self.pull_error:
            raise self.pull_error
        if not self.pages:
            return {"events": [], "cursor": cursor, "has_more": False}
        return self.pages.pop(0)

    def push(self, updates):
        if self.push_error:
            raise self.push_error
        results = []
        for u in updates:
            self.pushes.append(u.to_wire())
            key = next(iter(u.fields))  # rows are single-field by construction
            outcome = self.push_outcomes.get(key, "applied")
            results.append(
                PushResult(
                    submission_id=u.submission_id,
                    outcome=outcome,
                    reason=None if outcome != "rejected" else f"not_owned:{key}",
                )
            )
        return results

    def heartbeat(self, at=None):
        self.heartbeats += 1
        if self.heartbeat_error:
            raise self.heartbeat_error
        return self.heartbeat_ok

    # convenience for assertions
    @property
    def pushed_kinds(self) -> list[str]:
        return [next(iter(p["fields"])) for p in self.pushes]


def _design(round_no: int = 1) -> dict:
    return {"round": round_no, "outcome": "a thing", "bundle_url": "r2://b/1"}


# ── the ordering rule (#835) ────────────────────────────────────────────────


def test_enqueue_status_refuses_awaiting_signoff_with_no_design_round():
    with pytest.raises(PortalSyncError) as exc:
        enqueue_status(SUB, "awaiting-signoff")
    assert "design_round" in str(exc.value)
    assert portal_store.pending_outbox() == []


def test_enqueue_status_refuses_needs_input_with_no_question():
    with pytest.raises(PortalSyncError) as exc:
        enqueue_status(SUB, "needs-input")
    assert "question" in str(exc.value)


def test_non_announcing_status_needs_no_prerequisite():
    row = enqueue_status(SUB, "in-design")
    assert row.requires_kind == ""
    assert row.announces == ""


def test_design_round_is_pushed_before_the_status_that_announces_it():
    enqueue_design_round(SUB, _design())
    enqueue_status(SUB, "awaiting-signoff")
    client = FakeClient()

    result = sync_tick(client=client)

    assert client.pushed_kinds == ["design_round", "status"]
    assert result.applied == 2
    assert result.held == 0
    # ...and they were separate calls, so the design round was CONFIRMED
    # applied before the announcement was even sent.
    assert len(client.pushes) == 2


def test_announcement_is_held_while_its_design_round_is_unconfirmed():
    """The crash-window case: the design round push fails, so the mail must not go."""
    enqueue_design_round(SUB, _design())
    enqueue_status(SUB, "awaiting-signoff")
    client = FakeClient(push_error=PortalBridgeError("portal is down"))

    result = sync_tick(client=client)

    assert client.pushes == []  # nothing landed
    assert result.applied == 0
    assert result.errors  # the failure is surfaced, not swallowed

    # Next tick, the portal is back: the design round goes first, then the
    # announcement — same revisions, no duplicates.
    client2 = FakeClient()
    result2 = sync_tick(client=client2)
    assert client2.pushed_kinds == ["design_round", "status"]
    assert result2.applied == 2


def test_announcement_stays_held_when_its_design_round_is_rejected():
    """A rejected design round is terminal — the announcement must never go."""
    enqueue_design_round(SUB, _design())
    enqueue_status(SUB, "awaiting-signoff")
    client = FakeClient(push_outcomes={"design_round": "rejected"})

    result = sync_tick(client=client)

    assert client.pushed_kinds == ["design_round"]
    assert result.rejected == 1
    assert result.applied == 0

    # ...and it stays held on every subsequent tick, rather than eventually
    # leaking out.
    client2 = FakeClient()
    result2 = sync_tick(client=client2)
    assert client2.pushes == []
    assert result2.held == 1


def test_already_applied_on_a_first_attempt_is_not_confirmation():
    """#835 from the other side: the portal ignored it, so nothing landed.

    `already_applied` means "at or below my watermark, discarded". On a
    row's FIRST attempt there was no earlier send it could be acknowledging,
    so it means coord's revision allocator is behind the portal — the design
    round was NOT stored, and treating it as confirmed would release the
    `awaiting-signoff` mail toward an empty screen.
    """
    enqueue_design_round(SUB, _design())
    enqueue_status(SUB, "awaiting-signoff")
    client = FakeClient(push_outcomes={"design_round": "already_applied"})

    result = sync_tick(client=client)

    assert client.pushed_kinds == ["design_round"]  # the mail never went
    assert result.applied == 0
    assert portal_store.get_submission(SUB).design_round == 0

    # ...and the row was re-numbered above the allocator so the retry can
    # clear the portal's watermark.
    row = portal_store.outbox_for_submission(SUB)[0]
    assert row.state == portal_store.STATE_PENDING
    assert row.revision > 1


def test_already_applied_on_a_retry_is_confirmation():
    """A resend of a row we really did send: the lost-response case."""
    enqueue_design_round(SUB, _design())
    enqueue_status(SUB, "awaiting-signoff")

    # Attempt 1 fails in transport — the portal may well have stored it.
    sync_tick(client=FakeClient(push_error=PortalBridgeError("timeout")))
    # Attempt 2 comes back already_applied, which now means what it says.
    client = FakeClient(push_outcomes={"design_round": "already_applied"})
    result = sync_tick(client=client)

    assert client.pushed_kinds == ["design_round", "status"]
    assert result.applied == 2


def test_reallocation_converges_and_then_the_announcement_goes():
    enqueue_design_round(SUB, _design())
    enqueue_status(SUB, "awaiting-signoff")
    sync_tick(client=FakeClient(push_outcomes={"design_round": "already_applied"}))

    client = FakeClient()  # the re-numbered revision now clears the watermark
    result = sync_tick(client=client)
    assert client.pushed_kinds == ["design_round", "status"]
    assert result.applied == 2
    assert portal_store.get_submission(SUB).last_status == "awaiting-signoff"


def test_second_question_cannot_ride_on_the_first_questions_confirmation():
    enqueue_question(SUB, "which colour?")
    enqueue_status(SUB, "needs-input")
    sync_tick(client=FakeClient())  # round 1 lands cleanly

    enqueue_question(SUB, "which font?")
    enqueue_status(SUB, "needs-input")
    # The second question fails to send; its announcement must not overtake it.
    client = FakeClient(push_error=PortalBridgeError("boom"))
    sync_tick(client=client)
    assert client.pushes == []

    client2 = FakeClient()
    sync_tick(client=client2)
    assert client2.pushed_kinds == ["question", "status"]


def test_a_stalled_submission_does_not_stall_another_customers():
    enqueue_design_round(SUB, _design())
    enqueue_status(SUB, "awaiting-signoff")
    enqueue_status("sub-002", "in-progress")

    # sub-001's design round is rejected, so its announcement is held; the
    # other submission must still move.
    client = FakeClient(push_outcomes={"design_round": "rejected"})
    result = sync_tick(client=client)

    assert result.rejected == 1
    assert [p["submission_id"] for p in client.pushes] == [SUB, "sub-002"]
    assert result.applied == 1


# ── idempotency and revisions ───────────────────────────────────────────────


def test_a_retry_reuses_the_same_revision():
    enqueue_status(SUB, "in-progress")
    failing = FakeClient(push_error=PortalBridgeError("transient"))
    sync_tick(client=failing)

    ok = FakeClient()
    sync_tick(client=ok)
    sync_tick(client=ok)  # nothing left pending

    assert [p["revision"] for p in ok.pushes] == [1]


def test_revisions_are_monotonic_per_submission():
    enqueue_design_round(SUB, _design())
    enqueue_status(SUB, "awaiting-signoff")
    enqueue_status("sub-002", "planned")

    rows = portal_store.pending_outbox()
    by_sub: dict[str, list[int]] = {}
    for r in rows:
        by_sub.setdefault(r.submission_id, []).append(r.revision)
    assert by_sub[SUB] == [1, 2]
    assert by_sub["sub-002"] == [1]


def test_pulled_revision_seeds_the_allocator_above_the_portal_watermark():
    """Otherwise the first push comes back already_applied and silently drops."""
    client = FakeClient(
        pages=[
            {
                "events": [
                    {"id": "e1", "submission_id": SUB, "type": "submission.created",
                     "revision": 7}
                ],
                "cursor": "c1",
                "has_more": False,
            }
        ]
    )
    sync_tick(client=client)

    row = enqueue_status(SUB, "in-design")
    assert row.revision == 8


# ── pull, cursor, replay ────────────────────────────────────────────────────


def test_pull_records_events_and_advances_the_cursor():
    client = FakeClient(
        pages=[
            {
                "events": [
                    {"id": "e1", "submission_id": SUB, "type": "submission.created",
                     "at": "2026-08-14T10:00:00Z", "data": {"intake": "build me a thing"}},
                ],
                "cursor": "cursor-1",
                "has_more": False,
            }
        ]
    )
    result = sync_tick(client=client)

    assert result.pulled == 1
    assert portal_store.get_sync_state().pull_cursor == "cursor-1"
    events = portal_store.unhandled_events()
    assert [e.event_id for e in events] == ["e1"]
    assert events[0].payload["data"]["intake"] == "build me a thing"


def test_pull_starts_from_the_stored_cursor_on_the_next_pass():
    client = FakeClient(
        pages=[{"events": [], "cursor": "cursor-9", "has_more": False}]
    )
    sync_tick(client=client)
    client2 = FakeClient()
    sync_tick(client=client2)
    assert client2.pull_calls == ["cursor-9"]


def test_replaying_a_page_from_a_stale_cursor_inserts_nothing_twice():
    page = {
        "events": [{"id": "e1", "submission_id": SUB, "type": "signoff.approved"}],
        "cursor": "c1",
        "has_more": False,
    }
    first = sync_tick(client=FakeClient(pages=[dict(page)]))
    second = sync_tick(client=FakeClient(pages=[dict(page)]))

    assert first.pulled == 1
    assert second.pulled == 0
    assert len(portal_store.unhandled_events()) == 1


def test_cursor_does_not_advance_when_persisting_the_page_fails(monkeypatch):
    """A submission made while the daemon was down must queue, never vanish."""
    def _boom(*_a, **_kw):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(portal_store, "record_events", _boom)
    client = FakeClient(
        pages=[
            {
                "events": [{"id": "e1", "submission_id": SUB, "type": "x"}],
                "cursor": "c1",
                "has_more": False,
            }
        ]
    )
    result = sync_tick(client=client)

    assert result.pulled == 0
    assert result.errors
    assert portal_store.get_sync_state().pull_cursor is None


def test_pull_walks_multiple_pages_but_stops_at_the_page_budget():
    pages = [
        {
            "events": [{"id": f"e{i}", "submission_id": SUB, "type": "x"}],
            "cursor": f"c{i}",
            "has_more": True,
        }
        for i in range(5)
    ]
    client = FakeClient(pages=pages)
    result = sync_tick(client=client, pull_pages=3)

    assert result.pulled == 3
    assert portal_store.get_sync_state().pull_cursor == "c2"


def test_events_mirror_customer_facts_but_never_coord_owned_fields():
    client = FakeClient(
        pages=[
            {
                "events": [
                    {
                        "id": "e1",
                        "submission_id": SUB,
                        "type": "signoff.changes_requested",
                        "data": {
                            "verdict": "changes_requested",
                            "comments": "make it blue",
                            # The portal is not the writer of these; if one
                            # ever appears in an event it must NOT enter the
                            # mirror as if it were a customer fact.
                            "status": "in-design",
                            "design_round": {"round": 4},
                        },
                    }
                ],
                "cursor": "c1",
                "has_more": False,
            }
        ]
    )
    sync_tick(client=client)

    record = portal_store.get_submission(SUB)
    assert record is not None
    assert record.customer["verdict"] == "changes_requested"
    assert record.customer["comments"] == "make it blue"
    assert "status" not in record.customer
    assert "design_round" not in record.customer


def test_mirror_merges_rather_than_clobbers_across_events():
    sync_tick(
        client=FakeClient(
            pages=[
                {
                    "events": [{"id": "e1", "submission_id": SUB, "type": "created",
                                "data": {"intake": "the original ask"}}],
                    "cursor": "c1", "has_more": False,
                }
            ]
        )
    )
    sync_tick(
        client=FakeClient(
            pages=[
                {
                    "events": [{"id": "e2", "submission_id": SUB, "type": "signoff",
                                "data": {"verdict": "approved"}}],
                    "cursor": "c2", "has_more": False,
                }
            ]
        )
    )
    record = portal_store.get_submission(SUB)
    assert record.customer == {"intake": "the original ask", "verdict": "approved"}


def test_an_id_less_event_does_not_stop_the_rest_of_the_page_landing():
    """Both are stored — see the content-hash test below for why."""
    client = FakeClient(
        pages=[
            {
                "events": [
                    {"submission_id": SUB, "type": "no-id-here"},
                    {"id": "e2", "submission_id": SUB, "type": "fine"},
                ],
                "cursor": "c1",
                "has_more": False,
            }
        ]
    )
    result = sync_tick(client=client)
    assert result.pulled == 2
    assert "e2" in [e.event_id for e in portal_store.unhandled_events()]


# ── heartbeat and failure posture ───────────────────────────────────────────


def test_heartbeat_is_sent_even_when_pull_and_push_both_fail():
    enqueue_status(SUB, "in-progress")
    client = FakeClient(
        pull_error=PortalBridgeError("pull broke"),
        push_error=PortalBridgeError("push broke"),
    )
    result = sync_tick(client=client)

    assert client.heartbeats == 1
    assert result.heartbeat_ok is True
    assert len(result.errors) == 2
    assert portal_store.get_sync_state().last_heartbeat_at is not None


def test_sync_tick_never_raises_on_an_arbitrary_client_explosion():
    class Exploding:
        def pull(self, cursor=None, limit=None):
            raise ZeroDivisionError("not even a bridge error")

        def push(self, updates):
            raise ZeroDivisionError("nope")

        def heartbeat(self, at=None):
            raise ZeroDivisionError("nope")

    enqueue_status(SUB, "in-progress")  # so the push phase is actually reached
    result = sync_tick(client=Exploding())
    assert result.enabled is True
    assert result.heartbeat_ok is False
    # one per phase — none of the three may take the others down with it
    assert len(result.errors) == 3
    assert len(portal_store.pending_outbox()) == 1  # nothing was lost


def test_disabled_portal_config_sends_nothing():
    from coord.config import PortalConfig  # noqa: PLC0415

    class Cfg:
        portal = PortalConfig(enabled=False)

    result = sync_tick(Cfg())
    assert result.enabled is False
    assert result.summary() == "portal sync: disabled"


def test_errors_are_recorded_then_cleared_on_a_clean_pass():
    enqueue_status(SUB, "in-progress")
    sync_tick(client=FakeClient(push_error=PortalBridgeError("down")))
    assert "down" in portal_store.get_sync_state().last_error

    sync_tick(client=FakeClient())
    assert portal_store.get_sync_state().last_error == ""


def test_push_is_bounded_per_pass():
    for i in range(5):
        enqueue_status(f"sub-{i}", "in-progress")
    client = FakeClient()
    result = sync_tick(client=client, push_limit=2)
    assert result.applied == 2
    assert len(portal_store.pending_outbox()) == 3


def test_applied_rows_update_the_confirmed_record():
    enqueue_design_round(SUB, _design(round_no=3))
    enqueue_status(SUB, "awaiting-signoff")
    sync_tick(client=FakeClient())

    record = portal_store.get_submission(SUB)
    assert record.design_round == 3
    assert record.last_status == "awaiting-signoff"


# ── #2359: the preview-approval gate's ordering rule ────────────────────────


def test_enqueue_status_refuses_quality_check_with_no_preview():
    with pytest.raises(PortalSyncError) as exc:
        enqueue_status(SUB, "quality-check")
    assert "preview" in str(exc.value)
    assert portal_store.pending_outbox() == []


def test_enqueue_preview_refuses_an_empty_url():
    with pytest.raises(PortalSyncError):
        enqueue_preview(SUB, "")
    with pytest.raises(PortalSyncError):
        enqueue_preview(SUB, "   ")


def test_preview_is_pushed_before_the_status_that_announces_it():
    enqueue_preview(SUB, "https://pr-42.natal-chart.pages.dev")
    enqueue_status(SUB, "quality-check")
    client = FakeClient()

    result = sync_tick(client=client)

    assert client.pushed_kinds == ["preview_url", "status"]
    assert result.applied == 2
    assert result.held == 0


def test_quality_check_announcement_is_held_while_its_preview_is_unconfirmed():
    enqueue_preview(SUB, "https://pr-42.natal-chart.pages.dev")
    enqueue_status(SUB, "quality-check")
    client = FakeClient(push_outcomes={"preview_url": "rejected"})

    result = sync_tick(client=client)

    assert client.pushed_kinds == ["preview_url"]
    assert result.rejected == 1
    assert result.applied == 0

    # ...and it stays held on every subsequent tick, rather than leaking out.
    client2 = FakeClient()
    result2 = sync_tick(client=client2)
    assert client2.pushes == []
    assert result2.held == 1


def test_applied_preview_rows_update_the_confirmed_record():
    enqueue_preview(SUB, "https://pr-42.natal-chart.pages.dev")
    enqueue_status(SUB, "quality-check")
    sync_tick(client=FakeClient())

    record = portal_store.get_submission(SUB)
    assert record.preview_url == "https://pr-42.natal-chart.pages.dev"
    assert record.last_status == "quality-check"


def test_summary_reports_a_failed_heartbeat_loudly():
    result = sync_tick(client=FakeClient(heartbeat_ok=False))
    assert "heartbeat=FAILED" in result.summary()
    assert result.moved is False


# ── the daemon wiring ───────────────────────────────────────────────────────


def test_serve_tick_helper_delegates_to_sync_tick(monkeypatch):
    from coord import serve_app  # noqa: PLC0415

    seen = {}

    def _fake(config, **kw):
        seen["config"] = config
        return portal_sync.SyncResult(enabled=False)

    monkeypatch.setattr(portal_sync, "sync_tick", _fake)
    sentinel = object()
    result = serve_app._portal_sync_tick(sentinel)
    assert seen["config"] is sentinel
    assert result.enabled is False


def test_a_misconfigured_portal_block_does_not_read_as_merely_disabled():
    """Half a credential is not a credential — and must not print as 'disabled'."""
    from coord.config import PortalConfig  # noqa: PLC0415

    class Cfg:
        portal = PortalConfig(
            enabled=True, base_url="https://x", bridge_client_id="id"
        )  # no secret

    result = sync_tick(Cfg())
    assert result.enabled is False
    assert result.errors
    assert "NOT RUNNING" in result.summary()
    assert portal_store.get_sync_state().last_error


# ── review round 2: retry budget, malformed pages, nested revisions ─────────


def test_a_permanently_failing_row_is_retired_instead_of_freezing_the_queue():
    """A 4xx raises the same PortalBridgeError a timeout does — and repeats
    forever. Without a budget it would block every later row for this
    customer, re-issuing a known-bad request every tick."""
    enqueue_design_round(SUB, _design())
    enqueue_status(SUB, "in-progress")  # a later, innocent row

    for _ in range(portal_sync.MAX_PUSH_ATTEMPTS):
        sync_tick(client=FakeClient(push_error=PortalBridgeError("400 malformed")))

    rows = portal_store.outbox_for_submission(SUB)
    assert rows[0].state == portal_store.STATE_REJECTED
    assert "gave up after" in rows[0].reason

    # The innocent row behind it is now free to go.
    client = FakeClient()
    result = sync_tick(client=client)
    assert client.pushed_kinds == ["status"]
    assert result.applied == 1


def test_retiring_a_prerequisite_still_never_releases_its_announcement():
    """Failing forward on the retry budget must not fail OPEN on the mail."""
    enqueue_design_round(SUB, _design())
    enqueue_status(SUB, "awaiting-signoff")

    for _ in range(portal_sync.MAX_PUSH_ATTEMPTS + 2):
        sync_tick(client=FakeClient(push_error=PortalBridgeError("400 malformed")))

    rows = portal_store.outbox_for_submission(SUB)
    assert rows[0].state == portal_store.STATE_REJECTED
    assert rows[1].state == portal_store.STATE_PENDING  # held, never sent

    client = FakeClient()
    sync_tick(client=client)
    assert client.pushes == []


def test_an_event_with_no_id_is_stored_not_dropped_and_still_dedupes():
    """The cursor advances past it either way — dropping it would lose it."""
    page = {
        "events": [{"submission_id": SUB, "type": "submission.created"}],
        "cursor": "c1",
        "has_more": False,
    }
    first = sync_tick(client=FakeClient(pages=[dict(page)]))
    assert first.pulled == 1
    stored = portal_store.unhandled_events()
    assert len(stored) == 1
    assert stored[0].event_id.startswith("sha256:")

    # A replay of the same page derives the same content-hash id.
    portal_store.set_pull_cursor(None)
    second = sync_tick(client=FakeClient(pages=[dict(page)]))
    assert second.pulled == 0
    assert len(portal_store.unhandled_events()) == 1


def test_an_integer_zero_event_id_is_not_treated_as_missing():
    client = FakeClient(
        pages=[
            {"events": [{"id": 0, "submission_id": SUB, "type": "x"}],
             "cursor": "c1", "has_more": False}
        ]
    )
    sync_tick(client=client)
    assert [e.event_id for e in portal_store.unhandled_events()] == ["0"]


def test_a_malformed_page_does_not_advance_the_cursor():
    client = FakeClient(
        pages=[{"events": "not-a-list", "cursor": "c1", "has_more": False}]
    )
    result = sync_tick(client=client)
    assert result.errors
    assert portal_store.get_sync_state().pull_cursor is None


def test_a_nested_revision_seeds_the_allocator_too():
    """The mirror reads the nested shape, so the seed must as well."""
    client = FakeClient(
        pages=[
            {
                "events": [
                    {"id": "e1", "submission_id": SUB, "type": "created",
                     "data": {"revision": 4, "intake": "x"}}
                ],
                "cursor": "c1",
                "has_more": False,
            }
        ]
    )
    sync_tick(client=client)
    assert enqueue_status(SUB, "in-design").revision == 5


def test_sync_tick_returns_even_when_recording_pass_state_fails(monkeypatch):
    """The bookkeeping write must not be the thing that breaks 'never raises'."""
    def _boom(*_a, **_kw):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(portal_store, "clear_error", _boom)
    monkeypatch.setattr(portal_store, "note_error", _boom)
    result = sync_tick(client=FakeClient())
    assert result.enabled is True
    assert result.heartbeat_ok is True


def test_requeue_revives_a_retired_row_with_a_fresh_revision():
    enqueue_design_round(SUB, _design())
    for _ in range(portal_sync.MAX_PUSH_ATTEMPTS):
        sync_tick(client=FakeClient(push_error=PortalBridgeError("400 malformed")))
    retired = portal_store.outbox_for_submission(SUB)[0]
    assert retired.state == portal_store.STATE_REJECTED

    revived = portal_store.requeue(SUB, retired.seq)
    assert revived.state == portal_store.STATE_PENDING
    assert revived.attempts == 0
    assert revived.revision > retired.revision

    client = FakeClient()
    assert sync_tick(client=client).applied == 1


def test_requeue_of_an_unknown_row_is_a_clean_none():
    assert portal_store.requeue("nope", 1) is None


# ── consuming portal verdicts (#2509, PDR-4) ────────────────────────────────


class FakeRepoCfg:
    def __init__(self, github: str = "acme/portal-repo") -> None:
        self.github = github


class FakeConfig:
    """Just enough of `coord.config.Config` for `_consume_verdicts`: a
    `.repo(name)` lookup. Real dispatch is monkeypatched out in every test
    below, so nothing else on Config is ever touched."""

    def __init__(self, repos: dict | None = None) -> None:
        self._repos = repos or {}

    def repo(self, name):
        return self._repos.get(name)


def _changes_requested_page(comments: str | None = "make it blue") -> dict:
    data = {"verdict": "changes_requested"}
    if comments is not None:
        data["comments"] = comments
    return {
        "events": [
            {"id": "e1", "submission_id": SUB, "type": "signoff.changes_requested", "data": data}
        ],
        "cursor": "c1",
        "has_more": False,
    }


def test_changes_requested_dispatches_amend_and_marks_the_event_consumed(monkeypatch):
    portal_store.link_milestone(
        repo_name="acme-portal", milestone_number=5, submission_id=SUB
    )
    monkeypatch.setattr(portal_sync, "_resolve_tracking_issue", lambda repo_cfg, ms: 42)
    calls = []

    def fake_dispatch(repo_name, tracking_issue_number, config, *, amend_briefing=None, **_):
        calls.append((repo_name, tracking_issue_number, amend_briefing))
        return ("assignment-1", "machine-1")

    monkeypatch.setattr("coord.mock_author.dispatch_acceptance_mock", fake_dispatch)

    config = FakeConfig({"acme-portal": FakeRepoCfg()})
    client = FakeClient(pages=[_changes_requested_page()])
    result = sync_tick(config=config, client=client)

    assert result.verdicts_consumed == 1
    assert calls == [("acme-portal", 42, "make it blue")]
    assert portal_store.unhandled_events() == []


def test_approved_verdict_is_left_unconsumed_not_auto_decided(monkeypatch):
    """#2509's open policy question: an `approved` verdict must not silently
    auto-record Gate A here — it stays unhandled instead of being acted on."""
    dispatched = []
    monkeypatch.setattr(
        "coord.mock_author.dispatch_acceptance_mock",
        lambda *a, **kw: dispatched.append((a, kw)),
    )
    config = FakeConfig({"acme-portal": FakeRepoCfg()})
    client = FakeClient(
        pages=[
            {
                "events": [
                    {"id": "e1", "submission_id": SUB, "type": "signoff.approved"}
                ],
                "cursor": "c1",
                "has_more": False,
            }
        ]
    )
    result = sync_tick(config=config, client=client)

    assert result.verdicts_consumed == 0
    assert dispatched == []
    assert [e.event_id for e in portal_store.unhandled_events()] == ["e1"]


def test_changes_requested_with_no_link_recorded_stays_unhandled_and_errors():
    """No `coord portal link` for this submission yet — nothing to dispatch
    to, and the client's feedback must not be dropped."""
    config = FakeConfig({"acme-portal": FakeRepoCfg()})
    client = FakeClient(pages=[_changes_requested_page()])
    result = sync_tick(config=config, client=client)

    assert result.verdicts_consumed == 0
    assert any("no milestone is linked" in e for e in result.errors)
    assert [e.event_id for e in portal_store.unhandled_events()] == ["e1"]


def test_a_dispatch_failure_leaves_the_event_to_retry_next_tick(monkeypatch):
    portal_store.link_milestone(
        repo_name="acme-portal", milestone_number=5, submission_id=SUB
    )
    monkeypatch.setattr(portal_sync, "_resolve_tracking_issue", lambda repo_cfg, ms: 42)

    def boom(*_a, **_kw):
        raise RuntimeError("Gate A already in flight")

    monkeypatch.setattr("coord.mock_author.dispatch_acceptance_mock", boom)

    config = FakeConfig({"acme-portal": FakeRepoCfg()})
    client = FakeClient(pages=[_changes_requested_page()])
    result = sync_tick(config=config, client=client)

    assert result.verdicts_consumed == 0
    assert any("Gate A already in flight" in e for e in result.errors)
    assert [e.event_id for e in portal_store.unhandled_events()] == ["e1"]


def test_a_missing_comment_falls_back_to_a_placeholder_amend_text(monkeypatch):
    portal_store.link_milestone(
        repo_name="acme-portal", milestone_number=5, submission_id=SUB
    )
    monkeypatch.setattr(portal_sync, "_resolve_tracking_issue", lambda repo_cfg, ms: 42)
    calls = []
    monkeypatch.setattr(
        "coord.mock_author.dispatch_acceptance_mock",
        lambda repo, issue, cfg, **kw: calls.append(kw.get("amend_briefing")),
    )

    config = FakeConfig({"acme-portal": FakeRepoCfg()})
    client = FakeClient(pages=[_changes_requested_page(comments=None)])
    sync_tick(config=config, client=client)

    assert len(calls) == 1
    assert SUB in calls[0]


def test_with_no_config_the_verdict_phase_is_a_no_op_not_a_crash():
    """`sync_tick(client=...)` with no config is the documented test/CLI
    bypass — verdict consumption must not explode with nowhere to dispatch."""
    client = FakeClient(pages=[_changes_requested_page()])
    result = sync_tick(client=client)

    assert result.verdicts_consumed == 0
    assert [e.event_id for e in portal_store.unhandled_events()] == ["e1"]


class TestSignoffVerdict:
    def _event(self, kind: str, payload: dict | None = None):
        return portal_store.PortalEvent(
            event_id="e1",
            submission_id=SUB,
            kind=kind,
            occurred_at="",
            payload=payload or {},
            received_at=0.0,
        )

    def test_verdict_suffix_on_type(self):
        assert (
            portal_sync._signoff_verdict(self._event("signoff.changes_requested"))
            == "changes_requested"
        )

    def test_verdict_nested_in_data(self):
        event = self._event("signoff", {"data": {"verdict": "approved"}})
        assert portal_sync._signoff_verdict(event) == "approved"

    def test_non_signoff_kind_is_not_a_verdict(self):
        assert portal_sync._signoff_verdict(self._event("created")) is None

    def test_comment_read_from_nested_data(self):
        event = self._event(
            "signoff.changes_requested", {"data": {"comments": "make it bigger"}}
        )
        assert portal_sync._signoff_comment(event) == "make it bigger"

    def test_comment_defaults_to_empty_string(self):
        event = self._event("signoff.changes_requested")
        assert portal_sync._signoff_comment(event) == ""
