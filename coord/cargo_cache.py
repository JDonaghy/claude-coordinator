"""#1402: a shared, per-machine cargo target directory with a bounded GC.

Every Rust assignment used to pay a cold ``cargo`` build. Workers run inside an
ephemeral ``~/.coord/worktrees/<assignment_id>`` checkout, so cargo's default
``<workspace>/target`` lived *inside* the worktree and
:meth:`AgentServer._cleanup_worktree` deleted it along with the tree. Two
workers on the same machine, on the same repo, shared nothing either — each
worktree was its own cold build. Measured on ``tui/``: ~3 min cold vs ~18 s
warm.

This module points every worker on a machine at one cache per repo::

    ~/.coord/cargo-target/<repo_name>/

Concurrency is cargo's problem and cargo already solves it: it takes a build
lock on the target dir, so two workers building the same repo at the same time
block each other briefly rather than corrupting the cache. Correctness across
branches is likewise cargo's: it keys artifacts on a fingerprint of the source,
profile, features and rustc version, so a stale artifact from another branch is
rebuilt rather than reused.

Because the cache now *outlives* the worktree, it needs a bound. :func:`sweep`
is a least-recently-used GC: it totals the per-repo cache directories and
evicts whole directories, oldest-used first, until the total is back under the
cap (default 20 GiB, override with ``COORD_CARGO_CACHE_CAP_GB``). Evicting a
whole repo cache is safe by construction — it is a cache, and the next build
repopulates it. Caches belonging to a repo with a live assignment are never
evicted, so the GC can't delete a target dir out from under a running build.

Set ``COORD_SHARED_CARGO_TARGET=0`` to disable the whole feature; an operator
who exports their own ``CARGO_TARGET_DIR`` also wins (we never override it).
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

# Directory under the agent state dir (``~/.coord``) that holds one
# subdirectory per repo.
CACHE_DIRNAME = "cargo-target"

# Total cap across every per-repo cache on this machine.  A cold ``tui/``
# debug build is ~2 GiB, so 20 GiB comfortably holds several repos warm.
DEFAULT_CACHE_CAP_GB = 20.0

CAP_ENV = "COORD_CARGO_CACHE_CAP_GB"
ENABLE_ENV = "COORD_SHARED_CARGO_TARGET"
CARGO_ENV = "CARGO_TARGET_DIR"

_FALSEY = {"0", "false", "no", "off", ""}

# Repo names become a path component, so they must be a single safe segment.
# Anything else (a slash, "..", a control character) disables the cache for
# that repo rather than writing outside the cache root.
_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9._-]+$")


def _env(env: dict[str, str] | None) -> dict[str, str]:
    return dict(os.environ) if env is None else env


def enabled(env: dict[str, str] | None = None) -> bool:
    """True unless ``COORD_SHARED_CARGO_TARGET`` is set to a falsey value."""
    raw = _env(env).get(ENABLE_ENV)
    if raw is None:
        return True
    return raw.strip().lower() not in _FALSEY


def cache_root(state_dir: Path) -> Path:
    """The per-machine cache root: ``<state_dir>/cargo-target``."""
    return Path(state_dir) / CACHE_DIRNAME


def target_dir_for_repo(repo_name: str, state_dir: Path) -> Path | None:
    """The shared target dir for *repo_name*, or ``None`` if the name is not a
    safe single path component (never guessed, never sanitized into a
    collision — an unusable name simply opts that repo out)."""
    if not repo_name or not _SAFE_COMPONENT.match(repo_name):
        return None
    if repo_name in (".", ".."):
        return None
    return cache_root(state_dir) / repo_name


def cargo_env(
    repo_name: str,
    state_dir: Path,
    base_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """Environment overlay pointing cargo at the shared cache.

    Returns ``{}`` (a no-op overlay) when the feature is disabled, the repo
    name is unusable, or *base_env* already carries a ``CARGO_TARGET_DIR`` —
    an operator's explicit choice always wins.

    The directory is **not** created here: cargo does its own ``mkdir -p``, so
    a repo that never invokes cargo never leaves an empty dir behind.
    """
    env = _env(base_env)
    if not enabled(env):
        return {}
    if env.get(CARGO_ENV):
        return {}
    target = target_dir_for_repo(repo_name, state_dir)
    if target is None:
        return {}
    return {CARGO_ENV: str(target)}


def cap_bytes(env: dict[str, str] | None = None) -> int | None:
    """The GC cap in bytes, or ``None`` when GC is disabled (cap <= 0)."""
    raw = _env(env).get(CAP_ENV)
    gb = DEFAULT_CACHE_CAP_GB
    if raw is not None:
        try:
            gb = float(raw)
        except (TypeError, ValueError):
            gb = DEFAULT_CACHE_CAP_GB
    if gb <= 0:
        return None
    return int(gb * 1024 * 1024 * 1024)


def dir_size(path: Path) -> int:
    """Total size of the regular files under *path* (symlinks not followed)."""
    total = 0
    for root, dirnames, filenames in os.walk(path, onerror=lambda _e: None):
        # Never follow a symlinked subdirectory out of the cache.
        dirnames[:] = [
            d for d in dirnames if not os.path.islink(os.path.join(root, d))
        ]
        for name in filenames:
            fp = os.path.join(root, name)
            try:
                st = os.lstat(fp)
            except OSError:
                continue
            total += st.st_size
    return total


def _last_used(path: Path) -> float:
    """Best-effort "when was this cache last built into".

    cargo rewrites files throughout a build, so the newest mtime anywhere in
    the tree is a good proxy — but walking a multi-GiB tree twice is wasteful,
    so we sample the shallow entries cargo always touches (the profile dirs and
    their lock/fingerprint children) plus the directory itself.
    """
    try:
        newest = path.stat().st_mtime
    except OSError:
        return 0.0
    try:
        children = list(path.iterdir())
    except OSError:
        return newest
    for child in children:
        try:
            newest = max(newest, child.stat().st_mtime)
        except OSError:
            continue
        if child.is_dir() and not child.is_symlink():
            try:
                grandchildren = list(child.iterdir())
            except OSError:
                continue
            for entry in grandchildren:
                try:
                    newest = max(newest, entry.stat().st_mtime)
                except OSError:
                    continue
    return newest


def sweep(
    state_dir: Path,
    *,
    cap: int | None = -1,
    protect_repos: "set[str] | frozenset[str] | None" = None,
    dry_run: bool = False,
) -> dict:
    """Evict least-recently-used repo caches until the total is under the cap.

    *cap* defaults to the sentinel ``-1`` meaning "read it from the
    environment" (:func:`cap_bytes`); pass an explicit int to override, or
    ``None`` to disable eviction (the sweep then only reports sizes).

    *protect_repos* names repos with a live (pending/running) assignment on
    this machine — their caches are never evicted, so a build in flight can't
    have its target dir deleted underneath it. If the protected set alone
    exceeds the cap, the sweep evicts everything it may and reports the
    remaining overage via ``cargo_over_cap``; it never deletes a protected
    cache.

    Returns ``{"cargo_cache_bytes": B, "cargo_caches_evicted": N,
    "cargo_evicted_repos": [...], "cargo_cap_bytes": C|None,
    "cargo_over_cap": bool, "cargo_dry_run": bool}`` — ``cargo_cache_bytes``
    is the total *after* the sweep (or, for ``dry_run``, what it would be).
    """
    limit = cap_bytes() if cap == -1 else cap
    protected = set(protect_repos or ())
    result: dict = {
        "cargo_cache_bytes": 0,
        "cargo_caches_evicted": 0,
        "cargo_evicted_repos": [],
        "cargo_cap_bytes": limit,
        "cargo_over_cap": False,
        "cargo_dry_run": dry_run,
    }

    root = cache_root(state_dir)
    if not root.is_dir():
        return result

    entries: list[tuple[float, str, Path, int]] = []
    total = 0
    try:
        children = sorted(root.iterdir())
    except OSError:
        return result
    for child in children:
        # Skip symlinks outright — we never chase one out of the cache root.
        if child.is_symlink() or not child.is_dir():
            continue
        size = dir_size(child)
        total += size
        entries.append((_last_used(child), child.name, child, size))

    result["cargo_cache_bytes"] = total
    if limit is None or total <= limit:
        return result

    # Oldest-used first; ties broken by name so the sweep is deterministic.
    entries.sort(key=lambda e: (e[0], e[1]))
    evicted: list[str] = []
    for _mtime, name, path, size in entries:
        if total <= limit:
            break
        if name in protected:
            continue
        if not dry_run:
            try:
                shutil.rmtree(path)
            except OSError:
                continue
        total -= size
        evicted.append(name)

    result["cargo_cache_bytes"] = total
    result["cargo_caches_evicted"] = len(evicted)
    result["cargo_evicted_repos"] = evicted
    result["cargo_over_cap"] = total > limit
    return result
