# coord web — design mocks

Gate-A design mocks for the [coord web control center](../../WEB_CONTROL_CENTER.md)
program. Authored 2026-07-28, **before** any of milestone #51 / #52 was dispatched, so
the design is settled before a worker spends a token on it.

| File | Screen |
|---|---|
| [`pipeline-wide.html`](pipeline-wide.html) | Pipeline panel, desktop — rail + list + detail |
| [`pipeline-narrow.html`](pipeline-narrow.html) | Pipeline panel, phone — bottom row + drill-in |
| [`issue-detail.html`](issue-detail.html) | Issue detail — tab set, review findings, action menu |
| [`_tokens.css`](_tokens.css) | Shared palette + type + geometry (reference copy) |

**Open them in a browser.** No build step, no server, no external requests — that is a
hard requirement of the Gate-A mock shape (#1542), not a convenience. Each file inlines
its own copy of the tokens; `_tokens.css` is the shared reference and the seed for the
real token layer in #1546.

## What the design decides

- **Dark by default, light fully designed.** Ground is `#101418` — a cool slate biased
  slightly toward the accent, never pure black (halation on OLED makes pure black worse
  for long sessions, not better). Toggle the theme from the rail.
- **Monospace means "a value the machine owns"** — issue numbers, branches, SHAs, machine
  names, durations, costs. Never chrome, never prose. The TUI is mono everywhere; this
  one rule does most of the work of not feeling like the TUI, and makes machine truth
  scannable.
- **The stage strip is the signature element.** Work → Test → Review → Merge, full-size
  in the detail and as a four-segment miniature on every list row: a row's whole story
  reads from a 70px bar.
- **Attention before detail.** Both layouts open with what needs a human. The
  review-findings detail leads with a decision banner saying what the reviewer wants and
  what the button will do.
- **The accent means "work is happening here."** Cyan is never decorative — running
  stage, live session, selected row. Semantic pass/attention/fail are separate and
  deliberately desaturated.
- **Structure:** rail → list panel → main. The rail collapses to a 60px icon strip and
  becomes the bottom row under 900px; the list panel minimizes to nothing and main
  expands. Both are live in `pipeline-wide.html`.

## Status

These are **design** mocks. When milestone #52 is dispatched they become the Gate-A
contract an independent `test-author` writes DOM assertions against (#1542), at which
point they move under `tests/acceptance/ms-52/mocks/` and are frozen. Until then they
are free to change — the operator explicitly expects to have improvement ideas once the
thing is built and in daily use, and that feedback lands as new issues, not as edits to
a frozen contract.
