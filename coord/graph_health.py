"""Graphify knowledge-graph freshness + worktree-bootstrap health.

The repo ships a graphify graph in ``graphify-out/`` that agents are told to
query first (see CLAUDE.md).  Two things silently break it, and neither is
visible from the code:

**1. Linked worktrees are graph-blind.**  ``graphify-out/`` is gitignored by
design (only its ``.gitignore`` is tracked), so ``git worktree add`` produces
an empty one — and ``graphify query`` resolves ``graphify-out/graph.json``
strictly relative to cwd, with no upward walk and no ``--graph`` override.
``.githooks/post-checkout`` fixes this by symlinking each entry of the base
checkout's graph (``graph.json``, ``manifest.json``, ``cache/``, ...) into
the worktree's ``graphify-out/`` — the directory itself, and its tracked
``.gitignore``, are never touched (#1617: replacing the whole directory with
a symlink deleted the tracked ``.gitignore`` out from under git).  This hook
only runs where ``core.hooksPath`` points at ``.githooks`` — a one-time,
per-machine ``git config`` that nothing enforces.  :func:`hooks_path_status`
checks it.

**2. The graph drifts out of sync with HEAD.**  graphify's own hooks are
best-effort and structurally cannot cover every ref-moving operation:

* ``post-commit``/``post-checkout``/``post-merge`` all ``exit 0`` during a
  rebase, merge, or cherry-pick — so the merge agent's proactive rebase
  (#306), the single most common ref move in the fleet, never rebuilds.
* ``git reset --hard`` fires no rebuild hook at all; git has none.
* Every hook failure path is ``exit 0``, and the rebuild itself is a detached
  background process with a 600s ``SIGALRM`` timeout that logs to
  ``~/.cache/graphify-rebuild.log`` — a timeout, an OOM, or an ENOENT from a
  reaped worktree all fail invisibly.
* Concurrent triggers coalesce ("Rebuild already in progress — changes
  queued").
* The hooks' own ``[ ! -f graphify-out/graph.json ] && exit 0`` guard is a
  permanent off-switch: purge the graph once and they no-op forever.

So the hooks are an optimization, not the correctness mechanism.  Correctness
comes from a cheap *check*, which is possible because ``GRAPH_REPORT.md``
records the commit it was built from::

    - Built from commit: `5be69d08`

:func:`graph_status` compares that to HEAD.  ``coord diagnose --graph``
surfaces it, so drift shows up in a routine health check instead of being
discovered by a confused agent mid-task.
"""

from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

# ``- Built from commit: `5be69d08` `` in GRAPH_REPORT.md.  This is the only
# machine-readable record of the graph's source commit — manifest.json holds
# per-file hashes, not a commit.
_BUILT_FROM_RE = re.compile(r"^-\s*Built from commit:\s*`([0-9a-fA-F]+)`", re.MULTILINE)

# The versioned hooks directory this repo expects core.hooksPath to point at.
HOOKS_PATH = ".githooks"


