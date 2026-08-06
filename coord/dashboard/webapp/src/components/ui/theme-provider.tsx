import { createContext, useContext, useEffect, useState, type ReactNode } from 'react'

/**
 * Dark/light theme (#1546). Binary by design -- the mocks' rail has one
 * "Theme" button that flips dark <-> light (docs/mocks/web/pipeline-wide.html),
 * not a three-way light/dark/system picker, so that's the surface this
 * exposes too. "System" still matters as the *source* of the initial value
 * (see `systemTheme` below); it just isn't a mode a user can select back to
 * once they've picked explicitly, matching the mock.
 */
export type Theme = 'light' | 'dark'

export const THEME_STORAGE_KEY = 'coord-web-theme'

function systemTheme(): Theme {
  if (typeof window === 'undefined' || !window.matchMedia) return 'dark'
  // Dark is the default per the design (docs/mocks/web/README.md): only an
  // explicit OS preference for light should override it.
  return window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark'
}

function readStoredTheme(): Theme | null {
  if (typeof window === 'undefined') return null
  try {
    const stored = window.localStorage.getItem(THEME_STORAGE_KEY)
    return stored === 'light' || stored === 'dark' ? stored : null
  } catch {
    // localStorage can throw in private-browsing / lockdown modes.
    return null
  }
}

/** Mirrors the blocking script in index.html so React's first render already agrees with it -- no flash. */
function initialTheme(): Theme {
  return readStoredTheme() ?? systemTheme()
}

interface ThemeContextValue {
  theme: Theme
  setTheme: (theme: Theme) => void
  toggleTheme: () => void
}

const ThemeContext = createContext<ThemeContextValue | undefined>(undefined)

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setThemeState] = useState<Theme>(initialTheme)

  // The inline script in index.html already set data-theme before paint;
  // this keeps it in sync whenever `theme` changes from here on.
  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme)
  }, [theme])

  const setTheme = (next: Theme) => {
    setThemeState(next)
    try {
      window.localStorage.setItem(THEME_STORAGE_KEY, next)
    } catch {
      // Persistence is a nice-to-have; the theme still applies this session.
    }
  }

  const toggleTheme = () => setTheme(theme === 'dark' ? 'light' : 'dark')

  return (
    <ThemeContext.Provider value={{ theme, setTheme, toggleTheme }}>
      {children}
    </ThemeContext.Provider>
  )
}

// eslint-disable-next-line react-refresh/only-export-components -- a hook alongside its provider component is the standard context pattern (see components/ui/badge.tsx for the same precedent).
export function useTheme(): ThemeContextValue {
  const ctx = useContext(ThemeContext)
  if (!ctx) throw new Error('useTheme must be used within a ThemeProvider')
  return ctx
}
