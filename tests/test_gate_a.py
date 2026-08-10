"""Tests for the Gate A human sign-off gate (#2063).

Three layers, mirroring how the feature is built:

- :mod:`coord.gate_a` — the pure verdict/digest/marker logic (no I/O).
- :func:`coord.milestone_dispatch.issue_oracle_ready` — the refusal, at the
  point where the contract is *consumed* rather than at the merge (the
  Gate-A PR is merged with ``gh pr merge``, outside coord entirely).
- ``coord.drive_queue`` — that the refusal **parks** (re-checked each tick,
  #1891/#1892) instead of landing in terminal ``blocked`` (#2040), which
  ``coord drive-queue add`` cannot clear.
"""

from __future__ import annotations

import pytest

from coord import gate_a
from coord.config import (
    AcceptanceConfig,
    AcceptanceDriverConfig,
    Config,
)
from coord.milestone_dispatch import issue_oracle_ready
from coord.models import Machine, Repo

CONTRACT_V1 = "# Contract\n\n- the Save button says `Save`\n"
CONTRACT_V2 = "# Contract\n\n- the Save button says `Publish`\n"


def _cfg() -> Config:
    return Config(
        repos=[Repo(name="api", github="acme/api", default_branch="main")],
        machines=[
            Machine(
                name="laptop",
                host="laptop.tailnet",
                repos=["api"],
                repo_paths={"api": "/tmp/api"},
            )
        ],
        acceptance=AcceptanceConfig(
            drivers={"api": AcceptanceDriverConfig(kind="cli-pytest", run="pytest")}
        ),
    )


def _approved(contract: str = CONTRACT_V1, *, verdict: str = "approved") -> dict:
    return gate_a.make_record(
        repo_name="api",
        milestone_number=37,
        verdict=verdict,
        contract_sha=gate_a.contract_digest(contract),
        tracking_issue=900,
        now=1000.0,
    ).to_dict()


# ── contract_digest ─────────────────────────────────────────────────────────


class TestContractDigest:
    def test_identical_text_hashes_identically(self) -> None:
        assert gate_a.contract_digest(CONTRACT_V1) == gate_a.contract_digest(
            CONTRACT_V1
        )

    def test_pinned_surface_change_changes_the_digest(self) -> None:
        """The whole point: `Save` -> `Publish` is a different contract.

        Those strings become assertions in a sealed suite the worker may
        never edit, so an amend that rewords one must force a fresh look.
        """
        assert gate_a.contract_digest(CONTRACT_V1) != gate_a.contract_digest(
            CONTRACT_V2
        )

    def test_line_endings_and_trailing_newlines_are_not_a_change(self) -> None:
        crlf = CONTRACT_V1.replace("\n", "\r\n")
        assert gate_a.contract_digest(crlf) == gate_a.contract_digest(CONTRACT_V1)
        assert gate_a.contract_digest(CONTRACT_V1 + "\n\n") == gate_a.contract_digest(
            CONTRACT_V1
        )

    def test_accepts_bytes(self) -> None:
        assert gate_a.contract_digest(CONTRACT_V1.encode()) == gate_a.contract_digest(
            CONTRACT_V1
        )


# ── evaluate ────────────────────────────────────────────────────────────────


