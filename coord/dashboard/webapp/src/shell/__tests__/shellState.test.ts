/**
 * Unit tests for the shell's persisted geometry (#1547, trimmed by #1548 —
 * the selected view moved to the URL, see `routes/__tests__/paths.test.ts`).
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
      railCollapsed: true,
      listCollapsed: true,
      listWidthPx: 420,
    })
    expect(parseShellState(raw)).toEqual({
      railCollapsed: true,
      listCollapsed: true,
      listWidthPx: 420,
    })
  })

  it('clamps a persisted width that is out of bounds', () => {
    const raw = JSON.stringify({ listWidthPx: 5_000 })
    expect(parseShellState(raw).listWidthPx).toBe(LIST_WIDTH_MAX_PX)
  })

  it('ignores a leftover `view` field from a pre-#1548 build rather than choking on it', () => {
    const raw = JSON.stringify({ view: 'sessions', listWidthPx: 420 })
    expect(parseShellState(raw)).toEqual({
      railCollapsed: false,
      listCollapsed: false,
      listWidthPx: 420,
    })
  })

  it('ignores fields of the wrong type field-by-field', () => {
    const raw = JSON.stringify({ railCollapsed: 'yes', listWidthPx: 'wide' })
    expect(parseShellState(raw)).toEqual({
      railCollapsed: false,
      listCollapsed: false,
      listWidthPx: LIST_WIDTH_DEFAULT_PX,
    })
  })
})
