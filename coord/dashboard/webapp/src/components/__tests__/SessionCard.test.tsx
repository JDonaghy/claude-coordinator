/**
 * Component tests for SessionCard.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { SessionCard } from '@/components/SessionCard'
import { type SessionInfo } from '@/api/client'

// ── Test helpers ──────────────────────────────────────────────────────────────

function makeSession(overrides: Partial<SessionInfo> = {}): SessionInfo {
  return {
    session_id: 'work-1',
    session_name: 'coord-work-1',
    machine: 'laptop',
    host: 'laptop.tailnet.ts.net',
    repo: 'myrepo',
    issue: 42,
    issue_title: 'Fix the thing',
    stage: 'work',
    status: 'running',
    attached: false,
    pane_dead: false,
    ...overrides,
  }
}

describe('SessionCard', () => {
  it('renders issue title, repo, issue number, and machine', () => {
    render(<SessionCard session={makeSession()} onClick={() => undefined} />)

    const card = screen.getByRole('button')
    expect(card).toHaveTextContent('Fix the thing')
    expect(card).toHaveTextContent('#42')
    expect(card).toHaveTextContent('myrepo')
    expect(card).toHaveTextContent('laptop')
  })

  it('falls back to the tmux session name when no assignment is tracked', () => {
    render(
      <SessionCard
        session={makeSession({ issue_title: null, repo: null, issue: null, machine: null })}
        onClick={() => undefined}
      />,
    )
    expect(screen.getByRole('button')).toHaveTextContent('coord-work-1')
  })

  it('shows "live" badge for an untouched running session', () => {
    render(<SessionCard session={makeSession()} onClick={() => undefined} />)
    expect(screen.getByText('live')).toBeInTheDocument()
  })

  it('shows "attached" badge when a client is already attached', () => {
    render(<SessionCard session={makeSession({ attached: true })} onClick={() => undefined} />)
    expect(screen.getByText('attached')).toBeInTheDocument()
  })

  it('shows "ended" badge when the pane is dead', () => {
    render(<SessionCard session={makeSession({ pane_dead: true })} onClick={() => undefined} />)
    expect(screen.getByText('ended')).toBeInTheDocument()
  })

  it('calls onClick when the card is pressed', async () => {
    const onClick = vi.fn()
    render(<SessionCard session={makeSession()} onClick={onClick} />)
    await userEvent.click(screen.getByRole('button'))
    expect(onClick).toHaveBeenCalledOnce()
  })
})
