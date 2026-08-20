/**
 * ms-51 sealed acceptance slice — Home, Active tab (#1544, M-W0 exit gate).
 *
 * Contract: tests/acceptance/ms-51/contract.md
 * Mock:     tests/acceptance/ms-51/mocks/home-active.html
 *
 * Authored by an INDEPENDENT test-author session from the contract + mock
 * alone. Every assertion below cites the contract clause it encodes; nothing
 * here was derived by reading Home.tsx / PipelineCard.tsx.
 *
 * Note on colour of run: unlike a normal work-order slice, this one is
 * expected to be GREEN on its first execution — the contract deliberately
 * pins the *already-shipped* Home screen so that #1544 can prove the oracle
 * machinery (mock -> test-author -> driver -> routing -> verdict) composes.
 * See contract.md, "What this contract is for", point 1. The deliberate red
 * comes later, at #1544's step 5, under the operator's control.
 *
 * Run (the declared web-playwright driver command):
 *   cd coord/dashboard/webapp && npm run test:acceptance -- ms-51
 *
 * Seeding: playwright.acceptance.config.ts boots the Vite dev server, so —
 * exactly as that config's docstring requires — this spec intercepts its own
 * API calls with page.route(). No daemon, no live fleet, no network.
 */

import { test, expect, type Locator, type Page } from '@playwright/test'

// ── Seed ─────────────────────────────────────────────────────────────────────

/**
 * The /api/pipeline payload behind the mock's screen.
 *
 * Three non-merged items -> Active tab count `3` (contract §2d); two of them
 * in progress -> two visible cards (§3, §4); one "done-ish" (`review_done`)
 * -> collapsed `Work done (1)` (§5). Count != visible cards is the trap §2d
 * calls out by name.
 *
 * The `stages` arrays are the real four-entry `coding, review, smoke, merge`
 * strip the API emits today — including `smoke: skipped` on every item, which
 * is one of the two bugs contract §4d/§4f pin on purpose (#1724). Amend the
 * contract and re-author this slice when #1724 lands.
 *
 * No item carries `available_gates`, per §2e.
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
      { name: 'smoke', status: 'skipped', is_current: false },
      { name: 'merge', status: 'waiting', is_current: false },
    ],
    available_gates: [],
    progress_pct: 20,
    review_findings_pending: false,
    review_verdict: null,
    review_verdict_original: null,
    review_verdict_override_reason: null,
    review_findings_body: null,
    test_verdict: null,
    needs_attention: false,
    needs_attention_reason: null,
    needs_attention_detail: null,
    finished_at: null,
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
      { name: 'smoke', status: 'skipped', is_current: false },
      { name: 'merge', status: 'waiting', is_current: false },
    ],
    available_gates: [],
    progress_pct: 60,
    review_findings_pending: false,
    review_verdict: null,
    review_verdict_original: null,
    review_verdict_override_reason: null,
    review_findings_body: null,
    test_verdict: null,
    needs_attention: false,
    needs_attention_reason: null,
    needs_attention_detail: null,
    finished_at: null,
  },
  {
    // The third non-merged item: counted by the Active tab, NOT shown as a
    // card — it is collapsed into "Work done (1)" (§2d's note, §5c, #1218).
    assignment_id: 'work-3',
    issue_number: 7,
    issue_title: 'Tidy the changelog',
    repo_name: 'api',
    machine_name: 'laptop',
    current_stage: 'review_done',
    stages: [
      { name: 'coding', status: 'completed', is_current: false },
      { name: 'review', status: 'completed', is_current: true },
      { name: 'smoke', status: 'skipped', is_current: false },
      { name: 'merge', status: 'waiting', is_current: false },
    ],
    available_gates: [],
    progress_pct: 80,
    review_findings_pending: false,
    review_verdict: 'approve',
    review_verdict_original: null,
    review_verdict_override_reason: null,
    review_findings_body: null,
    test_verdict: null,
    needs_attention: false,
    needs_attention_reason: null,
    needs_attention_detail: null,
    finished_at: 1750000000,
  },
]

/**
 * Serve every /api/** call this screen can make from static seed data, so the
 * dev server never 404s a request into an error state the contract puts out
 * of scope ("Not in scope": loading / error / empty states).
 */
