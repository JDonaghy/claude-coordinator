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
reclaims space, oldest-used first, until the total is back under the cap
(default 20 GiB, override with ``COORD_CARGO_CACHE_CAP_GB``).

#2137: reclamation is **graduated**, cheapest-to-recreate first, because
whole-directory eviction alone cannot bound a machine whose cache root holds a
single repo. On 2026-08-11 ``cargo-target/quadraui`` reached 38G against the
20 GiB cap and ``/home`` hit 0 bytes free: every sweep had exactly two moves —
evict the entire 38G tree (throwing away every warm artifact), or, because a
live worker protected it, evict nothing at all. Protection is keyed on having
an assignment, so *the busier a repo is, the less likely its cache is ever
reclaimed*: the mechanism selected for failure on the hottest repo. The tiers
are now

1. ``incremental/`` — 30-50% of a debug target dir and purely a rebuild-speed
   cache; deleting it never changes what gets built.
2. stale profile dirs (``debug/``, ``release/``, ``<triple>/debug/``) untouched
   for ``COORD_CARGO_STALE_DAYS`` (default 7). A profile dir is the coarsest
   *self-consistent* unit: dropping one makes that profile cold, where deleting
   individual files inside one can leave cargo's fingerprints describing
   artifacts that are no longer there.
3. only then today's whole-directory eviction.

A repo with a live assignment is still never *evicted*, but it may be *pruned*
(tiers 1-2) when nothing is actually compiling against it — protection exists
to stop deleting a target dir out from under ``rustc``, which is much narrower
than "this repo has an assignment". :func:`build_active` is the gate: cargo's
own ``.cargo-lock`` build lock, probed non-blockingly, plus a ``/proc`` scan
for a compiler process pointed at the directory. When the sweep still cannot
get under the cap it says so — ``cargo_over_cap`` with a reason, a WARN in the
agent log, and a status file (:func:`write_gc_status`) the ``cargo_targets``
health check renders — rather than returning quietly the way it did while 38G
accumulated.

Set ``COORD_SHARED_CARGO_TARGET=0`` to disable the whole feature; an operator
who exports their own ``CARGO_TARGET_DIR`` also wins (we never override it).
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import time
from pathlib import Path

try:  # pragma: no cover - POSIX everywhere the agent runs
    import fcntl
except ImportError:  # pragma: no cover - Windows client
    fcntl = None  # type: ignore[assignment]

_log = logging.getLogger(__name__)

# Directory under the agent state dir (``~/.coord``) that holds one
# subdirectory per repo.
CACHE_DIRNAME = "cargo-target"

# Total cap across every per-repo cache on this machine.  A cold ``tui/``
# debug build is ~2 GiB, so 20 GiB comfortably holds several repos warm.
DEFAULT_CACHE_CAP_GB = 20.0

# #2137: an absolute free-space floor on the cache's own filesystem. The cap
# governs the shared cache only; what actually bit was "0 bytes free", which a
# cap can never see because the per-checkout ``target/`` dirs (17G + 13G + 11G
# on the machine that filled) are outside it. When free space is under this
# floor the sweep reclaims the shortfall from the cache even if the cache is
# under its cap — the cache is the cheapest thing on the disk to give up.
DEFAULT_FREE_FLOOR_GB = 10.0

# A profile dir untouched for this long is "stale": its artifacts predate the
# current toolchain/dependency set often enough that keeping them is not worth
# the bytes once we're over the cap.
DEFAULT_STALE_DAYS = 7.0

CAP_ENV = "COORD_CARGO_CACHE_CAP_GB"
FREE_FLOOR_ENV = "COORD_CARGO_FREE_FLOOR_GB"
STALE_DAYS_ENV = "COORD_CARGO_STALE_DAYS"
ENABLE_ENV = "COORD_SHARED_CARGO_TARGET"
CARGO_ENV = "CARGO_TARGET_DIR"

