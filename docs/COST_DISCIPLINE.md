# Cost Discipline

**Audience: the operator (and the coordinator session).** Moved out of `CLAUDE.md` by #2195 —
no worker acts on any of it, and it was being loaded into every worker leg, every review leg,
and every coordinator session.

The one rule from this section that *is* worker-facing — **only the coordinator writes docs** —
stays in `CLAUDE.md`, because workers must obey it. It is restated at the bottom here for the
operator's half of the contract.

## The basic economics

The coordinator session (typically Opus) costs **~10x more per token** than Sonnet workers.
Minimize direct code work in the coordinator — instead, write a good briefing and dispatch it.

- **Dispatch, don't do.** If a task can be described in a briefing, send it to a worker.
  Reserve the coordinator session for triage, review, and decisions.
- **Workers are cheap.** Sonnet workers typically cost $0.30–0.90 per task. An hour of Opus
  coordinator time costs $40+.
- **Compact aggressively.** Long coordinator sessions balloon cache reads. Use `/compact` when
  switching topics or after completing a batch of work.
- **Parallel workers, serial coordinator.** Dispatch multiple workers in parallel, then review
  results. Don't do two things at once in the coordinator session.

## Don't re-do the work you paid for

- **Trust the adversarial review.** When a review completes, read only the review comment — do
  not re-read the full PR diff to form an independent opinion. Summarize the reviewer's
  findings and ask the user how to proceed. Only read the diff if the review seems wrong or
  incomplete.
- **Audit before dispatching.** Include a step in briefings: "Before coding, verify this isn't
  already implemented." Workers have wasted full sessions building features that already
  existed.

## Dispatch traps that cost a whole round-trip

- **Never dispatch reviews via `coord assign`.** Workers have `gh` on the deny-list, so a
  worker dispatched with `coord assign` cannot run `gh pr diff` or `gh pr review`. Reviews must
  go through the review pipeline (`coord review`, or auto-dispatch on completion), which uses
  `type="review"` and grants GitHub access.
- **Catch platform violations at review time.** The adversarial reviewer should check for
  platform-specific code in shared/cross-platform paths. Catching after merge costs an entire
  round-trip.

## Only the coordinator writes docs — the operator's half

Workers must not update README, CHANGELOG, or shared documentation files; parallel doc edits
cause merge conflicts. Add docs to `files_forbidden` in briefings, and handle doc updates in
the coordinator session.

**The corollary is a dispatch rule: an issue whose entire deliverable is a doc edit can never
be completed by a worker, and must not be queued.** Workers read `CLAUDE.md`, so a worker
handed a doc-only task correctly refuses, exits clean with **0 commits**, and is reaped as
`advisory` — which the drive queue reads as a failed attempt. It burns both attempts and lands
in terminal `blocked`, and it will do so again on every retry.

Observed on **#2195 itself** (2026-08-14): worker `b1a090a5e011` spent 3 turns and $0.11 to
conclude that the rule forbidding the work "exists verbatim at CLAUDE.md line 156, and the
issue itself explicitly says this." That was the correct answer, and it would be the correct
answer every time. Doc-only issues are coordinator work by policy — do them in the operator
session, or split them so the worker does the code half and the coordinator does the doc half.

## Where the money actually goes

Measuring fleet spend is its own trap — bare `coord usage` undercounts, and estimated columns
are not authoritative. See [`OPERATING_GOTCHAS.md`](OPERATING_GOTCHAS.md) for the measurement
rules and #17 for the #2132 review-verdict cost analysis.
