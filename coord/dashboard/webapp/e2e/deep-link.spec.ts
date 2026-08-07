/**
 * Deep-link cold load + the phone regression net (#1551, M-W1's exit gate).
 *
 * "Cold load" means the FIRST navigation Playwright makes is the deep link
 * itself (`page.goto('/pipeline/api/42')`) -- never Home first, then a click.
 * `shell.spec.ts`'s "the selected view survives a reload" gets partway there
 * (a `reload()` re-runs the app's URL -> view resolution) but only for the
 * Sessions view, and only at the wide viewport. This file is the direct
 * `page.goto(deepPath)` version, at both breakpoints, for both a rail view
 * and a pipeline item's detail route -- the two `shellViewFromPath` /
 * `paths.pipelineItem` shapes `routes/paths.ts` (#1548) exists to make
 * addressable.
 *
 * The phone regression net closes this milestone's other named criterion:
 * "the flows that exist today still work at narrow width." `smoke.spec.ts`
 * (#741) is that net's primary home and stays exactly as it is; the test
 * below is a deliberately cheap, single end-to-end chain through the same
 * three flows (render -> filter -> drill in -> back), run here so this
 * milestone's own file carries a self-contained assertion of it rather than
 * only trusting a cross-file reference.
 *
 * Runs at both breakpoints as distinct Playwright projects ('wide' /
 * 'narrow', see playwright.config.ts).
 *
 * Run: npm run test:e2e
 */
import { test, expect, type Page } from '@playwright/test'

const SEEDED_PIPELINE = [
  {
    assignment_id: 'work-1',
    issue_number: 42,
    issue_title: 'Fix the dashboard rendering',
    repo_name: 'api',
    machine_name: 'laptop',
    current_stage: 'coding',
    stages: [
      { name: 'coding', status: 'active', is_current: true },
      { name: 'review', status: 'waiting', is_current: false },
      { name: 'merge', status: 'waiting', is_current: false },
    ],
    available_gates: [],
    progress_pct: 20,
    review_findings_pending: false,
    review_verdict: null,
    review_findings_body: null,
    test_verdict: null,
  },
  {
    assignment_id: 'work-2',
    issue_number: 99,
    issue_title: 'Refactor merge queue',
    repo_name: 'api',
    machine_name: 'server',
    current_stage: 'review_running',
    stages: [
      { name: 'coding', status: 'completed', is_current: false },
      { name: 'review', status: 'active', is_current: true },
      { name: 'merge', status: 'waiting', is_current: false },
    ],
    available_gates: [],
    progress_pct: 60,
    review_findings_pending: false,
    review_verdict: null,
    review_findings_body: null,
    test_verdict: null,
  },
]

const SEEDED_SESSIONS = [
  {
    session_id: 'sess-1',
    session_name: 'coord-sess-1',
    machine: 'dellserver',
    host: 'dellserver.local',
    repo: 'api',
    issue: 7,
    issue_title: 'Live session takeover',
    stage: 'work',
    status: 'running',
    attached: false,
    pane_dead: false,
  },
]

async function mockApi(page: Page): Promise<void> {
  await page.route('**/api/pipeline', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(SEEDED_PIPELINE) }),
  )
  await page.route('**/api/sessions', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(SEEDED_SESSIONS) }),
  )
  await page.route('**/api/board', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ round_number: 1, active: [], completed: [] }),
    }),
  )
  await page.route('**/api/diff/**', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ diff: '', source: 'compare' }) }),
  )
}

const detail = (page: Page) => page.locator('[data-region="detail"]')
const list = (page: Page) => page.locator('[data-region="list"]')

test.describe('deep-link cold load (#1551)', () => {
  test('goto /sessions cold restores the Sessions view', async ({ page }, testInfo) => {
    await mockApi(page)
    await page.goto('/sessions')

    await expect(page.getByRole('heading', { name: 'Sessions' })).toBeVisible()

    if (testInfo.project.name === 'wide') {
      await expect(page.locator('[data-region="rail"]').getByRole('button', { name: /^Sessions/ })).toHaveAttribute(
        'aria-current',
        'page',
      )
    }
  })

  test('goto a pipeline item URL cold restores that item\'s detail, without visiting Home first', async ({
    page,
  }, testInfo) => {
    await mockApi(page)
    await page.goto('/pipeline/api/42')

    await expect(detail(page).getByText('Fix the dashboard rendering')).toBeVisible()

    if (testInfo.project.name === 'wide') {
      // Cold-loading straight into a detail route still gives wide its whole
      // three-column layout -- the list panel didn't need a Home visit first.
      await expect(list(page).getByText('Refactor merge queue')).toBeVisible()
    } else {
      // Narrow's drill-in: landing directly on a detail URL shows only the
      // detail, and Back returns to the list -- the list was never dropped,
      // it just wasn't the first thing rendered.
      await expect(list(page)).toHaveCount(0)
      await page.getByLabel('Back').click()
      await expect(list(page).getByText('Fix the dashboard rendering')).toBeVisible()
      await expect(detail(page)).toHaveCount(0)
    }
  })
})

test.describe('phone regression net (#1551, #741)', () => {
  test('render -> filter -> open detail -> back still all work at narrow width', async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== 'narrow', 'narrow-only — see smoke.spec.ts for the full net')

    await mockApi(page)
    await page.goto('/')

    // Render: seeded cards visible.
    await expect(page.getByText('Fix the dashboard rendering')).toBeVisible()
    await expect(page.getByText('Refactor merge queue')).toBeVisible()

    // Filter: Needs-me hides both (seeded items carry no available_gates).
    await page.getByRole('tab', { name: /needs.me/i }).click()
    await expect(page.getByText('Fix the dashboard rendering')).toHaveCount(0)
    await page.getByRole('tab', { name: 'Active' }).click()

    // Drill in: click opens detail at the addressable repo/issue URL (#1548).
    await page.getByText('Fix the dashboard rendering').click()
    await expect(page).toHaveURL(/\/pipeline\/api\/42/)
    await expect(detail(page).getByText('Fix the dashboard rendering')).toBeVisible()

    // Back: returns to the list, drill-in intact.
    await page.getByLabel('Back').click()
    await expect(list(page).getByText('Fix the dashboard rendering')).toBeVisible()
  })
})
