"""Fleet ``coordinator.yml`` provenance: is the live config still the one that
was reviewed? (#1779)

``coordinator.yml`` used to be a plain file at ``~/.coord/coordinator.yml`` on
the daemon host, hand-copied from a tracked copy elsewhere — real, and
undetectable, content drift. That copy has since been replaced by a
**symlink** into the ``JDonaghy/coord-settings`` checkout
(``~/src/coord-settings/coord/coordinator.yml`` by default), so the live file
*is* the tracked file and content drift is structurally impossible. That
closes one hole and opens three narrower ones, none visible from a running
fleet:

1. **The symlink gets replaced by a regular file.** ``coord init`` offers to
   overwrite ``coordinator.yml`` (``coord/commands/setup.py``), and any
   ``scp``, ``cp``, or editor that writes-and-renames breaks the link. The
   fleet is then silently back to running an untracked file — this is the
   highest-value check here, and the loudest finding.
2. **The checkout is dirty.** A direct edit to the live path writes *through*
   the symlink into the checkout's working tree — recoverable, but the
   running config now has uncommitted changes nobody has reviewed.
3. **The checkout is behind (or ahead of) ``origin``.** Someone pushed a
   config change that was never pulled onto the daemon host (or committed
   locally but never pushed), so the reviewed intent and the running fleet
   disagree.

A fourth state is *not* a problem: **no checkout at all.** The coord-settings
checkout is deliberately absent from every machine except the daemon host and
the operator's box (agents, thin clients, and every ephemeral Azure worker
have no reason to carry it, and — see #1779's cross-repo constraint — a
dispatched worker must never be able to edit the file governing its own
concurrency limits and review gates). :func:`config_provenance` reports that
as a neutral skip, never a warning.

Mirrors :mod:`coord.graph_health`'s shape (``coord diagnose --graph``'s "is
the artifact current?" check): a single read-only, best-effort probe function
plus a renderer, wired into ``coord diagnose`` as another local-machine sweep
alongside ``--graph``/``--orphan-worktrees``. No network access is required —
sync-vs-``origin`` is judged against the existing remote-tracking ref, never a
fresh ``git fetch``.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# The tracked config's path inside the coord-settings checkout.
TRACKED_CONFIG_REL = Path("coord") / "coordinator.yml"


def default_settings_dir() -> Path:
    """``$COORD_SETTINGS_DIR``, defaulting to ``~/src/coord-settings``.

    Computed fresh on every call (not a module-level constant) so a test can
    override it via ``monkeypatch.setenv`` without also having to fight a
    value baked in at import time.
    """
    env = os.environ.get("COORD_SETTINGS_DIR")
    return Path(env).expanduser() if env else Path.home() / "src" / "coord-settings"


def default_live_config_path() -> Path:
    """Where the daemon's live ``coordinator.yml`` lives.

    Mirrors the first two steps of :func:`coord.config.resolve_config_path`
    (``$COORD_CONFIG``, then ``~/.coord/coordinator.yml``), computed fresh at
    call time rather than reusing ``coord.config.USER_CONFIG_PATH`` — that is
    a module-level constant fixed at import time against whatever ``$HOME``
    was then, so it would not follow a test's environment override.

    Deliberately excludes ``resolve_config_path``'s third step
    (``./coordinator.yml`` in cwd): that is a repo-checkout dev convenience
    that the running daemon never uses, and folding it in here would let a
    stray dev ``coordinator.yml`` in whatever directory this check happens to
    run from masquerade as the fleet's live config.
    """
    env = os.environ.get("COORD_CONFIG")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".coord" / "coordinator.yml"


@dataclass
class ConfigProvenance:
    """Provenance of one machine's live ``coordinator.yml``."""

    live_path: Path
    checkout_dir: Path
    # False on every machine with no coord-settings checkout — the expected,
    # neutral state everywhere except the daemon host / operator box.
    checkout_present: bool = False
    live_exists: bool = False
    is_symlink: bool = False
    resolved_target: Path | None = None
    # True only when the symlink resolves to THIS checkout's tracked
    # coord/coordinator.yml — not just "somewhere inside the checkout".
    in_checkout: bool = False
    dirty: bool = False
    dirty_files: list[str] = field(default_factory=list)
    ahead: int = 0
    behind: int = 0
    upstream: str | None = None
    sync_unknown_reason: str | None = None

    @property
    def skip(self) -> bool:
        """No coord-settings checkout here — nothing to check, not a problem."""
        return not self.checkout_present

    @property
    def regression(self) -> bool:
        """The highest-value finding: the live config is no longer a symlink
        into the reviewed checkout (#1779's central failure mode)."""
        return self.checkout_present and not self.in_checkout

    @property
    def in_sync(self) -> bool:
        return self.sync_unknown_reason is None and self.ahead == 0 and self.behind == 0

    @property
    def healthy(self) -> bool:
        return (
            self.checkout_present
            and self.in_checkout
            and not self.dirty
            and self.sync_unknown_reason is None
            and self.behind == 0
        )


