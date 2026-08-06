/**
 * E2E coverage for the responsive shell (#1547) — "acceptance coverage at both
 * breakpoints (Playwright viewport sizes)".
 *
 * `smoke.spec.ts` is pinned to a phone and guards the narrow app's existing
 * flows; this file drives the layouts that only exist in a real browser with
 * real layout: the three-column wide composition, the mid-width icon rail +
 * detail overlay, and the persistence of panel geometry across a reload.
 *
 * Everything is asserted through `[data-region="…"]`-scoped locators rather
 * than page-wide text, because the point of the wide layout is that the same
 * issue title is legitimately on screen twice (once as a list row, once as the
 * detail heading) — a page-wide `getByText` would be a strict-mode violation
 * *by design*.
 *
 * Run: npm run test:e2e
 */

import { test, expect, type Page } from '@playwright/test'

const PHONE = { width: 390, height: 844 }
const TABLET = { width: 900, height: 900 }
const DESKTOP = { width: 1440, height: 900 }

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

// Deliberately a *different* issue title from anything in SEEDED_PIPELINE:
// Home surfaces live sessions above the pipeline cards, so a shared title
// would resolve twice inside the list region and every row locator below
// would trip strict mode for reasons that have nothing to do with the shell.
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

const shell = (page: Page) => page.locator('[data-shell-mode]')
const rail = (page: Page) => page.locator('[data-region="rail"]')
const list = (page: Page) => page.locator('[data-region="list"]')
const detail = (page: Page) => page.locator('[data-region="detail"]')

// ── wide ──────────────────────────────────────────────────────────────────────

test.describe('shell — wide viewport', () => {
  test.use({ viewport: DESKTOP })

  test('renders rail, list and detail side by side', async ({ page }) => {
    await mockApi(page)
    await page.goto('/')

    await expect(shell(page)).toHaveAttribute('data-shell-mode', 'wide')
    await expect(rail(page)).toBeVisible()
    await expect(list(page)).toBeVisible()
    await expect(detail(page)).toBeVisible()

    // The list is the pipeline; the detail is the empty-selection placeholder.
    await expect(list(page).getByText('Fix the dashboard rendering')).toBeVisible()
    await expect(detail(page).getByText('Nothing selected')).toBeVisible()

    // Left to right, actually laid out as three columns.
    const [railBox, listBox, detailBox] = await Promise.all([
      rail(page).boundingBox(),
      list(page).boundingBox(),
      detail(page).boundingBox(),
    ])
    expect(railBox!.x).toBeLessThan(listBox!.x)
    expect(listBox!.x).toBeLessThan(detailBox!.x)
  })

  test('shows unbuilt panels in the rail as "soon" rather than hiding them', async ({ page }) => {
    await mockApi(page)
    await page.goto('/')

    await expect(rail(page).getByRole('button', { name: /Milestones/ })).toHaveAttribute(
      'aria-disabled',
      'true',
    )
    await expect(rail(page).getByText('soon').first()).toBeVisible()
  })

  test('selecting a row fills the detail column and keeps the list', async ({ page }) => {
    await mockApi(page)
    await page.goto('/')

    await list(page).getByText('Fix the dashboard rendering').click()
    await expect(page).toHaveURL(/work-1/)

    await expect(detail(page).getByText('Fix the dashboard rendering')).toBeVisible()
    // The list did not go anywhere — that's what makes this the wide layout.
    await expect(list(page).getByText('Refactor merge queue')).toBeVisible()
  })

  test('F6 cycles focus rail -> list -> detail', async ({ page }) => {
    await mockApi(page)
    await page.goto('/')
    await expect(list(page).getByText('Fix the dashboard rendering')).toBeVisible()

    const focusedRegion = () =>
      page.evaluate(() => document.activeElement?.getAttribute('data-region') ?? null)

    await page.keyboard.press('F6')
    expect(await focusedRegion()).toBe('rail')
    await page.keyboard.press('F6')
    expect(await focusedRegion()).toBe('list')
    await page.keyboard.press('F6')
    expect(await focusedRegion()).toBe('detail')
    await page.keyboard.press('F6')
    expect(await focusedRegion()).toBe('rail')

    await page.keyboard.press('Shift+F6')
    expect(await focusedRegion()).toBe('detail')
  })

  test('dragging the separator resizes the list, and the width survives a reload', async ({
    page,
  }) => {
    await mockApi(page)
    await page.goto('/')
    await expect(list(page).getByText('Fix the dashboard rendering')).toBeVisible()

    const before = (await list(page).boundingBox())!.width
    const handle = page.getByRole('separator', { name: 'Resize list panel' })
    const handleBox = (await handle.boundingBox())!

    await page.mouse.move(handleBox.x + handleBox.width / 2, handleBox.y + handleBox.height / 2)
    await page.mouse.down()
    await page.mouse.move(handleBox.x + handleBox.width / 2 + 90, handleBox.y + handleBox.height / 2, {
      steps: 8,
    })
    await page.mouse.up()

    await expect
      .poll(async () => (await list(page).boundingBox())!.width)
      .toBeGreaterThan(before + 60)

    const widened = (await list(page).boundingBox())!.width
    await page.reload()
    await expect(list(page).getByText('Fix the dashboard rendering')).toBeVisible()
    expect(Math.abs((await list(page).boundingBox())!.width - widened)).toBeLessThan(2)
  })

  test('the selected view survives a reload', async ({ page }) => {
    await mockApi(page)
    await page.goto('/')

    await rail(page).getByRole('button', { name: /^Sessions/ }).click()
    await expect(list(page).getByRole('heading', { name: 'Sessions' })).toBeVisible()

    await page.reload()
    await expect(list(page).getByRole('heading', { name: 'Sessions' })).toBeVisible()
  })

  test('minimising the list gives the whole width to the detail', async ({ page }) => {
    await mockApi(page)
    await page.goto('/')
    await expect(list(page)).toBeVisible()

    await rail(page).getByRole('button', { name: 'Minimize list panel' }).click()
    await expect(list(page)).toHaveCount(0)
    await expect(shell(page)).toHaveAttribute('data-panel', 'collapsed')

    await rail(page).getByRole('button', { name: 'Show list panel' }).click()
    await expect(list(page)).toBeVisible()
  })
})