# Where :func:`write_gc_status` parks the last sweep's verdict, relative to the
# agent state dir.  Read by the ``cargo_targets`` health check (#2137 item 3)
# so "the GC ran and could not get under cap" reaches an operator surface
# instead of dead-ending in a dict nobody looks at.
GC_STATUS_FILENAME = "cargo-gc-status.json"

# Names cargo uses for the per-profile incremental cache and its build lock.
INCREMENTAL_DIRNAME = "incremental"
BUILD_LOCK_NAME = ".cargo-lock"

# How deep below a repo cache root we look for profile dirs / build locks.
# ``<repo>/debug`` is depth 1 and ``<repo>/<triple>/debug`` is depth 2; nothing
# cargo creates puts one deeper than that.
_MAX_PROFILE_DEPTH = 2

# `comm` values worth stat-ing a `/proc` entry for.  Truncated to 15 chars by
# the kernel, which none of these reach.
_COMPILER_COMMS = frozenset(
    {"cargo", "rustc", "rustdoc", "cc", "cc1", "gcc", "clang", "ld", "lld", "collect2"}
)

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


def _float_env(env: dict[str, str] | None, name: str, default: float) -> float:
    raw = _env(env).get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def free_floor_bytes(env: dict[str, str] | None = None) -> int | None:
    """The absolute free-space floor in bytes, or ``None`` when disabled (#2137).

    ``COORD_CARGO_FREE_FLOOR_GB=0`` turns the floor off; the cap alone then
    governs, which is the pre-#2137 behaviour.
    """
    gb = _float_env(env, FREE_FLOOR_ENV, DEFAULT_FREE_FLOOR_GB)
    if gb <= 0:
        return None
    return int(gb * 1024 * 1024 * 1024)


def stale_secs(env: dict[str, str] | None = None) -> float | None:
    """Age at which a profile dir counts as stale, or ``None`` when disabled."""
    days = _float_env(env, STALE_DAYS_ENV, DEFAULT_STALE_DAYS)
    if days <= 0:
        return None
    return days * 86400.0


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


# ── #2137: intra-repo pruning ───────────────────────────────────────────────


def _real_subdirs(path: Path) -> list[Path]:
    """Immediate subdirectories of *path*, symlinks excluded, sorted."""
    try:
        children = sorted(path.iterdir())
    except OSError:
        return []
    out = []
    for child in children:
        try:
            if child.is_symlink() or not child.is_dir():
                continue
        except OSError:
            continue
        out.append(child)
    return out


def _shallow_dirs(repo_dir: Path) -> list[Path]:
    """Directories one and two levels below a repo cache, ``incremental``
    excluded, parents always before their children.

    ``<repo>/debug`` and ``<repo>/<triple>/debug`` are where cargo puts a
    profile; two levels reaches both.  Depth-bounded and symlink-free by
    construction (:func:`_real_subdirs`), so this can never walk out of the
    cache root — the same guard ``sweep`` has applied at the top level since
    #1402.
    """
    out: list[Path] = []
    frontier = [(repo_dir, 0)]
    while frontier:
        current, depth = frontier.pop(0)
        if depth >= _MAX_PROFILE_DEPTH:
            continue
        for child in _real_subdirs(current):
            if child.name == INCREMENTAL_DIRNAME:
                continue
            out.append(child)
            frontier.append((child, depth + 1))
    return out


def _looks_like_profile(path: Path) -> bool:
    """True for a cargo *profile* dir (``debug/``, ``release/``, ...).

    Load-bearing for tier 2: the unit we delete has to be one cargo can
    rebuild from nothing.  ``.fingerprint/`` (and its siblings ``deps/`` and
    the build lock) only ever exist at profile level, so this keeps the tier
    off ``debug/deps`` — removing *that* while leaving ``debug/.fingerprint``
    behind is precisely the piecemeal deletion that turns a cold rebuild into
    a failed one.
    """
    try:
        return (
            (path / ".fingerprint").is_dir()
            or (path / "deps").is_dir()
            or (path / BUILD_LOCK_NAME).is_file()
        )
    except OSError:  # pragma: no cover - defensive
        return False


