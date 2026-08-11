/**
 * Spawns a REAL `coord web --fixture` process (#1538, coord/dashboard/fixture.py)
 * for `live-update-fixture.spec.ts` (#1551, M-W1's exit gate).
 *
 * Every other spec in this repo's Playwright suites — `e2e/`'s own
 * `realtime.spec.ts` included — intercepts its own API calls with
 * `page.route()`, or fakes `window.EventSource` outright, against the Vite
 * dev server (see `playwright.config.ts` / `playwright.acceptance.config.ts`'s
 * documented contracts). That's deliberate and fast, but it can only prove
 * the *app* reacts correctly to a given wire shape — never that the real
 * server (`coord/dashboard/fixture.py`'s `FixtureServer` + its
 * `/api/fixture/events/replay` trigger) actually emits that shape. #1551 asks
 * for exactly that: "a scripted SSE sequence from the fixture server". This
 * module is what lets `live-update-fixture.spec.ts` be the one spec in the
 * repo that talks to a real `coord web` process instead of a mock.
 *
 * `--dist` needs a production build, so `startFixtureServer` runs one itself
 * rather than trusting an out-of-band `npm run build` to be fresh — worth the
 * ~5s given what a stale-`dist/` false pass would cost.
 *
 * Not part of `tests/acceptance/ms-52/` (the milestone's *sealed* slice,
 * `tests/acceptance/**`) — a Work-type session cannot write there by design
 * (see this file's spec's header comment). This is the worker-authored
 * black-box coverage the story falls back to per its own "Notes" section
 * when dispatched after M-W0 without a pre-authored sealed slice.
 */
import { type ChildProcessWithoutNullStreams, spawn, spawnSync } from 'node:child_process'
import { createServer } from 'node:net'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
export const WEBAPP_ROOT = path.resolve(here, '..')
export const REPO_ROOT = path.resolve(here, '../../../..')
export const DIST_DIR = path.join(WEBAPP_ROOT, 'dist')
export const FIXTURE_PATH = path.join(REPO_ROOT, 'tests/fixtures/board-pipeline-basic.json')

export interface FixtureServerHandle {
  baseUrl: string
  proc: ChildProcessWithoutNullStreams
  /** Graceful SIGTERM, escalating to SIGKILL after 3s if it won't die. */
  stop: () => Promise<void>
  /** Hard kill, for the "a dropped stream" test itself. */
  kill: () => void
}

/** An OS-assigned free TCP port — avoids clashing with the Vite dev server
 * (5173) or a concurrent acceptance run on the same machine. */
async function freePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const srv = createServer()
    srv.once('error', reject)
    srv.listen(0, '127.0.0.1', () => {
      const address = srv.address()
      if (address === null || typeof address === 'string') {
        reject(new Error('could not determine a free port'))
        return
      }
      const { port } = address
      srv.close(() => resolve(port))
    })
  })
}

/** Builds the production bundle `coord web --dist` serves. Synchronous and
 * blocking on purpose — nothing here should start before it's on disk. */
function buildDist(): void {
  const result = spawnSync('npm', ['run', 'build'], {
    cwd: WEBAPP_ROOT,
    stdio: 'pipe',
    encoding: 'utf-8',
  })
  if (result.status !== 0) {
    throw new Error(
      `npm run build failed (exit ${String(result.status)}) — live-update-fixture.spec.ts needs ` +
        `a fresh dist/ to serve via 'coord web --dist':\n${result.stdout}\n${result.stderr}`,
    )
  }
}

async function waitForReady(baseUrl: string, timeoutMs = 15_000): Promise<void> {
  const deadline = Date.now() + timeoutMs
  let lastError: unknown
  while (Date.now() < deadline) {
    try {
      const res = await fetch(`${baseUrl}/api/board`)
      if (res.ok) return
    } catch (err) {
      lastError = err
    }
    await new Promise((r) => setTimeout(r, 150))
  }
  throw new Error(`coord web --fixture never became ready at ${baseUrl}: ${String(lastError)}`)
}

/**
 * Starts a fresh `coord web --fixture <fixturePath>` on a free port, serving
 * the just-built `dist/`. Resolves `coord` on `$PATH` exactly the way a real
 * operator's shell (or the acceptance driver's `run:` command) would —
 * deliberately not hard-coded to any one machine's venv path.
 *
 * `fixturePath` defaults to `FIXTURE_PATH` (`board-pipeline-basic.json`, the
 * #1538 reference board `live-update-fixture.spec.ts` uses) — pass a
 * different fixture to drive the real server against other seeded scenarios
 * (e.g. `available-gates-terminal.spec.ts`'s #2084 fixture) without
 * duplicating the subprocess-management plumbing.
 */
export async function startFixtureServer(fixturePath: string = FIXTURE_PATH): Promise<FixtureServerHandle> {
  buildDist()
  const port = await freePort()
  const baseUrl = `http://127.0.0.1:${port}`

  const proc = spawn(
    'coord',
    ['web', '--fixture', fixturePath, '--dist', DIST_DIR, '--host', '127.0.0.1', '--port', String(port)],
    { cwd: REPO_ROOT, stdio: 'pipe' },
  )
  let output = ''
  proc.stdout.on('data', (chunk) => (output += String(chunk)))
  proc.stderr.on('data', (chunk) => (output += String(chunk)))

  const exited = new Promise<number | null>((resolve) => {
    proc.once('exit', (code) => resolve(code))
  })

  try {
    await Promise.race([
      waitForReady(baseUrl),
      exited.then((code) => {
        throw new Error(`coord web exited early (code ${String(code)}) before it became ready:\n${output}`)
      }),
    ])
  } catch (err) {
    if (!proc.killed) proc.kill('SIGKILL')
    throw err
  }

  return {
    baseUrl,
    proc,
    kill: () => proc.kill('SIGKILL'),
    stop: () =>
      new Promise<void>((resolve) => {
        if (proc.exitCode !== null || proc.signalCode !== null) {
          resolve()
          return
        }
        proc.once('exit', () => resolve())
        proc.kill('SIGTERM')
        setTimeout(() => {
          if (proc.exitCode === null && proc.signalCode === null) proc.kill('SIGKILL')
        }, 3_000)
      }),
  }
}
