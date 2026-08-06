/**
 * Pipeline predicates shared between the Pipeline panel and the shell.
 *
 * Lifted out of `Home.tsx` (#1547) because the activity rail needs the same
 * two answers Home's filter tabs need — how many items are in flight, and how
 * many are waiting on a human — and a second, independently-drifting copy of
 * "what counts as active" in the rail would be a lie the moment either
 * definition changed.
 */
import type { PipelineView } from '@/api/client'

/**
 * "Active": items that haven't finished (current_stage !== "merged").
 * Keeps the list (and the rail count) focused on in-flight work without
 * cluttering with history.
 */
export function isActive(view: PipelineView): boolean {
  return view.current_stage !== 'merged'
}

/**
 * "Needs me": items where at least one human gate action is available.
 * E.g.: work done (needs test dispatch), review approved (needs merge queue),
 * smoke passed (needs merge queue), merge ready (needs merge), failures (need
 * retry/fix).
 */
export function needsMe(view: PipelineView): boolean {
  return view.available_gates.length > 0
}
