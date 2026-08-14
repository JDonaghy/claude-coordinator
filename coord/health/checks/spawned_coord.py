"""What a running coord service actually SPAWNS, not what its venv says (#1834).

This is the lane that was blind on 2026-08-04, hours after v0.4.105 shipped:

===========================================  ========
readout                                      said
===========================================  ========
PyPI simple index                            0.4.105
``coord status`` (all three agents)          0.4.105
``~/.coord-venv/bin/coord version``          0.4.105
``~/.local/bin/coord version``               0.4.105
**what the daemon actually spawned**         **0.4.103**
===========================================  ========

Every existing lane check reads an *install* — a venv's ``pip show``, a
binary's mtime, a unit file's text.  None of them read the thing that
actually executes.  ``coord-serve``'s hand-installed unit began its
``Environment=PATH=`` with an editable checkout two releases behind, and
:func:`coord.drive.coord_argv` resolves every subprocess with
``shutil.which("coord")`` — so the daemon ran the release and everything it
spawned ran the checkout.  Four green readouts, one split brain.

:mod:`coord.health.checks.unit_drift` catches the *static* half of this: an
editable ``.venv/bin`` ahead of a release marker in the unit file's PATH
line.  That is necessary but not sufficient, and it was written from the
same incident, so it is worth being precise about why both exist:

* ``unit_drift`` reads ``deploy/`` and ``~/.config/systemd/user/`` — files.
  It is blind to a unit whose *on-disk* PATH is fine but whose running
  process inherited a different one (``systemctl --user set-environment``, a
  drop-in under ``<unit>.d/``, an ``EnvironmentFile``, a manually-started
  ``coord serve`` in a shell with a dev venv activated), and blind to a PATH
  entry that is not literally spelled ``.venv/bin`` but still resolves
  ``coord`` to a stale tree.
* This check reads ``/proc/<mainpid>/environ`` — the PATH the kernel is
  actually holding for the live process — resolves ``coord`` on it exactly
  the way ``coord_argv()`` will, and asks *that* binary its version.  It
  cannot be fooled by how the PATH got there.

The design constraint from #1834, learned the expensive way: **verify the
running process, not the venv.**  ``pip install --upgrade`` silently no-ops
often enough to be a documented fleet gotcha, so a venv reporting the right
version proves nothing about what is executing.

Two findings, either of which is CRIT on its own:

``version skew``
    The spawned ``coord`` reports a different version from the process doing
    the reporting (an agent that *is* a coord install, on the same box).
    This is 2026-08-04 exactly.

``editable on a service PATH``
    The spawned ``coord`` resolves into a checkout rather than
    site-packages — CRIT **regardless of the version it currently reports**,
    per #1834: an editable install on a service PATH is a drift amplifier
    that silently tracks a checkout nothing keeps current, so today's
    accidental agreement is not evidence of anything.

Absence is the common case and is never a fault: a box with no coord user
units running reports a single OK row, same convention as ``cli_venv`` /
``tui_binary`` (:mod:`coord.health.checks.deploy_lane_facts`).

Strictly read-only — no ``systemctl start/restart``, no writes anywhere.
``coord release verify`` is safe to run mid-flight (#1834's non-goal:
fixing drift belongs to ``coord agent update``).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from coord import __version__ as OWN_VERSION
from coord.health.models import CheckResult, HealthContext, Severity
from coord.health.registry import check

# The systemd *user* units in deploy/ that spawn `coord` subprocesses.
# Overridable via `health.spawned_coord_units` for a fleet that renames them;
# an empty list disables the check (the one knob here that does mean "off",
# because the unit *names* are the check's entire subject).
DEFAULT_UNITS: tuple[str, ...] = (
    "coord-serve",
    "coord-agent",
    "coord-web",
    "coord-drive-queue",
    "coord-notify",
)

# systemctl/`coord --version` are both fast, but a wedged one must not eat
# the ~2s registry budget for the whole tick.
_SYSTEMCTL_TIMEOUT = 5.0
_VERSION_TIMEOUT = 12.0

_PROC_ROOT = Path("/proc")


def configured_units(ctx: HealthContext) -> tuple[str, ...]:
    """Unit names to inspect, without the ``.service`` suffix."""
    configured = getattr(ctx.thresholds, "spawned_coord_units", None)
    if configured is None:
        return DEFAULT_UNITS
    return tuple(str(u).removesuffix(".service") for u in configured)


def running_unit_pids(units: tuple[str, ...]) -> dict[str, int]:
    """``unit -> MainPID`` for every one of *units* actually running here.

    One ``systemctl show`` call for all of them rather than one per unit:
    this runs on every health tick on every machine, and five subprocess
    spawns to learn that a box runs none of them is a bad trade.  Units that
    are absent, inactive, or have no main PID are simply left out — the
    per-unit distinction between "not installed" and "installed but stopped"
    belongs to ``systemctl status``, not to a deploy-lane version check.
    """
    if not units:
        return {}
    argv = [
        "systemctl",
        "--user",
        "show",
        "--property=Id",
        "--property=MainPID",
        "--property=ActiveState",
        *[f"{u}.service" for u in units],
    ]
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=_SYSTEMCTL_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        # No systemd (macOS, a container, a thin client) — not a fault.
        return {}

    out: dict[str, int] = {}
    # `systemctl show` emits one KEY=VALUE block per unit, blank-line
    # separated. Parse defensively: field order is not contractual.
    for block in proc.stdout.split("\n\n"):
        fields: dict[str, str] = {}
        for line in block.splitlines():
            key, sep, value = line.partition("=")
            if sep:
                fields[key.strip()] = value.strip()
        unit_id = fields.get("Id", "")
        if not unit_id:
            continue
        if fields.get("ActiveState") != "active":
            continue
        try:
            pid = int(fields.get("MainPID", "0"))
        except ValueError:
            continue
        if pid > 0:
            out[unit_id.removesuffix(".service")] = pid
    return out


def process_path(pid: int, *, proc_root: Path | None = None) -> str | None:
    """The ``PATH`` the live process *pid* is actually holding, or None.

    Read from ``/proc/<pid>/environ`` — the kernel's copy — deliberately
    *not* re-derived from the unit file.  A unit whose file says one thing
    and whose process holds another (drop-in, ``EnvironmentFile``,
    ``systemctl --user set-environment``, or a service started by hand from
    a shell with a dev venv activated) is precisely the case a file-reading
    check cannot see, and precisely the case that bit on 2026-08-04.
    """
    root = proc_root or _PROC_ROOT
    try:
        raw = (root / str(pid) / "environ").read_bytes()
    except OSError:
        # Permission denied (another user's process), or the service exited
        # between the systemctl call and here. Either way: no data, not OK.
        return None
    for entry in raw.split(b"\0"):
        key, sep, value = entry.partition(b"=")
        if sep and key == b"PATH":
            return value.decode("utf-8", "replace")
    return None


def resolve_coord(path_value: str) -> str | None:
    """``shutil.which("coord", path=...)`` — the exact call ``coord_argv`` makes.

    Not an approximation of it: :func:`coord.drive.coord_argv` literally
    calls ``shutil.which("coord")`` against the inherited environment, so
    resolving the same name against the same PATH string is the only way to
    predict which binary a spawned subprocess gets.  ``None`` here is the
    benign case — ``coord_argv`` then falls back to ``[sys.executable, "-m",
    "coord.cli"]``, i.e. the *parent's own* install, which by construction
    cannot be skewed against the parent.

    KNOWN BLIND SPOT: ``coord_argv()`` checks ``$COORD_DRIVE_COORD_BIN``
    *before* ever calling ``shutil.which`` and, if set, returns that override
    verbatim — this function has no way to see that env var (it isn't part
    of ``PATH``, which is all ``/proc/<pid>/environ`` gives this check reason
    to read) or predict it. A live service whose environment sets that
    variable would have its actual spawn target silently diverge from what
    this lane reports, in either direction (a false OK or a false CRIT,
    depending on what happens to also resolve on PATH). It is documented on
    ``coord_argv`` as existing "for tests" but is not fenced off from
    production, so this is a real if unlikely gap — not a hypothetical one.
    """
    if not path_value:
        return None
    return shutil.which("coord", path=path_value)


def spawned_identity(binary: str) -> tuple[str | None, str | None, str | None]:
    """``(version, module_file, error)`` for the ``coord`` at *binary*.

    Resolved through the console script's shebang interpreter and a bare
    ``import coord`` rather than ``coord --version``: importing the package
    is ~20x cheaper than building the whole click command tree, and it also
    yields ``coord.__file__``, which is what tells site-packages (a release)
    apart from a checkout (an editable install).  ``coord --version`` is the
    fallback for an exotic console script with no readable shebang — it
    gives the version but no module path, so editability then reads as
    "unknown" rather than being guessed.
    """
    interpreter = _shebang_interpreter(binary)
    if interpreter:
        code = "import coord;print(coord.__version__);print(coord.__file__)"
        try:
            # -P (3.11+, this repo is 3.12+): suppress prepending the
            # subprocess's cwd to sys.path. Without it, `python -c` resolves
            # `import coord` against whatever directory the *health check's
            # own caller* happens to be sitting in — so running `coord
            # health` from inside a coord checkout makes this probe read
            # ./coord/__init__.py instead of the spawned interpreter's real
            # site-packages, and reports a healthy venv as editable (#2227).
            # -P is the closer match to a real spawned worker than -I, which
            # would also drop PYTHONPATH/user site and change what this
            # measures.
            proc = subprocess.run(
                [interpreter, "-P", "-c", code],
                capture_output=True,
                text=True,
                timeout=_VERSION_TIMEOUT,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return None, None, f"{type(exc).__name__}: {exc}"
        if proc.returncode == 0:
            lines = proc.stdout.strip().splitlines()
            if len(lines) >= 2:
                return lines[0].strip(), lines[1].strip(), None
            if lines:
                return lines[0].strip(), None, None
        return None, None, (proc.stderr or proc.stdout).strip()[:300] or "no output"

    try:
        proc = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=_VERSION_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, None, f"{type(exc).__name__}: {exc}"
    if proc.returncode != 0:
        return None, None, (proc.stderr or proc.stdout).strip()[:300] or "no output"
    # "coord, version 0.4.108"
    token = proc.stdout.strip().rsplit(" ", 1)[-1] if proc.stdout.strip() else ""
    return (token or None), None, None


def _shebang_interpreter(binary: str) -> str | None:
    try:
        with open(binary, "rb") as fh:
            first = fh.readline(512)
    except OSError:
        return None
    if not first.startswith(b"#!"):
        return None
    parts = first[2:].decode("utf-8", "replace").strip().split()
    if not parts:
        return None
    # `#!/usr/bin/env python3` — take the argument, not `env` itself.
    if os.path.basename(parts[0]) == "env" and len(parts) > 1:
        return parts[1]
    return parts[0]


def is_editable(module_file: str | None) -> bool | None:
    """Whether *module_file* is a checkout rather than an installed release.

    ``None`` when there is no module path to judge (the ``coord --version``
    fallback above) — deliberately not ``False``: #1834 makes "editable on a
    service PATH" a finding in its own right, so guessing "not editable"
    from missing evidence would silence exactly the finding this exists for.
    """
    if not module_file:
        return None
    return "site-packages" not in Path(module_file).parts


@check(
    id="spawned_coord",
    scope="machine",
    title="spawned coord",
    order=46,
    description=(
        "The `coord` that each running coord service would actually spawn — "
        "resolved from the live process's own PATH via shutil.which, the way "
        "coord_argv() does — matches the version reporting it, and is not an "
        "editable checkout (#1834, the 2026-08-04 daemon-spawns-N-2 blind spot)."
    ),
)
def probe_spawned_coord(ctx: HealthContext) -> list[CheckResult]:
    units = configured_units(ctx)
    pids = running_unit_pids(units)

    if not pids:
        # Thin clients, workers with no user units, macOS, containers. Not a
        # fault, and not UNKNOWN: there is genuinely no spawning service here.
        return [
            CheckResult(
                check_id="spawned_coord",
                scope="machine",
                severity=Severity.OK,
                headroom="no coord service running on this machine",
                values={"units": list(units), "running": {}, "own_version": OWN_VERSION},
            )
        ]

    results: list[CheckResult] = []
    # One resolve+version subprocess per DISTINCT binary, not per unit: the
    # five units normally share one PATH, and this runs on every tick.
    identities: dict[str, tuple[str | None, str | None, str | None]] = {}

    for unit in sorted(pids):
        pid = pids[unit]
        values: dict = {"unit": unit, "pid": pid, "own_version": OWN_VERSION}

        path_value = process_path(pid)
        if path_value is None:
            results.append(
                CheckResult(
                    check_id="spawned_coord",
                    scope="machine",
                    subject=unit,
                    severity=Severity.UNKNOWN,
                    headroom=f"could not read PATH of pid {pid}",
                    detail=(
                        "/proc/<pid>/environ is unreadable — the service runs "
                        "as another user, or exited mid-probe. This lane is "
                        "unverified, which is not the same as in sync."
                    ),
                    error="environ unreadable",
                    values={**values, "path": None, "version": None},
                )
            )
            continue

        values["path"] = path_value
        resolved = resolve_coord(path_value)
        values["resolved"] = resolved

        if resolved is None:
            # coord_argv() falls back to `sys.executable -m coord.cli`, i.e.
            # the spawning process's OWN install — structurally unskewable.
            results.append(
                CheckResult(
                    check_id="spawned_coord",
                    scope="machine",
                    subject=unit,
                    severity=Severity.OK,
                    headroom="no `coord` on the service PATH — subprocesses use the parent's own interpreter",
                    detail=(
                        "coord_argv() falls back to `python -m coord.cli` "
                        "(coord/drive.py), which cannot disagree with the "
                        "process that spawned it."
                    ),
                    values={**values, "fallback": True, "version": None},
                )
            )
            continue

        if resolved not in identities:
            identities[resolved] = spawned_identity(resolved)
        version, module_file, error = identities[resolved]
        editable = is_editable(module_file)
        values.update(
            {
                "version": version,
                "module_file": module_file,
                "editable": editable,
                "fallback": False,
            }
        )

        if version is None:
            results.append(
                CheckResult(
                    check_id="spawned_coord",
                    scope="machine",
                    subject=unit,
                    severity=Severity.UNKNOWN,
                    headroom=f"{resolved} would not report a version",
                    detail=(
                        "the binary this unit's PATH resolves `coord` to could "
                        "not be introspected — treat as unverified, not as in sync"
                    ),
                    error=error,
                    values=values,
                )
            )
            continue

        # Editable first: #1834 makes it a finding independent of the version
        # it happens to report today, so it must not be masked by a version
        # that currently agrees.
        if editable:
            results.append(
                CheckResult(
                    check_id="spawned_coord",
                    scope="machine",
                    subject=unit,
                    severity=Severity.CRIT,
                    headroom=f"spawns an EDITABLE checkout ({version}) via {resolved}",
                    detail=(
                        f"`coord` on this unit's PATH resolves to {module_file} "
                        "— a checkout, not an installed release. Every "
                        "subprocess this service spawns runs whatever branch "
                        "that tree is parked on, tracked by no release. Put "
                        "~/.local/bin or ~/.coord-venv/bin first on the unit's "
                        "Environment=PATH= (#1834)."
                    ),
                    threshold="crit for any editable install on a service PATH",
                    values=values,
                )
            )
            continue

        if version != OWN_VERSION:
            results.append(
                CheckResult(
                    check_id="spawned_coord",
                    scope="machine",
                    subject=unit,
                    severity=Severity.CRIT,
                    headroom=f"spawns {version}, but this install is {OWN_VERSION}",
                    detail=(
                        f"{resolved} is what shutil.which(\"coord\") resolves to "
                        f"on this unit's live PATH, so every subprocess it "
                        f"spawns runs {version} while the service itself runs "
                        f"{OWN_VERSION}. This is the 2026-08-04 split brain "
                        "(#1834) — four other readouts said the fleet was on "
                        "one version while it was running two."
                    ),
                    threshold="crit when the spawned version differs from the spawning one",
                    values=values,
                )
            )
            continue

        results.append(
            CheckResult(
                check_id="spawned_coord",
                scope="machine",
                subject=unit,
                severity=Severity.OK,
                headroom=f"spawns {version} (matches)",
                values=values,
            )
        )

    return results
