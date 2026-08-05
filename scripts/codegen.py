"""Generate TypeScript wire types from the dashboard's OpenAPI spec (#1550).

There is no single source of truth for any wire type in this repo — every
contract is a hand-maintained mirror on both sides. #750 closed that gap by
generating `coord/dashboard/webapp/src/api/generated.ts` straight from the
Python dataclasses (`coord.models.Assignment`, `coord.pipeline.PipelineStage`
/ `PipelineGate` / `PipelineView`). #1550 moves the source of truth up one
level: this script now reads `coord.dashboard.server.openapi_spec()` —
the same `components/schemas` document served at `GET /openapi.json` and
already regression-tested against the real Starlette route table
(`tests/test_openapi.py`'s `declared_routes(...) == spec_routes(...)`,
#757) — instead of introspecting the dataclasses a second time. Generating
from the *served* contract, rather than from the Python types that happen to
back it today, means a future endpoint whose response shape isn't a bare
`dataclasses.asdict()` (a hand-composed object, a subset of fields, a $ref
array) still gets a correct TS mirror: whatever `coord/openapi.py` says the
wire shape is, is what ships to TypeScript.

`ENUM_OVERRIDES` below exists because JSON Schema (like the dataclasses
before it) can't express "this string is really one of these N values" —
`coord/openapi.py:json_schema_for` maps every `str` field to a bare
`{"type": "string"}`. These are hand-curated — update them alongside the
Python source when a new value is introduced. The `_ENUM_BLOCK` constants
(`AssignmentStatus`, `AssignmentType`, `TestVerdict`, `PipelineAction`) are
themselves hand-authored (not derived from a schema): they encode
wire-contract decisions — including actions the client supports that aren't
dispatched by `compute_pipeline` (e.g. "unstick") and forthcoming values
ahead of their backend implementation — that don't correspond 1:1 to a
single schema.

Usage:
    .venv/bin/python scripts/codegen.py            # regenerate generated.ts in place
    .venv/bin/python scripts/codegen.py --check     # exit 1 (no write) if generated.ts is stale

`tests/test_generated_types_fixture.py` runs the --check equivalent in CI (the
same pattern as `scripts/gen_board_fixture.py` / `tests/test_board_fixture.py`
for the /board golden fixture) so a stale checkout fails the build.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from coord.dashboard.server import openapi_spec

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_PATH = REPO_ROOT / "coord" / "dashboard" / "webapp" / "src" / "api" / "generated.ts"

# Schemas to emit as TS interfaces, in display order — purely cosmetic (TS
# `interface` declarations are hoisted, so forward references within
# generated.ts are legal regardless of order). Anything present in the spec
# but not listed here is appended afterwards, sorted by name, so a newly
# schema-registered dataclass is never silently dropped.
SCHEMA_DISPLAY_ORDER: tuple[str, ...] = (
    "PipelineStage",
    "PipelineGate",
    "PipelineView",
    "Assignment",
)

# (schema name, field name) -> literal TS type, bypassing the mechanical
# JSON-Schema-to-TS mapping below. See module docstring for why these exist
# and where each value set comes from.
ENUM_OVERRIDES: dict[tuple[str, str], str] = {
    # coord/models.py Assignment.status: default "pending"; dao.TERMINAL_STATUSES
    # adds "done"/"merged"/"failed"/"cancelled"/"advisory"; "running" once dispatched.
    ("Assignment", "status"): "AssignmentStatus",
    # coord/models.py Assignment.type — see AssignmentType below for the real
    # value set (#1550 found and fixed a drifted hand enum here, see PR).
    ("Assignment", "type"): "AssignmentType",
    # coord/models.py Assignment.smoke_test docstring: "None | pass | fail".
    ("Assignment", "smoke_test"): "'pass' | 'fail' | null",
    # coord/models.py Assignment.review_state docstring: pending|dispatched|done.
    ("Assignment", "review_state"): "'pending' | 'dispatched' | 'done' | null",
    # coord/models.py Assignment.test_state mirrors pipeline.py's test_verdict.
    # #1395: TestVerdict includes 'running' — a transient, non-verdict value a
    # driver sets while it runs the suite locally; every reader compares
    # against the terminal values explicitly, so this never gates as a verdict.
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
    # coord/pipeline.py PipelineStage.status: the four literal values
    # compute_pipeline assigns (see its "Build stages list" section) —
    # "active" | "completed" | "skipped" | "waiting". #1550: this was
    # generated as a bare `string` before the OpenAPI-spec switch; verified
    # against the four literal assignments in coord/pipeline.py and tightened
    # here since a JSON Schema `{"type": "string"}` can't express it either.
    ("PipelineStage", "status"): "'active' | 'completed' | 'skipped' | 'waiting'",
}

# Hand-authored wire-contract enums — see module docstring for why these are
# not mechanically derived from a schema.
_ENUM_BLOCK = """\
export type AssignmentStatus =
  | 'pending'
  | 'running'
  | 'done'
  | 'failed'
  | 'cancelled'
  | 'advisory'
  | 'merged'

