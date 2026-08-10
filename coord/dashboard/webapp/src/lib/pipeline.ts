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
 * "Needs me": defers entirely to the server's `needs_attention` verdict
 * (coord.notify.attention_signal, #846 — wall-clock / non-convergence
 * backstop) rather than recomputing a competing definition client-side.
 *
 * #1966: this used to be `available_gates.length > 0` — "does this item have
 * *any* human gate action available" — which is a much broader condition
 * than the server's "the daemon believes this needs a human" signal (nearly
 * every in-flight item has *some* available gate at some point, so that
 * definition badged ~672 of 672 items). Two independently-drifting answers
 * to "does this need me" is the split-brain class of bug this project has
 * been bitten by before: the phone and the CLI must agree, so the phone
 * reads the same field the CLI/daemon already computed instead of deriving
 * its own.
 *
 * If a broader "needs me" rule is ever wanted, teach it to
 * `coord.notify.attention_signal` server-side so every surface picks it up —
 * do not reintroduce a second client-side definition here.
 */
export function needsMe(view: PipelineView): boolean {
  return view.needs_attention
}

/**
 * "Has an available gate action": used for Active-tab sort *priority*
 * (items with something actionable float to the top of "in progress"), not
 * for the "Needs me" tab/badge — see `needsMe` above for why those two are
 * deliberately different questions now (#1966).
 */
export function hasAvailableGate(view: PipelineView): boolean {
  return view.available_gates.length > 0
}
