import Home from '@/components/Home'

/**
 * App root.  Client-side routing will be added in the Home / Detail screen issues.
 * For now the single route is the Home placeholder.
 */
export default function App() {
  return (
    <div className="min-h-screen bg-background text-foreground">
      <Home />
    </div>
  )
}
