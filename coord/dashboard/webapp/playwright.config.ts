/**
 * Playwright E2E configuration for the coord webapp.
 *
 * Run: npm run test:e2e
 *
 * Prerequisites (one-time per machine):
 *   npx playwright install chromium
 *
 * Design:
 *  - The Vite dev server is started automatically by Playwright's
 *    `webServer` option, so no separate `npm run dev` is needed.
 *  - API calls (`/api/pipeline`, `/api/board`, …) are intercepted via
 *    `page.route()` inside each test — no live Python daemon required.
 *    This keeps the suite deterministic and fast.
 *  - Only Chromium is targeted.  Safari/Firefox variants belong in the
 *    coord/dashboard/webapp smoke_tests capability group; gate them on a
 *    machine with those browsers installed via coordinator.yml.
 *
 * Routing to capable hardware:
 *   coordinator.yml smoke_tests.capability_rules:
 *     - capability: browser
 *       paths: [coord/dashboard/webapp/**]
 */

import { defineConfig, devices } from '@playwright/test'

export default defineConfig({
  testDir: './e2e',
  testMatch: '**/*.spec.ts',

  // Run tests sequentially (single file, small suite for now).
  fullyParallel: false,
  workers: 1,

  // Fail the build on CI if a test is `.only`-locked.
  forbidOnly: !!process.env.CI,

  // One retry on CI to guard against flaky timing.
  retries: process.env.CI ? 1 : 0,

  reporter: process.env.CI ? 'github' : 'list',

  use: {
    // Base URL is the Vite dev server started by webServer below.
    baseURL: 'http://localhost:5173',
    // Capture a screenshot on failure.
    screenshot: 'only-on-failure',
    // Short action timeout — these are simple DOM assertions.
    actionTimeout: 5_000,
  },

  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],

  // Start the Vite dev server before running tests.
  // Playwright waits for the URL to respond before dispatching tests.
  webServer: {
    command: 'npm run dev',
    url: 'http://localhost:5173',
    reuseExistingServer: !process.env.CI,
    timeout: 30_000,
  },
})