// ── mid ───────────────────────────────────────────────────────────────────────

test.describe('shell — mid viewport', () => {
  test.use({ viewport: TABLET })

  test('collapses the rail to icons and overlays the detail on the list', async ({ page }) => {
    await mockApi(page)
    await page.goto('/')

    await expect(shell(page)).toHaveAttribute('data-shell-mode', 'medium')
    // Icon strip: narrow, and the labels are gone.
    expect((await rail(page).boundingBox())!.width).toBeLessThan(80)
    await expect(rail(page).getByText('soon')).toHaveCount(0)

    await list(page).getByText('Fix the dashboard rendering').click()

    // The list is still mounted underneath, but inert — a drill-in that
    // doesn't cost you the list you drilled in from.
    await expect(list(page)).toHaveAttribute('inert', '')
    const [listBox, detailBox] = await Promise.all([
      list(page).boundingBox(),
      detail(page).boundingBox(),
    ])
    expect(Math.abs(listBox!.x - detailBox!.x)).toBeLessThan(2)
  })
})

// ── narrow ────────────────────────────────────────────────────────────────────

test.describe('shell — narrow viewport', () => {
  test.use({ viewport: PHONE })

  test('keeps the phone drill-in: list, then detail, then back', async ({ page }) => {
    await mockApi(page)
    await page.goto('/')

    await expect(shell(page)).toHaveAttribute('data-shell-mode', 'narrow')
    await expect(list(page)).toBeVisible()
    await expect(detail(page)).toHaveCount(0)

    await list(page).getByText('Fix the dashboard rendering').click()
    await expect(detail(page)).toBeVisible()
    // Drill-in: the list is gone, not merely covered.
    await expect(list(page)).toHaveCount(0)

    await page.getByLabel('Back').click()
    await expect(list(page)).toBeVisible()
    await expect(detail(page)).toHaveCount(0)
  })

  test('puts the rail at the bottom of the screen with only built views', async ({ page }) => {
    await mockApi(page)
    await page.goto('/')

    const [railBox, listBox] = await Promise.all([rail(page).boundingBox(), list(page).boundingBox()])
    expect(railBox!.y).toBeGreaterThan(listBox!.y)
    expect(railBox!.width).toBeGreaterThan(300)

    await expect(rail(page).getByRole('button', { name: /Pipeline/ })).toBeVisible()
    await expect(rail(page).getByRole('button', { name: /Milestones/ })).toHaveCount(0)
  })

  test('switches views from the bottom row', async ({ page }) => {
    await mockApi(page)
    await page.goto('/')

    await rail(page).getByRole('button', { name: /Sessions/ }).click()
    await expect(list(page).getByRole('heading', { name: 'Sessions' })).toBeVisible()

    await rail(page).getByRole('button', { name: /Pipeline/ }).click()
    await expect(list(page).getByRole('heading', { name: 'Pipeline' })).toBeVisible()
  })
})
