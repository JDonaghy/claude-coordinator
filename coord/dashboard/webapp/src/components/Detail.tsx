/**
 * Detail — stub screen for a single pipeline item.
 *
 * Full implementation is tracked in the next milestone issue (detail screen +
 * actions).  This stub confirms that card navigation routes work correctly.
 */
import { useNavigate, useParams } from 'react-router-dom'

export default function Detail() {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()

  return (
    <div className="mx-auto max-w-lg px-4 py-6">
      <header className="mb-6 flex items-center gap-3">
        <button
          type="button"
          onClick={() => navigate(-1)}
          className="rounded p-1 text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          aria-label="Back"
        >
          ←
        </button>
        <h1 className="text-xl font-bold text-primary">coord</h1>
      </header>

      <p className="text-sm text-muted-foreground">
        Assignment <span className="font-mono">{id}</span> — detail screen coming soon.
      </p>
    </div>
  )
}
