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
 * matches the filter `ms-40` without needing an exact relative path.
 *
 * `entrypoint:` — deliberately NOT set on this route (#1552). Playwright
 * discovers specs by walking `testDir`, exactly like `cli-pytest`'s
 * `pytest tests/acceptance/{ms}` — there is no crate-root-style file that
 * must `include!`/import each slice before it becomes reachable, so there is
 * nothing to fold into `AcceptanceConfig.sealed_paths()`.
 *
 * `webServer` — TODO(#1538): once the fixture server lands, point this at
 * it instead. Until then this reuses the same Vite dev server
 * `playwright.config.ts` already boots for `e2e/` — NOT "live fleet state":
 * every test (both here and in `e2e/`) is expected to intercept its own API
 * calls via `page.route()`, so the dev server only ever serves static
 * app shell/JS, never a live coordinator daemon. Known hazard inherited
 * from the dev server: React 18 StrictMode double-invokes effects in dev
 * mode, so acceptance specs must assert on eventual state (`expect(...).
 * toHaveText(...)`, `waitFor`) rather than exact effect/call counts.
 */

import { defineConfig, devices } from '@playwright/test'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))

export default defineConfig({
  testDir: path.resolve(here, '../../../tests/acceptance'),
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
    command: 'npm run dev',
    url: 'http://localhost:5173',
    reuseExistingServer: !process.env.CI,
    timeout: 30_000,
  },
})