class TestEvaluate:
    def _evaluate(self, **kw):
        base = dict(
            repo_name="api",
            milestone_number=37,
            contract_text=CONTRACT_V1,
            approval=None,
        )
        base.update(kw)
        return gate_a.evaluate(**base)

    def test_no_verdict_refuses(self) -> None:
        d = self._evaluate()
        assert d.ok is False
        assert d.state == gate_a.STATE_MISSING
        assert "no recorded human sign-off" in d.reason
        assert "coord gate-a --approved api" in d.reason

    def test_matching_approval_passes(self) -> None:
        d = self._evaluate(approval=_approved(CONTRACT_V1))
        assert d.ok is True
        assert d.state == gate_a.STATE_APPROVED
        assert d.reason is None

    def test_amend_invalidates_a_prior_approval(self) -> None:
        """#2063's own trap: approving v1 must not silently approve v2."""
        d = self._evaluate(
            contract_text=CONTRACT_V2, approval=_approved(CONTRACT_V1)
        )
        assert d.ok is False
        assert d.state == gate_a.STATE_STALE
        assert "stale" in d.reason

    def test_changes_requested_refuses_with_the_note(self) -> None:
        record = _approved(CONTRACT_V1, verdict=gate_a.VERDICT_CHANGES)
        record["note"] = "status vocabulary is wrong"
        d = self._evaluate(approval=record)
        assert d.ok is False
        assert d.state == gate_a.STATE_CHANGES
        assert "status vocabulary is wrong" in d.reason
        assert "--amend" in d.reason

    def test_changes_against_an_older_contract_still_refuses(self) -> None:
        record = _approved(CONTRACT_V1, verdict=gate_a.VERDICT_CHANGES)
        d = self._evaluate(contract_text=CONTRACT_V2, approval=record)
        assert d.ok is False
        assert d.state == gate_a.STATE_STALE

    def test_unreadable_contract_fails_closed(self) -> None:
        d = self._evaluate(contract_text=None, approval=_approved(CONTRACT_V1))
        assert d.ok is False
        assert d.state == gate_a.STATE_MISSING

    def test_declared_milestone_exemption_passes(self) -> None:
        d = self._evaluate(exempt=True)
        assert d.ok is True
        assert d.state == gate_a.STATE_EXEMPT

    def test_unknown_schema_degrades_to_no_approval(self) -> None:
        record = _approved(CONTRACT_V1)
        record["schema"] = 99
        d = self._evaluate(approval=record)
        assert d.ok is False
        assert d.state == gate_a.STATE_MISSING

    def test_every_refusal_carries_the_park_marker(self) -> None:
        """The marker is the only channel that survives the process
        boundary to `coord drive-queue`'s tick — a refusal without it would
        land the queue entry in terminal `blocked` (#2040)."""
        for kw in (
            {},
            {"contract_text": None},
            {"contract_text": CONTRACT_V2, "approval": _approved(CONTRACT_V1)},
            {
                "approval": _approved(
                    CONTRACT_V1, verdict=gate_a.VERDICT_CHANGES
                )
            },
        ):
            d = self._evaluate(**kw)
            assert d.ok is False
            assert gate_a.is_gate_a_refusal_reason(d.reason), kw
            parsed = gate_a.parse_park_marker(d.reason)
            assert parsed is not None
            assert parsed[0] == "api"
            assert parsed[1] == 37


class TestMakeRecord:
    def test_rejects_an_unknown_verdict(self) -> None:
        with pytest.raises(ValueError):
            gate_a.make_record(
                repo_name="api",
                milestone_number=37,
                verdict="maybe",
                contract_sha="deadbeef",
            )

    def test_roundtrips_through_dict(self) -> None:
        rec = gate_a.make_record(
            repo_name="api",
            milestone_number=37,
            verdict=gate_a.VERDICT_APPROVED,
            contract_sha="abc",
            note="ok",
            actor="john",
            now=5.0,
        )
        back = gate_a.GateAApproval.from_dict(rec.to_dict())
        assert back == rec


# ── the park marker ─────────────────────────────────────────────────────────


class TestParkMarker:
    def test_roundtrip(self) -> None:
        m = gate_a.park_marker("coord-portal", 3, "abc123")
        assert gate_a.parse_park_marker(f"blah {m} blah") == (
            "coord-portal",
            3,
            "abc123",
        )

    def test_unrelated_prose_is_not_a_gate_a_refusal(self) -> None:
        assert gate_a.parse_park_marker("drive session died") is None
        assert gate_a.is_gate_a_refusal_reason(None) is False
        assert gate_a.is_gate_a_refusal_reason("CI running: checks pending") is False

    def test_fingerprint_changes_when_the_verdict_changes(self) -> None:
        none_fp = gate_a.approval_fingerprint(None)
        assert none_fp == gate_a.NO_VERDICT
        approved = gate_a.approval_fingerprint(_approved(CONTRACT_V1))
        changes = gate_a.approval_fingerprint(
            _approved(CONTRACT_V1, verdict=gate_a.VERDICT_CHANGES)
        )
        assert len({none_fp, approved, changes}) == 3

    def test_fingerprint_is_stable_for_an_unchanged_verdict(self) -> None:
        assert gate_a.approval_fingerprint(
            _approved(CONTRACT_V1)
        ) == gate_a.approval_fingerprint(_approved(CONTRACT_V1))


# ── issue_oracle_ready: the refusal ─────────────────────────────────────────