async function seedApi(page: Page): Promise<void> {
  // Predicate, not a glob: `'**/api/**'` also matches the dev server's own
  // ESM module URL `/src/api/client.ts`, which then gets served as JSON and
  // the whole app fails to boot into a blank `<div id="root">`. Anchor on a
  // path that STARTS with /api/ instead.
  await page.route(url => url.pathname.startsWith('/api/'), async route => {
    const path = new URL(route.request().url()).pathname
    let body: unknown = {}
    if (path.endsWith('/api/pipeline')) body = SEEDED_PIPELINE
    else if (path.endsWith('/api/board')) body = { round_number: 1, active: [], completed: [] }
    else if (path.endsWith('/api/sessions')) body = []
    else if (path.endsWith('/api/machines')) body = []
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(body),
    })
  })
}

// ── Helpers ──────────────────────────────────────────────────────────────────

/** The whole-card button for one seeded item (§3b: the card IS the button). */
function card(page: Page, title: string): Locator {
  return page.getByRole('button', { name: new RegExp(escapeRe(title)) })
}

/** The stage chips of one card, in DOM order (§4d). */
function chips(cardLocator: Locator): Locator {
  return cardLocator.getByText(/^\s*(work|review|test|merge)\s*$/i)
}

/**
 * The rendering of one chip, reduced to the properties that can distinguish
 * one stage state from another.
 *
 * TODO(test-author): the contract gives chip state (completed / current /
 * skipped) NO semantic handle — no role, no aria-*, no accessible name
 * difference — while §"Selector policy" forbids asserting on class names and
 * "Not in scope" excludes colour. The §4e/§4f assertions below therefore
 * test the *relationships* the contract states in prose ("chip state is
 * derived per stage, not from one global status"; the test chip is "dimmed")
 * rather than pinning any specific palette value. If a future change wants a
 * firmer hook, add an aria-* or data-testid to the app AND amend the
 * contract — see contract.md, "Amending".
 */
async function chipStyle(chip: Locator): Promise<string> {
  return chip.evaluate(el => {
    const s = getComputedStyle(el)
    return [s.opacity, s.color, s.backgroundColor, s.borderColor, s.fontWeight].join('|')
  })
}

async function chipOpacity(chip: Locator): Promise<number> {
  return chip.evaluate(el => parseFloat(getComputedStyle(el).opacity))
}

