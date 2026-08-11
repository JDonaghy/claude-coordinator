/**
 * #2084 — terminal assignments must not inflate the "Needs me" badge.
 *
 * `coord/pipeline.py`'s gate projection used to offer "Dispatch
 * Review"/"Queue for Merge"/"Record Test Verdict" on assignments that had
 * already finished the whole pipeline — chiefly `status == "merged"` rows
 * `coord.reconcile`'s GitHub-truth sweep (`work_is_terminal`) sets
 * independently of the merge queue, which `compute_pipeline` had no branch
 * for and silently treated as indistinguishable from a work item that had
 * never been dispatched anywhere. On the live board this inflated
 * `available_gates` (and therefore the webapp's "Needs me" badge, per
 * `src/lib/pipeline.ts`'s `needsMe = available_gates.length > 0`) to nearly
 * the size of the whole board.
 *
 * This is the black-box regression CLAUDE.md's acceptance bar asks for: a
 * REAL `coord web --fixture` process (not a `page.route()` intercept) runs
 * the actual `compute_pipeline` server-side against a seeded board mixing
 * genuinely-terminal assignments with one genuinely-live one, and the
 * assertions read the rendered "Needs me"/"Active" tab badges — the same
 * counts an operator sees. `tests/test_pipeline.py` covers the unit-level
 * half of the fix; `tests/fixtures/board-pipeline-terminal-gates.json` is
 * this spec's fixture (see that file's header for the three seeded rows).
 *
 * Run: npm run test:e2e (requires `coord` on $PATH, see fixtureServer.ts)
 */
import { test, expect } from '@playwright/test'
import path from 'node:path'
import { startFixtureServer, REPO_ROOT, type FixtureServerHandle } from './fixtureServer'

const FIXTURE_PATH = path.join(REPO_ROOT, 'tests/fixtures/board-pipeline-terminal-gates.json')

test.describe('terminal assignments offer no gates (#2084)', () => {
  let server: FixtureServerHandle

  test.beforeAll(async () => {
    server = await startFixtureServer(FIXTURE_PATH)
  })

  test.afterAll(async () => {
    await server?.stop()
  })

  test('Needs me counts only the genuinely-actionable row, not the merged/advisory ones', async ({
    page,
  }) => {
    await page.goto(server.baseUrl)

    // Sanity: the real dist bundle booted against the real fixture data. The
    // Active tab (selected on load) collapses both "done" rows (#5201,
    // #5203 — neither is "merged") into the "Work done" section (#1218), so
    // assert on that collapsed count rather than a card's visibility.
    await expect(page.getByRole('button', { name: 'Work done (2)' })).toBeVisible()

    // "Needs me": only work-needs-me (#5201) has a genuinely-offerable gate.
    // work-merged-direct (#5202, status="merged" with no merge_queue entry —
    // the #2084 repro) and work-advisory (#5203, status="advisory") must
    // both read zero.
    const needsMeTab = page.getByRole('tab', { name: /^Needs me/ })
    await expect(needsMeTab).toHaveText('Needs me1')

    await needsMeTab.click()
    const list = page.getByRole('region', { name: 'Items needing attention' })
    await expect(list.getByText('#5201')).toBeVisible()
    await expect(list.getByText('#5202')).toHaveCount(0)
    await expect(list.getByText('#5203')).toHaveCount(0)

    // "Active": current_stage != "merged" — work-merged-direct (#5202) is
    // correctly excluded (it finished the whole pipeline), while
    // work-advisory (#5203, current_stage stays "done" — nothing to gate,
    // but not yet aged/settled either) still counts as in-flight alongside
    // work-needs-me (#5201). Two, not three.
    const activeTab = page.getByRole('tab', { name: /^Active/ })
    await expect(activeTab).toHaveText('Active2')
  })
})
