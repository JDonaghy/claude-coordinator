/**
 * ms-51 sealed acceptance slice, part 2 — contract clauses the first slice
 * left unpinned (#1544, M-W0 exit gate).
 *
 * Contract: tests/acceptance/ms-51/contract.md
 * Mock:     tests/acceptance/ms-51/mocks/home-active.html
 *
 * Authored by an INDEPENDENT test-author session from the contract + mock
 * alone. Every assertion cites the clause it encodes; nothing here was
 * derived by reading Home.tsx / PanelHeader.tsx / PipelineCard.tsx.
 *
 * Why a second file rather than edits to `home-active.spec.ts`: that slice is
 * #1544's proving rig, and its ten green tests are the evidence the oracle
 * machinery composes. Adding coverage there would churn a file whose value is
 * that it is stable. The one edit it did need was mandatory — §1 was amended
 * (#1950/#1951) without the slice being re-authored, so its header test was
 * asserting the pre-#1547 `coord`/`pipeline` shape and had gone red.
 *
 * This file pins three clauses the first slice states but does not assert:
 *
 *   §1b  the header count is a DISTINCT element from the per-tab count, is
 *        monospace, immediately follows the h1, and carries the same value
 *   §3a  the list region is named `Items needing attention` while `Needs me`
 *        is selected — "Both names are part of the contract"
 *   §4e  chip state is derived PER STAGE, not from one global status — pinned
 *        by cross-card comparison, which is sharper than the within-card
 *        inequality the first slice asserts
 *
 * Run (the declared web-playwright driver command):
 *   cd coord/dashboard/webapp && npm run test:acceptance -- ms-51
 *
 * Seeding: ms-51 is the one milestone deliberately left on `page.route()`
 * rather than a `fixtures/*.json` seed — see `playwright.acceptance.config.ts`
 * ("ms-51 (#1544's proving rig) is deliberately left on `page.route()` ...
 * ms-52 onward is where a slice is expected to declare its own
 * `fixtures/*.json`") and #1818's "recommend leaving ms-51 alone". Declaring a
 * fixture here would re-seed the webServer for the WHOLE ms-51 run, putting
 * #1544's proving rig behind a fixture this milestone was explicitly exempted
 * from. This file therefore matches the sibling slice's interception style.
 */

import { test, expect, type Locator, type Page } from '@playwright/test'

// ── Seed ─────────────────────────────────────────────────────────────────────

/**
 * The /api/pipeline payload behind the mock's screen — the same three-item
 * board `home-active.spec.ts` seeds, restated here so this slice is
 * self-contained (a sealed slice that reaches into a sibling's internals is
 * one refactor away from a wiring failure).
 *
 * Three non-merged items -> Active tab count `3` and header count `3 tracked`
 * (§1b, §2d); two in progress -> two visible cards (§3, §4); one "done-ish"
 * (`review_done`) -> collapsed `Work done (1)` (§5). No item carries
 * `available_gates`, per §2e.
 *
 * The `stages` arrays are the real four-entry `coding, review, smoke, merge`
 * strip the API emits today, including `smoke: skipped` on every item — one of
 * the two bugs §4d/§4f pin on purpose (#1724).
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
      // work is the CURRENT stage on this card...
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
      // ...and COMPLETED on this one, with review current instead (§4e).
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
    // Counted by the Active tab, NOT shown as a card — collapsed into
    // "Work done (1)" (§2d's note, §5c, #1218).
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
 * A variant board in which item #42 DOES carry an `available_gates` entry, so
 * the `Needs me` filter is non-empty and §3a's second region name has
 * something to render around.
 *
 * §2e states the mechanism outright — it attributes the empty `Needs me` set
 * to "no item has `available_gates`" — so inverting exactly that field is
 * derived from the contract, not guessed at from the app. The gate's shape
 * ({action, label, endpoint}) is the published `PipelineGate` wire type.
 */
const NEEDS_ME_PIPELINE = [
  {
    ...SEEDED_PIPELINE[0],
    available_gates: [
      {
        action: 'record-review-verdict',
        label: 'Record review verdict',
        endpoint: '/api/pipeline/action',
      },
    ],
  },
  ...SEEDED_PIPELINE.slice(1),
]

/**
 * Serve every /api/** call this screen can make from static seed data, so the
 * server never 404s a request into an error state the contract puts out of
 * scope ("Not in scope": loading / error / empty states).
 *
 * Predicate, not a glob: `'**\/api/**'` would also match a bundled module URL
 * containing `/api/`, which then gets served as JSON and the whole app fails
 * to boot into a blank `<div id="root">`. Anchor on a path that STARTS with
 * `/api/` instead.
 */
