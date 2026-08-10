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
 * "Has an available gate action": the item is parked on a gate the human can
 * act on right now (`available_gates` is non-empty — record a test verdict,
 * queue for merge, merge, retry, dispatch a fix...). Server-computed in
 * `coord/pipeline.py`; the client only reads the field.
 *
 * Also used on its own for Active-tab sort *priority* (items with something
 * actionable float to the top of "in progress").
 */
export function hasAvailableGate(view: PipelineView): boolean {
  return view.available_gates.length > 0
}

/**
 * "Needs me": the item is waiting on a human, for either of the two reasons
 * the *server* reports. Both halves are fields the API computes — the client
 * derives nothing of its own here:
 *
 *   1. `needs_attention` — the daemon's #846 backstop verdict
 *      (`coord.notify.attention_signal`: wall-clock overrun /
 *      non-convergence), and
 *   2. `available_gates` — a human gate action is currently offered
 *      (`coord/pipeline.py`'s gate projection).
 *
 * #1966 reported the badge reading 671 while `needs_attention` read 0. The
 * defect this encodes is that the tab used to consult (2) *only* and drop (1)
 * on the floor: an item the daemon had explicitly flagged as stuck, but which
 * happened to be mid-`coding` with no gate offered, was invisible in the very
 * tab whose job is "what is waiting for me" — while the badge simultaneously
 * claimed a number the `needs_attention` field never agreed with, with no
 * relationship stated between them. `needs_attention` is now honoured, so the
 * count can never again be smaller than the server's flagged set, and every
 * input to the answer is a server field.
 *
 * It does NOT narrow the tab to `needs_attention` alone. That would be a
 * behaviour change the ms-51 acceptance contract forbids: §2e/§3a pin the
 * `Needs me` set to items carrying `available_gates` (its seeds differ in
 * exactly that field and in nothing else — see
 * tests/acceptance/ms-51/home-active.spec.ts:258 vs
 * home-active-extended.spec.ts:408), and that tree is sealed. Dropping the
 * gate half would also hide every genuinely actionable item behind a tab that
 * only ever lights up when a timeout fires.
 *
 * The residual "671 vs 0" gap is a SERVER-side question, and that is where it
 * belongs: if 671 items really are parked on gates, the daemon's attention
 * signal is under-reporting and `coord.notify.attention_signal` should learn
 * about parked gates, so that every surface (phone, CLI, GitHub comments)
 * picks the widening up at once. Do not "fix" it by reintroducing a third,
 * client-only definition of the question here.
 */
export function needsMe(view: PipelineView): boolean {
  return view.needs_attention || hasAvailableGate(view)
}
