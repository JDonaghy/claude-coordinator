"""Generate TypeScript wire types from the Python dataclasses that define them (#750).

There is no single source of truth for any wire type in this repo — every
contract is a hand-maintained mirror on both sides. For the dashboard API
(`coord/pipeline.py` / `coord/dashboard/server.py` dataclasses `asdict`'d over
`GET /api/board` and `GET /api/pipeline`) the TS mirror lives in
`coord/dashboard/webapp/src/api/client.ts` and had already started drifting in
the open (fields added to `Assignment`/`PipelineView` with no corresponding TS
field, or vice versa).

This script closes that gap for the TS side: it introspects the real Python
dataclasses (`coord.models.Assignment`, `coord.pipeline.PipelineStage`,
`coord.pipeline.PipelineGate`, `coord.pipeline.PipelineView`) via
`dataclasses.fields()` + `typing.get_type_hints()` and emits matching
TypeScript `interface`s to `coord/dashboard/webapp/src/api/generated.ts`. A
Python field addition/removal/type change regenerates the TS automatically —
no more manually keeping two files in sync.

`ENUM_OVERRIDES` below exists because several fields are typed as a bare `str`
in Python (dataclasses can't express "this string is really one of these N
values") but are documented as small fixed enums, either in an inline comment
next to the field or by their real call-site usage. These are hand-curated —
update them alongside the Python source when a new value is introduced. The
`_ENUM_BLOCK` constants (`AssignmentStatus`, `AssignmentType`, `TestVerdict`,
`PipelineAction`) are themselves hand-authored (not derived from a dataclass):
they encode wire-contract decisions — including actions the client supports
that aren't dispatched by `compute_pipeline` (e.g. "unstick") and forthcoming
values ahead of their backend implementation — that don't correspond 1:1 to
a single Python type.

Usage:
    .venv/bin/python scripts/codegen.py            # regenerate generated.ts in place
    .venv/bin/python scripts/codegen.py --check     # exit 1 (no write) if generated.ts is stale

`tests/test_generated_types_fixture.py` runs the --check equivalent in CI (the
same pattern as `scripts/gen_board_fixture.py` / `tests/test_board_fixture.py`
for the /board golden fixture) so a stale checkout fails the build.
"""

from __future__ import annotations

import dataclasses
import sys
import types
import typing
from pathlib import Path

from coord.models import Assignment
from coord.pipeline import PipelineGate, PipelineStage, PipelineView

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_PATH = REPO_ROOT / "coord" / "dashboard" / "webapp" / "src" / "api" / "generated.ts"

# Dataclasses to emit as TS interfaces, in dependency order (a dataclass
# referenced by a later one — e.g. PipelineView.stages: list[PipelineStage] —
# must be emitted first so the generated file doesn't forward-reference).
DATACLASSES: tuple[type, ...] = (PipelineStage, PipelineGate, PipelineView, Assignment)

# (dataclass name, field name) -> literal TS type, bypassing the mechanical
# str/int/bool/list/dict mapping below. See module docstring for why these
# exist and where each value set comes from.
ENUM_OVERRIDES: dict[tuple[str, str], str] = {
    # coord/models.py Assignment.status: default "pending"; dao.TERMINAL_STATUSES
    # adds "done"/"merged"/"failed"/"cancelled"/"advisory"; "running" once dispatched.
    ("Assignment", "status"): "AssignmentStatus",
    # coord/models.py Assignment.type: "work" (default) | "review" | "plan" |
    # "smoke" | "conflict-fix", plus "merge"/"fix" per the client's forward-looking
    # PipelineAction-adjacent contract (see AssignmentType below).
    ("Assignment", "type"): "AssignmentType",
    # coord/models.py Assignment.smoke_test docstring: "None | pass | fail".
    ("Assignment", "smoke_test"): "'pass' | 'fail' | null",
    # coord/models.py Assignment.review_state docstring: pending|dispatched|done.
    ("Assignment", "review_state"): "'pending' | 'dispatched' | 'done' | null",
    # coord/models.py Assignment.test_state mirrors pipeline.py's test_verdict.
    ("Assignment", "test_state"): "TestVerdict | null",
    # coord/models.py Assignment.review_verdict docstring: None | approve | request-changes.
    ("Assignment", "review_verdict"): "'approve' | 'request-changes' | null",
    # coord/pipeline.py PipelineView.review_verdict: same 2-value verdict.
    ("PipelineView", "review_verdict"): "'approve' | 'request-changes' | null",
    # coord/pipeline.py PipelineView.test_verdict mirrors Assignment.test_state.
    ("PipelineView", "test_verdict"): "TestVerdict | null",
    # coord/pipeline.py PipelineGate.action: real values emitted by
    # compute_pipeline (test-verdict, dispatch_review, dispatch_smoke, enqueue,
    # post_findings, record-review-verdict, dispatch_fix, merge, retry) are a
    # subset of the full PipelineAction contract below.
    ("PipelineGate", "action"): "PipelineAction",
}