@dataclass
class GraphStatus:
    """Freshness of one checkout's graphify graph."""

    repo_path: Path
    present: bool = False
    # True when graphify-out/graph.json is a symlink — i.e. a worktree
    # borrowing the base checkout's graph (the .githooks/post-checkout
    # bootstrap ran).  graphify-out/ itself is always a real directory (its
    # tracked .gitignore has to survive — see #1617); only the entries
    # inside it are symlinked.
    is_symlink: bool = False
    # The base checkout's graphify-out/ directory (graph.json's resolved
    # parent), not graph.json itself — kept as a directory path so existing
    # "owner checkout" math (``link_target.parent``) still lands on the base
    # checkout root.
    link_target: Path | None = None
    built_sha: str | None = None
    head_sha: str | None = None
    in_sync: bool = False
    # HEAD vs origin/<default_branch> — the axis graph<->HEAD alone cannot see
    # (#2211).  The base checkout is fetched but never pulled by design (see
    # module docstring), so HEAD can sit arbitrarily far behind origin while
    # graph == HEAD reports a clean bill of health.  ``default_branch`` is the
    # branch name the comparison used; ``origin_sha`` / ``commits_behind_origin``
    # are ``None`` when it could not be determined (no remote, ref never
    # fetched, or the git call failed) rather than treated as "0 behind".
    default_branch: str | None = None
    origin_sha: str | None = None
    commits_behind_origin: int | None = None
    age_seconds: float | None = None
    # mtime of graphify-out/manifest.json — the last time graphify *checked* the
    # graph against the working tree, whether or not it rewrote anything.
    verified_at: float | None = None
    # Commit timestamp of HEAD, to compare against verified_at.
    head_committed_at: float | None = None
    # Set when we could not determine freshness at all (no report, no git).
    unknown_reason: str | None = None

    @property
    def stamp_behind(self) -> bool:
        """``GRAPH_REPORT.md``'s "Built from commit" differs from HEAD."""
        return bool(self.present and self.built_sha and self.head_sha and not self.in_sync)

    @property
    def verified_current(self) -> bool:
        """graphify has checked the graph against the tree since HEAD landed.

        ``graphify update`` re-extracts, compares topology, and when nothing
        changed prints "No code-graph topology changes detected; outputs left
        untouched" — it deliberately does NOT rewrite ``graph.json`` or
        ``GRAPH_REPORT.md``, so the "Built from commit" stamp stays at whatever
        HEAD was the last time the content actually changed.  It *does* still
        call ``save_manifest``, so ``manifest.json``'s mtime is the honest
        record of "last verified".

        Without this, a checkout whose graph is genuinely current but whose
        stamp is behind would be reported STALE on every run, forever — a
        health check that cries wolf is worse than none.
        """
        if self.verified_at is None or self.head_committed_at is None:
            return False
        return self.verified_at >= self.head_committed_at

    @property
    def stale(self) -> bool:
        """The stamp is behind AND graphify has not verified the graph against
        the tree since HEAD landed.  Deliberately False when freshness is
        *unknown* — an unknown is reported separately, not counted as drift."""
        return self.stamp_behind and not self.verified_current

    @property
    def origin_behind(self) -> bool:
        """HEAD (of the checkout that owns the graph) is behind
        ``origin/<default_branch>`` by at least one commit — i.e. the graph
        may match HEAD exactly and still describe stale code, because HEAD
        itself is stale relative to the remote (#2211).

        False — not True — when this could not be proven (no remote, the ref
        was never fetched, or the git call failed): mirrors ``stale``'s rule
        that an unknown must never be counted as drift.
        """
        return bool(self.commits_behind_origin)


def read_built_sha(report_path: Path) -> str | None:
    """The commit ``GRAPH_REPORT.md`` says the graph was built from."""
    try:
        text = report_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = _BUILT_FROM_RE.search(text)
    return m.group(1) if m else None


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


def _head_sha(repo_path: Path) -> str | None:
    return _git_out(repo_path, "rev-parse", "HEAD")


def _head_committed_at(repo_path: Path) -> float | None:
    raw = _git_out(repo_path, "log", "-1", "--format=%ct", "HEAD")
    try:
        return float(raw) if raw else None
    except ValueError:
        return None


def _commits_ahead(repo_path: Path, base: str, ahead_of: str) -> int | None:
    """Count of commits reachable from *ahead_of* that are not in *base*
    (``git rev-list --count base..ahead_of``).

    Best-effort like the rest of this module: ``None`` (never raised) when
    git can't answer — an unproven comparison must not be reported as drift.
    """
    out = _git_out(repo_path, "rev-list", "--count", f"{base}..{ahead_of}")
    if out is None:
        return None
    try:
        return int(out)
    except ValueError:
        return None


def _shas_agree(built: str, head: str) -> bool:
    """Compare on the shorter of the two — the report abbreviates (8 chars by
    default) while ``git rev-parse HEAD`` is full-length."""
    n = min(len(built), len(head))
    if n < 4:  # too short to be a meaningful comparison
        return False
    return built[:n].lower() == head[:n].lower()


