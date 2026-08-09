"""The `deploy/**` lane's missing *deploy step* (#1831, wired up by #1835).

#1831 gave `deploy/**` a **detector** — ``coord.health.checks.unit_drift``
diffs each host's installed unit under ``~/.config/systemd/user/`` against
the units packaged in the wheel (``coord/deploy/``, #1927) — and a printed
remedy that a human then runs by hand. That was the right first half. It is
not a deploy lane: cutting v0.4.106 still meant installing five files into
``~/.config/systemd/user/`` and ``~/.local/bin/`` on dellserver, plus a
``daemon-reload``, plus retiring a machine-local drop-in.

#1835 cannot claim "the fleet reaches that version" while a whole lane needs
a human with ``cp`` and ``systemctl``, and this is not a corner case: #1543's
actual mechanism was three unit files and a shell script, while its Python
change was a single ``--dist`` flag. A release that propagated only the
Python lane would have shipped the flag and none of the behaviour.

So this module applies what ``unit_drift`` reports.

THREE SAFETY PROPERTIES, ALL DELIBERATE
---------------------------------------
1. **Refresh only units this host already runs.** Which services a host runs
   is a *topology* decision (``coordinator.yml``), not a release decision. A
   packaged unit with no installed counterpart is reported as ``new`` and
   left alone — installing ``coord-web.service`` onto a machine that never
   wanted a web server, because a release happened to contain the file, is a
   far worse failure than a human running one ``cp``. The report names them
   so the human action is visible rather than implicit.

2. **Templates are rendered, never copied verbatim (#1928).** Several units
   carry ``<MACHINE_NAME>`` / ``<PORT>`` placeholders. Copying one verbatim
   installs the placeholder as literal text and the unit then refuses to
   start — the exact hazard #1928 documented. Placeholders with no known
   substitution abort *that unit* (reported, not written); they never get
   guessed.

3. **The previous content is kept.** Every overwrite writes
   ``<name>.pre-<version>.bak`` next to the unit first, so the rollback for
   this lane is a file copy the operator can see and `diff`, not a re-run of
   an install script whose inputs have moved on.

The write itself is atomic per file (temp file + ``os.replace``), so a unit
is never observed half-written by a ``daemon-reload`` racing this.

Pure-ish by construction: every path is a parameter, so the whole thing is
testable against ``tmp_path`` with no systemd, no fleet, and no root.
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path

# Reuse #1831's own definitions rather than re-spelling them. Two
# definitions of "which files are units" or "what a placeholder looks like"
# would let the detector and the deployer disagree — and a deployer that
# disagrees with its detector reports clean while shipping nothing.
from coord.health.checks.unit_drift import (
    _KNOWN_PLACEHOLDER_VALUES,
    _PLACEHOLDER_RE,
    _SYSTEMD_USER_DIR,
    _UNIT_GLOBS,
    packaged_unit_dir,
)

#: Placeholder -> how to fill it, given the host facts we actually know.
#: ``unit_drift`` renders these as *shell* text for a copy-pasteable remedy
#: (``$(hostname -s)``); here they must be real values, so the mapping is
#: from placeholder name to the keyword of :func:`install_units`.
_PLACEHOLDER_SOURCES = {
    "MACHINE_NAME": "machine_name",
    "PORT": "port",
}

#: Outcome of one unit's deploy step.
ACTION_UNCHANGED = "unchanged"
ACTION_UPDATED = "updated"
ACTION_NEW = "new"
ACTION_SKIPPED = "skipped"
ACTION_FAILED = "failed"


@dataclass(frozen=True)
class UnitOutcome:
    name: str
    action: str
    detail: str = ""
    backup: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class InstallReport:
    """What the deploy step did, per unit."""

    units: list[UnitOutcome] = field(default_factory=list)
    reference: str | None = None
    error: str | None = None
    #: True once at least one unit's bytes changed — the only case that
    #: needs a ``systemctl --user daemon-reload``.
    @property
    def changed(self) -> bool:
        return any(u.action == ACTION_UPDATED for u in self.units)

    @property
    def ok(self) -> bool:
        return self.error is None and not any(
            u.action == ACTION_FAILED for u in self.units
        )

    def to_dict(self) -> dict:
        return {
            "reference": self.reference,
            "error": self.error,
            "changed": self.changed,
            "ok": self.ok,
            "units": [u.to_dict() for u in self.units],
        }

    def summary(self) -> str:
        counts: dict[str, int] = {}
        for unit in self.units:
            counts[unit.action] = counts.get(unit.action, 0) + 1
        if self.error:
            return f"units: {self.error}"
        if not counts:
            return "units: nothing packaged to install"
        return "units: " + ", ".join(
            f"{count} {action}" for action, count in sorted(counts.items())
        )


def systemd_user_dir(home: Path | None = None) -> Path:
    base = home or Path.home()
    return Path(str(_SYSTEMD_USER_DIR).replace("~", str(base), 1))


def _packaged_units(reference_dir: Path) -> list[Path]:
    seen: set[str] = set()
    out: list[Path] = []
    for pattern in _UNIT_GLOBS:
        for path in sorted(reference_dir.glob(pattern)):
            if path.name in seen:
                continue
            seen.add(path.name)
            out.append(path)
    return sorted(out, key=lambda p: p.name)


def render_unit(text: str, *, machine_name: str | None, port: int | str | None) -> tuple[str | None, str]:
    """Fill ``<PLACEHOLDER>`` tokens. Returns ``(rendered_or_None, note)``.

    ``None`` means "this unit is a template with a placeholder we cannot
    fill" — the caller must skip it and say so. Guessing a value here is how
    a unit lands with ``<MACHINE_NAME>`` as literal text and then refuses to
    start (#1928).
    """
    names = sorted(set(_PLACEHOLDER_RE.findall(text)))
    if not names:
        return text, ""
    values = {"machine_name": machine_name, "port": port}
    filled: dict[str, str] = {}
    for name in names:
        key = _PLACEHOLDER_SOURCES.get(name)
        value = values.get(key) if key else None
        if value in (None, ""):
            fallback = _KNOWN_PLACEHOLDER_VALUES.get(name)
            return None, (
                f"template placeholder <{name}> has no value for this host"
                + (f" (unit_drift's documented default is {fallback})" if fallback else "")
                + " — refusing to install it verbatim (#1928); install this "
                "unit by hand"
            )
        filled[name] = str(value)

    def _sub(match: re.Match[str]) -> str:
        return filled[match.group(1)]

    return _PLACEHOLDER_RE.sub(_sub, text), f"rendered {', '.join(names)}"


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".coord-tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def install_units(
    *,
    target_dir: Path | None = None,
    reference_dir: Path | None = None,
    machine_name: str | None = None,
    port: int | str | None = None,
    version: str | None = None,
    dry_run: bool = False,
    home: Path | None = None,
) -> InstallReport:
    """Refresh this host's installed systemd user units from the wheel.

    *reference_dir* defaults to :func:`~coord.health.checks.unit_drift.
    packaged_unit_dir` — ``coord/deploy/`` inside the *installed*
    distribution, i.e. the released artifact, which cannot drift with the
    host's checkout (#1927). Only units already present in *target_dir* are
    rewritten; see the module docstring for why.
    """
    report = InstallReport()
    ref = reference_dir or packaged_unit_dir()
    if ref is None:
        report.error = (
            "this install ships no coord/deploy/ — it predates #1927, so "
            "there is no released unit set to deploy from. Upgrade the "
            "Python lane first."
        )
        return report
    report.reference = str(ref)

    dest_dir = target_dir or systemd_user_dir(home)
    suffix = f".pre-{version}" if version else ".pre-update"

    for source in _packaged_units(ref):
        installed = dest_dir / source.name
        if not installed.exists():
            report.units.append(
                UnitOutcome(
                    source.name,
                    ACTION_NEW,
                    "packaged but not installed on this host — a release does "
                    "not decide which services a host runs; install and enable "
                    "it by hand if this host should have it",
                )
            )
            continue

        try:
            source_text = source.read_text(encoding="utf-8")
        except OSError as exc:
            report.units.append(
                UnitOutcome(source.name, ACTION_FAILED, f"unreadable reference: {exc}")
            )
            continue

        rendered, note = render_unit(
            source_text, machine_name=machine_name, port=port
        )
        if rendered is None:
            report.units.append(UnitOutcome(source.name, ACTION_SKIPPED, note))
            continue

        try:
            current = installed.read_text(encoding="utf-8")
        except OSError as exc:
            report.units.append(
                UnitOutcome(source.name, ACTION_FAILED, f"unreadable installed unit: {exc}")
            )
            continue

        if current == rendered:
            report.units.append(UnitOutcome(source.name, ACTION_UNCHANGED, note))
            continue

        if dry_run:
            report.units.append(
                UnitOutcome(source.name, ACTION_UPDATED, f"would rewrite ({note})".strip())
            )
            continue

        backup = installed.with_name(installed.name + suffix + ".bak")
        try:
            shutil.copy2(installed, backup)
            _atomic_write(installed, rendered)
        except OSError as exc:
            report.units.append(
                UnitOutcome(source.name, ACTION_FAILED, f"write failed: {exc}")
            )
            continue
        report.units.append(
            UnitOutcome(
                source.name,
                ACTION_UPDATED,
                note or "content refreshed from the packaged release",
                backup=str(backup),
            )
        )

    return report


def daemon_reload(*, runner=None, timeout: float = 30.0) -> tuple[bool, str]:
    """``systemctl --user daemon-reload``. Returns ``(ok, output)``.

    Split out and injectable so :func:`install_units` stays a pure filesystem
    operation testable without systemd — and so a host with no systemd (a
    macOS worker) degrades to a reported skip rather than a traceback.
    """
    import subprocess  # noqa: PLC0415

    run = runner or subprocess.run
    try:
        proc = run(
            ["systemctl", "--user", "daemon-reload"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return False, "systemctl not found (no systemd on this host)"
    except Exception as exc:  # noqa: BLE001 — a reload must never crash a roll
        return False, f"{type(exc).__name__}: {exc}"
    ok = getattr(proc, "returncode", 1) == 0
    out = (getattr(proc, "stderr", "") or getattr(proc, "stdout", "") or "").strip()
    return ok, out or ("daemon-reload ok" if ok else "daemon-reload failed")
