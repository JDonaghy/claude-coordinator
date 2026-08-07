/**
 * Theme coverage for the acceptance slice (#1551, M-W1's exit gate).
 *
 * Nothing in the repo exercised dark/light before this file: `#1546` shipped
 * the tokens, the provider and the toggle, but no E2E spec ever clicked it.
 * "Both themes, including the toggle and its persistence" is one of #1551's
 * named acceptance criteria precisely because a chassis milestone can ship
 * looking done while a whole theme (or a whole breakpoint's access to the
 * toggle) is quietly unreachable -- which is exactly what this file found:
 * `ActivityRail`'s "Theme" button is `{!narrow && ...}` (wide/medium only,
 * `ActivityRail.tsx`), and nothing stood in for it at narrow width. Fixed
 * alongside this spec in `PanelHeader.tsx` -- a `md:hidden` `<ThemeToggle/>`
 * next to the panel title, matching `docs/mocks/web/pipeline-narrow.html`'s
 * per-view topbar (`id="theme"`), which already puts it there for exactly
 * this reason (the bottom nav has no room for it -- see that file's own
 * comment). Both controls share the same accessible name
 * (`Switch to dark/light theme`), so `themeToggle()` below finds whichever
 * one is actually in the accessibility tree at the current viewport --
 * `md:hidden` is a real `display:none`, which a11y-tree role queries already
 * exclude, so there is never a strict-mode ambiguity between the two.
 *
 * Runs at both breakpoints as distinct Playwright projects ('wide' /
 * 'narrow', see playwright.config.ts) rather than a single project's
 * `test.use({ viewport })` override, so a theme regression pinned to one
 * breakpoint shows up as its own failing project/line rather than being
 * folded into a single generic run.
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
]

async function mockApi(page: Page): Promise<void> {
  await page.route('**/api/pipeline', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(SEEDED_PIPELINE) }),
  )
  await page.route('**/api/sessions', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: '[]' }),
  )
  await page.route('**/api/board', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ round_number: 1, active: [], completed: [] }),
    }),
  )
}

/** Whichever theme toggle is actually in the accessibility tree at this
 * viewport -- the rail's (wide/medium) or PanelHeader's (narrow). Exactly
 * one exists at a time; see this file's header comment. */
function themeToggle(page: Page) {
  return page.getByRole('button', { name: /Switch to (dark|light) theme/ })
}

function htmlTheme(page: Page): Promise<string | null> {
  return page.evaluate(() => document.documentElement.getAttribute('data-theme'))
}

function storedTheme(page: Page): Promise<string | null> {
  return page.evaluate(() => window.localStorage.getItem('coord-web-theme'))
}

// Pin the emulated OS preference so "no stored theme yet" resolves
// deterministically to dark (the documented default, docs/mocks/web/README.md)
// rather than depending on Playwright's own default colour scheme.
test.use({ colorScheme: 'dark' })

test.describe('theme (#1551)', () => {
  test('defaults to dark, and the toggle flips data-theme on <html> to light and back', async ({ page }) => {
    await mockApi(page)
    await page.goto('/')

    await expect.poll(() => htmlTheme(page)).toBe('dark')
    await expect(themeToggle(page)).toHaveAccessibleName('Switch to light theme')

    await themeToggle(page).click()
    await expect.poll(() => htmlTheme(page)).toBe('light')
    await expect(themeToggle(page)).toHaveAccessibleName('Switch to dark theme')

    await themeToggle(page).click()
    await expect.poll(() => htmlTheme(page)).toBe('dark')
  })

  test('switching theme visibly changes the rendered surface, not just the attribute', async ({ page }) => {
    await mockApi(page)
    await page.goto('/')

    const surface = page.locator('body')
    const darkBg = await surface.evaluate((el) => getComputedStyle(el).backgroundColor)

    await themeToggle(page).click()
    await expect.poll(() => htmlTheme(page)).toBe('light')
    const lightBg = await surface.evaluate((el) => getComputedStyle(el).backgroundColor)

    expect(lightBg).not.toEqual(darkBg)
  })

  test('the choice persists to localStorage and survives a reload with no flash back to dark', async ({
    page,
  }) => {
    await mockApi(page)
    await page.goto('/')

    await themeToggle(page).click()
    await expect.poll(() => htmlTheme(page)).toBe('light')
    await expect.poll(() => storedTheme(page)).toBe('light')

    await page.reload()

    // Asserted immediately after load, not just eventually -- index.html's
    // blocking inline script is what's supposed to prevent a dark flash
    // before React hydrates; a poll alone wouldn't catch a one-frame flash.
    await expect.poll(() => htmlTheme(page), { timeout: 2_000 }).toBe('light')
    await expect(themeToggle(page)).toHaveAccessibleName('Switch to dark theme')
  })
})
