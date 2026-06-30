"""Generate the golden `/board` fixture used by both sides of the wire seam (#748).

The `/board` payload is `SELECT * FROM <table>` shipped as raw SQLite rows
(`coord/dao.py`).  The wire schema *is* the SQLite DDL (`coord/db.py`), and the
Rust structs (`tui/src/app/types.rs`) are hand-typed mirrors of DB columns.  A
single type mismatch (the classic case: a SQLite `INTEGER` boolean vs a Rust
`bool`) fails the **entire** `BoardPayload` parse and blanks the board
(#632/#546/#628).

This script builds a representative, freshly-migrated coord.db (headless +
interactive assignments, a review, a merge-queue row, a proposal, an open
issue, a machine) with fully deterministic content (no wall-clock timestamps,
fixed IDs) and runs it through the exact same `SqliteStore.board_projection()`
the daemon serves, then writes the result to `tui/tests/fixtures/board_sample.json`.

That committed fixture is read by BOTH sides of the seam:
- Rust: `tui/src/app/tests.rs::board_payload_deserializes_real_sample` parses
  it into `BoardPayload` and asserts the round-trip succeeds — runs in CI via
  `.github/workflows/cargo-test.yml`.
- Python: `tests/test_board_fixture.py` asserts the checked-in file is byte-
  identical to what this generator produces *right now*, so the fixture can
  never silently drift from the schema that created it.

Regenerate after any `coord/db.py` schema change that should be reflected in
the golden fixture:

    .venv/bin/python scripts/gen_board_fixture.py
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from coord.dao import SqliteStore
from coord.db import _ensure_schema

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_PATH = REPO_ROOT / "tui" / "tests" / "fixtures" / "board_sample.json"


def build_fixture_db(conn: sqlite3.Connection) -> None:
    """Populate *conn* (already schema-migrated) with deterministic, representative rows.

    Every timestamp is a fixed epoch value (not `time.time()`) so the
    generated payload — and therefore the committed fixture — never changes
    between runs except when this function or the schema itself changes.
    """
    # ── assignments ──────────────────────────────────────────────────────
    # 1. A finished headless (claude -p) work assignment: smoke_tests + a
    #    test_plan (JSON object — decoded to a native object on the wire,
    #    NOT an array, per #584) + review_findings (kept as a raw JSON
    #    string on the wire) + cost/token accounting.
    conn.execute(
        "INSERT INTO assignments (assignment_id, machine_name, repo_name, repo_github, "
        "issue_number, issue_title, status, type, branch, model, dispatched_at, "
        "finished_at, exit_code, cost_usd, smoke_tests, review_findings, test_plan, "
        "input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens, "
        "is_interactive, test_state, review_verdict) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "work-748a", "precision", "claude-coordinator", "JDonaghy/claude-coordinator",
            748, "seam: golden /board fixture", "done", "work", "issue-748-fixture",
            "sonnet", 1000000000.0, 1000000600.0, 0, 0.42,
            '["fixture loads in the TUI", "round_number is non-zero"]',
            '{"verdict": "approve", "body": "Looks good."}',
            '{"steps": [{"kind": "run", "cmd": "cargo test", "label": "run tui tests"}]}',
            1200, 340, 0, 5000, 0, "passed", "approve",
        ),
    )
    # 2. A running human-attended interactive (Max/Pro) assignment —
    #    is_interactive=1, no cost/token data (the #546 case this fixture
    #    exists to guard).
    conn.execute(
        "INSERT INTO assignments (assignment_id, machine_name, repo_name, repo_github, "
        "issue_number, issue_title, status, type, branch, dispatched_at, is_interactive) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            "work-748b", "dellserver", "claude-coordinator", "JDonaghy/claude-coordinator",
            749, "interactive follow-up", "running", "work", "issue-749-followup",
            1000001000.0, 1,
        ),
    )
    # 3. A review of assignment 1 (pairs via review_of_assignment_id).
    conn.execute(
        "INSERT INTO assignments (assignment_id, machine_name, repo_name, repo_github, "
        "issue_number, issue_title, status, type, review_of_assignment_id, dispatched_at, "
        "finished_at, review_verdict, is_interactive) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "rev-748a", "dellserver", "claude-coordinator", "JDonaghy/claude-coordinator",
            748, "seam: golden /board fixture", "done", "review", "work-748a",
            1000000700.0, 1000000900.0, "approve", 0,
        ),
    )

    # ── machines ─────────────────────────────────────────────────────────
    conn.execute(
        "INSERT INTO machines (name, host, capabilities, repos) VALUES (?,?,?,?)",
        ("precision", "precision.tailnet", '["python", "rust"]', '["claude-coordinator"]'),
    )
    conn.execute(
        "INSERT INTO machines (name, host, capabilities, repos) VALUES (?,?,?,?)",
        ("dellserver", "dellserver.tailnet", '["python", "gtk"]', '["claude-coordinator"]'),
    )

    # ── merge_queue ──────────────────────────────────────────────────────
    conn.execute(
        "INSERT INTO merge_queue (assignment_id, repo_name, repo_github, branch, "
        "target_branch, issue_number, issue_title, state, pr_number, pr_url, size, "
        "enqueued_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "work-748a", "claude-coordinator", "JDonaghy/claude-coordinator",
            "issue-748-fixture", "main", 748, "seam: golden /board fixture",
            "queued", 9001, "https://github.com/JDonaghy/claude-coordinator/pull/9001",
            240, 1000000950.0,
        ),
    )

    # ── proposals ────────────────────────────────────────────────────────
    conn.execute(
        "INSERT INTO proposals (machine_name, repo_name, issue_number, issue_title, "
        "rationale, type) VALUES (?,?,?,?,?,?)",
        (
            "precision", "claude-coordinator", 750, "next seam-hardening pass",
            "precision is idle and has touched dao.py recently", "work",
        ),
    )

    # ── issues ───────────────────────────────────────────────────────────
    conn.execute(
        "INSERT INTO issues (repo_name, number, title, body, state, labels, synced_at, "
        "milestone_number, milestone_title) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            "claude-coordinator", 748, "seam: golden /board fixture + CI round-trip parse test",
            "## Context\n\nThe /board payload is shipped as raw SQLite rows...",
            "open", '["coord", "status:ready"]', 1000001100.0, 12, "Tech Debt: seam hardening",
        ),
    )

    # ── board_meta ───────────────────────────────────────────────────────
    conn.execute("INSERT OR REPLACE INTO board_meta (key, value) VALUES ('round_number', '3')")
    conn.execute("INSERT OR REPLACE INTO board_meta (key, value) VALUES ('board_initialized', '1')")
    conn.execute(
        "INSERT OR REPLACE INTO board_meta (key, value) VALUES "
        "('pipeline_default_gates', '[\"test\", \"review\", \"merge\"]')"
    )
    conn.commit()


def build_fixture_payload() -> dict:
    """Build the fixture DB in-memory and return its `/board` projection.

    Uses a real on-disk temp file (not `:memory:`) because `SqliteStore` opens
    its own `mode=ro` connection by URI — it cannot see an in-process
    `:memory:` DB.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "board_fixture.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        _ensure_schema(conn)
        build_fixture_db(conn)
        conn.close()
        return SqliteStore(db_path).board_projection()


def fixture_json_text() -> str:
    """Deterministic JSON text for the fixture (sorted keys, stable indent)."""
    payload = build_fixture_payload()
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def main() -> None:
    FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE_PATH.write_text(fixture_json_text())
    print(f"wrote {FIXTURE_PATH}")


if __name__ == "__main__":
    main()
