import { Moon, Sun } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { useTheme } from '@/components/ui/theme-provider'

/** The rail's "Theme" button (docs/mocks/web/pipeline-wide.html) — flips dark/light, persists. */
export function ThemeToggle() {
  const { theme, toggleTheme } = useTheme()

  return (
    <Button
      variant="ghost"
      size="icon"
      onClick={toggleTheme}
      aria-label={theme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme'}
      title="Theme"
    >
      {theme === 'dark' ? <Sun /> : <Moon />}
    </Button>
  )
}