def graph_status(repo_path: Path, default_branch: str = "main") -> GraphStatus:
    """Freshness of the graphify graph for the checkout at *repo_path*.

    Read-only and best-effort: a missing graph, a missing report, or a repo
    git can't read all return a populated :class:`GraphStatus` with
    ``unknown_reason`` set rather than raising.

    *default_branch* names the branch to compare HEAD against on
    ``origin`` (#2211) — pass the repo's configured default branch
    (``coordinator.yml``'s ``default_branch``, "main" if unset).  Never
    fetches: it only reads whatever ``origin/<default_branch>`` the last
    ``git fetch`` left behind, so a checkout that was never fetched or has
    no ``origin`` remote simply leaves ``origin_sha``/``commits_behind_origin``
    unset rather than reporting drift it can't prove.
    """
    st = GraphStatus(repo_path=repo_path)
    out_dir = repo_path / "graphify-out"
    graph_file = out_dir / "graph.json"

    # A borrowed graph symlinks graph.json (and friends) individually;
    # graphify-out/ itself is always a real directory (#1617).
    st.is_symlink = graph_file.is_symlink()
    if st.is_symlink:
        try:
            st.link_target = graph_file.resolve().parent
        except OSError:
            st.link_target = None

    if not graph_file.is_file():
        st.unknown_reason = "no graphify-out/graph.json (graph never built here)"
        return st
    st.present = True

    try:
        st.age_seconds = max(0.0, time.time() - graph_file.stat().st_mtime)
    except OSError:
        st.age_seconds = None

    try:
        st.verified_at = (out_dir / "manifest.json").stat().st_mtime
    except OSError:
        st.verified_at = None

    st.built_sha = read_built_sha(out_dir / "GRAPH_REPORT.md")
    # Freshness is always judged against the checkout that OWNS the graph.  For
    # a symlinked worktree that's the base checkout, not the worktree's own
    # HEAD — the worktree is on a feature branch by definition and comparing
    # against it would report permanent, meaningless drift.
    owner = st.link_target.parent if (st.is_symlink and st.link_target) else repo_path
    st.head_sha = _head_sha(owner)
    st.head_committed_at = _head_committed_at(owner)
    st.default_branch = default_branch

    if not st.built_sha:
        st.unknown_reason = "GRAPH_REPORT.md has no 'Built from commit' line"
    elif not st.head_sha:
        st.unknown_reason = f"could not read HEAD of {owner}"
    else:
        st.in_sync = _shas_agree(st.built_sha, st.head_sha)

    # HEAD vs origin/<default_branch> — independent of the graph<->HEAD
    # comparison above.  Best-effort: no remote, or the ref not fetched yet,
    # just leaves this unset (see docstring).
    if st.head_sha:
        origin_sha = _git_out(owner, "rev-parse", f"origin/{default_branch}")
        if origin_sha:
            st.origin_sha = origin_sha
            st.commits_behind_origin = _commits_ahead(owner, st.head_sha, origin_sha)
    return st


