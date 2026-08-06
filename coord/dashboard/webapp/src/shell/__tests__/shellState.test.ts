/**
 * Unit tests for the shell's persisted geometry (#1547).
 *
 * `parseShellState` is the trust boundary: the blob it reads is user-writable
 * (devtools) and will be read by future builds after the schema grows, so a
 * garbage field must degrade to the default rather than reach the grid as
 * `NaN` columns.
 */
import { describe, it, expect } from 'vitest'

import {
  LIST_WIDTH_DEFAULT_PX,
  LIST_WIDTH_MAX_PX,
  LIST_WIDTH_MIN_PX,
  clampListWidth,
  parseShellState,
} from '../shellState'

describe('clampListWidth', () => {
  it('keeps a sane width untouched', () => {
    expect(clampListWidth(400)).toBe(400)
  })

  it('clamps to the min and max bounds', () => {
    expect(clampListWidth(10)).toBe(LIST_WIDTH_MIN_PX)
    expect(clampListWidth(99_999)).toBe(LIST_WIDTH_MAX_PX)
  })

  it('rejects non-finite input rather than passing NaN into a grid template', () => {
    expect(clampListWidth(Number.NaN)).toBe(LIST_WIDTH_DEFAULT_PX)
    expect(clampListWidth(Number.POSITIVE_INFINITY)).toBe(LIST_WIDTH_DEFAULT_PX)
    expect(clampListWidth(Number.NEGATIVE_INFINITY)).toBe(LIST_WIDTH_DEFAULT_PX)
  })

  it('rounds to whole pixels so a drag does not persist sub-pixel noise', () => {
    expect(clampListWidth(400.4)).toBe(400)
    expect(clampListWidth(400.6)).toBe(401)
  })
})

describe('parseShellState', () => {
  it('returns defaults for a missing key', () => {
    expect(parseShellState(null)).toEqual({
      view: 'pipeline',
      railCollapsed: false,
      listCollapsed: false,
      listWidthPx: LIST_WIDTH_DEFAULT_PX,
    })
  })

  it('returns defaults for unparseable JSON', () => {
    expect(parseShellState('{not json')).toEqual(parseShellState(null))
  })

  it('returns defaults for a non-object payload', () => {
    expect(parseShellState('"pipeline"')).toEqual(parseShellState(null))
    expect(parseShellState('null')).toEqual(parseShellState(null))
  })

  it('round-trips a well-formed blob', () => {
    const raw = JSON.stringify({
      view: 'sessions',
      railCollapsed: true,
      listCollapsed: true,
      listWidthPx: 420,
    })
    expect(parseShellState(raw)).toEqual({
      view: 'sessions',
      railCollapsed: true,
      listCollapsed: true,
      listWidthPx: 420,
    })
  })

  it('clamps a persisted width that is out of bounds', () => {
    const raw = JSON.stringify({ listWidthPx: 5_000 })
    expect(parseShellState(raw).listWidthPx).toBe(LIST_WIDTH_MAX_PX)
  })

  it('falls back to pipeline for a view that is not (or is no longer) ready', () => {
    // 'audit' is a real ShellView but only a rail placeholder — restoring it
    // would strand the user on a blank list panel.
    expect(parseShellState(JSON.stringify({ view: 'audit' })).view).toBe('pipeline')
    expect(parseShellState(JSON.stringify({ view: 'nonsense' })).view).toBe('pipeline')
  })

  it('ignores fields of the wrong type field-by-field', () => {
    const raw = JSON.stringify({ railCollapsed: 'yes', listWidthPx: 'wide', view: 'sessions' })
    expect(parseShellState(raw)).toEqual({
      view: 'sessions',
      railCollapsed: false,
      listCollapsed: false,
      listWidthPx: LIST_WIDTH_DEFAULT_PX,
    })
  })
})
