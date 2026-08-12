---
name: Bug report
about: >-
  Report broken behaviour for the test-first bug lane (docs/TEST_FIRST_BUG_LANE.md).
  Fill in all four sections — a bug report missing any of them can't be turned
  into a red test.
title: ''
labels: bug
assignees: ''
---

<!--
This is the test-first bug lane's intake contract (docs/TEST_FIRST_BUG_LANE.md
"The intake contract", #1964). All four sections below are required. They are
what a hand-authored (or agent-assisted) `contract.md` gets derived from — no
milestone, no UX mock, no Gate-A sign-off required.

`coord issue create --expected --actual --repro --evidence` renders this same
structure from the CLI (coord/bug_intake.py is the single source of truth for
the four headings — keep this file's headings in sync with it).
-->

## Expected behaviour

<!-- What should happen, in observable terms. -->

## Actual behaviour

<!-- What happens instead. -->

## Reproduction

<!-- The shortest path to see it: exact steps/commands, starting from a clean checkout. -->

## Evidence

<!--
Screenshot, wireframe, OR a reference implementation that behaves correctly
(a sibling backend, or a prior release). A reference implementation is the
highest-value form of evidence — it makes the expectation executable by
construction ("X should look like Y here" is a complete specification).
-->
