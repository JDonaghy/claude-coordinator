"""Stale ``.git/index.lock`` across known checkouts (#2206).

**The elitebook incident, 2026-08-13.** A zero-byte ``index.lock`` sat in
the base ``claude-coordinator`` checkout for 8h45m — created by a git
process that got killed before it wrote anything, and never cleaned up.
``git pull`` is fetch + merge, and only the *working-tree* half is blocked
by the lock: the fetch kept succeeding the whole time, so ``origin/*`` refs
stayed current and every worktree cut from ``origin/main`` was unaffected.
That's exactly why the condition is silent by construction — the host
reports online and idle, ``coord status`` shows nothing, the agent keeps
accepting work, and the only thing actually wrong is that the *base
checkout's files* sat 8h45m stale while the graphify graph, tooling, and any
human reading source from it all read code that no longer matched HEAD.
``grep -rn "index.lock" coord/`` returned nothing before this file: no
surface anywhere reported it. The only signal was a human happening to run
``git pull`` by hand and reading the error.

**Both conditions matter, and either one alone is not enough.** A lock
that's merely old could still be a legitimate multi-minute operation on an
enormous repo — flagging on age alone would false-positive on that. A lock
with no way to check for a holder could still be actively held by a process
this box can't see into — flagging without ever trying to check for a
holder would be reckless. So: present, older than a generous threshold
(``index_lock_stale_minutes`` — real git index operations are sub-second),
**and** no live process holding it, is what gets reported. A lock a running
git legitimately holds is never flagged, at any age.

**This check never deletes anything.** ``coord diagnose`` already has a
history of being destructive when it looked read-only (#1693 destroyed a
worker worktree base with an unguarded operation), and the one case where
deleting a lock is wrong — a live holder — is exactly the case a false
"no holder" verdict would produce. The finding names the path and the
remedy (``rm -f <path>``, once a human has confirmed no live git process
holds it) and stops there.
"""

from __future__ import annotations

import os
from pathlib import Path

from coord.health.models import CheckResult, HealthContext, Severity
from coord.health.registry import check
from coord.health.units import human_hours

# Overridable in tests; there is no config knob for this because /proc is a
# platform fact, not an operator preference.
_PROC_ROOT = Path("/proc")

_DEFAULT_STALE_MINUTES = 10.0

# A fd symlink whose target has been unlinked while still open (exactly the
# state of a lock a git process is mid-delete on) reads as
# "/path/to/index.lock (deleted)", not the bare path.
_DELETED_SUFFIX = " (deleted)"


def _fd_target(link: str) -> str:
    if link.endswith(_DELETED_SUFFIX):
        return link[: -len(_DELETED_SUFFIX)]
    return link


def has_open_holder(lock_path: Path, *, proc_root: Path | None = None) -> bool | None:
    """Whether a live process holds *lock_path* open right now.

    Answers what ``fuser <path>`` answers, without assuming ``fuser`` is
    installed: walk ``/proc/*/fd`` looking for a descriptor whose target is
    this exact path.

    Returns ``True``/``False`` when the scan actually ran, ``None`` when
    ``/proc`` itself could not be listed at all — no Linux ``/proc``, or a
    sandbox that hides it entirely. The caller treats ``None`` as "fell back
    to age-only" and says so in the finding at reduced confidence, rather
    than either silently assuming a holder (never flags) or silently
    assuming none (flags something a live process might hold).

    A single process whose own ``fd`` directory can't be read (another
    user's, or one that exited between the pid listing and the read) is
    skipped, not treated as a scan failure — every relevant process on this
    fleet runs as the same user, and letting one unreadable pid abort the
    whole scan would hide a holder this box *can* see among the rest.
    """
    root = proc_root or _PROC_ROOT
    try:
        pids = [entry.name for entry in root.iterdir() if entry.name.isdigit()]
    except OSError:
        return None

    target = str(lock_path)
    for pid in pids:
        try:
            fds = list((root / pid / "fd").iterdir())
        except OSError:
            continue
        for fd in fds:
            try:
                link = os.readlink(fd)
            except OSError:
                continue
            if _fd_target(link) == target:
                return True
    return False


@check(
    id="index_lock",
    scope="machine",
    title="index lock",
    order=31,
    description="Stale .git/index.lock files blocking a checkout's working tree.",
)
def probe_index_lock(ctx: HealthContext) -> CheckResult | None:
    """One machine-scope result: every known checkout's index.lock, if stale."""
    if not ctx.checkouts:
        return None

    th = ctx.thresholds
    stale_minutes = float(
        getattr(th, "index_lock_stale_minutes", _DEFAULT_STALE_MINUTES)
    )
    stale_seconds = stale_minutes * 60.0

    stale: list[tuple[str, str, float, str]] = []  # name, path, age_secs, confidence
    held: list[str] = []
    reduced_confidence = False

    for checkout in ctx.checkouts:
        lock_path = checkout.path / ".git" / "index.lock"
        try:
            mtime = lock_path.stat().st_mtime
        except OSError:
            continue  # no lock — the overwhelmingly common state

        age = ctx.now - mtime
        if age < stale_seconds:
            continue  # young enough to be an in-flight git operation

        holder = has_open_holder(lock_path)
        if holder is True:
            held.append(checkout.name)
            continue

        if holder is None:
            reduced_confidence = True
            confidence = "reduced (no /proc access to confirm no holder)"
        else:
            confidence = "high"
        stale.append((checkout.name, str(lock_path), age, confidence))

    if not stale:
        headroom = "no stale locks"
        if held:
            headroom += f" ({', '.join(held)} in use)"
        return CheckResult(
            check_id="index_lock",
            scope="machine",
            severity=Severity.OK,
            headroom=headroom,
            threshold=f"crit past {stale_minutes:.0f}m with no live holder",
            values={
                "checked": len(ctx.checkouts),
                "stale": [],
                "held": held,
                "stale_minutes_threshold": stale_minutes,
            },
        )

    stale.sort(key=lambda row: row[2], reverse=True)
    named = "; ".join(
        f"{name}: {path} ({human_hours(age)} old)" for name, path, age, _ in stale
    )
    plural = "s" if len(stale) != 1 else ""
    headroom = f"{len(stale)} stale lock{plural}: {named}"
    if reduced_confidence:
        headroom += " [holder check unavailable on this box — age-only]"

    detail = "; ".join(
        f"rm -f {path}  # once no live git process is confirmed holding it"
        for _, path, _age, _conf in stale
    )

    return CheckResult(
        check_id="index_lock",
        scope="machine",
        severity=Severity.CRIT,
        headroom=headroom,
        threshold=f"crit past {stale_minutes:.0f}m with no live holder",
        detail=detail,
        values={
            "checked": len(ctx.checkouts),
            "stale": [
                {
                    "name": name,
                    "path": path,
                    "age_hours": round(age / 3600.0, 2),
                    "confidence": confidence,
                }
                for name, path, age, confidence in stale
            ],
            "held": held,
            "stale_minutes_threshold": stale_minutes,
        },
    )
