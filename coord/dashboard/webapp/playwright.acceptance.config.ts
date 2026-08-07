/**
 * Playwright config for the SEALED oracle-loop acceptance suite (#1540),
 * as opposed to `playwright.config.ts` (the `e2e/` smoke net — untouched,
 * keeps running via `npm run test:e2e`).
 *
 * Why a sibling config rather than editing `playwright.config.ts` in place
 * (docs/ORACLE_LOOP.md, #1552's "wiring failure, not a test failure" bug,
 * arriving here in Playwright-shaped form):
 *
 * 1. `playwright.config.ts`'s `testDir: './e2e'` is invisible to anything
 *    under repo-root `tests/acceptance/ms-NN/` — a path filter pointing
 *    outside `testDir` matches zero files and Playwright exits 0 with 0
 *    tests. That is a silent, confidently-wrong green, not a red. This
 *    config's `testDir` points at the sealed tree instead so a slice
 *    actually gets discovered.
 * 2. The acceptance run must select a machine-readable reporter, not
 *    inherit `e2e`'s CI-conditional `'github'`. In practice the
 *    `web-playwright` driver (#1539, coord/acceptance_drivers.py
 *    `_run_web_playwright`) always appends `--reporter=json` on the CLI,
 *    which overrides whatever `reporter:` this file declares — but this
 *    file still picks an explicit, non-CI-conditional default (`'list'`)
 *    so a human running it directly (outside the driver) gets readable
 *    output instead of silently inheriting a `CI` env var's default.
 *
 * `run:` (acceptance.drivers.claude-coordinator.routes, coordinator.yml):
 *   cd coord/dashboard/webapp && npm run test:acceptance -- {ms}
 * `{ms}` (e.g. "ms-40") is substituted by `render_run_command` and passed as
 * Playwright's positional test-file filter — it matches by substring against
 * each spec file's resolved path, so `tests/acceptance/ms-40/foo.spec.ts`
 * matches the filter `ms-40` without needing an exact relative path. This
 * config also reads that same `ms-NN` token back out of `process.argv` (see
 * `resolveFixturePath` below) to decide what to seed the webServer with.
 *
 * `entrypoint:` — deliberately NOT set on this route (#1552). Playwright
 * discovers specs by walking `testDir`, exactly like `cli-pytest`'s
 * `pytest tests/acceptance/{ms}` — there is no crate-root-style file that
 * must `include!`/import each slice before it becomes reachable, so there is
 * nothing to fold into `AcceptanceConfig.sealed_paths()`.
 *
 * `webServer` (#1818): boots a REAL `coord web --fixture <file> --dist dist`
 * process (`coord/dashboard/fixture.py`, #1538) instead of the Vite dev
 * server — deterministic reads with no daemon, no live fleet, no network,
 * mirroring what `e2e/fixtureServer.ts` already does for
 * `live-update-fixture.spec.ts`. `npm run build` runs first because
 * `--dist` needs a production bundle on disk; both steps are chained into
 * one `command` string so nothing outside this file has to remember to
 * build before testing. `coord` must be resolvable on `$PATH` — true by
 * construction here: this suite is only ever invoked by the `web-playwright`
 * acceptance driver, which is itself `coord acceptance run`/`record`, so the
 * `coord` CLI is always already installed on whatever machine reaches this
 * config.
 *
 * Seeding convention for a slice (#1818): a milestone directory may declare
 * `tests/acceptance/ms-NN/fixtures/<name>.json` — a `coord/dashboard/
 * fixture.py`-shaped fixture, sealed alongside that milestone's
 * `mocks/*.html`, so a slice's seed is part of the tree the test-author
 * owns (see `coord.test_author.TEST_AUTHOR_SYSTEM_PROMPT`, which tells the
 * next web test-author to write one of these instead of inventing an inline
 * `page.route()` payload). Exactly one `*.json` file is expected per `ms-NN`
 * directory; `resolveFixturePath` raises at config-load time if it finds
 * more than one, rather than silently picking one and hiding the ambiguity.
 * A milestone with no `fixtures/` directory falls back to the repo-wide
 * `tests/fixtures/board-pipeline-basic.json` default (the same fixture
 * `e2e/fixtureServer.ts` uses) — harmless for a slice that seeds itself
 * another way.
 *
 * ms-51 (#1544's proving rig) is deliberately left on `page.route()` — see
 * that slice's own header comment and #1818's "recommend leaving ms-51
 * alone" — so it falls back to the default fixture above and ignores
 * whatever it serves; ms-52 onward is where a slice is expected to declare
 * its own `fixtures/*.json` and drop `page.route()` entirely. Known hazard
 * ms-51 no longer inherits from a dev server (worth stating precisely,
 * since a stale hazard note is worse than none): React 18 StrictMode's
 * double-invoked effects were a *Vite dev mode* quirk, not a production
 * bundle one, so a fixture-backed slice authored from here on doesn't need
 * to write around it — but assertions should still prefer eventual state
 * (`expect(...).toHaveText(...)`, `waitFor`) over exact effect/call counts,
 * which is good practice regardless of backend.
 */

