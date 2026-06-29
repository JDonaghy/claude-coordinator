# CLI Surface Smoke Set — Regression Baseline for #741 Decomposition

This document records the **core regression baseline** for the Python CLI surface
(`coord/cli.py`) as it exists on `main` at the time of issue #741.  Its purpose
is to give future refactor workers (decomposing `cli.py` into a `commands/`
package) a clear signal about what is **already guarded** and what would
constitute a regression.

Run the full set:

```
pytest tests/test_cli_assign.py tests/test_cli_merge.py tests/test_coord_test.py \
       tests/test_cli_network.py tests/test_issue_store_seam.py -v
```

Or just check that the existing suite is green:

```
pytest
```

---

## Surface 1 — `assign()` mode-routing

**File:** `tests/test_cli_assign.py` (1 596 lines, 63 tests)

`assign()` is the most complex entry point in `cli.py` — it branches on
`--interactive`, `--review-of`, `--fix-of`, `--smoke-of`, `--merge-of`,
`--remote`, `--dry-run`, and several combinations.  All major modes are covered:

| Class | What it guards |
|---|---|
| `TestAssignValidation` | machine/repo unknown; machine can't serve repo |
| `TestAssignDryRun` | dry-run skips network dispatch |
| `TestAssignDispatch` | happy-path dispatch, claim-check, briefing passthrough |
| `TestAssignLabelGateResolution` | label→gate mapping (`documentation`, `hotfix`, `needs-smoke`) |
| `TestAssignFreshness` | stale-dep detection, auto-pull, `--no-pull` addendum |
| `TestAssignInteractiveReview` | `--interactive --review-of` — reviews, remote, session-ended finalise |
| `TestAssignInteractiveFix` | `--fix-of` — continues existing branch, request-changes routing |
| `TestAssignInteractiveRework` | `--fix-of` rework iteration |
| `TestAssignInteractiveRemoteWork` | `--remote` flag smoke |
| `TestAssignBriefingFileAndTroubleshoot` | `--briefing-file`, `--troubleshoot` |
| `TestAssignInteractiveFixFromTestFail` | `--fix-of` accepting a test-failed work id (#581) |
| `TestAssignInteractiveSmoke` | `--smoke-of` — testing agent workflow |
| `TestAssignInteractiveMerge` | `--merge-of` — merge agent proactive rebase |
| `TestAssignInteractiveFixModelEscalation` | model-escalation on fix iteration |

**Refactor invariant:** After decomposing `assign()` into `commands/assign.py`,
`pytest tests/test_cli_assign.py` must still be fully green with no test
deletions.  Any mode the test suite doesn't cover is a gap; do not rely on
"it looks right" — add a test if you're unsure.

---

## Surface 2 — `merge` command

**File:** `tests/test_cli_merge.py` (≈ 600 lines, 20 tests)

Covers the full `coord merge` path: empty-queue message, dry-run, size-order
sequencing, conflict detection/HUMAN_REQUIRED, review-gate refuse/approve,
deleted/existing branches, `--only`, `--order`, `--force-merge`, auto-enqueue.

Key regression signals:
- `test_review_gate_refuses_merge_without_approval` — merge must be gated on review
- `test_conflict_marks_state_and_warns` — conflict state must persist
- `test_merges_in_size_order` — sequencing must be deterministic

---

## Surface 3 — `test` command (smoke verdicts)

**File:** `tests/test_coord_test.py` (≈ 530 lines, 20 tests)

Covers `coord test --passed|--fail|--skipped`: verdict recording, worktree
cleanup, default-branch restoration, git-fetch failure reporting, reconcile on
PR head-ref mismatch, `--set-test-mode`.

Key regression signals:
- `test_pass_records_on_board` — passed verdict persists
- `test_fail_records_reason` — fail reason surfaced
- `test_builds_in_throwaway_worktree_never_checks_out_base` — isolation invariant

---

## Surface 4 — `status` command (network)

**File:** `tests/test_cli_network.py` (≈ 320 lines, 13 tests)

Covers `coord status`: machine online/offline, freshness, fetch failure, timeout,
machine filter, auto-reconcile, `--no-reconcile`.

---

## Surface 5 — `report-result` command

**File:** `tests/test_issue_store_seam.py` — class `TestReportResultCli`
(lines 685–850, 10 tests)

Covers `coord report-result --assignment --status --summary [--verdict] [--body]`:
all status values (`done`, `already-implemented`, `blocked`), verdict recording,
`--body` persistence, missing/unknown assignment errors.

Also relevant in the same file: the `TestIssueStoreFinalise` class
(lines 1–680+) covers the underlying seam (`IssueStore.finalise()`) that
`report-result` routes through — this is the behavior-preserving seam that
#741's `commands/*` split must not break.

---

## TUI surface (Rust)

**File:** `tui/src/app.rs` — `mod tests` block (added by #741)

Six `TuiDriver`-based black-box tests guard the coord-tui rendering:
`tuidriver_kanban_view_activated_by_key_6`,
`tuidriver_kanban_in_flight_column_shows_running_issue`,
`tuidriver_key_1_returns_to_board_from_merge_queue`,
`tuidriver_key_3_switches_to_pipeline`,
`tuidriver_right_click_opens_board_context_menu`,
`tuidriver_pipeline_summary_tab_renders`.

Run: `cargo test` from `tui/`.

---

## Phone webapp surface (TypeScript)

**Files:**
- `coord/dashboard/webapp/src/components/__tests__/` — Vitest unit tests
- `coord/dashboard/webapp/e2e/smoke.spec.ts` — Playwright E2E (added by #741)

Run: `npm test` (Vitest) and `npm run test:e2e` (Playwright) from
`coord/dashboard/webapp/`.

Both suites run in CI (`test.yml` → `e2e` job added by #741).

---

## Green on `main`?

All tests in this baseline were green on `main` at the time of #741 merge.
Before starting any decomposition task, run `pytest` and `cargo test` (in `tui/`)
to confirm you are starting from green.  **If either is red, fix it before
decomposing** — you cannot tell what your refactor broke if the baseline is
already broken.