function escapeRe(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

// ── Tests ────────────────────────────────────────────────────────────────────

test.describe('ms-51 Home Active tab (#1544)', () => {
  test.beforeEach(async ({ page }) => {
    await seedApi(page)
    await page.goto('/')
  })

  /**
   * §1a — an h1 whose exact text is `Pipeline`.
   * §1b — a mono count element reading `3 tracked`.
   *
   * RE-AUTHORED against the amended §1 (#1950/#1951, commit c6ab101). #1547
   * replaced the old `coord` / `pipeline` header with the shared PanelHeader,
   * and `coord`/`pipeline` no longer appear in the header at all — but the
   * contract was amended without re-authoring this slice, so the test kept
   * asserting the pre-#1547 shape and went red. That drift between a contract
   * and its executable half is precisely what contract.md's "Amending" section
   * forbids, and what this oracle exists to catch.
   *
   * Deliberately NOT asserted: the `aria-hidden="true"` `co` badge that
   * precedes the h1 — §1b calls it decorative and breakpoint-dependent (only
   * visible below md/768px) and instructs the slice not to assert it.
   */
  test('header shows the Pipeline title and the tracked count', async ({ page }) => {
    await expect(
      page.getByRole('heading', { level: 1, name: 'Pipeline', exact: true }),
    ).toBeVisible()                                                          // §1a
    await expect(page.getByText('3 tracked', { exact: true })).toBeVisible() // §1b
  })

  /**
   * §2a — a tablist named `Pipeline filters`.
   * §2b — exactly two tabs, `Active` and `Needs me`.
   * §2c — `Active` is selected on load, `Needs me` is not.
   */
  test('filter tablist offers Active and Needs me with Active selected by default', async ({ page }) => {
    const tablist = page.getByRole('tablist', { name: 'Pipeline filters' })
    await expect(tablist).toBeVisible()

    await expect(tablist.getByRole('tab')).toHaveCount(2)

    const active = tablist.getByRole('tab', { name: 'Active' })
    const needsMe = tablist.getByRole('tab', { name: 'Needs me' })
    await expect(active).toBeVisible()
    await expect(needsMe).toBeVisible()

    await expect(active).toHaveAttribute('aria-selected', 'true')
    await expect(needsMe).toHaveAttribute('aria-selected', 'false')
  })

  /**
   * §2d — a non-zero count renders as text inside the tab; a zero count
   * renders NO count element. And the count is emphatically NOT the number of
   * visible cards: three non-merged items, two in-progress cards, one folded
   * into `Work done`. Asserted separately, exactly as §2d demands.
   */
  test('Active tab counts every non-merged item while Needs me shows no count', async ({ page }) => {
    const tablist = page.getByRole('tablist', { name: 'Pipeline filters' })

    // Non-zero count -> rendered inside the tab.
    await expect(tablist.getByRole('tab', { name: 'Active' })).toContainText('3')

    // Zero count -> no count element at all: the tab's text is just its label.
    await expect(tablist.getByRole('tab', { name: 'Needs me' })).toHaveText(/^\s*Needs me\s*$/)

    // ...and the count is NOT the number of cards below it.
    await expect(page.getByRole('region', { name: 'Active items' }).getByRole('button')).toHaveCount(2)
  })

  /**
   * §2e — activating `Needs me` moves the selection to it, and with the
   * seeded fixture (no item has `available_gates`) the pipeline cards are no
   * longer visible.
   *
   * TODO(test-author): §3a says the list region is named `Items needing
   * attention` while `Needs me` is selected, but the contract is silent on
   * whether that region renders at all when the filtered set is empty (the
   * empty state is explicitly "Not in scope"). Unasserted here rather than
   * guessed.
   */
  test('activating Needs me selects it and hides the pipeline cards', async ({ page }) => {
    const tablist = page.getByRole('tablist', { name: 'Pipeline filters' })
    const active = tablist.getByRole('tab', { name: 'Active' })
    const needsMe = tablist.getByRole('tab', { name: 'Needs me' })

    await needsMe.click()

    await expect(needsMe).toHaveAttribute('aria-selected', 'true')
    await expect(active).toHaveAttribute('aria-selected', 'false')

    await expect(page.getByText('Fix the dashboard rendering')).toHaveCount(0)
    await expect(page.getByText('Refactor merge queue')).toHaveCount(0)
  })

  /**
   * §3a — a region named `Active items` while the Active tab is selected.
   * §3b — one card per in-flight item, each a `button`: the whole card is the
   * tap target, not a link.
   */
  test('Active items region holds one button card per in-flight item', async ({ page }) => {
    const region = page.getByRole('region', { name: 'Active items' })
    await expect(region).toBeVisible()

    await expect(region.getByRole('button')).toHaveCount(2)
    await expect(region.getByRole('button', { name: /Fix the dashboard rendering/ })).toBeVisible()
    await expect(region.getByRole('button', { name: /Refactor merge queue/ })).toBeVisible()

    // Not a link (§3b).
    await expect(region.getByRole('link')).toHaveCount(0)
  })

  /**
   * §4a — the issue title.
   * §4b — the repo name, the issue number and the machine name.
   * §4c — a status badge reading `running`.
   */
  test('pipeline card for issue 42 shows title, repo, number, machine and status', async ({ page }) => {
    const c = card(page, 'Fix the dashboard rendering')
    await expect(c).toBeVisible()

    await expect(c.getByText('Fix the dashboard rendering')).toBeVisible()   // §4a
    await expect(c).toContainText('api')                                     // §4b
    await expect(c).toContainText('#42')                                     // §4b
    await expect(c).toContainText('laptop')                                  // §4b
    await expect(c.getByText('running', { exact: true })).toBeVisible()      // §4c
  })

  /**
   * §4b — the issue number is rendered in a monospace element ("monospace
   * means a value the machine owns").
   *
   * TODO(test-author): this is the one place the contract pins something the
   * "Not in scope: any visual property ... font" clause would otherwise
   * exclude. §4b states it explicitly and gives it a meaning, so it is
   * asserted — but on the computed font-family only, never on a class name.
   */
  test('issue number is rendered in a monospace element', async ({ page }) => {
    const number = card(page, 'Fix the dashboard rendering').getByText('#42', { exact: true })
    await expect(number).toBeVisible()

    const fontFamily = await number.evaluate(el => getComputedStyle(el).fontFamily)
    expect(fontFamily.toLowerCase()).toMatch(/mono/)
  })

  /**
   * §4d — FOUR stage chips, labelled `work`, `review`, `test`, `merge`, in
   * that order. Display names, not the API's stage names (`coding -> work`,
   * `smoke -> test`) — asserting the API name here would pass against the
   * wrong thing.
   *
   * The Review-before-Test order is a known bug (#1724), pinned on purpose so
   * #1544's first run cannot go red for a reason unrelated to the machinery
   * it exists to prove. Re-author this test when #1724 lands.
   */
  test('pipeline card renders four stage chips in work review test merge order', async ({ page }) => {
    // toHaveText(array) pins BOTH the count (exactly four) and the DOM order,
    // and auto-retries — unlike allTextContents(), which snapshots whatever
    // has rendered so far and races the first paint.
    await expect(chips(card(page, 'Fix the dashboard rendering')))
      .toHaveText(['work', 'review', 'test', 'merge'])
  })

  /**
   * §4e — for item #99 the `work` chip renders as completed and `review` as
   * the current stage: chip state is derived per stage, not from one global
   * status. Asserted as the distinctions the contract states in prose (see
   * chipStyle's TODO), not as any particular colour.
   */
  test('chip state is derived per stage on issue 99 not from one global status', async ({ page }) => {
    const c = card(page, 'Refactor merge queue')
    const chip = chips(c)
    await expect(chip).toHaveCount(4)

    const [work, review, , merge] = [chip.nth(0), chip.nth(1), chip.nth(2), chip.nth(3)]
    const [workStyle, reviewStyle, mergeStyle] = await Promise.all([
      chipStyle(work), chipStyle(review), chipStyle(merge),
    ])

    // completed (work) != current (review): two different states on one card.
    expect(workStyle).not.toEqual(reviewStyle)
    // completed (work) != waiting (merge).
    expect(workStyle).not.toEqual(mergeStyle)
    // current (review) != waiting (merge).
    expect(reviewStyle).not.toEqual(mergeStyle)
  })

  /**
   * §4f — the `test` chip renders in the skipped style (dimmed).
   *
   * Bug #1724 pinned on purpose: the projection calls the gate `smoke` while
   * the config calls it `test`, so the chip is emitted as skipped on EVERY
   * item regardless of the real verdict. Re-author when #1724 lands.
   */
  test('test chip renders dimmed in the skipped style', async ({ page }) => {
    const chip = chips(card(page, 'Refactor merge queue'))
    const testChip = chip.nth(2)
    await expect(testChip).toHaveText(/^\s*test\s*$/i)

    // Dimmed, both absolutely and relative to a plain waiting chip.
    expect(await chipOpacity(testChip)).toBeLessThan(1)
    expect(await chipStyle(testChip)).not.toEqual(await chipStyle(chip.nth(3)))
  })

  /**
   * §5a — a region named `Work done`.
   * §5b — its toggle is a button with aria-expanded="false" on load (#1218).
   * §5c — the toggle's text includes the item count in parentheses.
   */
  test('Work done section is collapsed by default and shows its item count', async ({ page }) => {
    const region = page.getByRole('region', { name: 'Work done' })
    await expect(region).toBeVisible()

    const toggle = region.getByRole('button')
    await expect(toggle).toHaveAttribute('aria-expanded', 'false')
    await expect(toggle).toContainText('(1)')
  })
})
