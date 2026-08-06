/**
 * Smoke test for the dev-only component gallery (#1546) — the acceptance
 * bar is "every primitive renders correctly in both themes; the gallery
 * proves it," so this renders the gallery, checks every primitive's marker
 * text is on screen, and exercises the live theme toggle end-to-end
 * (button click → data-theme attribute → localStorage persistence).
 *
 * Deliberately does not open the Radix-portalled overlays (Dialog/
 * DropdownMenu/Sheet/Tooltip) here -- doing so needs jsdom polyfills for
 * pointer capture / scrollIntoView that Radix's own test suite already
 * covers; this test's job is "does our styling wrapper mount," not
 * "does Radix work."
 */
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import Gallery from '../Gallery'
import { THEME_STORAGE_KEY } from '@/components/ui/theme-provider'

describe('Gallery', () => {
  beforeEach(() => {
    window.localStorage.clear()
    document.documentElement.removeAttribute('data-theme')
  })

  afterEach(() => {
    window.localStorage.clear()
    document.documentElement.removeAttribute('data-theme')
  })

  it('renders every primitive section', () => {
    render(<Gallery />)

    // Button ("Primary"/"Outline"/"Destructive" also label buttons in the
    // side-by-side preview and the toast triggers further down, hence
    // getAllByRole rather than getByRole for those.
    expect(screen.getAllByRole('button', { name: 'Primary' }).length).toBeGreaterThanOrEqual(1)
    expect(screen.getAllByRole('button', { name: 'Destructive' }).length).toBeGreaterThanOrEqual(1)
    expect(screen.getByRole('button', { name: 'Disabled' })).toBeDisabled()

    // Badge ("Passed"/"Failed" also appear in the side-by-side preview).
    expect(screen.getAllByText('Passed').length).toBeGreaterThanOrEqual(1)
    expect(screen.getAllByText('Failed').length).toBeGreaterThanOrEqual(1)

    // Card
    expect(screen.getByText('W1-1: design tokens')).toBeInTheDocument()

    // Tabs
    expect(screen.getByRole('tab', { name: /Overview/ })).toHaveAttribute(
      'aria-selected',
      'true',
    )

    // Dialog / dropdown-menu / sheet / tooltip triggers
    expect(screen.getByRole('button', { name: 'Open dialog' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'More actions' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Open action sheet' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Help' })).toBeInTheDocument()

    // Empty state
    expect(screen.getByText('No issues match this filter')).toBeInTheDocument()

    // Toast triggers
    expect(screen.getByRole('button', { name: 'Success' })).toBeInTheDocument()

    // Side-by-side dark/light preview (plus the header's current-theme label,
    // which also reads "dark" -- hence getAllByText, not getByText).
    expect(screen.getAllByText('dark').length).toBeGreaterThanOrEqual(1)
    expect(screen.getByText('light')).toBeInTheDocument()
  })

  it('the live theme toggle flips data-theme and persists the choice', () => {
    window.localStorage.setItem(THEME_STORAGE_KEY, 'dark')
    render(<Gallery />)

    expect(document.documentElement.getAttribute('data-theme')).toBe('dark')

    fireEvent.click(screen.getByRole('button', { name: 'Switch to light theme' }))

    expect(document.documentElement.getAttribute('data-theme')).toBe('light')
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe('light')
    expect(screen.getByRole('button', { name: 'Switch to dark theme' })).toBeInTheDocument()
  })
})