# Hand-authored wire-contract enums — see module docstring for why these are
# not mechanically derived from a Python type.
_ENUM_BLOCK = """\
export type AssignmentStatus =
  | 'pending'
  | 'running'
  | 'done'
  | 'failed'
  | 'cancelled'
  | 'advisory'
  | 'merged'

export type AssignmentType =
  | 'work'
  | 'review'
  | 'plan'
  | 'smoke'
  | 'conflict-fix'
  | 'merge'
  | 'fix'

export type TestVerdict = 'passed' | 'failed' | 'skipped'

/**
 * Actions supported by POST /api/pipeline/action.
 *
 * dispatch_review    — kick off an adversarial review assignment
 * dispatch_smoke     — kick off a smoke-test assignment
 * enqueue            — add to merge queue
 * merge              — merge a queued PR (must be in "pending" state)
 * post_findings      — post orphaned review findings to GitHub
 * unstick            — cancel a stuck assignment and mark it failed
 * retry              — (forthcoming) retry a failed work assignment
 * dispatch_fix       — (forthcoming) dispatch a fix for a test failure / review request-changes
 * test-verdict       — (forthcoming) record passed/failed/skipped test verdict
 * record-review-verdict — (forthcoming) record an approved/changes-requested review verdict
 */
export type PipelineAction =
  | 'dispatch_review'
  | 'dispatch_smoke'
  | 'enqueue'
  | 'merge'
  | 'post_findings'
  | 'unstick'
  | 'retry'
  | 'dispatch_fix'
  | 'test-verdict'
  | 'record-review-verdict'\
"""

HEADER = """\
/**
 * AUTO-GENERATED — DO NOT EDIT BY HAND.
 *
 * Generated by `scripts/codegen.py` from the Python dataclasses that define
 * the coordinator's wire types (coord/models.py, coord/pipeline.py) — #750.
 * Regenerate after any field change:
 *
 *     .venv/bin/python scripts/codegen.py
 *
 * `tests/test_generated_types_fixture.py` fails CI if this file drifts from
 * what the generator produces right now, so a stale checkout can't merge.
 */\
"""


def _ts_scalar(tp: object) -> str | None:
    if tp is str:
        return "string"
    if tp is bool:
        return "boolean"
    if tp in (int, float):
        return "number"
    return None


def ts_type(tp: object) -> str:
    """Map a resolved Python type (from typing.get_type_hints) to a TS type string."""
    if tp is type(None):
        return "null"
    if isinstance(tp, type):
        scalar = _ts_scalar(tp)
        if scalar is not None:
            return scalar
        if dataclasses.is_dataclass(tp):
            return tp.__name__
        if tp is dict:
            return "Record<string, unknown>"
    if tp is typing.Any:
        return "unknown"

    origin = typing.get_origin(tp)
    args = typing.get_args(tp)

    if origin in (list, typing.List):
        (inner,) = args
        return f"{ts_type(inner)}[]"
    if origin in (dict, typing.Dict):
        if len(args) == 2:
            return f"Record<string, {ts_type(args[1])}>"
        return "Record<string, unknown>"
    if origin is typing.Union or origin is types.UnionType:
        non_none = [a for a in args if a is not type(None)]
        has_none = len(non_none) != len(args)
        mapped = [ts_type(a) for a in non_none]
        result = " | ".join(mapped) if mapped else "unknown"
        return f"{result} | null" if has_none else result

    raise TypeError(
        f"scripts/codegen.py: no TS mapping for Python type {tp!r} — add one to ts_type() "
        "or an entry to ENUM_OVERRIDES."
    )


def emit_interface(cls: type) -> str:
    hints = typing.get_type_hints(cls)
    lines = [f"export interface {cls.__name__} {{"]
    for f in dataclasses.fields(cls):
        override = ENUM_OVERRIDES.get((cls.__name__, f.name))
        ts = override if override is not None else ts_type(hints[f.name])
        lines.append(f"  {f.name}: {ts}")
    lines.append("}")
    return "\n".join(lines)


def generate() -> str:
    parts = [HEADER, _ENUM_BLOCK]
    parts.extend(emit_interface(cls) for cls in DATACLASSES)
    return "\n\n".join(parts) + "\n"


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    content = generate()
    if "--check" in args:
        current = OUTPUT_PATH.read_text() if OUTPUT_PATH.exists() else ""
        if current != content:
            print(
                f"{OUTPUT_PATH} is stale — run `.venv/bin/python scripts/codegen.py` "
                "to regenerate.",
                file=sys.stderr,
            )
            return 1
        print(f"{OUTPUT_PATH} is up to date.")
        return 0
    OUTPUT_PATH.write_text(content)
    print(f"wrote {OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
