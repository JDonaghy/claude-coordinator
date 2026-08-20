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

#2009 (epic #2002) — THIS SCRIPT IS NOW CROSS-REPO. The consumer it writes
for, `src/api/generated.ts`, moved to the `coord-web` repo along with the
rest of the webapp, but the *producer* — `coord.dashboard.server`'s OpenAPI
spec — is necessarily still here. So the destination is no longer a fixed
path inside this repo and must be named explicitly, by `--out PATH` or by
`$COORD_WEB_SRC` pointing at a `coord-web` checkout's root. There is
deliberately no fallback default: silently writing a hard-coded path that
this repo no longer contains would either recreate a dead directory nobody
consumes or, worse, report "up to date" against a file that does not exist.

That also relocates the drift GATE. It used to be `webapp-types` in
`.github/workflows/test.yml` (`python scripts/codegen.py --check`), which
could only work while both halves lived in one checkout; that job is gone.
The check now belongs to `coord-web`'s CI, which has its `generated.ts` and
installs `code-coordinator[server]` from PyPI (docs/ADR_COORD_WEB_CI.md,
#2006) to get this script. What still runs here is
`tests/test_generated_types_fixture.py`, narrowed to what a single checkout
can actually prove: that the generator produces complete, well-formed output
covering every schema in the served spec.

Usage:
    # regenerate into a coord-web checkout
    .venv/bin/python scripts/codegen.py --out ~/src/coord-web/src/api/generated.ts
    COORD_WEB_SRC=~/src/coord-web .venv/bin/python scripts/codegen.py
    # exit 1 (no write) if that file is stale
    COORD_WEB_SRC=~/src/coord-web .venv/bin/python scripts/codegen.py --check
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from coord.dashboard.server import openapi_spec

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Path of the emitted file RELATIVE to a `coord-web` checkout's root.
OUTPUT_RELPATH = Path("src") / "api" / "generated.ts"

#: Env var naming a `coord-web` checkout root, used when `--out` is absent.
OUTPUT_ENV_VAR = "COORD_WEB_SRC"


class OutputPathError(Exception):
    """No destination was named — see :func:`resolve_output_path`."""


def resolve_output_path(explicit: str | Path | None = None) -> Path:
    """Where to write/check ``generated.ts``: ``--out`` > ``$COORD_WEB_SRC``.

    Raises :class:`OutputPathError` when neither is set, rather than guessing
    (#2009): the old hard-coded ``coord/dashboard/webapp/src/api/generated.ts``
    is not in this repo any more, so a guess is always wrong and — under
    ``--check`` — wrong in the direction that reports success.
    """
    if explicit is not None:
        return Path(explicit).expanduser()
    root = os.environ.get(OUTPUT_ENV_VAR)
    if root:
        return Path(root).expanduser() / OUTPUT_RELPATH
    raise OutputPathError(
        "no destination for generated.ts. Since #2009 the webapp lives in "
        f"the coord-web repo, so pass --out PATH or set ${OUTPUT_ENV_VAR} to "
        f"a coord-web checkout root (the file is written to its "
        f"{OUTPUT_RELPATH}). See this script's module docstring."
    )

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


def _parse_out(args: list[str]) -> str | None:
    """``--out PATH`` / ``--out=PATH`` from *args*, or None."""
    for i, arg in enumerate(args):
        if arg.startswith("--out="):
            return arg.split("=", 1)[1]
        if arg == "--out":
            if i + 1 >= len(args):
                raise OutputPathError("--out requires a PATH argument")
            return args[i + 1]
    return None


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    try:
        output_path = resolve_output_path(_parse_out(args))
    except OutputPathError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    content = generate()
    if "--check" in args:
        # #2009: a MISSING file is now a hard failure, not "stale vs empty".
        # Pre-split, absence meant a fresh checkout that had simply never run
        # the generator; post-split it means `--out`/$COORD_WEB_SRC is
        # pointing somewhere that is not a coord-web checkout, and treating
        # that as ordinary staleness would send an operator off to regenerate
        # a file into the wrong directory.
        if not output_path.exists():
            print(
                f"{output_path} does not exist — is --out/${OUTPUT_ENV_VAR} "
                "pointing at a coord-web checkout?",
                file=sys.stderr,
            )
            return 1
        if output_path.read_text() != content:
            print(
                f"{output_path} is stale — run `python scripts/codegen.py "
                f"--out {output_path}` to regenerate.",
                file=sys.stderr,
            )
            return 1
        print(f"{output_path} is up to date.")
        return 0
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(content)
    print(f"wrote {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
