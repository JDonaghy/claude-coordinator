/**
 * Whole-app 404 (#1548) — what a URL that matches no route in the tree at
 * all renders, e.g. a typo'd path or a link to a feature that was removed.
 *
 * Distinct from `Detail`'s own not-found state (a syntactically valid
 * `/pipeline/:repo/:issue` whose issue isn't in today's pipeline — a *stale*
 * link, not an *unknown* one): that one already exists per-item and knows
 * the repo/issue it failed to find. This one is the catch-all `*` route
 * inside `ShellLayout`, so it still renders inside the shell (rail, status
 * bar) rather than a blank document — "a real not-found state, not a blank
 * panel or a crash" per the story's acceptance criteria.
 */
import { Compass } from 'lucide-react'
import { Link } from 'react-router-dom'
import { paths } from '@/routes/paths'

export function RouteNotFound() {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-3 px-6 py-12 text-center">
      <Compass className="h-7 w-7 text-faint" aria-hidden="true" />
      <p className="text-sm font-medium text-foreground">Page not found</p>
      <p className="max-w-xs text-xs text-muted-foreground">
        There's nothing at this address.
      </p>
      <Link
        to={paths.pipeline()}
        className="mt-1 rounded-full border border-border px-3 py-1 text-xs text-muted-foreground hover:text-foreground"
      >
        Go to Pipeline
      </Link>
    </div>
  )
}