async function seedApi(page: Page, pipeline: unknown): Promise<void> {
  await page.route(
    url => url.pathname.startsWith('/api/'),
    async route => {
      const path = new URL(route.request().url()).pathname
      let body: unknown = {}
      if (path.endsWith('/api/pipeline')) body = pipeline
      else if (path.endsWith('/api/board')) body = { round_number: 1, active: [], completed: [] }
      else if (path.endsWith('/api/sessions')) body = []
      else if (path.endsWith('/api/machines')) body = []
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(body),
      })
    },
  )
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
 * waiting / skipped) NO semantic handle — no role, no aria-*, no accessible
 * name difference — while the "Selector policy" section forbids asserting on
 * class names and "Not in scope" excludes colour. So §4e cannot be pinned as
 * "this chip is completed"; it can only be pinned as the *relationships* the
 * contract states in prose. This function exists to compare two chips for
 * same-ness, never to name a palette value. If a future change wants a firmer
 * hook, add an aria-* or data-testid to the app AND amend the contract — see
 * contract.md, "Amending".
 */
async function chipStyle(chip: Locator): Promise<string> {
  return chip.evaluate(el => {
    const s = getComputedStyle(el)
    return [s.opacity, s.color, s.backgroundColor, s.borderColor, s.fontWeight].join('|')
  })
}

