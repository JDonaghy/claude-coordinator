"""Tiny rendering helpers shared by probes (#1628).

These live next to the probes, not in the renderer, on purpose: a probe owns
its ``headroom`` string end to end (see ``coord.health.models``), so the
byte/duration formatting it needs has to be reachable without importing a
renderer.  A renderer importing *this* is fine; a probe importing a renderer
is the fork we're preventing.
"""

from __future__ import annotations

from pathlib import Path

_GIB = 1024.0 ** 3


def human_bytes(n: float) -> str:
    """``78G`` / ``512M`` / ``0B`` — short enough for a one-line report."""
    n = float(n)
    sign = "-" if n < 0 else ""
    n = abs(n)
    for unit, size in (("T", 1024.0 ** 4), ("G", _GIB), ("M", 1024.0 ** 2), ("K", 1024.0)):
        if n >= size:
            value = n / size
            # 43.2G reads better than 43G below 10; above that the decimal is noise.
            return f"{sign}{value:.1f}{unit}" if value < 10 else f"{sign}{value:.0f}{unit}"
    return f"{sign}{n:.0f}B"


def gib(n: float) -> float:
    """Bytes → GiB, as a float."""
    return float(n) / _GIB


def human_hours(seconds: float) -> str:
    """``128.8h`` — the unit the graph-staleness incident was reported in."""
    return f"{seconds / 3600.0:.1f}h"


def expand(path: str | Path, home: str | Path) -> Path:
    """``~``/``~/x`` → under *home*, everything else verbatim.

    Deliberately NOT ``Path.expanduser()``: probes must expand against the
    context's home so a test can point the whole engine at a tmp dir without
    monkeypatching ``Path.home`` globally.
    """
    s = str(path)
    if s == "~":
        return Path(home)
    if s.startswith("~/"):
        return Path(home) / s[2:]
    return Path(s)


def shorten_path(path: str, home: str) -> str:
    """``/home/john/src/vimcode`` → ``~/src/vimcode`` when it's under *home*."""
    p, h = str(path), str(home).rstrip("/")
    if h and (p == h or p.startswith(h + "/")):
        return "~" + p[len(h):]
    return p
