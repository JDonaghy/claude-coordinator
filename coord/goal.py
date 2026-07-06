"""#978: GOAL.md → Plans-panel north-star header projection.

`GOAL.md` (repo root) is the living cross-repo/cross-machine intent doc —
`coordinator.yml` is the source of truth for *topology*, GOAL.md is the
source of truth for *intent* (see the project's own CLAUDE.md). The coord-tui
Plans panel pins a short read-only excerpt of it above the plan roster.

**Fail-open, server-computed (mirrors `coord.plans` / #975-#976).** GOAL.md is
a repo-root doc, not shipped in the `coord` PyPI package (`pyproject.toml`
only packages `coord*`) — so it only exists on disk when `coord serve` is
running from an actual git checkout of this repo (the coordinator's own dev/
always-on host, typically an editable install). `read_goal_header()` never
raises: any failure to locate/read/parse the file returns
`{"available": False}`, and the coord-tui Plans panel simply renders without
the pinned header in that case — identical to today's behaviour, no
regression for older daemons or packaged installs.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

# Matches the italic "_Last updated: 2026-07-04_" line convention used at the
# top of GOAL.md.
_LAST_UPDATED_RE = re.compile(r"_Last updated:\s*(\d{4}-\d{2}-\d{2})_")

# The "## \U0001F3AF North star" heading (or a plain "## North star" fallback)
# — matched case-insensitively so an emoji-less rewrite still resolves.
# Restricted to heading levels 2-6: GOAL.md's own H1 title
# ("# Current Goal — North Star") also contains the words "north star", and a
# level-1-inclusive match would bind here instead of the intended `## North
# star` section below it (#978 review) — the first bold text after the H1
# would then be the blockquote's framing sentence, not the real headline.
_NORTH_STAR_HEADING_RE = re.compile(r"^#{2,6}[^\n]*north star[^\n]*$", re.IGNORECASE | re.MULTILINE)

_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_H1_RE = re.compile(r"^#\s+(.+)$", re.MULTILINE)

_HEADLINE_MAX_LEN = 220


def _resolve_goal_md_path() -> Path | None:
    """Best-effort location of this checkout's GOAL.md.

    Reuses the same editable-checkout detection as `cli.py`'s
    `_warn_if_editable_checkout_moved()`: `coord.__file__`'s grandparent
    directory is the repo root only when running from a checkout (not a
    `site-packages` install, which has no repo root at all). Returns `None`
    when GOAL.md can't be resolved to an existing file.
    """
    try:
        import coord as _coord  # noqa: PLC0415

        coord_file = _coord.__file__ or ""
        if "site-packages" in coord_file:
            return None  # PyPI install — GOAL.md was never shipped with it.
        repo_root = Path(coord_file).resolve().parents[1]
        candidate = repo_root / "GOAL.md"
        return candidate if candidate.is_file() else None
    except Exception:  # noqa: BLE001 — best-effort, never raise
        return None


def parse_goal_header(text: str) -> dict:
    """Pure parser: GOAL.md text -> the Plans-panel header projection.

    Split out from `read_goal_header()` so tests can exercise parsing edge
    cases (malformed/missing date, missing north-star section, ...) directly
    against a string fixture, independent of the on-disk file-resolution
    story above.
    """
    result: dict = {
        "available": True,
        "headline": "",
        "last_updated": None,
        "days_since_update": None,
    }

    m = _LAST_UPDATED_RE.search(text)
    if m:
        date_str = m.group(1)
        result["last_updated"] = date_str
        try:
            parsed = date.fromisoformat(date_str)
            result["days_since_update"] = (date.today() - parsed).days
        except ValueError:
            pass  # leave days_since_update as None — an unparseable date still surfaces as text

    # Headline: first bolded sentence under the "North star" heading — falls
    # back to the document's H1 title so a reformatted GOAL.md still surfaces
    # *something* rather than an empty pinned header.
    headline = ""
    heading = _NORTH_STAR_HEADING_RE.search(text)
    if heading:
        bold = _BOLD_RE.search(text[heading.end():])
        if bold:
            headline = bold.group(1).strip()
    if not headline:
        h1 = _H1_RE.search(text)
        if h1:
            headline = h1.group(1).strip()
    if len(headline) > _HEADLINE_MAX_LEN:
        headline = headline[: _HEADLINE_MAX_LEN - 1].rstrip() + "…"
    result["headline"] = headline
    return result


def read_goal_header() -> dict:
    """#978: fail-open GOAL.md -> Plans-panel-header projection.

    Returns `{"available": False}` when GOAL.md can't be located (packaged
    install, no discoverable repo root, missing file) or read. Never raises —
    called from `coord/serve_app.py`'s `board()` handler, where a failure
    here must never blank the rest of the board payload.
    """
    try:
        path = _resolve_goal_md_path()
        if path is None:
            return {"available": False}
        text = path.read_text(encoding="utf-8")
        return parse_goal_header(text)
    except Exception:  # noqa: BLE001 — best-effort, never raise
        return {"available": False}
