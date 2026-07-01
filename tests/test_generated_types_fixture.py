"""#750: generated.ts must never drift from what scripts/codegen.py produces.

Mirrors tests/test_board_fixture.py's freshness-check pattern for the golden
/board fixture — here the "fixture" is the generated TypeScript wire-type
file itself (coord/dashboard/webapp/src/api/generated.ts), mechanically
derived from coord/models.py:Assignment and coord/pipeline.py:PipelineStage /
PipelineGate / PipelineView.

If a Python dataclass field is added, removed, or retyped without
regenerating, this test goes red — closing the "hand-mirrored wire contract"
drift class described in #750 (the same class #632/#748 closed for the
Rust /board struct).
"""

from __future__ import annotations

from scripts.codegen import OUTPUT_PATH, generate


def test_generated_ts_matches_codegen_output():
    assert OUTPUT_PATH.exists(), (
        f"{OUTPUT_PATH} is missing — run "
        "`.venv/bin/python scripts/codegen.py` to generate it."
    )
    on_disk = OUTPUT_PATH.read_text()
    regenerated = generate()
    assert on_disk == regenerated, (
        f"{OUTPUT_PATH} is stale — regenerate it with "
        "`.venv/bin/python scripts/codegen.py` and commit the result."
    )
