"""Blue/green, version-pinned venv swap for `coord agent update` (#1241).

`POST /update` used to run `pip install --upgrade` **in place** on the live
`~/.coord-venv` (see the old ``coord.agent_app._do_update``). That leaves a
window — while pip is rewriting site-packages file by file — during which a
concurrent `coord` invocation can observe a *partial* install: some modules
already the new version, others still the old one. This repo hit that for
real: mid-upgrade, ``state.py`` had already been swapped to a version that
imports ``coord.board_service``, but ``board_service.py`` hadn't landed yet,
so a concurrent ``coord report-result`` crashed with ``ModuleNotFoundError``.
An update must be all-or-nothing.

The fix: never write into the venv that's live. Install the target version
into a **fresh** venv — one of two fixed "slots" next to the live one
(``~/.coord-venv.blue`` / ``~/.coord-venv.green``) — smoke-check it, and
only then flip a symlink so ``~/.coord-venv`` always resolves to one
*complete* slot, old or new, never a mix. Rename of a symlink onto an
existing path is atomic on POSIX (same filesystem), so any `coord`
invocation racing the flip sees either the fully-old or the fully-new
install — there is no observable in-between state.

Using exactly two named slots (rather than a fresh directory per release)
also gives rollback for free: the slot that was live before the swap is
left untouched, so it's still there — one generation back — until the
*next* update reuses it. See :func:`rollback`.

#2140: "the slot that's live" means two different things that usually —
but not always — agree: the slot ``venv_dir`` *symlinks to*, and the slot
whatever process is *actually running perform_update* was started from
(``sys.executable``). A swap flips the symlink without restarting anyone,
so the moment that happens the two diverge — normal on a fleet where
restarts are gated on a drain (#2138/#2136), not a rare race. If a second
update then reuses the slot the symlink no longer points at, it is
deleting the running caller's own interpreter and site-packages out from
under it: the subprocess spawn of ``sys.executable`` fails because the
path is now gone, cleanup deletes it a second time for good measure, and
the generation that was the rollback target is destroyed along with it.
Two independent guards below close this: :func:`perform_update` refuses
outright when the slot it would rebuild is the one backing its own
``sys.executable`` (recoverable — the caller just needs a restart first),
and venv creation always uses the *symlinked* slot's python rather than
``sys.executable``, so the tool building the new environment is never the
thing this update is about to delete.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

#: Suffixes for the two blue/green slots, relative to the live venv dir
#: (e.g. ``~/.coord-venv`` -> ``~/.coord-venv.blue`` / ``~/.coord-venv.green``).
_BLUE_SUFFIX = ".blue"
_GREEN_SUFFIX = ".green"

#: What the smoke check imports to prove the new install actually boots —
#: the two modules whose disagreement caused the ModuleNotFoundError this
#: whole mechanism exists to prevent (state.py -> board_service.py).
_SMOKE_IMPORTS = "coord.state, coord.commands.review"


@dataclass
class UpdateResult:
    """Outcome of :func:`perform_update` or :func:`rollback`.

    ``new_version`` is read from the new slot *before* it goes live (via the
    smoke check), so it's available even though the caller's own
    ``importlib.metadata`` read of ``~/.coord-venv`` won't reflect it until
    that process's next fresh read after the swap.
    """

    ok: bool
    swapped: bool
    slot: Path | None = None
    previous_slot: Path | None = None
    new_version: str | None = None
    error: str | None = None
    log: str = ""


def _slots(venv_dir: Path) -> tuple[Path, Path]:
    """Return the ``(blue, green)`` sibling directories for *venv_dir*."""
    parent = venv_dir.parent
    name = venv_dir.name
    return parent / f"{name}{_BLUE_SUFFIX}", parent / f"{name}{_GREEN_SUFFIX}"


def current_slot(venv_dir: Path) -> Path | None:
    """Return the slot ``venv_dir`` currently resolves to.

    ``None`` when *venv_dir* doesn't exist yet, or exists as a plain
    directory that hasn't been migrated to the blue/green layout (see
    :func:`ensure_symlink_layout`) — every pre-#1241 install starts this
    way, since ``install-agent.sh`` creates ``~/.coord-venv`` as a real
    directory.
    """
    if not venv_dir.is_symlink():
        return None
    target = venv_dir.readlink()
    if not target.is_absolute():
        target = (venv_dir.parent / target).resolve()
    return target


def ensure_symlink_layout(venv_dir: Path) -> Path:
    """Migrate *venv_dir* to the blue/green symlink layout if it isn't already.

    Idempotent: if *venv_dir* is already a symlink, just returns its current
    target. Otherwise renames the existing plain directory into the
    ``.blue`` slot and replaces *venv_dir* with a symlink pointing at it.
    This is the one-time, one-machine migration every pre-#1241 install
    needs; every update after that stays in the symlink layout, so this
    becomes a no-op for the rest of that machine's life.
    """
    existing = current_slot(venv_dir)
    if existing is not None:
        return existing
    if not venv_dir.exists():
        raise FileNotFoundError(f"no venv at {venv_dir} to migrate")
    blue, _green = _slots(venv_dir)
    if blue.exists():
        # Should be unreachable — `blue`/`green` only ever come into being
        # via this function or `perform_update`, both gated on `venv_dir`
        # not already being a symlink. Refuse rather than clobber whatever
        # is there.
        raise FileExistsError(
            f"{blue} already exists — refusing to migrate {venv_dir} over it"
        )
    venv_dir.rename(blue)
    venv_dir.symlink_to(blue, target_is_directory=True)
    return blue


def _other_slot(venv_dir: Path, active: Path) -> Path:
    blue, green = _slots(venv_dir)
    return green if active == blue else blue


def _slot_backing_interpreter(venv_dir: Path, interpreter: Path) -> Path | None:
    """Return whichever blue/green slot *interpreter* physically lives under.

    ``None`` if *interpreter* resolves to neither slot (e.g. a dev/editable
    install not using the blue/green layout at all).

    #2140: deliberately keyed off the slot *directories* themselves, not
    off :func:`current_slot` — ``sys.executable`` is the literal path baked
    into a process at start time (e.g. ``~/.coord-venv.blue/bin/python3``)
    and stays pinned to that slot for the process's whole life, even after
    a later swap moves the ``venv_dir`` symlink onto the other slot. Those
    two — "what the symlink currently says" and "what this process is
    actually running from" — regularly disagree for hours on a fleet where
    restarts are gated on a drain (#2138/#2136); this function answers the
    second question, which is the one that matters before deleting a slot.
    """
    try:
        resolved = interpreter.resolve()
    except OSError:
        return None
    blue, green = _slots(venv_dir)
    for slot in (blue, green):
        try:
            resolved.relative_to(slot.resolve())
        except (OSError, ValueError):
            continue
        return slot
    return None


def _atomic_swap(venv_dir: Path, new_slot: Path) -> None:
    """Flip *venv_dir* to point at *new_slot* in one filesystem operation.

    Builds the new symlink at a temp path next to *venv_dir* and renames it
    directly onto *venv_dir* — ``rename()`` replacing an existing path is
    atomic on POSIX when both are on the same filesystem (true here: both
    are siblings under the same parent directory), so any `coord`
    invocation racing this always sees either the old, complete slot or the
    new, complete slot — never a half-updated ``venv_dir``.
    """
    tmp_link = venv_dir.parent / f".{venv_dir.name}.next-link"
    if tmp_link.is_symlink() or tmp_link.exists():
        tmp_link.unlink()
    tmp_link.symlink_to(new_slot, target_is_directory=True)
    tmp_link.replace(venv_dir)


def _smoke_check(slot: Path, *, target_version: str | None) -> tuple[bool, str | None, str]:
    """Run the two smoke checks against a freshly-installed *slot*.

    Returns ``(ok, detected_version, log)``. ``detected_version`` is parsed
    out of the import-check's own ``importlib.metadata`` read (the same
    mechanism :func:`_installed_version` uses) so a single subprocess call
    covers both "does it import" and "what version is this."

    #2103: the version print resolves via ``coord.dist_name.resolve_installed``
    (tries `code-coordinator` then falls back to `claude-coordinator`) rather
    than a hardcoded ``m.version('claude-coordinator')`` — *slot* was just
    installed from a pkg spec that itself resolved tolerantly (see
    ``coord.agent_app._agent_pkg_spec``), so a hardcoded name here would
    raise inside the new slot's own interpreter the moment that pkg spec
    picked the other name, failing every smoke check fleet-wide the instant
    the rename lands. Raising when *neither* name is installed is still the
    right behavior here (not caught) — that means the fresh install is
    genuinely broken, which is exactly what a failed smoke check should
    report.
    """
    python = slot / "bin" / "python"
    coord_bin = slot / "bin" / "coord"
    lines: list[str] = []

    try:
        result = subprocess.run(
            [
                str(python),
                "-c",
                f"import {_SMOKE_IMPORTS}\n"
                "from coord.dist_name import resolve_installed\n"
                "print(resolve_installed().version)",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, None, f"smoke import check raised {type(exc).__name__}: {exc}"

    lines.append(
        f"$ python -c 'import {_SMOKE_IMPORTS}'\n{result.stdout}{result.stderr}"
    )
    if result.returncode != 0:
        return False, None, "\n".join(lines)
    detected_version = result.stdout.strip() or None

    try:
        result = subprocess.run(
            [str(coord_bin), "--version"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        lines.append(f"coord --version raised {type(exc).__name__}: {exc}")
        return False, detected_version, "\n".join(lines)

    lines.append(f"$ coord --version\n{result.stdout}{result.stderr}")
    if result.returncode != 0:
        return False, detected_version, "\n".join(lines)

    if target_version and target_version not in (result.stdout + result.stderr):
        lines.append(
            f"version mismatch: expected {target_version!r} in `coord --version` output"
        )
        return False, detected_version, "\n".join(lines)

    return True, detected_version, "\n".join(lines)


def perform_update(
    venv_dir: Path,
    pkg_spec: str,
    *,
    target_version: str | None = None,
    pip_timeout: float = 180.0,
) -> UpdateResult:
    """Install ``pkg_spec`` (optionally pinned to *target_version*) into a
    fresh slot, smoke-check it, and atomically swap it into place.

    Never mutates *venv_dir*'s currently-live slot. On any failure — venv
    creation, pip, or the smoke check — the half-built next slot is removed
    and *venv_dir* is left exactly as it was; the caller's process keeps
    running the old code with no restart needed. Returns a failed
    :class:`UpdateResult` rather than raising, except for genuinely
    unexpected setup errors (e.g. *venv_dir* doesn't exist at all).

    #2140: also refuses — before touching anything — if *next_slot* (the
    one about to be rebuilt) is the slot backing this very process's own
    ``sys.executable``. That happens when a previous swap flipped the
    symlink without a restart following it; proceeding would delete the
    caller's own running interpreter and site-packages, and destroy the
    rollback generation along with it. A refusal here is recoverable (the
    caller just needs restarting first); reaching into that slot is not.
    """
    log_parts: list[str] = []
    active = ensure_symlink_layout(venv_dir)
    next_slot = _other_slot(venv_dir, active)

    running_slot = _slot_backing_interpreter(venv_dir, Path(sys.executable))
    if running_slot is not None and running_slot == next_slot:
        return UpdateResult(
            ok=False,
            swapped=False,
            error=(
                f"refusing to update: this process's own interpreter "
                f"({sys.executable}) is running from {next_slot}, the slot "
                "this update would delete and rebuild. venv_dir currently "
                f"symlinks to {active}, so a prior swap flipped it without "
                "this process restarting — restart the caller (or wait for "
                "idle self-restart, #2139) and retry (#2140)."
            ),
        )

    # Always build fresh — a stale, possibly half-built slot left over from
    # an interrupted update two generations back must never be reused.
    if next_slot.exists():
        shutil.rmtree(next_slot, ignore_errors=True)

    def _fail(error: str) -> UpdateResult:
        shutil.rmtree(next_slot, ignore_errors=True)
        return UpdateResult(ok=False, swapped=False, error=error, log="\n".join(log_parts))

    # #2140: build with the *symlinked* slot's python, not sys.executable —
    # `active` is guaranteed to differ from `next_slot` (they're the two
    # distinct blue/green slots), so the interpreter doing the building can
    # never be the thing this update is about to rmtree, regardless of
    # which slot the calling process itself happens to be running from.
    builder_python = active / "bin" / "python"
    if not builder_python.exists():
        builder_python = Path(sys.executable)
    try:
        result = subprocess.run(
            [str(builder_python), "-m", "venv", str(next_slot)],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired as exc:
        return _fail(f"venv creation timed out: {exc}")
    log_parts.append(f"$ python -m venv {next_slot}\n{result.stdout}{result.stderr}")
    if result.returncode != 0:
        return _fail(f"venv creation failed (exit {result.returncode})")

    pip = str(next_slot / "bin" / "pip")
    install_spec = f"{pkg_spec}=={target_version}" if target_version else pkg_spec
    try:
        result = subprocess.run(
            [pip, "install", "--no-cache-dir", install_spec],
            capture_output=True,
            text=True,
            timeout=pip_timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return _fail(f"pip install timed out: {exc}")
    log_parts.append(f"$ pip install --no-cache-dir {install_spec}\n{result.stdout}{result.stderr}")
    if result.returncode != 0:
        return _fail(f"pip install failed (exit {result.returncode})")

    ok, new_version, smoke_log = _smoke_check(next_slot, target_version=target_version)
    log_parts.append(smoke_log)
    if not ok:
        return _fail("smoke check failed on the new install; see log")

    previous = active
    _atomic_swap(venv_dir, next_slot)
    return UpdateResult(
        ok=True,
        swapped=True,
        slot=next_slot,
        previous_slot=previous,
        new_version=new_version,
        log="\n".join(log_parts),
    )


def rollback(venv_dir: Path) -> UpdateResult:
    """Flip *venv_dir* back onto the previous generation, if one exists.

    The previous slot is smoke-checked before the swap — a rollback that
    would land on a broken install is refused, leaving the current
    (presumably also broken, but at least known) slot in place rather than
    trading one failure for another.
    """
    active = current_slot(venv_dir)
    if active is None:
        return UpdateResult(
            ok=False, swapped=False, error=f"{venv_dir} is not a migrated blue/green venv"
        )
    previous = _other_slot(venv_dir, active)
    if not previous.exists():
        return UpdateResult(
            ok=False, swapped=False, error=f"no previous generation at {previous}"
        )

    ok, version, log = _smoke_check(previous, target_version=None)
    if not ok:
        return UpdateResult(
            ok=False,
            swapped=False,
            error=f"previous slot {previous} fails its smoke check — refusing to roll back onto it",
            log=log,
        )

    _atomic_swap(venv_dir, previous)
    return UpdateResult(
        ok=True,
        swapped=True,
        slot=previous,
        previous_slot=active,
        new_version=version,
        log=log,
    )