def profile_dirs(repo_dir: Path) -> list[Path]:
    """Every cargo profile dir under a repo cache, parents before children."""
    return [d for d in _shallow_dirs(repo_dir) if _looks_like_profile(d)]


def incremental_dirs(repo_dir: Path) -> list[Path]:
    """Every ``incremental/`` dir under a repo cache (tier 1).

    Purely a rebuild-speed cache: cargo re-creates it and the artifacts it
    produces without it are identical, so this is the cheapest thing in the
    tree to give back.
    """
    out: list[Path] = []
    for parent in [repo_dir, *_shallow_dirs(repo_dir)]:
        candidate = parent / INCREMENTAL_DIRNAME
        try:
            if candidate.is_dir() and not candidate.is_symlink():
                out.append(candidate)
        except OSError:
            continue
    return out


def stale_profile_dirs(repo_dir: Path, older_than_secs: float, now: float) -> list[Path]:
    """Profile dirs (tier 2) whose newest activity is older than *older_than_secs*.

    Whole profile dirs, never individual files: cargo's fingerprints describe
    artifacts it expects to find, so removing files piecemeal can leave a build
    that fails rather than one that rebuilds.  Dropping a whole profile just
    makes that profile cold.
    """
    if older_than_secs <= 0:
        return []
    out: list[Path] = []
    for profile in profile_dirs(repo_dir):
        # A parent already selected covers its children; don't double-count.
        if any(parent in profile.parents for parent in out):
            continue
        if (now - _last_used(profile)) >= older_than_secs:
            out.append(profile)
    return out


def _build_lock_held(repo_dir: Path) -> bool:
    """True when cargo's own build lock on this target dir is held.

    cargo takes an exclusive ``flock`` on ``<target>/<profile>/.cargo-lock``
    for the duration of a build.  Probing it non-blockingly is the most
    reliable "is something compiling in here right now" signal available, and
    it costs one ``open``/``flock``/``close`` per profile dir.  We release
    immediately: ``flock`` is per open-file-description, so taking and dropping
    ours never disturbs a cargo that is waiting for it.
    """
    if fcntl is None:  # pragma: no cover - Windows client never runs the GC
        return False
    for parent in [repo_dir, *_shallow_dirs(repo_dir)]:
        lock = parent / BUILD_LOCK_NAME
        try:
            if lock.is_symlink() or not lock.is_file():
                continue
            fd = os.open(str(lock), os.O_RDONLY)
        except OSError:
            continue
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            # Held by someone else — a build is in flight.
            return True
        else:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:  # pragma: no cover - defensive
                pass
        finally:
            os.close(fd)
    return False


def _compiler_process_against(repo_dir: Path) -> bool:
    """True when a cargo/rustc-ish process on this machine points at *repo_dir*.

    Second line of defence behind the build lock, for the window where cargo
    has handed off to a long ``rustc`` invocation, and for anything driving the
    target dir without cargo's lock.  Linux-only (``/proc``); absent elsewhere
    it simply contributes nothing.
    """
    proc = Path("/proc")
    try:
        if not proc.is_dir():
            return False
        entries = list(proc.iterdir())
    except OSError:  # pragma: no cover - defensive
        return False

    target = str(repo_dir)
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            comm = (entry / "comm").read_text(errors="replace").strip()
        except OSError:
            continue
        if comm not in _COMPILER_COMMS:
            continue
        try:
            environ = (entry / "environ").read_bytes().decode("utf-8", "replace")
        except OSError:
            environ = ""
        for chunk in environ.split("\0"):
            name, sep, value = chunk.partition("=")
            if sep and name == CARGO_ENV and _is_within(value, target):
                return True
        try:
            cwd = os.readlink(str(entry / "cwd"))
        except OSError:
            cwd = ""
        if cwd and _is_within(cwd, target):
            return True
        try:
            cmdline = (entry / "cmdline").read_bytes().decode("utf-8", "replace")
        except OSError:
            cmdline = ""
        if target and target in cmdline:
            return True
    return False