function escapeRe(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

// ── Tests ────────────────────────────────────────────────────────────────────

test.describe('ms-51 Home Active tab, extended (#1544)', () => {
  test.describe('seeded board — no item needs attention', () => {
    test.beforeEach(async ({ page }) => {
      await seedApi(page, SEEDED_PIPELINE)
      await page.goto('/')
    })

    /**
     * §1b — the header count "immediately follows the h1", is a *mono* element,
     * and is "a distinct element from the per-tab counts in §2d — but ... driven
     * by the same underlying value ... so on the seeded fixture it is always the
     * same number as the Active tab's count (3)".
     *
     * The sibling slice asserts only that the text `3 tracked` is visible. This
     * pins the three structural claims that sentence actually makes: adjacency,
     * monospace, and value-identity-across-two-distinct-elements.
     *
     * The `co` badge is NOT asserted anywhere here — §1b calls it decorative,
     * `aria-hidden`, and visible only below the md (768px) breakpoint, and
     * instructs the slice not to assert it.
     */
    test('header count is a distinct mono element next to the h1 carrying the tab count value', async ({
      page,
    }) => {
      const h1 = page.getByRole('heading', { level: 1, name: 'Pipeline', exact: true })
      await expect(h1).toBeVisible()

      // "immediately follows the h1" — the h1's own next element sibling.
      const followingText = await h1.evaluate(
        el => el.nextElementSibling?.textContent?.replace(/\s+/g, ' ').trim() ?? null,
      )
      expect(followingText).toBe('3 tracked')

      // "A mono count element" — computed font only, never a class name.
      const headerCount = page.getByText('3 tracked', { exact: true })
      const fontFamily = await headerCount.evaluate(el => getComputedStyle(el).fontFamily)
      expect(fontFamily.toLowerCase()).toMatch(/mono/)

      // "a distinct element from the per-tab counts in §2d" — the header count
      // lives outside the tablist entirely.
      const tablist = page.getByRole('tablist', { name: 'Pipeline filters' })
      await expect(tablist.getByText('3 tracked', { exact: true })).toHaveCount(0)
      expect(
        await headerCount.evaluate(el => !!el.closest('[role="tablist"]')),
      ).toBe(false)

      // "...driven by the same underlying value ... always the same number as
      // the Active tab's count (3)". Two elements, one value.
      const activeTabText = await tablist.getByRole('tab', { name: 'Active' }).innerText()
      expect(activeTabText.match(/\d+/)?.[0]).toBe('3')
    })

    /**
     * §4e — "chip state is derived per stage, not from one global status".
     *
     * The sibling slice pins this as an inequality WITHIN card #99 (work !=
     * review != merge). That is satisfiable by a card that simply renders its
     * four chips differently for unrelated reasons. The sharper pin is
     * ACROSS cards, using the seed's deliberate asymmetry:
     *
     *   #42: work = current    #99: work   = completed
     *   #42: review = waiting  #99: review = current
     *
     *   - the SAME label `work` must render DIFFERENTLY on the two cards
     *     (one global per-card status could not produce that), and
     *   - `review` on #99 must render IDENTICALLY to `work` on #42, because
     *     both are the current stage — state follows the stage, not the card.
     *
     * Both are comparisons between two live renderings; neither names a colour
     * or a class (see chipStyle's TODO).
     */
    test('same stage label renders differently across cards while equal states render alike', async ({
      page,
    }) => {
      const c42 = chips(card(page, 'Fix the dashboard rendering'))
      const c99 = chips(card(page, 'Refactor merge queue'))
      await expect(c42).toHaveCount(4)
      await expect(c99).toHaveCount(4)

      // Index 0 = work, 1 = review, 3 = merge (§4d's pinned order).
      const [work42, review42, merge42, work99, review99, merge99] = await Promise.all([
        chipStyle(c42.nth(0)),
        chipStyle(c42.nth(1)),
        chipStyle(c42.nth(3)),
        chipStyle(c99.nth(0)),
        chipStyle(c99.nth(1)),
        chipStyle(c99.nth(3)),
      ])

      // `work` is current on #42 and completed on #99 -> must differ.
      expect(work42).not.toEqual(work99)

      // Both are the current stage -> must render alike.
      expect(review99).toEqual(work42)

      // Both are waiting -> must render alike; and waiting != current.
      expect(merge99).toEqual(merge42)
      expect(merge42).not.toEqual(work42)

      // #42's review is waiting, #99's review is current -> must differ.
      expect(review42).not.toEqual(review99)
    })
  })

  test.describe('board where one item needs attention', () => {
    test.beforeEach(async ({ page }) => {
      await seedApi(page, NEEDS_ME_PIPELINE)
      await page.goto('/')
    })

    /**
     * §3a — "When `Needs me` is selected the same list region is named `Items
     * needing attention`. Both names are part of the contract."
     *
     * The sibling slice covers only the `Active items` name; its §2e test
     * explicitly defers this one because on the all-empty seed the contract is
     * silent on whether the region renders at all. Seeding one item WITH an
     * `available_gates` entry removes that ambiguity: the filtered set is
     * non-empty, so the region must exist and must carry the second name.
     *
     * §3b is re-asserted here for the second name too — the contract describes
     * "the same list region", so a card in it is still a `button`.
     */
    test('list region is named Items needing attention while Needs me is selected', async ({
      page,
    }) => {
      const tablist = page.getByRole('tablist', { name: 'Pipeline filters' })

      // Sanity: on this seed the Active name is what shows first (§3a).
      await expect(page.getByRole('region', { name: 'Active items' })).toBeVisible()

      await tablist.getByRole('tab', { name: 'Needs me' }).click()
      await expect(tablist.getByRole('tab', { name: 'Needs me' })).toHaveAttribute(
        'aria-selected',
        'true',
      )

      const region = page.getByRole('region', { name: 'Items needing attention' })
      await expect(region).toBeVisible()

      // The Active name is gone — it is the SAME region, renamed, not a second one.
      await expect(page.getByRole('region', { name: 'Active items' })).toHaveCount(0)

      // §3b — still one button card per item, and the gated item is in it.
      await expect(region.getByRole('button', { name: /Fix the dashboard rendering/ })).toBeVisible()
      await expect(region.getByRole('link')).toHaveCount(0)
    })

    /**
     * §2d — "A tab whose count is greater than zero renders that count as text
     * inside the tab."
     *
     * The sibling slice pins the zero case on `Needs me` (no count element) but
     * never the non-zero case for that tab, so nothing yet proves the rule is
     * general rather than an `Active`-only quirk. One gated item makes the
     * `Needs me` count non-zero, and the same rule must hold.
     *
     * TODO(test-author): the contract does not state whether the `Needs me`
     * count is "items with available_gates" or some wider notion of attention
     * (§2d defines only the `Active` count precisely — "every item whose stage
     * is not `merged`"). This asserts only that SOME count is rendered, not a
     * specific number, rather than inventing the counting rule.
     */
    test('Needs me tab renders a count once its filtered set is non-empty', async ({ page }) => {
      const needsMe = page
        .getByRole('tablist', { name: 'Pipeline filters' })
        .getByRole('tab', { name: 'Needs me' })

      await expect(needsMe).toBeVisible()
      // Not just the bare label any more — a count is rendered as text inside it.
      await expect(needsMe).toHaveText(/Needs me\s*\d+/)
    })
  })
})