def _fetch(mapping: dict[str, str]):
    def _f(repo_github: str, path: str, branch: str) -> str | None:
        return mapping.get(path)

    return _f


MANIFEST_PATH = "tests/acceptance/ms-37/manifest.yml"
CONTRACT_PATH = "tests/acceptance/ms-37/contract.md"


class TestIssueOracleReadyGateA:
    def _ready(self, *, manifest: str, contract: str | None, approval):
        files = {MANIFEST_PATH: manifest}
        if contract is not None:
            files[CONTRACT_PATH] = contract
        return issue_oracle_ready(
            _cfg().repo("api"),
            _cfg(),
            37,
            1118,
            file_exists=lambda *a: True,
            fetch_manifest=_fetch(files),
            fetch_gate_a_approval=lambda *a: approval,
        )

    def test_refuses_when_contract_has_no_recorded_verdict(self) -> None:
        r = self._ready(
            manifest="tests:\n  ms37::a: 1118\n", contract=CONTRACT_V1, approval=None
        )
        assert r.applies is True
        assert r.has_slice is True  # the slice gate is satisfied...
        assert r.gate_a_state == gate_a.STATE_MISSING
        assert r.reason is not None  # ...and it still refuses
        assert "coord gate-a --approved api" in r.reason

    def test_proceeds_once_a_verdict_is_recorded(self) -> None:
        r = self._ready(
            manifest="tests:\n  ms37::a: 1118\n",
            contract=CONTRACT_V1,
            approval=_approved(CONTRACT_V1),
        )
        assert r.reason is None
        assert r.gate_a_state == gate_a.STATE_APPROVED

    def test_amended_contract_refuses_again(self) -> None:
        r = self._ready(
            manifest="tests:\n  ms37::a: 1118\n",
            contract=CONTRACT_V2,
            approval=_approved(CONTRACT_V1),
        )
        assert r.gate_a_state == gate_a.STATE_STALE
        assert r.reason is not None

    def test_gate_a_refusal_wins_over_the_slice_refusal(self) -> None:
        """Both gates are unsatisfied; the human one is reported.

        Gate A is the cheap moment to change direction — telling the
        operator to author a slice against a contract nobody approved is
        exactly the sequence that burned ~$2.70 on coord-portal ms-2.
        """
        r = self._ready(manifest="", contract=CONTRACT_V1, approval=None)
        assert r.has_slice is False
        assert "no recorded human sign-off" in r.reason
        assert "coord acceptance author" not in r.reason

    def test_issue_level_exempt_does_not_bypass_the_human_gate(self) -> None:
        """`exempt:` says "this ISSUE doesn't consume the sealed suite" —
        it says nothing about whether a human read the milestone's
        contract, which every sibling issue is built against."""
        r = self._ready(
            manifest="exempt: [1118]\n", contract=CONTRACT_V1, approval=None
        )
        assert r.reason is not None
        assert r.gate_a_state == gate_a.STATE_MISSING

    def test_declared_milestone_opt_out_bypasses_it(self) -> None:
        r = self._ready(
            manifest=(
                "tests:\n  ms37::a: 1118\n"
                "gate_a:\n  exempt: true\n  reason: no user-visible surface\n"
            ),
            contract=CONTRACT_V1,
            approval=None,
        )
        assert r.reason is None
        assert r.gate_a_state == gate_a.STATE_EXEMPT

    def test_no_driver_configured_is_still_a_no_op(self) -> None:
        cfg = Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[],
        )
        r = issue_oracle_ready(
            cfg.repo("api"), cfg, 37, 1118, file_exists=lambda *a: True
        )
        assert r.applies is False
        assert r.reason is None
        assert r.gate_a_state == ""

    def test_missing_contract_is_still_gate_a_status_s_refusal(self) -> None:
        """Contract absent entirely => `gate_a_status` already refuses with
        its own message; this gate must not double-block."""
        r = issue_oracle_ready(
            _cfg().repo("api"),
            _cfg(),
            37,
            1118,
            file_exists=lambda *a: False,
        )
        assert r.applies is False
        assert r.reason is None


# ── the manifest opt-out ────────────────────────────────────────────────────