def _is_within(candidate: str, root: str) -> bool:
    """Path containment on strings, without touching the filesystem."""
    if not candidate or not root:
        return False
    return candidate == root or candidate.startswith(root.rstrip("/") + "/")


def build_active(repo_dir: Path) -> bool:
    """True when something looks like a live build against *repo_dir* (#2137).

    The gate that makes pruning a *protected* repo safe.  Deliberately
    fail-safe: any error anywhere in the probes is reported as "busy", because
    refusing to prune costs disk while pruning mid-``rustc`` costs a corrupted
    build.
    """
    try:
        if _build_lock_held(repo_dir):
            return True
        return _compiler_process_against(repo_dir)
    except Exception:  # noqa: BLE001 — an unreadable probe means "assume busy"
        _log.warning("cargo build-activity probe failed for %s", repo_dir, exc_info=True)
        return True


# ── #2137: the GC's verdict, for operator surfaces ──────────────────────────


def gc_status_path(state_dir: Path) -> Path:
    """Where the last sweep's verdict is parked."""
    return Path(state_dir) / GC_STATUS_FILENAME


def write_gc_status(state_dir: Path, result: dict, *, now: float | None = None) -> None:
    """Persist a sweep *result* so a later reader can see it (#2137 item 3).

    ``cargo_over_cap`` used to be set by :func:`sweep` and read by nothing at
    all — the single most actionable bit the GC produces, dead-ended, which is
    why 38G accumulated in silence.  The sweep runs inside the agent; the
    health check that renders it runs on its own timer in the same process, so
    a small JSON file is the join.  Best-effort: never raises.
    """
    payload = {"schema": 1, "checked_at": time.time() if now is None else now, **result}
    path = gc_status_path(state_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload))
        tmp.replace(path)
    except (OSError, TypeError, ValueError):  # pragma: no cover - defensive
        _log.warning("could not write cargo GC status to %s", path, exc_info=True)


