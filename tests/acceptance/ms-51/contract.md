# ms-51 acceptance contract — Home, Active tab

**Milestone ms-51 · "Web acceptance oracle (web-playwright)" · tracking issue #1537**
**Driver: `web-playwright` · mock: [`mocks/home-active.html`](mocks/home-active.html)**

## What this contract is for — read first

This is **not** a design contract. It pins the **already-shipped** Home screen
(`src/components/Home.tsx` + `PipelineCard.tsx`, route `/`) so that #1544 — M-W0's exit
gate — can prove the web oracle composes end to end:

> mock → independent `test-author` → sealed Playwright slice → `coord acceptance run`
> → `coord acceptance record` → a **deliberate** red run → green again

Every earlier ms-51 story landed one piece of that chain and none proved the pieces fit.
The failure this guards against is discovering during M-W2 — mid-programme, with workers
dispatched — that the driver, the routing, the fixture server and the mock shape don't
actually fit together.

**Consequences of that purpose, which constrain this contract:**

1. **The first run must be GREEN.** Assertions here describe the app *as it exists today*.
   A red run means the machinery is broken, not that a feature is unbuilt — that
   distinction is the entire value of #1544. The deliberate red comes later, at step 5,
   under our control.
2. **It is deliberately small and dull.** A larger surface adds ways for the proving run
   to fail for reasons that are not the machinery.
3. **It is a throwaway rig, not a design.** The real design contract for the web control
   center is `docs/mocks/web/*`, which belongs to **ms-52** and describes a shell that
   does not exist yet. Do not merge the two. Do not "improve" this mock toward that
   design — improvements are ms-52's job.

## Selector policy

The webapp uses **no `data-testid` attributes**, and this contract deliberately does not
introduce any. It ships semantic markup — ARIA roles, `aria-label`, `aria-selected`,
`aria-expanded`, and visible text — and every assertion below targets those.

This is a stronger contract, not a weaker one: a `data-testid` can stay green while the
accessible name rots. It also means the slice asserts things a screen reader user
depends on. If a future change needs a hook that semantics cannot express, add the
`data-testid` to the app **and** amend this contract — never assert on a class name or a
Tailwind utility, which are refactor noise.

## §1 — Header

- **§1a** An `h1` with the exact text `coord`.
- **§1b** The text `pipeline` is present as the subtitle beneath it.

## §2 — Filter tabs

- **§2a** An element with `role="tablist"` and accessible name **`Pipeline filters`**.
- **§2b** It contains exactly two `role="tab"` elements, named **`Active`** and
  **`Needs me`**.
- **§2c** On load, the `Active` tab has `aria-selected="true"` and `Needs me` has
  `aria-selected="false"` — Active is the default.
- **§2d** A tab whose count is greater than zero renders that count as text inside the
  tab. A tab with a zero count renders **no** count element.

  > **The count is not the number of visible cards.** `Active` counts every item whose
  > stage is not `merged`; the list below it shows only the *in-progress* subset, because
  > "done-ish" items (`done`, `review_done`, `smoke_passed`, `merge_ready`) are collapsed
  > into the **Work done** section (§5, #1218). In the mock: count `3`, two cards visible,
  > `Work done (1)`. Asserting `count === cards.length` will pass on some fixtures and
  > fail on others — assert them separately. This was caught by running the contract
  > against the real app; the first draft of the mock had it wrong.
- **§2e** Activating the `Needs me` tab moves `aria-selected="true"` to it. With the
  seeded fixture (no item has `available_gates`), the pipeline cards are then **not**
  visible.

## §3 — Active items list

- **§3a** A `section` with accessible name **`Active items`** when the Active tab is
  selected.
  *(When `Needs me` is selected the same list region is named `Items needing attention`.
  Both names are part of the contract.)*
- **§3b** It contains one card per in-flight item, each a `button` — the whole card is
  the tap target, not a link.

## §4 — Pipeline card

For the seeded item `#42`:

- **§4a** The issue title `Fix the dashboard rendering` is visible.
- **§4b** The repo name `api`, the issue number `#42`, and the machine name `laptop` are
  all visible on the card. The issue number is rendered in a monospace element —
  monospace means "a value the machine owns".
- **§4c** A status badge with the text `running`.
- **§4d** **Four** stage chips, labelled `work`, `review`, `test`, `merge` — **in that
  order**. The labels are display names, not the API's stage names: the API emits
  `coding`, `review`, `smoke`, `merge`, and the card maps `coding → work`, `smoke → test`.
  Asserting the API name here would pass against the wrong thing.
- **§4e** For item `#99`, the `work` chip renders as completed and `review` as the
  current stage — chip state is derived per stage, not from one global status.
- **§4f** The `test` chip renders in the **skipped** style (dimmed).

  > ### §4d/§4f pin two known bugs on purpose — see #1724
  >
  > Both of these are **wrong**, and are pinned anyway because this contract must describe
  > the app as it *is*, or #1544's first run goes red for a reason that has nothing to do
  > with the machinery it exists to prove.
  >
  > 1. **The order is Review-before-Test.** `coord/pipeline.py:258` hardcodes
  >    `["coding","review","smoke","merge"]` and never reads `pipeline.default_gates`,
  >    which is `["test","review","merge"]`. The web stage strip still shows the #520-era
  >    order that everything else abandoned.
  > 2. **The test chip is `skipped` on every item, always** — 616 of 616 measured. The
  >    projection calls the gate `smoke`; the config calls it `test`; `"smoke" not in
  >    ["test","review","merge"]` is always true, so it is emitted as skipped regardless of
  >    the real verdict. A recorded `passed` test is invisible on the web stage strip.
  >
  > **When #1724 lands, amend this contract and re-author the affected slice** — do not
  > let the suite keep asserting the buggy shape once the app is correct. That is precisely
  > the amendment path the section below describes.

## §5 — Work done section

- **§5a** A `section` with accessible name **`Work done`**.
- **§5b** Its toggle is a `button` with `aria-expanded="false"` on load — collapsed by
  default (#1218).
- **§5c** The toggle's text includes the item count in parentheses, e.g. `Work done (1)`.

## Not in scope

Deliberately unpinned, so the proving run stays narrow:

- The **empty**, **loading** and **error** states of Home (`No active pipeline items`,
  `All clear`, `Loading pipeline…`, `Failed to load pipeline`). All shipped; none needed
  to prove the chain.
- The **Live sessions** section (#1067) — conditional on live sessions existing, which
  makes it fixture-dependent in a way that adds noise here.
- The Detail view, the Terminal view, pull-to-refresh, and the refreshing indicator.
- Any visual property: colour, spacing, font, or layout. The mock renders them so a human
  can recognise the screen; the contract asserts none of them.

## Amending

This contract is versioned and amendable, not frozen — `coord acceptance mock
claude-coordinator 1537 --amend "<correction>"` dispatches a targeted correction and the
test-author updates the affected slice. Do **not** hand-edit a merged contract without
re-authoring the slice; the suite is the executable half of this document, and the two
drifting apart is exactly what the oracle exists to prevent.