def _git(checkout_dir: Path, *args: str) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=str(checkout_dir),
            capture_output=True,
            text=True,
            timeout=10.0,
        )
    except (subprocess.SubprocessError, OSError):
        return None


def config_provenance(
    *, live_path: Path | None = None, checkout_dir: Path | None = None
) -> ConfigProvenance:
    """Provenance of *live_path* (default: this machine's live
    ``coordinator.yml``) against *checkout_dir* (default: the coord-settings
    checkout).

    Read-only and best-effort: git failures, a missing checkout, or a missing
    live file all return a populated :class:`ConfigProvenance` rather than
    raising. Never touches the network — sync-vs-origin is read from the
    existing remote-tracking ref, never a fresh ``git fetch``.
    """
    live_path = live_path or default_live_config_path()
    checkout_dir = checkout_dir or default_settings_dir()
    prov = ConfigProvenance(live_path=live_path, checkout_dir=checkout_dir)

    # #1779: the checkout's mere presence is the whole "is this machine in
    # scope" signal — absent on every machine except the daemon host and the
    # operator box. Anything past this point only runs where it's present.
    if not (checkout_dir / ".git").exists():
        return prov
    prov.checkout_present = True

    prov.is_symlink = live_path.is_symlink()
    prov.live_exists = live_path.exists()  # follows the symlink; False if dangling/absent

    tracked_path = checkout_dir / TRACKED_CONFIG_REL
    if prov.is_symlink:
        try:
            prov.resolved_target = live_path.resolve()
        except OSError:
            prov.resolved_target = None
        if prov.resolved_target is not None:
            prov.in_checkout = prov.resolved_target == tracked_path.resolve()

    if not prov.in_checkout:
        # Regression case (not a symlink at all, or a symlink pointing
        # somewhere else) — nothing downstream (dirty/sync) is meaningful
        # against a file that isn't even the tracked one.
        return prov

    # ── Checkout clean? ──────────────────────────────────────────────────
    status = _git(checkout_dir, "status", "--porcelain", "--", str(TRACKED_CONFIG_REL))
    if status is not None and status.returncode == 0:
        out = status.stdout.strip()
        if out:
            prov.dirty = True
            prov.dirty_files = [line for line in out.splitlines() if line.strip()]

    # ── In sync with origin? (no fetch — existing remote-tracking ref only) ─
    upstream = _git(checkout_dir, "rev-parse", "--abbrev-ref", "@{upstream}")
    if upstream is None or upstream.returncode != 0:
        prov.sync_unknown_reason = (
            "no upstream tracking ref configured for the coord-settings checkout"
        )
        return prov
    prov.upstream = upstream.stdout.strip()

    counts = _git(
        checkout_dir, "rev-list", "--left-right", "--count", f"{prov.upstream}...HEAD"
    )
    if counts is None or counts.returncode != 0:
        prov.sync_unknown_reason = f"could not compare HEAD to {prov.upstream}"
        return prov
    parts = counts.stdout.split()
    if len(parts) != 2:
        prov.sync_unknown_reason = (
            f"unexpected `git rev-list` output comparing HEAD to {prov.upstream}"
        )
        return prov
    try:
        prov.behind, prov.ahead = int(parts[0]), int(parts[1])
    except ValueError:
        prov.sync_unknown_reason = (
            f"unexpected `git rev-list` output comparing HEAD to {prov.upstream}"
        )
    return prov


