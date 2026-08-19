"""Freshness of THIS coordinator process's own editable ``coord`` install
(#2436).

``docs/OPERATING_GOTCHAS.md`` #1 already names the risk: any fix to
``coord/**`` (other than ``agent.py``/``serve_app.py``, which have their own
deploy steps) is live the instant ``git pull`` lands in whatever checkout
``coord.__file__`` resolves into — no restart needed. But nothing has ever
checked whether that ``git pull`` happened. A coordinator host can keep
running last week's code indefinitely with zero signal: every board row,
every ``coord diagnose`` finding, every escalation reads exactly as if the
bug behind it were still unfixed, because the process producing them
genuinely does not have the fix.

This is the #2286 incident: a fix had already merged to ``origin/main``, but
this host's installed ``coord`` was one commit behind — nothing had pulled it
— so every symptom looked identical to "still broken", and the actual cause
took manual ``git log``/``ps aux``/``tmux capture-pane`` archaeology to find.

Mirrors :mod:`coord.graph_health` / :mod:`coord.fleet_config_health`'s shape:
one read-only, best-effort probe (:func:`self_freshness`) plus a renderer,
wired into ``coord diagnose --self``.

**Locates the ACTUAL running install, never an assumed path.** The #2436
incident's second half was exactly a doc/reality mismatch:
``docs/OPERATING_GOTCHAS.md`` assumed the editable install lives at
``~/src/claude-coordinator``; the real install on that host resolved to
``~/src/code-coordinator``, a second, disconnected checkout. So this module
never guesses a ``~/src/<name>`` path — it asks the interpreter that is
actually running: ``Path(coord.__file__).resolve().parents[1]``.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path


def default_install_path() -> Path:
    """Where THIS process's ``coord`` import resolves to.

    ``$COORD_SELF_CHECKOUT`` overrides it (tests, and an operator who wants
    to point this at a different checkout than the one currently running);
    otherwise ``Path(coord.__file__).resolve().parents[1]`` — the package's
    grandparent directory, i.e. the checkout/site-packages root, never an
    assumed ``~/src/<name>`` path (see module docstring).

    Computed fresh on every call, not a module-level constant, so a test can
    override it via ``monkeypatch.setenv`` without fighting a value baked in
    at import time — same convention as
    :func:`coord.fleet_config_health.default_settings_dir`.
    """
    env = os.environ.get("COORD_SELF_CHECKOUT")
    if env:
        return Path(env).expanduser()
    import coord as _coord  # noqa: PLC0415 — avoid a self-import at module load

    return Path(_coord.__file__).resolve().parents[1]


@dataclass
class SelfFreshness:
    """Freshness of one checkout's ``coord`` install against its origin."""

    install_path: Path
    is_git_checkout: bool = False
    default_branch: str | None = None
    head_sha: str | None = None
    origin_sha: str | None = None
    commits_behind: int | None = None
    # Whether a `git fetch` was attempted/succeeded before comparing. `None`
    # means fetch was skipped (`fetch=False`); the comparison then reads
    # whatever origin/<branch> the last fetch left behind — same
    # never-fetch-by-default posture as coord.graph_health's origin check,
    # available here as an opt-out for callers on a fast/tight budget.
    fetch_ok: bool | None = None
    fetch_error: str | None = None
    # Set when freshness could not be determined at all (no report, no git,
    # no origin ref) — never silently reported as "up to date".
    unknown_reason: str | None = None

    @property
    def stale(self) -> bool:
        """HEAD is behind ``origin/<default_branch>`` by at least one commit.

        Deliberately False — not True — when this could not be proven
        (``unknown_reason`` set): an unknown must never be counted as drift,
        the same rule :func:`coord.graph_health.GraphStatus.stale` follows.
        """
        return bool(self.commits_behind)

    @property
    def healthy(self) -> bool:
        return self.is_git_checkout and self.unknown_reason is None and not self.stale


