/**
 * Component tests for MobileKeyBar (#1070, #1299) -- the acceptance criterion
 * is "each soft-key emits the correct escape / control bytes to the terminal
 * write path (mocked)". `onSend` stands in for that write path here;
 * `Terminal.test.tsx` covers the real (fake) WebSocket wiring end-to-end.
 *
 * Scroll-mode tests (#1299): assert the toggle, swapped key rows, and the
 * line-submit-exits-scroll-first invariant.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import MobileKeyBar, { KEY_BYTES } from '@/components/MobileKeyBar'

const noop = () => {}

describe('MobileKeyBar', () => {
  it.each([
    ['Escape', KEY_BYTES.escape],
    ['Tab', KEY_BYTES.tab],
    ['Ctrl-C', KEY_BYTES.ctrlC],
    ['Slash', KEY_BYTES.slash],
    ['Left', KEY_BYTES.left],
    ['Up', KEY_BYTES.up],
    ['Down', KEY_BYTES.down],
    ['Right', KEY_BYTES.right],
    ['Enter', KEY_BYTES.enter],
  ])('%s sends %j', async (ariaLabel, expectedBytes) => {
    const onSend = vi.fn()
    render(<MobileKeyBar onSend={onSend} onControl={noop} />)

    await userEvent.click(screen.getByRole('button', { name: ariaLabel }))

    expect(onSend).toHaveBeenCalledOnce()
    expect(onSend).toHaveBeenCalledWith(expectedBytes)
  })

  it('sends the typed line followed by a carriage return on submit, then clears the field', async () => {
    const onSend = vi.fn()
    render(<MobileKeyBar onSend={onSend} onControl={noop} />)

    const input = screen.getByRole('textbox', { name: 'Command line' })
    await userEvent.type(input, '/status')
    await userEvent.click(screen.getByRole('button', { name: 'Send line' }))

    expect(onSend).toHaveBeenCalledOnce()
    expect(onSend).toHaveBeenCalledWith('/status\r')
    expect(input).toHaveValue('')
  })

  it('does not send an empty line', async () => {
    const onSend = vi.fn()
    render(<MobileKeyBar onSend={onSend} onControl={noop} />)

    await userEvent.click(screen.getByRole('button', { name: 'Send line' }))

    expect(onSend).not.toHaveBeenCalled()
  })

  it("submits the line on the field's own Enter key, same as tapping Send", async () => {
    const onSend = vi.fn()
    render(<MobileKeyBar onSend={onSend} onControl={noop} />)

    const input = screen.getByRole('textbox', { name: 'Command line' })
    await userEvent.type(input, 'ls{Enter}')

    expect(onSend).toHaveBeenCalledOnce()
    expect(onSend).toHaveBeenCalledWith('ls\r')
  })

  it('does not steal focus from the line-input when a soft key is tapped', async () => {
    const onSend = vi.fn()
    render(<MobileKeyBar onSend={onSend} onControl={noop} />)

    const input = screen.getByRole('textbox', { name: 'Command line' })
    input.focus()
    expect(input).toHaveFocus()

    await userEvent.click(screen.getByRole('button', { name: 'Ctrl-C' }))

    expect(input).toHaveFocus()
    expect(onSend).toHaveBeenCalledOnce()
    expect(onSend).toHaveBeenCalledWith(KEY_BYTES.ctrlC)
  })
})

describe('MobileKeyBar — scroll mode (#1299)', () => {
  it('tapping Scroll emits onControl("enter") and shows the SCROLL badge', async () => {
    const onControl = vi.fn()
    render(<MobileKeyBar onSend={noop} onControl={onControl} />)

    // Scroll button exists in normal mode
    await userEvent.click(screen.getByRole('button', { name: 'Scroll' }))

    expect(onControl).toHaveBeenCalledOnce()
    expect(onControl).toHaveBeenCalledWith('enter')

    // SCROLL badge is now visible
    expect(screen.getByRole('status', { name: 'Scroll mode active' })).toBeInTheDocument()
  })

  it('tapping Exit Scroll emits onControl("exit") and hides the badge', async () => {
    const onControl = vi.fn()
    render(<MobileKeyBar onSend={noop} onControl={onControl} />)

    // Enter scroll mode first
    await userEvent.click(screen.getByRole('button', { name: 'Scroll' }))
    onControl.mockClear()

    // Exit scroll mode
    await userEvent.click(screen.getByRole('button', { name: 'Exit Scroll' }))

    expect(onControl).toHaveBeenCalledOnce()
    expect(onControl).toHaveBeenCalledWith('exit')

    // Badge is gone; normal keys are back
    expect(screen.queryByRole('status', { name: 'Scroll mode active' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Scroll' })).toBeInTheDocument()
  })

  it('scroll controls emit the correct actions while in scroll mode', async () => {
    const onControl = vi.fn()
    render(<MobileKeyBar onSend={noop} onControl={onControl} />)

    await userEvent.click(screen.getByRole('button', { name: 'Scroll' }))
    onControl.mockClear()

    const cases: Array<[string, string]> = [
      ['Page Up', 'page-up'],
      ['Page Down', 'page-down'],
      ['Top', 'top'],
      ['Bottom', 'bottom'],
    ]
    for (const [label, action] of cases) {
      await userEvent.click(screen.getByRole('button', { name: label }))
      expect(onControl).toHaveBeenLastCalledWith(action)
    }
  })

  it('normal key rows are absent while in scroll mode', async () => {
    render(<MobileKeyBar onSend={noop} onControl={noop} />)

    await userEvent.click(screen.getByRole('button', { name: 'Scroll' }))

    // Normal-mode-only keys must be gone while in scroll mode
    expect(screen.queryByRole('button', { name: 'Escape' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Up' })).not.toBeInTheDocument()
  })

  it('submitting the line-input in scroll mode sends exit before the text', async () => {
    const onSend = vi.fn()
    const onControl = vi.fn()
    render(<MobileKeyBar onSend={onSend} onControl={onControl} />)

    // Enter scroll mode
    await userEvent.click(screen.getByRole('button', { name: 'Scroll' }))
    onControl.mockClear()

    // Type and submit text
    const input = screen.getByRole('textbox', { name: 'Command line' })
    await userEvent.type(input, 'hello')
    await userEvent.click(screen.getByRole('button', { name: 'Send line' }))

    // exit must have been called before onSend
    expect(onControl).toHaveBeenCalledOnce()
    expect(onControl).toHaveBeenCalledWith('exit')

    expect(onSend).toHaveBeenCalledOnce()
    expect(onSend).toHaveBeenCalledWith('hello\r')

    // Scroll mode is now off
    expect(screen.queryByRole('status', { name: 'Scroll mode active' })).not.toBeInTheDocument()
  })

  it('scroll mode resets to off when connectionKey changes (reconnect)', async () => {
    const onControl = vi.fn()
    const { rerender } = render(
      <MobileKeyBar onSend={noop} onControl={onControl} connectionKey={1} />,
    )

    // Enter scroll mode
    await userEvent.click(screen.getByRole('button', { name: 'Scroll' }))
    expect(screen.getByRole('status', { name: 'Scroll mode active' })).toBeInTheDocument()

    // Simulate reconnect: connectionKey increments
    rerender(<MobileKeyBar onSend={noop} onControl={onControl} connectionKey={2} />)

    // Badge must be gone
    await waitFor(() =>
      expect(
        screen.queryByRole('status', { name: 'Scroll mode active' }),
      ).not.toBeInTheDocument(),
    )
    // Normal Scroll button is back
    expect(screen.getByRole('button', { name: 'Scroll' })).toBeInTheDocument()
  })
})
