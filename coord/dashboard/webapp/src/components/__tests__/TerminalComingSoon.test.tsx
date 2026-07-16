/**
 * Component tests for the TerminalComingSoon stub — the placeholder that the
 * SessionCard entry point (#1067) navigates to at `/terminal/:sessionId`
 * until the real take-over pane lands in its own issue.
 */
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import TerminalComingSoon from '@/components/TerminalComingSoon'

function renderStub(sessionId: string) {
  return render(
    <MemoryRouter initialEntries={[`/terminal/${sessionId}`]}>
      <Routes>
        <Route path="/terminal/:sessionId" element={<TerminalComingSoon />} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('TerminalComingSoon', () => {
  it('renders a coming-soon message referencing the session id', () => {
    renderStub('work-2')

    expect(screen.getByText('Terminal view coming soon')).toBeInTheDocument()
    expect(screen.getByText('work-2')).toBeInTheDocument()
  })

  it('renders a back button', () => {
    renderStub('work-2')

    expect(screen.getByRole('button', { name: 'Back' })).toBeInTheDocument()
  })
})
