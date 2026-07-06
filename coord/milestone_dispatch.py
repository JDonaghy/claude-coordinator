"""Milestone dispatch — Phase 1 of #767 (milestone-driven workflow, #769).

Turns Phase 0's pure DAG/frontier (:mod:`coord.milestone_order`) into actual
dispatches: fetch a milestone's tracking-issue context from GitHub, compute
the ready frontier, pick an idle/capable machine for each ready issue, and
dispatch it through the same primitives ``coord assign`` uses
(:func:`coord.dispatch.dispatch` + ``record_dispatched`` + ``post_briefing``)
— no new dispatch mechanism.

Deliberately mechanical / ~zero-Claude-per-decision, matching Phase 0's
design note: machine selection is a plain deterministic filter (idle,
``Machine.can_work_on(repo)``, not routing-paused — the same candidate filter
``coord.reconcile._reassign`` and ``coord.review.pick_reviewer_machine`` use),
not an LLM judgment call like ``coord.brain.propose``.

Three call sites share this module:

- ``coord milestone dispatch`` (``coord/commands/milestone.py``) — the
  one-shot CLI dispatch (bulk or ``--next`` single-pick).
- The daemon's auto-drain tick (``coord.serve_app._milestone_drain_tick``,
  opt-in via ``coordinator.yml`` ``milestone.auto_dispatch``) — re-runs the
  same fetch → plan → dispatch sequence for milestones registered via a
  non-dry-run ``coord milestone dispatch`` call, so newly-unblocked frontier
  entries dispatch automatically as dependencies complete.
- Tests exercise the pure ``plan_dispatch``/``pick_machine`` functions
  directly with a seeded :class:`~coord.models.Board`, no GitHub or HTTP.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

import httpx

from coord.milestone_order import (
    BlockedNode,
    FrontierEntry,
    WorkOrder,
    WorkOrderError,
    parse_work_order,
    ready_frontier,
    validate_milestone_membership,
)
from coord.models import Assignment, Board, Machine, Proposal, Repo

if TYPE_CHECKING:
    from coord.config import Config

__all__ = [
    "MilestoneDispatchError",
    "MilestoneContext",
    "fetch_milestone_context",
    "GateAFileExists",
    "gate_a_status",
    "pick_machine",
    "MachinePick",
    "NoMachineAvailable",
    "MilestonePlan",
    "plan_dispatch",
    "DispatchOutcome",
    "dispatch_entry",
    "is_milestone_complete",
]


class MilestoneDispatchError(Exception):
    """A milestone's tracking-issue context could not be fetched or is invalid.

    Covers the same failure modes as ``coord milestone order``'s inline
    error handling (GitHub fetch failure, no milestone on the tracking
    issue, a malformed ``## Work order`` block, or a node that isn't a
    member of the milestone) but as a plain exception rather than
    ``click.echo`` + ``sys.exit`` — so both the CLI and the daemon tick can
    catch it and decide how to report it themselves.
    """


@dataclass(frozen=True)
class MilestoneContext:
    """The fetched + validated inputs to :func:`plan_dispatch`."""

    tracking_issue: int
    milestone_number: int
    work_order: WorkOrder
    terminal_issues: frozenset[int] = field(default_factory=frozenset)


def fetch_milestone_context(repo_cfg: Repo, tracking_issue: int) -> MilestoneContext:
    """Fetch the tracking issue, parse its work order, and resolve terminal state.

    Shared by ``coord milestone order`` and ``coord milestone dispatch`` (and
    the daemon's auto-drain tick) so all three compute the frontier from
    identical inputs. Raises :class:`MilestoneDispatchError` on any fetch,
    parse, or membership-validation failure.
    """
    from coord import github_ops  # noqa: PLC0415

    try:
        issue_data = github_ops.get_issue(repo_cfg.github, tracking_issue)
    except RuntimeError as e:
        raise MilestoneDispatchError(f"could not fetch #{tracking_issue}: {e}") from e

    milestone = issue_data.get("milestone") or {}
    milestone_number = milestone.get("number")
    if milestone_number is None:
        raise MilestoneDispatchError(f"#{tracking_issue} has no milestone")

    body = issue_data.get("body") or ""
    try:
        work_order = parse_work_order(body)
    except WorkOrderError as e:
        raise MilestoneDispatchError(str(e)) from e

    if not work_order.nodes:
        return MilestoneContext(
            tracking_issue=tracking_issue,
            milestone_number=milestone_number,
            work_order=work_order,
            terminal_issues=frozenset(),
        )

    # Membership + terminal state — mirrors coord/commands/milestone.py's
    # original inline logic (Phase 0): issues currently open under the
    # milestone come free from one `get_open_issues` call; anything a node
    # references that isn't in that set gets an individual lookup (closed,
    # or foreign).
    open_issues = github_ops.get_open_issues(repo_cfg.github)
    milestone_issue_numbers = {
        i["number"]
        for i in open_issues
        if (i.get("milestone") or {}).get("number") == milestone_number
    }
    terminal_issues: set[int] = set()
    for node in work_order.nodes:
        if node.issue_number in milestone_issue_numbers:
            continue
        try:
            node_data = github_ops.get_issue(repo_cfg.github, node.issue_number)
        except RuntimeError as e:
            raise MilestoneDispatchError(
                f"could not fetch #{node.issue_number}: {e}"
            ) from e
        node_milestone_number = (node_data.get("milestone") or {}).get("number")
        if node_milestone_number == milestone_number:
            milestone_issue_numbers.add(node.issue_number)
        if node_data.get("state", "").upper() == "CLOSED":
            terminal_issues.add(node.issue_number)

    try:
        validate_milestone_membership(work_order, milestone_issue_numbers)
    except WorkOrderError as e:
        raise MilestoneDispatchError(str(e)) from e

    return MilestoneContext(
        tracking_issue=tracking_issue,
        milestone_number=milestone_number,
        work_order=work_order,
        terminal_issues=frozenset(terminal_issues),
    )


def is_milestone_complete(ctx: MilestoneContext) -> bool:
    """Whether every node in the work order has reached a terminal state."""
    return all(
        n.issue_number in ctx.terminal_issues for n in ctx.work_order.nodes
    )


# (repo_github, path, branch) -> True if the file exists at that ref.
# Injected so tests never hit `gh` — mirrors ``coord.claim``'s BranchLookup.
GateAFileExists = Callable[[str, str, str], bool]


def _default_gate_a_file_exists(repo_github: str, path: str, branch: str) -> bool:
    from coord import github_ops  # noqa: PLC0415

    try:
        github_ops.get_repo_file(repo_github, path, branch=branch)
        return True
    except RuntimeError:
        return False


def gate_a_status(
    repo_cfg: Repo,
    config: "Config",
    milestone_number: int,
    *,
    file_exists: GateAFileExists | None = None,
) -> str | None:
    """Gate A (docs/ORACLE_LOOP.md, #930): a milestone's issues may not
    dispatch until its black-box contract exists.

    Returns ``None`` when dispatch may proceed — either the repo has no
    ``acceptance.drivers`` entry configured (Gate A is an oracle-loop
    concept; repos outside that model dispatch exactly as before #930), or
    the contract file already exists on the repo's default branch. Returns a
    human-readable block reason otherwise, naming the missing path and the
    command that produces it.
    """
    if config.acceptance.driver_for(repo_cfg.name) is None:
        return None

    from coord.acceptance import gate_a_contract_path  # noqa: PLC0415

    path = gate_a_contract_path(milestone_number)
    check = file_exists or _default_gate_a_file_exists
    if check(repo_cfg.github, path, repo_cfg.default_branch):
        return None
    return (
        f"Gate A not satisfied: {path!r} does not exist yet on "
        f"{repo_cfg.default_branch!r}. Run `coord acceptance mock {repo_cfg.name} "
        "<tracking_issue>` (docs/ORACLE_LOOP.md) to render the mock + write "
        "the contract before dispatching this milestone's issues."
    )


def pick_machine(
    repo_name: str,
    board: Board,
    config: "Config",
    *,
    exclude: frozenset[str] = frozenset(),
) -> Machine | None:
    """Deterministically pick an idle, capable, unpaused machine for *repo_name*.

    Mirrors the candidate filter ``coord.reconcile._reassign`` and
    ``coord.review.pick_reviewer_machine`` already use: idle (no running
    assignment on the board), lists *repo_name* in its ``repos:`` (this is
    what keeps coord-self work off a machine like dellserver whose
    ``coordinator.yml`` entry omits ``claude-coordinator`` — #688), has a
    configured ``repo_paths`` entry, and isn't routing-paused
    (``coord pause``). First match wins in ``config.machines`` order — no
    scoring, no LLM.

    Deliberately computes "busy" from ``board.active`` directly (like
    ``_reassign``/``pick_reviewer_machine`` do) rather than via
    ``Board.idle_machines()``, which filters ``board.machines`` — a separate
    DB-synced snapshot that isn't guaranteed to be populated on every board
    read path. ``config.machines`` is the authoritative machine list here.
    """
    from coord.machine_pause import paused_set  # noqa: PLC0415

    busy = {a.machine_name for a in board.active if a.status == "running"}
    paused = paused_set()
    for m in config.machines:
        if m.name in exclude:
            continue
        if m.name in busy:
            continue
        if m.name in paused:
            continue
        if not m.can_work_on(repo_name):
            continue
        if m.repo_path(repo_name) is None:
            continue
        return m
    return None


@dataclass(frozen=True)
class MachinePick:
    """A ready-frontier entry paired with the machine it would dispatch to."""

    entry: FrontierEntry
    machine: Machine


@dataclass(frozen=True)
class NoMachineAvailable:
    """A ready-frontier entry with nowhere to dispatch it *right now*.

    Distinct from :class:`~coord.milestone_order.BlockedNode` — the frontier
    itself considers this entry ready (dependencies satisfied, unclaimed,
    unconflicted); it just has no idle capable machine this tick. It will be
    reconsidered on the next ``coord milestone dispatch`` / daemon tick.
    """

    entry: FrontierEntry
    reason: str = "no idle machine available for this repo"


@dataclass(frozen=True)
class MilestonePlan:
    """The result of :func:`plan_dispatch`: what to dispatch now, what's
    idle-machine-starved, and what Phase 0's frontier says is still blocked.
    """

    to_dispatch: tuple[MachinePick, ...] = ()
    skipped: tuple[NoMachineAvailable, ...] = ()
    waiting: tuple[BlockedNode, ...] = ()


def plan_dispatch(
    work_order: WorkOrder,
    board: Board,
    config: "Config",
    repo_cfg: Repo,
    terminal_issues: frozenset[int] | set[int],
) -> MilestonePlan:
    """Compute the ready frontier and pick a machine for each ready entry.

    Pure — no GitHub/HTTP calls, no dispatch side effects. Greedily assigns
    each :class:`~coord.milestone_order.FrontierEntry` in frontier order to
    the first idle+capable machine not already claimed by an earlier entry
    in *this* call (so a cohort of N ready issues fans out across up to N
    distinct idle machines instead of piling onto one).
    """
    frontier = ready_frontier(
        work_order,
        board,
        repo_name=repo_cfg.name,
        repo_github=repo_cfg.github,
        terminal_issues=set(terminal_issues),
    )
    picks: list[MachinePick] = []
    skipped: list[NoMachineAvailable] = []
    used: set[str] = set()
    for entry in frontier.ready:
        machine = pick_machine(repo_cfg.name, board, config, exclude=frozenset(used))
        if machine is None:
            skipped.append(NoMachineAvailable(entry))
            continue
        used.add(machine.name)
        picks.append(MachinePick(entry, machine))
    return MilestonePlan(
        to_dispatch=tuple(picks), skipped=tuple(skipped), waiting=frontier.blocked
    )


@dataclass(frozen=True)
class DispatchOutcome:
    """The result of one :func:`dispatch_entry` call."""

    issue_number: int
    machine_name: str
    ok: bool
    assignment_id: str | None = None
    error: str | None = None


def dispatch_entry(
    pick: MachinePick,
    repo_cfg: Repo,
    config: "Config",
    board: Board,
    *,
    tracking_issue: int | None = None,
) -> DispatchOutcome:
    """Dispatch one ready-frontier entry to its picked machine.

    Mirrors ``coord.commands.dispatch_workers._dispatch_headless``'s logic
    (build a :class:`~coord.models.Proposal` → defensive claim recheck →
    :func:`coord.dispatch.dispatch` → ``record_dispatched`` →
    ``post_briefing``) without its ``click.echo``/``sys.exit`` coupling, so
    it's usable from both ``coord milestone dispatch`` and the daemon's
    auto-drain tick.

    On success, appends a lightweight ``running`` :class:`~coord.models.
    Assignment` stub to *board*'s ``active`` list in place — so a caller
    dispatching several entries (or several milestones) in the same batch
    sees the machine as busy for the *next* :func:`plan_dispatch` /
    :func:`pick_machine` call without re-reading the board over the network.
    This does not itself persist the board; ``record_dispatched`` already
    wrote the real assignment row.

    Re-checks :func:`coord.claim.find_work_claim` immediately before
    dispatching (defense-in-depth against the frontier snapshot going stale
    between planning and dispatch — e.g. a race with a manual `coord
    assign`), matching the same check ``_dispatch_headless`` performs.
    """
    from coord import github_ops  # noqa: PLC0415
    from coord.claim import claim_message, find_work_claim  # noqa: PLC0415
    from coord.dispatch import dispatch, post_briefing  # noqa: PLC0415
    from coord.state import record_dispatched  # noqa: PLC0415

    issue_number = pick.entry.issue_number
    machine = pick.machine

    claim = find_work_claim(issue_number, repo_cfg.name, repo_cfg.github, board)
    if claim is not None:
        return DispatchOutcome(
            issue_number=issue_number,
            machine_name=machine.name,
            ok=False,
            error=claim_message(claim),
        )

    try:
        issue_data = github_ops.get_issue(repo_cfg.github, issue_number)
    except RuntimeError as e:
        return DispatchOutcome(
            issue_number=issue_number,
            machine_name=machine.name,
            ok=False,
            error=f"could not fetch #{issue_number}: {e}",
        )
    issue_title = issue_data.get("title", f"Issue #{issue_number}")
    issue_body = issue_data.get("body") or ""
    briefing = f"Issue #{issue_number}: {issue_title}\n\n{issue_body}"
    if tracking_issue is not None:
        group_note = f" (group {pick.entry.group})" if pick.entry.group else ""
        briefing += (
            "\n\n---\nDispatched by `coord milestone dispatch` as part of the "
            f"declared work order in #{tracking_issue}{group_note}."
        )

    issue_labels = [lbl.get("name", "") for lbl in (issue_data.get("labels") or [])]
    required_gates = list(config.pipeline.default_gates)
    for lbl in issue_labels:
        if lbl in config.pipeline.labels:
            required_gates = list(config.pipeline.labels[lbl])
            break

    proposal = Proposal(
        id=0,
        machine_name=machine.name,
        repo_name=repo_cfg.name,
        issue_number=issue_number,
        issue_title=issue_title,
        rationale="milestone work-order dispatch (coord milestone dispatch)",
        briefing=briefing,
        model=config.models.default,
        type="plan" if config.dispatch.require_plan else "work",
        required_gates=required_gates,
    )

    try:
        response = dispatch(proposal, config)
    except (httpx.HTTPError, ValueError) as e:
        return DispatchOutcome(
            issue_number=issue_number, machine_name=machine.name, ok=False, error=str(e)
        )

    assignment_id = response.get("id", "pending")
    record_dispatched(
        assignment_id=assignment_id,
        proposal=proposal,
        repo_github=repo_cfg.github,
        provider_name=response.get("_provider_name"),
    )

    try:
        post_briefing(proposal, config, assignment_id=assignment_id, do_not_touch=())
    except Exception:  # noqa: BLE001 — best-effort, mirrors _dispatch_headless
        pass

    board.active.append(
        Assignment(
            machine_name=machine.name,
            repo_name=repo_cfg.name,
            issue_number=issue_number,
            issue_title=issue_title,
            assignment_id=str(assignment_id),
            status="running",
            type=proposal.type,
        )
    )

    return DispatchOutcome(
        issue_number=issue_number,
        machine_name=machine.name,
        ok=True,
        assignment_id=str(assignment_id),
    )