def format_provenance_lines(prov: ConfigProvenance) -> list[str]:
    """Human-readable report lines for *prov* (used by ``coord diagnose
    --config-provenance``). Each of the three failure modes gets its own,
    distinctly-worded line — never collapsed into one generic "drift" line."""
    lines: list[str] = []

    if not prov.checkout_present:
        lines.append(
            f"· no coord-settings checkout at {prov.checkout_dir} — skipping "
            "config provenance (expected on every machine except the daemon "
            "host and the operator box)"
        )
        return lines

    fix = f"ln -sf {prov.checkout_dir / TRACKED_CONFIG_REL} {prov.live_path}"
    if not prov.is_symlink:
        exists_note = "exists as a REGULAR FILE" if prov.live_exists else "does not exist"
        lines.append(
            f"✗ REGRESSION: {prov.live_path} is NOT a symlink into "
            f"{prov.checkout_dir} — it {exists_note}. The fleet is running "
            f"an untracked config again. Fix: {fix}"
        )
        return lines
    if not prov.in_checkout:
        lines.append(
            f"✗ REGRESSION: {prov.live_path} is a symlink, but resolves to "
            f"{prov.resolved_target} — not {prov.checkout_dir / TRACKED_CONFIG_REL}. "
            f"Fix: {fix}"
        )
        return lines

    lines.append(f"✓ {prov.live_path} → {prov.resolved_target} (symlinked into the checkout)")

    if prov.dirty:
        lines.append(
            f"⚠ uncommitted changes to {TRACKED_CONFIG_REL} in {prov.checkout_dir} "
            f"({len(prov.dirty_files)} line(s) of `git status --porcelain`) — the "
            "running config has changes nobody has reviewed"
        )
    else:
        lines.append(f"✓ {prov.checkout_dir}: checkout is clean (no uncommitted config changes)")

    if prov.sync_unknown_reason:
        lines.append(f"? sync vs origin unknown — {prov.sync_unknown_reason}")
    elif prov.behind and prov.ahead:
        lines.append(
            f"⚠ diverged from {prov.upstream}: {prov.behind} commit(s) behind, "
            f"{prov.ahead} ahead — reconcile on the daemon host"
        )
    elif prov.behind:
        lines.append(
            f"⚠ {prov.behind} commit(s) behind {prov.upstream} — reviewed config "
            f"not yet deployed. Fix: git -C {prov.checkout_dir} pull"
        )
    elif prov.ahead:
        lines.append(
            f"⚠ {prov.ahead} commit(s) ahead of {prov.upstream} — local commit(s) "
            "not yet pushed"
        )
    else:
        lines.append(f"✓ in sync with {prov.upstream}")

    return lines


def summary_line(prov: ConfigProvenance) -> str:
    """The machine-readable trailer ``coord diagnose --config-provenance``
    prints, mirroring ``GRAPH_HEALTH:``/``DIAGNOSE_RESULT:``."""
    if not prov.checkout_present:
        return "CONFIG_PROVENANCE: checkout=absent skip=true"
    return (
        "CONFIG_PROVENANCE: checkout=present "
        f"symlinked={'true' if prov.in_checkout else 'false'} "
        f"dirty={'true' if prov.dirty else 'false'} "
        f"behind={prov.behind} ahead={prov.ahead}"
    )