def hooks_path_status(repo_path: Path) -> tuple[bool, str]:
    """``(ok, detail)`` for this checkout's ``core.hooksPath``.

    The versioned ``.githooks/post-checkout`` bootstrap only runs when
    ``core.hooksPath`` points at it — a one-time per-machine ``git config``.
    Without it, worktrees on this machine stay graph-blind and nothing says so.
    """
    try:
        r = subprocess.run(
            ["git", "config", "--get", "core.hooksPath"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=10.0,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        return False, f"could not read core.hooksPath ({exc})"
    value = r.stdout.strip()
    have_hook = (repo_path / HOOKS_PATH / "post-checkout").is_file()
    if not value:
        if not have_hook:
            # The repo doesn't ship the bootstrap at all — telling the operator
            # to point core.hooksPath at a directory that isn't there would
            # silently disable ALL hooks for that checkout.
            return False, (
                f"no {HOOKS_PATH}/post-checkout in this repo — worktrees here get "
                f"no linked graph (port the hook to this repo to enable it)"
            )
        return False, (
            f"core.hooksPath is unset — worktrees on this machine will NOT get "
            f"a linked graph. Fix: git -C {repo_path} config core.hooksPath {HOOKS_PATH}"
        )
    if Path(value).name != Path(HOOKS_PATH).name:
        return False, (
            f"core.hooksPath is {value!r}, expected {HOOKS_PATH!r} — the worktree "
            f"graph bootstrap will not run"
        )
    if not have_hook:
        return False, (
            f"core.hooksPath={value} but {HOOKS_PATH}/post-checkout is missing "
            f"(stale checkout?)"
        )
    orphaned = orphaned_hooks(repo_path)
    if orphaned:
        return False, (
            f"core.hooksPath={value} but {', '.join(orphaned)} exist(s) only in "
            f".git/hooks — git no longer runs them, so those graphify rebuilds "
            f"are SILENTLY DISABLED. Add a shim in {HOOKS_PATH}/"
        )
    return True, f"core.hooksPath={value}"


def orphaned_hooks(repo_path: Path) -> list[str]:
    """Hooks installed in the machine-local hooks dir with no counterpart in
    :data:`HOOKS_PATH`.

    Setting ``core.hooksPath`` makes git ignore ``.git/hooks`` **entirely** —
    it does not merge or fall back.  So any hook graphify installed there
    (``post-commit``, ``post-checkout``, ``post-merge``) that has no shim in
    the versioned directory stops running, with no error and no log line.
    This shipped exactly once: only ``post-checkout`` had a shim, which
    silently killed graphify's commit- and merge-triggered rebuilds.

    Returns hook names, sorted.  Empty when nothing is orphaned (including
    when ``core.hooksPath`` isn't set — then ``.git/hooks`` is live and
    there is nothing to orphan).
    """
    local_dir = _git_out(repo_path, "rev-parse", "--git-common-dir")
    if not local_dir:
        return []
    common = Path(local_dir)
    if not common.is_absolute():
        common = repo_path / common
    hooks_dir = common / "hooks"
    versioned = repo_path / HOOKS_PATH
    if not hooks_dir.is_dir() or not versioned.is_dir():
        return []

    out: list[str] = []
    for entry in hooks_dir.iterdir():
        name = entry.name
        # graphify keeps .bak copies alongside; only real, executable hooks
        # matter, and only ones git would actually invoke.
        if name.endswith(".sample") or name.endswith(".bak") or "." in name:
            continue
        if not entry.is_file():
            continue
        if not (versioned / name).is_file():
            out.append(name)
    return sorted(out)


def format_status_lines(st: GraphStatus) -> list[str]:
    """Human-readable report lines for *st* (used by ``coord diagnose --graph``)."""
    lines: list[str] = []
    where = str(st.repo_path)
    if not st.present:
        lines.append(f"✗ {where}: {st.unknown_reason}")
        return lines

    if st.is_symlink:
        lines.append(f"↳ {where}/graphify-out → {st.link_target} (linked worktree)")

    # #2211: graph == HEAD only proves the graph matches the checkout's own
    # HEAD — it says nothing about whether that HEAD itself is stale relative
    # to origin (the base checkout is fetched but never pulled, by design;
    # see module docstring).  Shared by both "graph matches HEAD" branches
    # below so a genuinely-current-but-unpushed-tracking checkout doesn't
    # report a false ✓.
    origin_note = ""
    if st.origin_behind:
        n = st.commits_behind_origin or 0
        origin_note = (
            f" — HEAD is {n} commit{'' if n == 1 else 's'} behind "
            f"origin/{st.default_branch}; the graph describes stale code "
            f"(fix: review + pull — not automatic, see #2211)"
        )

    if st.unknown_reason:
        lines.append(f"? {where}: freshness unknown — {st.unknown_reason}")
    elif st.in_sync:
        if origin_note:
            lines.append(
                f"⚠ {where}: graph matches HEAD (built from {st.built_sha}){origin_note}"
            )
        else:
            lines.append(f"✓ {where}: graph in sync (built from {st.built_sha})")
    elif st.verified_current:
        # Stamp behind, but graphify has re-checked the tree since HEAD landed
        # and found no topology change — the graph content is current.
        mark = "⚠" if origin_note else "✓"
        lines.append(
            f"{mark} {where}: graph content current — stamp says {st.built_sha} "
            f"(HEAD {(st.head_sha or '')[:8]}), but verified against the tree "
            f"since; graphify leaves outputs untouched when topology is "
            f"unchanged{origin_note}"
        )
    else:
        lines.append(
            f"⚠ {where}: graph is STALE — built from {st.built_sha}, "
            f"HEAD is {(st.head_sha or '')[:8]}"
        )
    if st.age_seconds is not None:
        lines.append(f"    graph.json age: {st.age_seconds / 3600.0:.1f}h")
    return lines
