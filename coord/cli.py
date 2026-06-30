"""Click CLI entry point for the `coord` command.

#747: this module just builds the `main`/`agent`/`issue`/`context` groups and
registers the commands implemented in coord/commands/*.py — the actual
command bodies (~70 commands across ~12k lines pre-#747) now live in those
focused modules. Keep this file thin: new commands belong in
coord/commands/<area>.py, imported and attached here in one place.
"""

from __future__ import annotations

# Compatibility shims, not used directly by this file: a number of existing
# tests patch e.g. "coord.cli.subprocess.run" / "coord.cli.socket.gethostname"
# / "coord.cli.httpx.get" / "coord.cli.Path.home". Those patches work by
# replacing an attribute on the *shared* stdlib/third-party module or class
# object (the same object every coord.commands.* module below imports), so
# keeping these imports here — even though cli.py itself no longer calls
# them — keeps those existing test-patch targets resolving. noqa: F401
import os  # noqa: F401
import shutil  # noqa: F401
import socket  # noqa: F401
import subprocess  # noqa: F401
import time  # noqa: F401
from pathlib import Path  # noqa: F401

import click
import httpx  # noqa: F401

from coord import __version__

# Re-exported for back-compat: some tests do `from coord.cli import
# _save_config_snapshot` / `_load_config` / etc. directly.
from coord.commands._common import (  # noqa: F401
    AGENT_PORT,
    SERVE_PORT,
    _apply_label_change,
    _CONFIG_OPTION,
    _load_config,
    _not_implemented,
    _save_config_snapshot,
)

from coord.commands.setup import (
    _ensure_coord_permissions,  # noqa: F401 — re-exported for tests
    _parse_github_remote,  # noqa: F401 — re-exported for tests
    config_cmd,
    init,
    install_skills,
    version,
)
from coord.commands.agent_ops import agent, pause, unpause
from coord.commands.status import diagnose, show_plan, status, usage
from coord.commands.dispatch import (
    approve,
    assign,
    chat_continue,
    inject,
    plan,
    retry,
    stop,
)
from coord.commands.sessions import (
    _prune_dead_sessions,  # noqa: F401 — re-exported for tests
    log,
    pull_artifact,
    reattach,
    session,
    sessions_cmd,
    wait,
    watch,
)
from coord.commands.merge import (
    bounce,
    merge,
    post_pending_reviews,
    reconcile_merges,
    verify_merge,
)
from coord.commands.review import (
    _prompt_and_relay_review_verdict,  # noqa: F401 — re-exported for tests
    fix_briefing_cmd,
    report_result,
    set_review_findings,
)
from coord.commands.test_gate import (
    _get_assignment_branch_head,  # noqa: F401 — re-exported for tests
    set_test_mode,
    test,
    test_cmd,
    test_plan_cmd,
)
from coord.commands.chat import (
    new_issue_chat,
    ready,
    refine,
    refine_board,
    refine_chat,
    test_chat,
)
from coord.commands.issues import (
    backlog,
    context_group,
    issue_group,
    sync,
    track,
    untrack,
)
from coord.commands.lifecycle import done, housekeeping, notify, resume, serve, web
from coord.commands.plan_followup import (
    _dispatch_followup,  # noqa: F401 — re-exported for tests
    approve_plan,
    fix,
    pr,
    reject_plan,
    resume_stuck,
    split,
)


def _warn_if_source_install_drift() -> None:
    """Warn when the CLI is running from a non-editable install of a package
    whose source checkout is the current working directory.

    Root cause of #222: ``pip install .`` (without ``-e``) copies a snapshot
    into site-packages. Subsequent edits in the source tree don't reach the
    CLI, while ``python -c "from coord.... import ..."`` from the source dir
    DOES pick them up (cwd shadows site-packages on import). Result: the same
    workflow gives different answers depending on entry path.

    Heuristic: ``coord.__file__`` lives in ``site-packages`` AND the cwd has
    a sibling ``coord/`` package — that's exactly the drift case.
    """
    import os  # noqa: PLC0415

    try:
        import coord as _coord  # noqa: PLC0415

        coord_file = _coord.__file__ or ""
        if "site-packages" not in coord_file:
            return  # Editable install — source IS the import path, no drift.
        local_init = Path(os.getcwd()) / "coord" / "__init__.py"
        if not local_init.exists():
            return  # Not running from a source checkout.
        # Inside a source checkout but CLI uses snapshot copy → drift possible.
        click.echo(
            "warning: coord CLI is running from a non-editable install "
            "(site-packages snapshot) but a source checkout exists at "
            f"{local_init.parent}.\n"
            "         Edits to the source tree will NOT reach the CLI.  "
            "Fix:  pip install -e .",
            err=True,
        )
    except Exception:  # noqa: BLE001 — best-effort, never break the CLI
        pass