import { defineConfig, devices } from '@playwright/test'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
const REPO_ROOT = path.resolve(here, '../../..')
const ACCEPTANCE_DIR = path.resolve(here, '../../../tests/acceptance')
const DIST_DIR = path.join(here, 'dist')
const DEFAULT_FIXTURE = path.join(REPO_ROOT, 'tests/fixtures/board-pipeline-basic.json')

/**
 * Resolve which fixture JSON seeds `coord web --fixture` for this run.
 *
 * The driver's `run:` command passes the target milestone as a positional
 * filter (`npm run test:acceptance -- ms-51`), and Playwright forwards CLI
 * args through verbatim, so `process.argv` already carries the `ms-NN`
 * token this config needs — no second side-channel required. Falls back to
 * `DEFAULT_FIXTURE` when no `ms-NN` arg is present (a bare `npm run
 * test:acceptance` running the whole tree) or the matched milestone has no
 * `fixtures/` directory of its own.
 */
function resolveFixturePath(): string {
  const msArg = process.argv.find((a) => /^ms-\d+$/.test(a))
  if (msArg) {
    const fixturesDir = path.join(ACCEPTANCE_DIR, msArg, 'fixtures')
    if (fs.existsSync(fixturesDir)) {
      const files = fs
        .readdirSync(fixturesDir)
        .filter((f) => f.endsWith('.json'))
        .sort()
      if (files.length > 1) {
        throw new Error(
          `${fixturesDir} declares ${files.length} fixture files ` +
            `(${files.join(', ')}) — the acceptance webServer expects at ` +
            'most one per ms-NN directory. Split into separate ms-NN ' +
            'milestones instead of disambiguating fixtures within one.',
        )
      }
      if (files.length === 1) return path.join(fixturesDir, files[0])
    }
  }
  return DEFAULT_FIXTURE
}

const FIXTURE_PATH = resolveFixturePath()

export default defineConfig({
  testDir: ACCEPTANCE_DIR,
  testMatch: '**/*.spec.ts',

  fullyParallel: false,
  workers: 1,

  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,

  // Explicit, non-CI-conditional default — see module docstring point 2.
  // The web-playwright driver overrides this at invocation with its own
  // `--reporter=json` CLI flag regardless.
  reporter: 'list',

  use: {
    baseURL: 'http://localhost:5173',
    screenshot: 'only-on-failure',
    actionTimeout: 5_000,
  },

  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],

  webServer: {
    command:
      `npm run build && coord web --fixture "${FIXTURE_PATH}" ` +
      `--dist "${DIST_DIR}" --host 127.0.0.1 --port 5173`,
    url: 'http://localhost:5173',
    reuseExistingServer: !process.env.CI,
    // `npm run build` (tsc && vite build) plus process boot, comfortably
    // over the old Vite-dev-only 30s budget.
    timeout: 90_000,
  },
})