def read_gc_status(state_dir: Path) -> dict | None:
    """The last sweep's verdict, or ``None`` when absent/unreadable."""
    try:
        raw = gc_status_path(state_dir).read_text()
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def sweep(
    state_dir: Path,
    *,
    cap: int | None = -1,
    protect_repos: "set[str] | frozenset[str] | None" = None,
    dry_run: bool = False,
    free_floor: int | None = None,
    stale_after_secs: float | None = -1.0,
    now: float | None = None,
) -> dict:
    """Reclaim cache space until the total is under the cap.

    Three graduated tiers, cheapest-to-recreate first (#2137): every repo's
    ``incremental/`` dirs, then stale profile dirs, then — only for repos with
    no live assignment — today's whole-directory eviction.  Each tier runs
    least-recently-used first and stops the moment the total fits, so a machine
    whose cache root holds a *single* oversized repo can now be brought under
    the cap without discarding every warm artifact it has.

    *cap* defaults to the sentinel ``-1`` meaning "read it from the
    environment" (:func:`cap_bytes`); pass an explicit int to override, or
    ``None`` to disable the GC entirely (the sweep then only reports sizes).

    *protect_repos* names repos with a live (pending/running) assignment on
    this machine — their caches are never *evicted*.  They are still *pruned*
    (tiers 1-2) when :func:`build_active` says nothing is compiling against
    them, because protection exists to stop deleting a target dir out from
    under ``rustc``, not to make a busy repo permanently unreclaimable.  When
    the sweep cannot get under the cap it sets ``cargo_over_cap`` with a
    ``cargo_over_cap_reason`` and logs a warning rather than returning quietly.

    *free_floor* (bytes, ``None`` = off) is an absolute free-space floor on the
    cache's filesystem: when free space is below it, the sweep reclaims the
    shortfall from the cache even if the cache is under its cap.  The cap
    cannot see the per-checkout ``target/`` dirs that filled ``/home``; this
    can.  Callers opt in — :meth:`coord.agent.AgentServer._gc_cargo_cache`
    passes :func:`free_floor_bytes`.

    *stale_after_secs* defaults to the sentinel ``-1`` meaning "read it from
    the environment" (:func:`stale_secs`); ``None`` disables tier 2.

    Returns ``{"cargo_cache_bytes": B, "cargo_caches_evicted": N,
    "cargo_evicted_repos": [...], "cargo_cap_bytes": C|None,
    "cargo_over_cap": bool, "cargo_dry_run": bool}`` plus, since #2137,
    ``cargo_pruned_bytes``, ``cargo_pruned_repos``, ``cargo_pruned`` (one
    ``{repo, tier, path, bytes}`` per pruned subtree), ``cargo_prune_blocked``
    (repos left untouched because a build is live), ``cargo_over_cap_reason``,
    ``cargo_limit_bytes`` (what the sweep actually aimed at — the cap, or
    tighter when the floor bites), ``cargo_disk_free_bytes``,
    ``cargo_disk_floor_bytes`` and ``cargo_disk_low``.  ``cargo_cache_bytes`` is the total *after* the sweep
    (or, for ``dry_run``, what it would be).
    """
    limit = cap_bytes() if cap == -1 else cap
    stale_cutoff = stale_secs() if stale_after_secs == -1.0 else stale_after_secs
    clock = time.time() if now is None else now
    protected = set(protect_repos or ())
    result: dict = {
        "cargo_cache_bytes": 0,
        "cargo_caches_evicted": 0,
        "cargo_evicted_repos": [],
        "cargo_cap_bytes": limit,
        "cargo_over_cap": False,
        "cargo_dry_run": dry_run,
        # #2137.  `cargo_limit_bytes` is what the sweep actually aimed at: the
        # cap, or something tighter when the free-disk floor bites.
        # `cargo_cap_bytes` keeps meaning "the configured cap" so a reader
        # cannot mistake one for the other.
        "cargo_limit_bytes": limit,
        "cargo_pruned_bytes": 0,
        "cargo_pruned_repos": [],
        "cargo_pruned": [],
        "cargo_prune_blocked": [],
        "cargo_over_cap_reason": None,
        "cargo_disk_free_bytes": None,
        "cargo_disk_floor_bytes": free_floor,
        "cargo_disk_low": False,
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

    # #2137 item 4: the failure that actually bit was "0 bytes free", not
    # "cache over cap".  A floor on absolute free space tightens the limit so
    # the cache gives back the shortfall — it is the cheapest thing on the
    # filesystem to lose, and the only thing here we have authority over.
    if free_floor:
        try:
            usage = shutil.disk_usage(str(root))
        except OSError:  # pragma: no cover - defensive
            usage = None
        if usage is not None:
            result["cargo_disk_free_bytes"] = usage.free
            shortfall = free_floor - usage.free
            if shortfall > 0:
                result["cargo_disk_low"] = True
                disk_limit = max(0, total - shortfall)
                limit = disk_limit if limit is None else min(limit, disk_limit)
                result["cargo_limit_bytes"] = limit

    if limit is None or total <= limit:
        return result

    # Oldest-used first; ties broken by name so the sweep is deterministic.
    entries.sort(key=lambda e: (e[0], e[1]))
    remaining = {name: size for _m, name, _p, size in entries}
    pruned: list[dict] = []
    blocked: list[str] = []
    # A repo's build-activity verdict is probed at most once per sweep: the
    # /proc scan is not free and the answer cannot usefully change mid-sweep.
    idle: dict[str, bool] = {}

    def _may_touch(name: str, path: Path) -> bool:
        """Never delete anything out of a tree something is compiling into.

        This is what makes pruning a *protected* repo safe, and it applies to
        unprotected ones too: an operator's own ``cargo build`` against the
        shared cache holds no coord assignment, and refusing to prune costs
        disk where pruning mid-``rustc`` costs their build.  Probed only once
        the cache is already over its limit, so an under-cap sweep stays as
        cheap as it was.
        """
        if name not in idle:
            active = build_active(path)
            idle[name] = not active
            if active:
                blocked.append(name)
        return idle[name]

    def _reclaim(name: str, subtree: Path, tier: str) -> int:
        size = dir_size(subtree)
        if size <= 0:
            # Nothing to gain; leave it rather than report a 0-byte "prune".
            return 0
        if not dry_run:
            try:
                shutil.rmtree(subtree)
            except OSError:
                return 0
        remaining[name] = max(0, remaining[name] - size)
        pruned.append(
            {"repo": name, "tier": tier, "path": str(subtree), "bytes": size}
        )
        return size

    # Tier 1 then tier 2, each across every repo before the next escalates —
    # "cheapest to recreate first" is a property of the whole cache, not of one
    # repo, so an incremental dir on a hot repo goes before a stale profile dir
    # on a cold one.
    for tier in ("incremental", "stale"):
        for _mtime, name, path, _size in entries:
            if total <= limit:
                break
            if not _may_touch(name, path):
                continue
            if tier == "incremental":
                subtrees = incremental_dirs(path)
            elif stale_cutoff is None:
                subtrees = []
            else:
                subtrees = stale_profile_dirs(path, stale_cutoff, clock)
            for subtree in subtrees:
                if total <= limit:
                    break
                total -= _reclaim(name, subtree, tier)
        if total <= limit:
            break

    # Tier 3: whole-directory eviction, as since #1402 — the last resort, and
    # never against a repo with a live assignment.  The build-activity gate
    # applies here too: refusing to prune a busy tree and then rmtree-ing the
    # whole thing two lines later would be the worse of both behaviours.
    evicted: list[str] = []
    for _mtime, name, path, _size in entries:
        if total <= limit:
            break
        if name in protected:
            continue
        if not _may_touch(name, path):
            continue
        if not dry_run:
            try:
                shutil.rmtree(path)
            except OSError:
                continue
        total -= remaining[name]
        remaining[name] = 0
        evicted.append(name)

    pruned_bytes = sum(int(p["bytes"]) for p in pruned)
    result["cargo_cache_bytes"] = total
    result["cargo_caches_evicted"] = len(evicted)
    result["cargo_evicted_repos"] = evicted
    result["cargo_pruned"] = pruned
    result["cargo_pruned_bytes"] = pruned_bytes
    result["cargo_pruned_repos"] = sorted({str(p["repo"]) for p in pruned})
    result["cargo_prune_blocked"] = blocked
    result["cargo_over_cap"] = total > limit

    if result["cargo_over_cap"]:
        reason = _over_cap_reason(total, limit, protected, blocked, remaining)
        result["cargo_over_cap_reason"] = reason
        # Escalate rather than return quietly (#2137): this is the exact state
        # in which 38G accumulated unremarked.
        _log.warning("cargo cache GC could not get under cap: %s", reason)
    return result


def _over_cap_reason(
    total: int,
    limit: int,
    protected: "set[str]",
    blocked: "list[str]",
    remaining: dict[str, int],
) -> str:
    """One line an operator can act on: how far over, and what stopped us."""
    over = _human(total - limit)
    if blocked:
        why = f"live build in {', '.join(sorted(blocked))}"
    elif protected:
        held = sorted(n for n in protected if remaining.get(n))
        why = f"protected by a live assignment: {', '.join(held)}" if held else (
            "protected repos hold the remainder"
        )
    else:
        why = "nothing left to reclaim"
    return f"{_human(total)} of {_human(limit)} cap ({over} over) — {why}"


def _human(nbytes: int) -> str:
    value = float(nbytes)
    for unit in ("B", "K", "M", "G"):
        if abs(value) < 1024.0 or unit == "G":
            return f"{value:.1f}{unit}" if unit != "B" else f"{int(value)}B"
        value /= 1024.0
    return f"{value:.1f}G"  # pragma: no cover - unreachable