def _warn_if_editable_checkout_moved() -> None:
    """#561/#601 backstop: when running from an EDITABLE checkout, warn loudly if
    its branch was moved off the default.

    A Build/`coord test`/smoke that git-checkout'd the base — or an interactive
    agent inspecting a branch in the live checkout — silently puts the running
    coordinator on that branch's code until restored (#561 incident: disabled
    guards; #601 incident: old code + retired local DB). This makes that state
    visible on every command instead of waiting for a verdict or manual restore.
    """
    import subprocess  # noqa: PLC0415
    import sys as _sys  # noqa: PLC0415

    if "pytest" in _sys.modules:
        return  # don't add startup noise to the test suite
    try:
        import coord as _coord  # noqa: PLC0415

        coord_file = _coord.__file__ or ""
        if "site-packages" in coord_file:
            return  # PyPI/snapshot install — moving a checkout can't affect it.
        repo_root = Path(coord_file).resolve().parents[1]
        if not (repo_root / ".git").exists():
            return
        head = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(repo_root), capture_output=True, text=True, timeout=3,
        )
        if head.returncode != 0:
            return
        branch = head.stdout.strip()
        if branch in ("main", "master"):
            return
        shown = "(detached HEAD)" if branch == "HEAD" else f"'{branch}'"
        click.echo(
            f"⚠ coord: editable checkout {repo_root} is on {shown}, not the "
            "default branch — the running coordinator is on THAT code. A "
            "Build/smoke/test may have checked it out. Restore with:  "
            f"git -C {repo_root} checkout main",
            err=True,
        )
    except Exception:  # noqa: BLE001 — best-effort, never break the CLI
        pass


@click.group(help="Multi-agent coordinator for Claude Code workers.")
@click.version_option(__version__, prog_name="coord")
def main() -> None:
    """coord — coordinate Claude Code workers across machines and repos."""
    _warn_if_source_install_drift()
    _warn_if_editable_checkout_moved()


# Registration order below matches the historical decoration order in the
# pre-#747 cli.py exactly. This matters for one pair: test_cmd ("queue a
# smoke test", registered first) and test ("pull/record verdict", registered
# second) both claim the Click command name "test" — the later add_command
# wins, same as the later @main.command(...) decorator used to win. See
# coord/commands/test_gate.py's module docstring for the (pre-existing, not
# fixed here) detail.
main.add_command(version)
main.add_command(config_cmd)
main.add_command(init)
main.add_command(agent)
main.add_command(status)
main.add_command(plan)
main.add_command(approve)
main.add_command(assign)
main.add_command(log)
main.add_command(show_plan)
main.add_command(inject)
main.add_command(chat_continue)
main.add_command(stop)
main.add_command(report_result)
main.add_command(verify_merge)
main.add_command(set_review_findings)
main.add_command(test_cmd)
main.add_command(retry)
main.add_command(pull_artifact)
main.add_command(bounce)
main.add_command(sync)
main.add_command(pause)
main.add_command(unpause)
main.add_command(refine_chat)
main.add_command(test_chat)
main.add_command(new_issue_chat)
main.add_command(refine_board)
main.add_command(ready)
main.add_command(refine)
main.add_command(reconcile_merges)
main.add_command(housekeeping)
main.add_command(diagnose)
main.add_command(issue_group)
main.add_command(context_group)
main.add_command(fix_briefing_cmd)
main.add_command(track)
main.add_command(untrack)
main.add_command(backlog)
main.add_command(set_test_mode)
main.add_command(notify)
main.add_command(post_pending_reviews)
main.add_command(merge)
main.add_command(resume)
main.add_command(test)  # wins over test_cmd's "test" registration above
main.add_command(test_plan_cmd)
main.add_command(split)
main.add_command(done)
main.add_command(session)
main.add_command(sessions_cmd)
main.add_command(reattach)
main.add_command(usage)
main.add_command(web)
main.add_command(serve)
main.add_command(wait)
main.add_command(watch)
main.add_command(pr)
main.add_command(fix)
main.add_command(approve_plan)
main.add_command(reject_plan)
main.add_command(resume_stuck)
main.add_command(install_skills)
