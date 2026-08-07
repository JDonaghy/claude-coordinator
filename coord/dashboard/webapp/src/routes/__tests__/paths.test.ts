/**
 * Unit tests for the route tree's path builders (#1548).
 *
 * `paths` and `shellViewFromPath` are the trust boundary between "what the
 * URL says" and "what the shell renders" — `ShellLayout` and every list
 * panel import from here rather than templating strings themselves, so a bug
 * here is a bug everywhere at once. Worth its own focused coverage.
 */
import { describe, it, expect } from 'vitest'
import { RAIL_VIEW_PATH, isDetailTab, paths, shellViewFromPath } from '../paths'

describe('paths.pipelineItem', () => {
  it('builds the two-segment form with no tab', () => {
    expect(paths.pipelineItem('myrepo', 42)).toBe('/pipeline/myrepo/42')
  })

  it('collapses the default "overview" tab back to the two-segment form', () => {
    expect(paths.pipelineItem('myrepo', 42, 'overview')).toBe('/pipeline/myrepo/42')
  })

  it('appends a non-default tab as a third segment', () => {
    expect(paths.pipelineItem('myrepo', 42, 'log')).toBe('/pipeline/myrepo/42/log')
  })

  it('encodes a repo name that needs escaping (an owner/name slug)', () => {
    expect(paths.pipelineItem('owner/name', 42)).toBe('/pipeline/owner%2Fname/42')
  })

  it('accepts a string issue number unchanged', () => {
    expect(paths.pipelineItem('myrepo', '42')).toBe('/pipeline/myrepo/42')
  })
})

describe('paths.session / paths.terminal', () => {
  it('encodes the session id', () => {
    expect(paths.session('a b')).toBe('/sessions/a%20b')
    expect(paths.terminal('a b')).toBe('/terminal/a%20b')
  })
})

describe('isDetailTab', () => {
  it('accepts every member of the detail tab set', () => {
    for (const tab of ['overview', 'issue', 'log', 'findings', 'summary']) {
      expect(isDetailTab(tab)).toBe(true)
    }
  })

  it('rejects an unknown or missing value', () => {
    expect(isDetailTab('bogus')).toBe(false)
    expect(isDetailTab(undefined)).toBe(false)
  })
})

describe('shellViewFromPath', () => {
  it('resolves the base path for every rail-selectable view', () => {
    expect(shellViewFromPath(paths.pipeline())).toBe('pipeline')
    expect(shellViewFromPath(paths.sessions())).toBe('sessions')
    expect(shellViewFromPath(paths.board())).toBe('board')
    expect(shellViewFromPath(paths.machines())).toBe('machines')
    expect(shellViewFromPath(paths.mergeQueue())).toBe('merge-queue')
    expect(shellViewFromPath(paths.milestones())).toBe('milestones')
    expect(shellViewFromPath(paths.audit())).toBe('audit')
    expect(shellViewFromPath(paths.spend())).toBe('spend')
    expect(shellViewFromPath(paths.settings())).toBe('settings')
  })

  it('resolves an item route under its view', () => {
    expect(shellViewFromPath(paths.pipelineItem('myrepo', 42))).toBe('pipeline')
    expect(shellViewFromPath(paths.pipelineItem('myrepo', 42, 'log'))).toBe('pipeline')
    expect(shellViewFromPath(paths.session('sess-1'))).toBe('sessions')
  })

  it('does not let one view path prefix-match another', () => {
    // '/merge-queue' is not a sub-path of anything else in the map, but this
    // guards the general case: a prefix match must respect the segment
    // boundary (a trailing '/'), not just string startsWith.
    expect(shellViewFromPath('/pipelines')).toBeNull()
    expect(shellViewFromPath('/sessionsx')).toBeNull()
  })

  it('returns null for root, terminal, and unknown paths', () => {
    expect(shellViewFromPath('/')).toBeNull()
    expect(shellViewFromPath('/terminal/sess-1')).toBeNull()
    expect(shellViewFromPath('/nope')).toBeNull()
  })

  it('every RAIL_VIEW_PATH entry round-trips through shellViewFromPath', () => {
    for (const [view, base] of Object.entries(RAIL_VIEW_PATH)) {
      expect(shellViewFromPath(base as string)).toBe(view)
    }
  })
})
