/**
 * Component tests for MobileKeyBar (#1070) -- the acceptance criterion is
 * "each soft-key emits the correct escape / control bytes to the terminal
 * write path (mocked)". `onSend` stands in for that write path here;
 * `Terminal.test.tsx` covers the real (fake) WebSocket wiring end-to-end.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import MobileKeyBar, { KEY_BYTES } from '@/components/MobileKeyBar'

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
    render(<MobileKeyBar onSend={onSend} />)

    await userEvent.click(screen.getByRole('button', { name: ariaLabel }))

    expect(onSend).toHaveBeenCalledOnce()
    expect(onSend).toHaveBeenCalledWith(expectedBytes)
  })

  it('sends the typed line followed by a carriage return on submit, then clears the field', async () => {
    const onSend = vi.fn()
    render(<MobileKeyBar onSend={onSend} />)

    const input = screen.getByRole('textbox', { name: 'Command line' })
    await userEvent.type(input, '/status')
    await userEvent.click(screen.getByRole('button', { name: 'Send line' }))

    expect(onSend).toHaveBeenCalledOnce()
    expect(onSend).toHaveBeenCalledWith('/status\r')
    expect(input).toHaveValue('')
  })

  it('does not send an empty line', async () => {
    const onSend = vi.fn()
    render(<MobileKeyBar onSend={onSend} />)

    await userEvent.click(screen.getByRole('button', { name: 'Send line' }))

    expect(onSend).not.toHaveBeenCalled()
  })

  it('submits the line on the field’s own Enter key, same as tapping Send', async () => {
    const onSend = vi.fn()
    render(<MobileKeyBar onSend={onSend} />)

    const input = screen.getByRole('textbox', { name: 'Command line' })
    await userEvent.type(input, 'ls{Enter}')

    expect(onSend).toHaveBeenCalledOnce()
    expect(onSend).toHaveBeenCalledWith('ls\r')
  })

  it('does not steal focus from the line-input when a soft key is tapped', async () => {
    const onSend = vi.fn()
    render(<MobileKeyBar onSend={onSend} />)

    const input = screen.getByRole('textbox', { name: 'Command line' })
    input.focus()
    expect(input).toHaveFocus()

    await userEvent.click(screen.getByRole('button', { name: 'Ctrl-C' }))

    expect(input).toHaveFocus()
    expect(onSend).toHaveBeenCalledOnce()
    expect(onSend).toHaveBeenCalledWith(KEY_BYTES.ctrlC)
  })
})
