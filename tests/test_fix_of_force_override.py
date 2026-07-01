"""`--force` overrides the max_review_iterations cap on the interactive
`coord assign --fix-of` path.

The cap (``pipeline.max_review_iterations``) is a safety stop against runaway
fix loops, but intractable stories (e.g. vimcode#515) legitimately need more
rounds.  A hard error with no override is poor UX; ``--force`` is the
operator's explicit "I know, keep going" escape hatch.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from coord.commands.dispatch_workers import _dispatch_fix_of
from coord.config import Config, PipelineConfig
from coord.models import Assignment, Machine, Repo


def _maxed_fix_of_kwargs(tmp_path) -> dict:
    """Kwargs for _dispatch_fix_of targeting a test-failed work already AT the
    iteration cap (the #581 test-fail front door: fix_of is the work id).

    review_iteration == max, so next_iteration (== max + 1) trips the cap.
    """
    cfg = Config(
        repos=[Repo(name="api", github="acme/api", default_branch="main")],
        machines=[
            Machine(
                name="laptop",
                host="laptop.tailnet",
                repos=["api"],
                repo_paths={"api": str(tmp_path)},
            )
        ],
        pipeline=PipelineConfig(
            default_gates=["test", "review", "merge"],
            max_review_iterations=5,
        ),
    )
    work = Assignment(
        machine_name="laptop",
        repo_name="api",
        issue_number=515,
        issue_title="Intractable",
        assignment_id="cbe335b109a6",
        status="done",
        type="work",
        branch="issue-515-intractable",
        test_state="failed",
        test_reason="still broken",
        review_iteration=5,  # next would be 6 > max=5 → cap hit
    )
    board = MagicMock()
    board.find_by_id.return_value = work
    provider = MagicMock()
    provider.build_command.return_value = ["claude", "-p"]

    return dict(
        machine="laptop",
        repo="api",
        issue=515,
        briefing="",
        model=None,
        fix_of="cbe335b109a6",
        cfg=cfg,
        machine_obj=cfg.machines[0],
        repo_cfg=cfg.repo("api"),
        issue_title="Intractable",
        provider=provider,
        _is_local=True,
        _svc=MagicMock(),
        _interactive_board=lambda _loader: board,
        _issue_ctx="",
        _ctx_write_hint="",
    )


def test_fix_of_cap_blocks_without_force(tmp_path):
    """Default behaviour is unchanged: hitting the cap exits non-zero."""
    kwargs = _maxed_fix_of_kwargs(tmp_path)
    with pytest.raises(SystemExit) as exc:
        _dispatch_fix_of(dry_run=True, force=False, **kwargs)
    assert exc.value.code == 2


def test_fix_of_force_overrides_cap(tmp_path):
    """--force pushes past the cap. With --dry-run the function returns before
    launching, so a clean return (no SystemExit) proves the override worked."""
    kwargs = _maxed_fix_of_kwargs(tmp_path)
    # Would raise SystemExit(2) at the cap if --force were not honoured.
    _dispatch_fix_of(dry_run=True, force=True, **kwargs)
