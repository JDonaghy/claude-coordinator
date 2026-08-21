"""Deterministic post-render step for Gate-A HTML mock sets (#2512).

A Gate-A render produces one flat, disconnected `.html` file per
screen-state under `tests/acceptance/ms-NN/mocks/` (`coord/mock_author.py`)
— there was no way to get from one to another except knowing the filename
and opening it directly. This script closes that gap with pure navigation
glue: it scans `mocks/*.html` and writes `mocks/index.html`, a plain list
of links, one per mock, labelled from each mock's own `<title>` tag (every
mock already carries a descriptive one — see the `MOCK_AUTHOR_SYSTEM_PROMPT`
rule in `coord/agent.py`).

This is deliberately a *script the mock-author worker runs*, not something
the mock-author LLM free-hands per milestone — the same "run a provided
script, don't hand-roll it" posture this repo already uses for sealed-suite
tooling (`scripts/coord-test-runner.sh`). Every Gate-A mock set gets the
exact same glue page, generated the exact same way, so nothing drifts by
taste between milestones or repos.

Zero JS, no framework, no live data — `mocks/index.html` is still
"self-contained static HTML" per docs/CUSTOMER_PORTAL.md's mocks-stay-static
rule; it just links between already-static siblings.

Usage:
    python scripts/gen_mock_index.py tests/acceptance/ms-NN/mocks

`coord/mock_author.py`'s seed briefings (`build_mock_author_briefing` /
`build_mock_author_amend_briefing`) tell the mock-author worker to run
exactly this, as the last step before committing.
"""
from __future__ import annotations

import html
import re
import sys
from pathlib import Path

INDEX_NAME = "index.html"

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


def _extract_title(page_html: str, *, fallback: str) -> str:
    """Pull the `<title>` text out of a rendered mock, collapsing internal
    whitespace/newlines. Falls back to the filename when a mock is missing
    a `<title>` tag entirely (should not happen per the mock-author system
    prompt, but a missing label beats a crashed script)."""
    match = _TITLE_RE.search(page_html)
    if not match:
        return fallback
    title = re.sub(r"\s+", " ", match.group(1)).strip()
    return title or fallback


def index_mock_files(mocks_dir: Path) -> list[Path]:
    """Every `*.html` mock in *mocks_dir* other than the index itself,
    sorted by filename for a stable, reproducible ordering across runs."""
    return sorted(
        p for p in mocks_dir.glob("*.html") if p.name != INDEX_NAME
    )


def generate_index(mocks_dir: Path) -> str:
    """Build the `index.html` content: an unstyled `<ul>` of links, one per
    mock file, labelled from that mock's own `<title>`."""
    items = []
    for path in index_mock_files(mocks_dir):
        page_html = path.read_text(encoding="utf-8")
        title = _extract_title(page_html, fallback=path.name)
        items.append(
            f'    <li><a href="{html.escape(path.name)}">'
            f"{html.escape(title)}</a></li>"
        )
    body = "\n".join(items) if items else "    <li>(no mocks found)</li>"
    return (
        "<!DOCTYPE html>\n"
        "<html>\n"
        "<head>\n"
        '<meta charset="utf-8">\n'
        "<title>Gate-A mock set</title>\n"
        "</head>\n"
        "<body>\n"
        "<h1>Gate-A mock set</h1>\n"
        "<ul>\n"
        f"{body}\n"
        "</ul>\n"
        "</body>\n"
        "</html>\n"
    )


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: gen_mock_index.py <mocks-dir>", file=sys.stderr)
        return 2
    mocks_dir = Path(argv[1])
    if not mocks_dir.is_dir():
        print(f"error: not a directory: {mocks_dir}", file=sys.stderr)
        return 1
    content = generate_index(mocks_dir)
    out_path = mocks_dir / INDEX_NAME
    out_path.write_text(content, encoding="utf-8")
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
