"""Smart task splitting for large worker dispatches.

When a proposal touches more files than the configured threshold, this module
analyses the file list and splits the work into parallel/sequential chunks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from coord.config import DispatchConfig


@dataclass
class WorkChunk:
    """A chunk of work split from a larger plan."""

    chunk_id: int
    files: list[str]
    briefing_fragment: str
    depends_on: list[int] = field(default_factory=list)  # chunk IDs this depends on
    estimated_turns: int = 20


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_test_file(f: str) -> bool:
    """Return True if the file is a test file."""
    p = Path(f)
    return (
        len(p.parts) > 0 and p.parts[0] in ("tests", "test")
        or p.name.startswith("test_")
        or p.name.endswith("_test.py")
    )


def _subsystem(f: str) -> str:
    """Return the top-level directory of a file path, or '' for root-level files."""
    parts = Path(f).parts
    return parts[0] if len(parts) > 1 else ""


def _estimate_turns(files: list[str]) -> int:
    """Rough turn estimate: 15 turns per file, minimum 10."""
    return max(10, len(files) * 15)


def _describe_files(files: list[str]) -> str:
    """Short description of files for a briefing fragment."""
    if not files:
        return ""
    if len(files) <= 3:
        return ", ".join(files)
    return f"{', '.join(files[:3])}, and {len(files) - 3} more"


def _batch(items: list[str], size: int) -> list[list[str]]:
    """Split *items* into batches of at most *size* items."""
    return [items[i : i + size] for i in range(0, len(items), size)]


def _module_stem(f: str) -> str | None:
    """Return the bare module name (no test_ prefix, no .py suffix) for .py files."""
    p = Path(f)
    if p.suffix != ".py":
        return None
    stem = p.stem
    if stem.startswith("test_"):
        return stem[5:]
    if stem.endswith("_test"):
        return stem[:-5]
    return stem


# ---------------------------------------------------------------------------
# Core entry point
# ---------------------------------------------------------------------------

def analyze_plan(
    files_likely: list[str],
    dispatch_config: "DispatchConfig",
    plan: dict | None = None,
) -> list[WorkChunk]:
    """Analyse files and split into parallel/sequential chunks if needed.

    Returns a single-chunk list (chunk_id=1) when the total file count is at or
    below ``dispatch_config.max_files_per_worker``.

    Splitting heuristics
    --------------------
    a. **File count**: more than ``max_files_per_worker`` triggers a split.
    b. **Subsystem grouping**: files are grouped by their top-level directory.
       Each subsystem becomes its own chunk (sub-split again if still too big).
    c. **New module + consumers**: the impl chunk that introduces a new module
       is labelled chunk A; test chunks that are named ``test_{module}`` are
       given ``depends_on=[A.chunk_id]`` so they run after the module exists.
    d. **Mechanical/cleanup chunk**: all test files (``tests/`` dir or
       ``test_*.py``) are batched into a single test chunk that depends on
       every implementation chunk, or on the specific impl chunk it tests
       (when the name matches ``test_{module}`` ↔ ``{module}.py``).

    Args:
        files_likely: List of file paths the worker will touch.
        dispatch_config: Config section with splitting thresholds.
        plan: Optional structured plan dict (reserved for future enrichment).

    Returns:
        Ordered list of WorkChunk objects.  Independent chunks can be
        dispatched in parallel; chunks with ``depends_on`` must wait.
    """
    if not files_likely or len(files_likely) <= dispatch_config.max_files_per_worker:
        return [
            WorkChunk(
                chunk_id=1,
                files=list(files_likely),
                briefing_fragment=_describe_files(files_likely) if files_likely else "",
                depends_on=[],
                estimated_turns=_estimate_turns(files_likely),
            )
        ]

    # ── Separate tests from implementation files ──────────────────────────
    test_files = [f for f in files_likely if _is_test_file(f)]
    impl_files = [f for f in files_likely if not _is_test_file(f)]

    # ── Group impl files by top-level directory (subsystem) ───────────────
    groups: dict[str, list[str]] = {}
    for f in impl_files:
        key = _subsystem(f)
        groups.setdefault(key, []).append(f)

    # ── Create one chunk per subsystem (sub-split if still oversized) ─────
    chunks: list[WorkChunk] = []
    chunk_id = 1

    for subsystem in sorted(groups.keys()):
        sub_files = groups[subsystem]
        label = subsystem if subsystem else "root"
        for batch in _batch(sub_files, dispatch_config.max_files_per_worker):
            frag = f"{label}: {_describe_files(batch)}"
            chunks.append(
                WorkChunk(
                    chunk_id=chunk_id,
                    files=batch,
                    briefing_fragment=frag,
                    depends_on=[],
                    estimated_turns=_estimate_turns(batch),
                )
            )
            chunk_id += 1

    # ── Build module-name → chunk-id map for dependency wiring ───────────
    # Maps bare stem (e.g. "split_work") → chunk_id of the impl chunk that
    # introduces it.  We prefer the *first* chunk that owns the file.
    module_to_chunk: dict[str, int] = {}
    for chunk in chunks:
        for f in chunk.files:
            stem = _module_stem(f)
            if stem and stem not in module_to_chunk:
                module_to_chunk[stem] = chunk.chunk_id

    # ── Add test chunk (if any test files exist) ──────────────────────────
    if test_files:
        test_deps: set[int] = set()
        has_unmatched = False
        for tf in test_files:
            stem = _module_stem(tf)
            if stem and stem in module_to_chunk:
                # This test is specifically for a new module in chunk N
                test_deps.add(module_to_chunk[stem])
            else:
                has_unmatched = True

        if not test_deps or has_unmatched:
            # Some test files have no name-based match → conservatively depend
            # on ALL implementation chunks to guarantee ordering correctness.
            test_deps = {c.chunk_id for c in chunks}

        test_label = _subsystem(test_files[0]) or "tests"
        frag = f"{test_label}: {_describe_files(test_files)}"
        chunks.append(
            WorkChunk(
                chunk_id=chunk_id,
                files=test_files,
                briefing_fragment=frag,
                depends_on=sorted(test_deps),
                estimated_turns=_estimate_turns(test_files),
            )
        )

    return chunks


def format_chunks_summary(chunks: list[WorkChunk]) -> str:
    """Return a human-readable summary of the split chunks."""
    if len(chunks) <= 1:
        return ""
    lines: list[str] = [f"  Split into {len(chunks)} chunks:"]
    for chunk in chunks:
        dep_str = ""
        if chunk.depends_on:
            dep_str = f" (after chunk {', '.join(str(d) for d in chunk.depends_on)})"
        lines.append(
            f"    Chunk {chunk.chunk_id}: {len(chunk.files)} files — "
            f"{chunk.briefing_fragment}{dep_str}"
        )
    return "\n".join(lines)