/**
 * coord/models.py Assignment.type's real value set — #1550 found this had
 * drifted: the hand-authored enum this replaces listed 'merge' and 'fix',
 * neither of which is ever a literal `type=` value (coord/config.py's #1137
 * audit note: a dedicated `type="merge"` was tried and reverted; `type="fix"`
 * was deliberately never introduced — both share `type="work"` with their
 * headless counterpart and are distinguished by `provider_name`/
 * `review_of_assignment_id` instead, see `attention_threshold_for`) — while
 * missing seven values that are real: 'audit' (coord/models.py docstring,
 * #885 --audit-of), and the six interactive session types from
 * coord/config.py's `INTERACTIVE_SESSION_TYPES` plus the two headless
 * lightweight-worker types from `_DEFAULT_ATTENTION_THRESHOLDS`.
 */
export type AssignmentType =
  | 'work'
  | 'review'
  | 'plan'
  | 'smoke'
  | 'conflict-fix'
  | 'mock-author'
  | 'test-author'
  | 'audit'
  | 'chat'
  | 'troubleshoot'
  | 'milestone-chat'
  | 'refinement'
  | 'new-issue-chat'
  | 'test-chat'

export type TestVerdict = 'passed' | 'failed' | 'skipped' | 'running'

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
 * Generated by `scripts/codegen.py` from the dashboard's OpenAPI 3 spec
 * (`coord.dashboard.server.openapi_spec()`, itself built by
 * `coord/openapi.py` from `coord/models.py` / `coord/pipeline.py`) — #1550
 * (originally #750). Regenerate after any field change:
 *
 *     .venv/bin/python scripts/codegen.py
 *
 * `tests/test_generated_types_fixture.py` fails CI if this file drifts from
 * what the generator produces right now, so a stale checkout can't merge.
 */\
"""


def ts_type_from_schema(schema: dict[str, Any]) -> str:
    """Map a JSON Schema fragment (as produced by ``coord/openapi.py``'s
    ``json_schema_for``/``dataclass_schema``) to a TypeScript type string.

    Mirrors the shape of ``coord/openapi.py:json_schema_for`` structurally,
    just targeting TypeScript instead of building the schema itself.
    """
    if "$ref" in schema:
        base = schema["$ref"].rsplit("/", 1)[-1]
    elif "anyOf" in schema:
        base = " | ".join(ts_type_from_schema(s) for s in schema["anyOf"])
    else:
        json_type = schema.get("type")
        if json_type == "null":
            return "null"
        if json_type == "string":
            base = "string"
        elif json_type == "boolean":
            base = "boolean"
        elif json_type in ("integer", "number"):
            base = "number"
        elif json_type == "array":
            items = schema.get("items") or {}
            base = f"{ts_type_from_schema(items)}[]" if items else "unknown[]"
        elif json_type == "object":
            addl = schema.get("additionalProperties")
            if isinstance(addl, dict):
                base = f"Record<string, {ts_type_from_schema(addl)}>"
            else:
                base = "Record<string, unknown>"
        elif json_type is None:
            base = "unknown"
        else:
            raise TypeError(
                f"scripts/codegen.py: no TS mapping for JSON Schema type {json_type!r} "
                f"(schema={schema!r}) — add one to ts_type_from_schema()."
            )

    return f"{base} | null" if schema.get("nullable") else base


def emit_interface(name: str, schema: dict[str, Any]) -> str:
    properties: dict[str, Any] = schema.get("properties", {})
    lines = [f"export interface {name} {{"]
    for field_name, field_schema in properties.items():
        override = ENUM_OVERRIDES.get((name, field_name))
        ts = override if override is not None else ts_type_from_schema(field_schema)
        lines.append(f"  {field_name}: {ts}")
    lines.append("}")
    return "\n".join(lines)


def _ordered_schema_names(schemas: dict[str, Any]) -> list[str]:
    """#1550: display order for the emitted interfaces — see
    ``SCHEMA_DISPLAY_ORDER``'s docstring. Every schema in the spec is
    emitted; nothing is silently dropped."""
    known = [name for name in SCHEMA_DISPLAY_ORDER if name in schemas]
    unknown = sorted(name for name in schemas if name not in SCHEMA_DISPLAY_ORDER)
    return known + unknown


def generate() -> str:
    spec = openapi_spec()
    schemas: dict[str, Any] = spec.get("components", {}).get("schemas", {})
    parts = [HEADER, _ENUM_BLOCK]
    parts.extend(emit_interface(name, schemas[name]) for name in _ordered_schema_names(schemas))
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