class TestManifestGateAKey:
    def test_absent_key_leaves_the_gate_on(self) -> None:
        from coord.acceptance import parse_manifest_text

        assert parse_manifest_text("tests: {a: 1}\n").gate_a_exempt is False

    def test_block_form(self) -> None:
        from coord.acceptance import parse_manifest_text

        data = parse_manifest_text("gate_a:\n  exempt: true\n  reason: internal\n")
        assert data.gate_a_exempt is True
        assert data.gate_a_exempt_reason == "internal"

    def test_shorthand_bool_form(self) -> None:
        from coord.acceptance import parse_manifest_text

        assert parse_manifest_text("gate_a: true\n").gate_a_exempt is True

    def test_garbage_value_leaves_the_gate_on(self) -> None:
        from coord.acceptance import parse_manifest_text

        assert parse_manifest_text("gate_a: [1, 2]\n").gate_a_exempt is False


# ── the drive-queue disposition (#2040: park, never terminal `blocked`) ─────


class TestDriveQueueParksNotBlocks:
    def _entry(self, **kw):
        from coord.drive_queue import QueueEntry

        base = dict(
            repo="api",
            issue=1118,
            position=1,
            state="running",
            attempts=0,
            session_name="drive-api-1118",
            launched_at=0.0,
        )
        base.update(kw)
        return QueueEntry(**base)

    def _board(self):
        from coord.drive_queue import build_board_view

        return build_board_view({"active": [], "completed": []}, [])

    def test_gate_a_refusal_parks_without_spending_an_attempt(self) -> None:
        from coord.drive_queue import STATE_PARKED, _reconcile_running

        entry = self._entry()
        reason = (
            "drive exited for api#1118: Gate A has no recorded human sign-off "
            f"for ms-37. {gate_a.park_marker('api', 37)} (exit_code=5)"
        )
        reconcile, blocked = _reconcile_running(
            entry,
            self._board(),
            max_attempts=2,
            now=10_000.0,
            exit_reasons={entry.key: reason},
            exit_refused={entry.key: True},
        )
        assert blocked is None, "a Gate-A refusal must never escalate"
        assert reconcile.outcome == "parked"
        assert reconcile.updates["state"] == STATE_PARKED
        assert "attempts" not in reconcile.updates

    def test_an_ordinary_refusal_still_blocks(self) -> None:
        from coord.drive_queue import STATE_BLOCKED, _reconcile_running

        entry = self._entry()
        reconcile, blocked = _reconcile_running(
            entry,
            self._board(),
            max_attempts=2,
            now=10_000.0,
            exit_reasons={entry.key: "machine lacks the capability"},
            exit_refused={entry.key: True},
        )
        assert blocked is not None
        assert reconcile.outcome == "refused"
        assert blocked.updates["state"] == STATE_BLOCKED

    def test_parked_entry_stays_parked_until_a_verdict_is_recorded(self) -> None:
        from coord.drive_queue import STATE_PARKED, plan_tick

        park_reason = f"parked ... {gate_a.park_marker('api', 37)}"
        entry = self._entry(state=STATE_PARKED, last_reason=park_reason)
        plan = plan_tick(
            [entry],
            self._board(),
            capacity=4,
            now=10_000.0,
            gate_a_pending={entry.key: True},
        )
        assert plan.launch is None
        assert not [r for r in plan.reconciles if r.outcome == "resumed"]

    def test_parked_entry_resumes_once_the_verdict_lands(self) -> None:
        from coord.drive_queue import STATE_PARKED, STATE_WAITING, plan_tick

        park_reason = f"parked ... {gate_a.park_marker('api', 37)}"
        entry = self._entry(state=STATE_PARKED, last_reason=park_reason)
        plan = plan_tick(
            [entry],
            self._board(),
            capacity=4,
            now=10_000.0,
            gate_a_pending={entry.key: False},
        )
        resumed = [r for r in plan.reconciles if r.outcome == "resumed"]
        assert len(resumed) == 1
        assert resumed[0].updates["state"] == STATE_WAITING
        assert "#2063" in resumed[0].reason

    def test_unresolvable_gate_a_park_stays_parked(self) -> None:
        """Fail closed: no entry in `gate_a_pending` (the shell could not
        resolve it) must not be read as "cleared"."""
        from coord.drive_queue import STATE_PARKED, plan_tick

        park_reason = f"parked ... {gate_a.park_marker('api', 37)}"
        entry = self._entry(state=STATE_PARKED, last_reason=park_reason)
        plan = plan_tick(
            [entry], self._board(), capacity=4, now=10_000.0, gate_a_pending={}
        )
        assert not [r for r in plan.reconciles if r.outcome == "resumed"]
