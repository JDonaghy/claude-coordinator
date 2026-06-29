/**
 * E2E smoke tests for the coord phone webapp.
 *
 * These form the **Playwright acceptance gate** called out in issue #741 and
 * CLAUDE.md.  The suite drives a real headless browser against the Vite dev
 * server; API calls are intercepted by Playwright's `page.route()` so no live
 * Python daemon is required.
 *
 * Run:  npm run test:e2e
 *
 * Prerequisites (one-time per machine):
 *   npx playwright install chromium
 *
 * Add new tests here as the webapp grows — one test per new behaviour-changing
 * PR.  Coverage should ratchet upward, never shrink.
 */

import { test, expect, type Page } from '@playwright/test'

// ── Shared seed data (mirrors BoardData / PipelineView shapes) ────────────────

/**
 * A minimal /api/pipeline response with two active items and one completed.
 * Mirrors the `make_test_app(BoardData)` pattern from coord-tui.
 */
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
      { name: 'merge',  status: 'waiting', is_current: false },
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
      { name: 'review', status: 'active',    is_current: true  },
      { name: 'merge',  status: 'waiting',   is_current: false },
    ],
    available_gates: [],
    progress_pct: 60,
    review_findings_pending: false,
    review_verdict: null,
    review_findings_body: null,
    test_verdict: null,
  },
]

/**
 * Install API route mocks on the page so no live backend is required.
 * Call this at the top of every test that navigates to the webapp.
 */
async function mockApi(page: Page): Promise<void> {
  // Pipeline list
  await page.route('**/api/pipeline', async route => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(SEEDED_PIPELINE),
    })
  })
  // Board (used by some views)
  await page.route('**/api/board', async route => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ round_number: 1, active: [], completed: [] }),
    })
  })
}

// ── Tests ─────────────────────────────────────────────────────────────────────

test.describe('coord webapp smoke suite (#741)', () => {
  /**
   * Core smoke 1: the home screen must render at least one PipelineCard for
   * each active item in the seeded /api/pipeline response.
   *
   * This guards the full data-fetch → React render → DOM paint path.
   * A structural rename of `PipelineCard` or `fetchPipeline()` that breaks
   * the render would fail here.
   */
  test('home screen renders pipeline cards from seeded API', async ({ page }) => {
    await mockApi(page)
    await page.goto('/')

    // Both active items must appear as cards.
    await expect(page.getByText('Fix the dashboard rendering')).toBeVisible()
    await expect(page.getByText('Refactor merge queue')).toBeVisible()

    // Issue numbers must be visible.
    await expect(page.getByText('#42')).toBeVisible()
    await expect(page.getByText('#99')).toBeVisible()
  })

  /**
   * Core smoke 2: the "Active" filter tab must be the default, and the
   * "Needs me" tab must switch the visible set to only items with available
   * gate actions.  With the seeded data (no gates), "Needs me" renders empty.
   *
   * Guards the filter-tab click → state update → re-render path.
   */
  test('filter tabs switch between Active and Needs-me', async ({ page }) => {
    await mockApi(page)
    await page.goto('/')

    // Default: both items visible under "Active".
    await expect(page.getByText('Fix the dashboard rendering')).toBeVisible()

    // Click "Needs me" — seeded items have no gates → list should be empty.
    // The filter tabs render with role="tab" (inside role="tablist").
    await page.getByRole('tab', { name: /needs.me/i }).click()

    // Neither card should be visible.
    await expect(page.getByText('Fix the dashboard rendering')).not.toBeVisible()
    await expect(page.getByText('Refactor merge queue')).not.toBeVisible()
  })

  /**
   * Core smoke 3: clicking a pipeline card must navigate to the Detail view
   * for that issue.  The URL must include the assignment id, and the detail
   * panel must render the issue title.
   *
   * Guards the click → react-router navigate → Detail component mount path.
   */
  test('clicking a pipeline card opens its detail view', async ({ page }) => {
    await mockApi(page)
    // Also mock the diff endpoint so the Detail view doesn't get a network error.
    await page.route('**/api/diff/**', async route => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ diff: '', source: 'compare' }),
      })
    })

    await page.goto('/')

    // Click the first card.
    await page.getByText('Fix the dashboard rendering').click()

    // The URL must include the assignment id.
    await expect(page).toHaveURL(/work-1/)

    // The detail view must render the issue title.
    await expect(page.getByText('Fix the dashboard rendering')).toBeVisible()
  })
})