def _git_out(repo_path: Path, *args: str) -> str | None:
    try:
        r = subprocess.run(
            ["git", *args],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=10.0,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if r.returncode != 0:
        return None
    return r.stdout.strip() or None


def _fetch(repo_path: Path, branch: str, timeout: float) -> tuple[bool, str | None]:
    try:
        r = subprocess.run(
            ["git", "fetch", "--quiet", "origin", branch],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if r.returncode != 0:
        return False, (r.stderr or r.stdout).strip() or f"git fetch exited {r.returncode}"
    return True, None


def _commits_behind(repo_path: Path, head: str, origin: str) -> int | None:
    """Count of commits reachable from *origin* that are not in *head*
    (``git rev-list --count head..origin``). ``None`` (never raises) when
    git can't answer — an unproven comparison must not be reported as drift.
    """
    out = _git_out(repo_path, "rev-list", "--count", f"{head}..{origin}")
    if out is None:
        return None
    try:
        return int(out)
    except ValueError:
        return None


def _detect_default_branch(repo_path: Path) -> str:
    """``origin/HEAD``'s branch name, falling back to ``"main"``.

    Read-only (``symbolic-ref``, not a network call) — relies on whatever a
    prior ``git clone``/``git remote set-head`` already recorded.
    """
    ref = _git_out(repo_path, "symbolic-ref", "refs/remotes/origin/HEAD")
    if ref and "/" in ref:
        return ref.rsplit("/", 1)[-1]
    return "main"


def self_freshness(
    *,
    install_path: Path | None = None,
    default_branch: str | None = None,
    fetch: bool = True,
    timeout: float = 10.0,
) -> SelfFreshness:
    """Freshness of *install_path* (default: :func:`default_install_path`)
    against ``origin/<default_branch>``.

    Read-only w.r.t. the working tree and best-effort: a missing ``.git``, an
    unreadable HEAD, or a git failure all return a populated
    :class:`SelfFreshness` with ``unknown_reason`` set rather than raising.

    *fetch* (default ``True``) runs a bounded ``git fetch --quiet origin
    <branch>`` before comparing — unlike :mod:`coord.graph_health`'s
    never-fetch origin check, this probe's entire purpose is answering "did
    anyone actually pull", so reading a stale remote-tracking ref by default
    would silently defeat the point. A failed fetch is reported (never
    raised) and the comparison falls back to whatever ``origin/<branch>`` the
    last successful fetch left behind. Pass ``fetch=False`` to skip it
    entirely (e.g. a caller on a tight time budget, or offline).
    """
    install_path = install_path or default_install_path()
    st = SelfFreshness(install_path=install_path)

    if not (install_path / ".git").exists():
        st.unknown_reason = (
            f"{install_path} is not a git checkout (release install, or no "
            ".git directory found here) — nothing to compare against origin"
        )
        return st
    st.is_git_checkout = True

    st.head_sha = _git_out(install_path, "rev-parse", "HEAD")
    if not st.head_sha:
        st.unknown_reason = f"could not read HEAD of {install_path}"
        return st

    branch = default_branch or _detect_default_branch(install_path)
    st.default_branch = branch

    if fetch:
        st.fetch_ok, st.fetch_error = _fetch(install_path, branch, timeout)

    origin_sha = _git_out(install_path, "rev-parse", f"origin/{branch}")
    if not origin_sha:
        st.unknown_reason = (
            f"no origin/{branch} ref for {install_path} — never fetched, no "
            f"'origin' remote, or {branch!r} is not this checkout's default branch"
        )
        return st
    st.origin_sha = origin_sha
    st.commits_behind = _commits_behind(install_path, st.head_sha, origin_sha)
    if st.commits_behind is None:
        st.unknown_reason = (
            f"could not compare HEAD ({st.head_sha[:8]}) against "
            f"origin/{branch} ({origin_sha[:8]}) — git rev-list failed"
        )
    return st


def format_status_lines(st: SelfFreshness) -> list[str]:
    """Human-readable report lines for *st* (used by ``coord diagnose
    --self``)."""
    lines: list[str] = []
    where = str(st.install_path)

    if not st.is_git_checkout:
        lines.append(f"✓ {where}: {st.unknown_reason}")
        return lines

    if st.fetch_ok is False:
        lines.append(
            f"  ⚠ git fetch failed ({st.fetch_error}) — comparing against "
            "whatever origin ref the last successful fetch left behind"
        )

    if st.unknown_reason:
        lines.append(f"? {where}: freshness unknown — {st.unknown_reason}")
        return lines

    if st.commits_behind:
        n = st.commits_behind
        lines.append(
            f"✗ STALE: recorded HEAD {(st.head_sha or '')[:8]}, "
            f"origin/{st.default_branch} is now {(st.origin_sha or '')[:8]} — "
            f"{n} commit{'' if n == 1 else 's'} behind. This process is "
            "running OLD coord/** code — a merged fix here is silently inert "
            "until this checkout is pulled (docs/OPERATING_GOTCHAS.md #1). "
            f"Fix: git -C {where} pull"
        )
    else:
        lines.append(
            f"✓ {where}: up to date with origin/{st.default_branch} "
            f"({(st.head_sha or '')[:8]})"
        )
    return lines


def summary_line(st: SelfFreshness) -> str:
    """The machine-readable trailer ``coord diagnose --self`` prints,
    mirroring ``GRAPH_HEALTH:``/``CONFIG_PROVENANCE:``."""
    if not st.is_git_checkout:
        return f"SELF_FRESHNESS: git_checkout=false path={st.install_path}"
    if st.unknown_reason:
        return (
            f"SELF_FRESHNESS: git_checkout=true unknown=true path={st.install_path}"
        )
    return (
        "SELF_FRESHNESS: git_checkout=true "
        f"stale={'true' if st.stale else 'false'} "
        f"commits_behind={st.commits_behind if st.commits_behind is not None else '?'} "
        f"path={st.install_path}"
    )
