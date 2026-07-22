"""Dispatch approved assignments to agent servers and post briefings."""

from __future__ import annotations

import time
from typing import Iterable

import httpx

from coord import github_ops
from coord.comments import (
    format_advisory,
    format_briefing,
    format_completion,
    format_failure,
)
from coord.config import Config
from coord.models import Proposal, Repo

AGENT_PORT = 7433


def enforce_oracle_readiness(
    *, proposal_type: str, repo: Repo | None, config: Config, issue_number: int,
) -> None:
    """#1138: hard-gate a ``type="work"`` dispatch on the issue-level oracle
    gate (:func:`coord.milestone_dispatch.issue_oracle_ready`) — refuses an
    issue that belongs to an oracle-opted-in milestone (Gate A already
    satisfied) but has no JIT-authored acceptance slice yet, or whose repo
    declares a driver ``kind`` this install doesn't implement.

    Raises :class:`ValueError` on refusal — the same exception type every
    existing dispatch-time check (missing ``repo_path``, the #437 TOS gate)
    already raises, so callers get "refuse cleanly" for free via their
    existing ``except ValueError`` handling (``coord approve``, ``coord
    assign``, ``coord milestone dispatch``) with zero CLI-layer changes.

    Cheap no-op — no network call — for every dispatch outside #1138's
    scope: non-work proposal types (``plan``, ``review``, ``smoke``, ...),
    an unknown repo, or a repo with no ``acceptance.drivers`` entry
    configured (``has_driver`` is a local dict lookup). Shared by the
    headless ``dispatch()`` POST below and the ``--interactive``
    human-attended work launcher (``_dispatch_interactive_work``, which
    never calls ``dispatch()``) so both flavours of Work dispatch are
    covered, not just the unattended one.

    Fails OPEN (proceeds, doesn't gate) if the issue itself can't be
    fetched — mirroring the fail-soft posture the rest of the oracle-loop
    machinery already uses (``oracle_loop_contract_block``, #945: "never let
    a [...] read break dispatch"). By the time ``dispatch()`` runs, the
    caller has already successfully fetched this same issue once (for its
    title/briefing) moments earlier, so a failure here is a genuine
    transient blip, not a sign the issue doesn't exist — treating it as a
    hard stop would turn a GitHub hiccup into a fleet-wide outage for every
    oracle-configured repo, which is a worse failure mode than the gap
    #1138 closes.
    """
    if proposal_type != "work" or repo is None:
        return
    if not config.acceptance.has_driver(repo.name):
        return

    from coord import github_ops  # noqa: PLC0415
    from coord.milestone_dispatch import issue_oracle_ready  # noqa: PLC0415

    try:
        issue_data = github_ops.get_issue(repo.github, issue_number)
    except RuntimeError:
        return

    milestone_number = (issue_data.get("milestone") or {}).get("number")
    issue_labels = [lbl.get("name", "") for lbl in (issue_data.get("labels") or [])]

    readiness = issue_oracle_ready(
        repo, config, milestone_number, issue_number, issue_labels,
    )
    if readiness.reason is not None:
        raise ValueError(readiness.reason)


def enforce_epic_dispatch_guard(
    *, proposal_type: str, repo: Repo | None, config: Config, issue_number: int,
) -> None:
    """#1314: refuse a dispatch that would auto-close an epic/tracking issue
    on merge (``proposal_type`` in :data:`coord.models.CLOSES_ISSUE_TYPES`,
    e.g. ``"work"``) when *issue_number* itself carries the ``"epic"``
    label (:data:`coord.milestone_order.TRACKING_ISSUE_LABEL`).

    The #1077/#1142 ``CLOSES_ISSUE_TYPES`` split (see ``coord/models.py``)
    already assumes only ``mock-author``/``test-author`` are ever dispatched
    directly against a tracking issue's own number — a small correction to
    an already-merged Gate-A contract, with no properly-typed tool for it
    yet, falls back to a plain ``coord assign`` (``type="work"``) instead.
    That silently breaks the same assumption a ``type="work"`` merge relies
    on everywhere else: that ``issue_number`` is real, resolvable work, not
    a milestone's tracking issue. Hit in practice against epic #1120's Gate
    A contract (PR #1312) — this is the dispatch-time half of the fix;
    ``coord/commands/plan_followup.py``'s ``pr()`` command independently
    checks the same label so the PR body never carries the closing keyword
    even for an already-dispatched assignment.

    Override: label the issue ``oracle:exempt`` (the existing "I know what
    I'm doing, let this bypass oracle-loop-specific gating" signal — see
    :func:`enforce_oracle_readiness`) to dispatch anyway. Raises
    :class:`ValueError` on refusal, same as every other dispatch-time gate
    here, so callers get "refuse cleanly" for free via their existing
    ``except ValueError`` handling.

    Fails OPEN (proceeds) if the issue can't be fetched or *repo* is
    ``None`` — mirrors :func:`enforce_oracle_readiness`'s posture; a
    transient GitHub read failure must not turn into a fleet-wide dispatch
    outage.

    Scoped to repos with an ``acceptance.drivers`` entry configured (same
    cheap no-op test :func:`enforce_oracle_readiness` uses) — a local dict
    lookup, no network call, for every dispatch outside this scope. #1314's
    actual failure mode is inherent to the oracle loop's own convention of
    dispatching ``mock-author``/``test-author`` against a tracking issue's
    number in the first place; a repo with no acceptance driver has no such
    convention, so this intentionally does not add a `gh` round-trip to
    every "work" dispatch fleet-wide for a scenario that can't arise there.
    A plain (non-oracle) repo whose operator manually dispatches "work"
    against an epic's own number is a real but separate gap, same as the
    one #1138's oracle-readiness gate already accepts for the same reason.
    """
    from coord.models import CLOSES_ISSUE_TYPES  # noqa: PLC0415

    if proposal_type not in CLOSES_ISSUE_TYPES or repo is None:
        return
    if not config.acceptance.has_driver(repo.name):
        return

    from coord.milestone_order import TRACKING_ISSUE_LABEL  # noqa: PLC0415

    try:
        issue_data = github_ops.get_issue(repo.github, issue_number)
    except RuntimeError:
        return

    issue_labels = {lbl.get("name", "") for lbl in (issue_data.get("labels") or [])}
    if TRACKING_ISSUE_LABEL not in issue_labels or "oracle:exempt" in issue_labels:
        return

    raise ValueError(
        f"refusing type={proposal_type!r} dispatch against #{issue_number}: it "
        f"carries the {TRACKING_ISSUE_LABEL!r} label (a milestone tracking/epic "
        "issue) — merging this would close the epic while its real sub-issues "
        "stay open/untouched (#1314). If this is a deliberate meta-level "
        "dispatch against the tracking issue's own number (e.g. a Gate-A "
        "contract correction), label the issue 'oracle:exempt' to override, "
        "or use a properly-typed dispatch (e.g. mock-author) instead."
    )


def dispatch(
    proposal: Proposal,
    config: Config,
    *,
    pull_repos: Iterable[str] = (),
    fresh_branch: bool = False,
) -> dict:
    """POST an assignment to the agent server on the target machine.

    Returns the response JSON from the agent server (which includes the
    server-assigned `id`).
    """
    machine = next(
        (m for m in config.machines if m.name == proposal.machine_name), None
    )
    if machine is None:
        raise ValueError(f"Unknown machine: {proposal.machine_name!r}")

    repo_path = machine.repo_path(proposal.repo_name)
    if repo_path is None:
        raise ValueError(
            f"No repo_path configured for {proposal.repo_name!r} on machine {machine.name!r}. "
            f"Add it to coordinator.yml under machines[].repo_paths."
        )

    # Resolve deny-list from the repo's worker_permissions config.
    repo = config.repo(proposal.repo_name)

    # #1138: STRUCTURAL ORACLE-LOOP GATE — refuse a `type="work"` dispatch
    # for an issue inside an oracle-opted-in milestone (Gate A satisfied)
    # that has no JIT-authored acceptance slice yet, or whose repo declares
    # a driver kind this install doesn't implement. Placed early / before
    # the TOS gate below so a refusal never depends on provider resolution
    # succeeding first.
    enforce_oracle_readiness(
        proposal_type=proposal.type, repo=repo, config=config,
        issue_number=proposal.issue_number,
    )

    # #1314: STRUCTURAL EPIC-TARGET GATE — refuse a dispatch that would
    # auto-close a tracking/epic issue on merge (see
    # `enforce_epic_dispatch_guard`'s docstring). Placed alongside the
    # oracle-readiness gate above, before the TOS gate, for the same reason.
    enforce_epic_dispatch_guard(
        proposal_type=proposal.type, repo=repo, config=config,
        issue_number=proposal.issue_number,
    )

    # #437: STRUCTURAL TOS-COMPLIANCE GATE — refuse to route an
    # unattended dispatch through a provider whose capabilities mark it
    # ``human_attended_only`` (subscription-billed interactive Claude
    # Code).  Precedence: per-proposal override (if the brain ever sets
    # one) → per-repo ``Repo.provider`` → ``config.providers.default``.
    # Deferred import so the unattended dispatch surface stays free of a
    # module-level cycle with the provider registry.
    from coord.providers import guard_unattended_dispatch  # noqa: PLC0415
    spec_provider = getattr(proposal, "provider", None)
    # #324: resolve the effective provider name (spec > repo > default) so
    # the coordinator DB always records the winning provider regardless of
    # which level supplied it, and the wire payload carries the exact name
    # the agent should look up in its registry.
    effective_provider_name: str = guard_unattended_dispatch(
        spec_provider=spec_provider,
        repo_provider=repo.provider if repo is not None else None,
        providers_cfg=config.providers,
        models_cfg=config.models,
        where="coord approve / dispatch",
    )
    deny_commands: list[str] = []
    if repo is not None and repo.worker_permissions is not None:
        deny_commands = repo.worker_permissions.deny

    # Resolve coordinator-only files (workers must not read or modify these).
    files_forbidden: list[str] = []
    if repo is not None and repo.coordinator_only_files:
        files_forbidden = list(repo.coordinator_only_files)

    # #944 sealing v1 (docs/ORACLE_LOOP.md): the acceptance oracle is
    # read-only/run-only for the worker — it's authored by an independent
    # test-author, not the worker under test. Auto-forbid it for any repo
    # with an acceptance driver configured, so sealing doesn't depend on an
    # operator remembering to also list it under coordinator_only_files.
    # #930: exempt `mock-author` — the one type whose entire job IS writing
    # under tests/acceptance/ms-NN/ (Gate A). A future `test-author` (#931)
    # gets the same exemption when it lands.
    if (
        proposal.type != "mock-author"
        and config.acceptance.has_driver(proposal.repo_name)
    ):
        if "tests/acceptance/" not in files_forbidden:
            files_forbidden.append("tests/acceptance/")

    # Resolve model: proposal override → config default → None (let claude pick).
    # The board/DB stores the alias for legibility; only the wire payload is
    # translated to an exact model id via models.versions (when configured).
    model = proposal.model if proposal.model else config.models.default
    wire_model = config.models.resolve(model)

    # #255: pin the worker's branch base to the repo's configured default
    # branch.  Without this the agent fell back to a hardcoded "main", which
    # silently routed around `default_branch: develop` repos like quadraui
    # and let local-only commits on the default branch slip into worker
    # branches.
    #
    # #934: when the target issue belongs to a milestone (`proposal.
    # milestone_number`, set by callers like `coord.milestone_dispatch.
    # dispatch_entry` that already fetched the issue) and the repo has
    # opted into the develop + feature-branch-per-milestone git model
    # (`repo.develop_branch` set), branch off `feature/ms-NN` instead —
    # `coord.branch_model.resolve_base_branch` falls back to today's flat
    # `default_branch` behavior for every other repo/proposal.
    from coord.branch_model import resolve_base_branch  # noqa: PLC0415

    if repo is not None:
        default_branch = resolve_base_branch(repo, proposal.milestone_number)
    else:
        default_branch = "main"

    # #305: artifact_paths are only relevant for work assignments.  Skip for
    # review, smoke, refinement, and other non-work types.
    artifact_paths: list[str] = []
    if proposal.type == "work" and repo is not None:
        artifact_paths = list(repo.artifact_paths)

    # #352: resolve new-issue guidance for new-issue-chat assignments.
    # Only resolve when the repo *explicitly configured* new_issue_guidance —
    # the resolver always returns a non-empty _DEFAULT, so checking
    # `if new_issue_guidance:` below would always send the field, causing
    # agents that predate #352 to reject the payload with a 400.  Gating on
    # the raw config field lets repos without guidance dispatch to any agent
    # (the agent's built-in NEW_ISSUE_CHAT_SYSTEM_PROMPT is fine without it).
    new_issue_guidance: str = ""
    if proposal.type == "new-issue-chat" and repo is not None and repo.new_issue_guidance:
        from pathlib import Path
        new_issue_guidance = repo.resolve_new_issue_guidance(Path(repo_path).expanduser())

    # #603: prepend the per-issue context digest to the TOP of a -p WORK
    # briefing (cross-repo deps / prior-attempt findings) so the worker reads
    # them first.  Only `work` (chat/refinement/conflict-fix carry no issue
    # context); the interactive and auto-loop fix/review paths inject at their
    # own sites, so this is the single -p work chokepoint (no double injection).
    #
    # #945 (docs/ORACLE_LOOP.md "The worker briefing contract"): right after
    # the #603 digest, prepend the oracle-loop contract when this repo has an
    # acceptance driver configured (the oracle-loop proxy — #944 never landed
    # a milestone-level flag, so "driver configured for this repo" is the
    # signal, mirroring the tests/acceptance/ auto-seal above) AND this issue
    # already has an authored slice (oracle_loop_contract_block returns ""
    # otherwise, e.g. before Gate A/#931 has run for it).
    briefing_text = proposal.briefing
    if proposal.type == "work" and proposal.issue_number:
        from pathlib import Path  # noqa: PLC0415

        from coord.state import issue_context_block  # noqa: PLC0415

        oracle_contract = ""
        if config.acceptance.has_driver(proposal.repo_name):
            from coord.acceptance import (  # noqa: PLC0415
                ACCEPTANCE_DIRNAME,
                oracle_loop_contract_block,
            )

            oracle_contract = oracle_loop_contract_block(
                Path(repo_path).expanduser() / ACCEPTANCE_DIRNAME,
                proposal.repo_name,
                proposal.issue_number,
            )

        briefing_text = (
            issue_context_block(proposal.repo_name, proposal.issue_number)
            + oracle_contract
            + briefing_text
        )

    url = f"http://{machine.host}:{AGENT_PORT}/assign"
    payload: dict = {
        "repo_name": proposal.repo_name,
        "repo_path": repo_path,
        "issue_number": proposal.issue_number,
        "issue_title": proposal.issue_title,
        "briefing": briefing_text,
        "files_allowed": proposal.files_likely,
        "files_forbidden": files_forbidden,
        "pull_repos": list(pull_repos),
        "deny_commands": deny_commands,
        "model": wire_model,
        "type": proposal.type,
        "branch": default_branch,
    }
    # #351: only send artifact_paths when non-empty — older agents reject
    # unknown payload keys with a 400.  When absent the agent falls back to
    # self.artifact_paths (startup config).
    if artifact_paths:
        payload["artifact_paths"] = artifact_paths
    # #352: only send new_issue_guidance when non-empty — older agents don't
    # have this field and will reject the payload with a 400.
    if new_issue_guidance:
        payload["new_issue_guidance"] = new_issue_guidance
    # Only send fresh_branch when True — older agents don't have this field
    # and will reject the payload with a 400.
    if fresh_branch:
        payload["fresh_branch"] = True
    # Only send target_branch when set — agents predating #target_branch
    # (and the AssignmentSpec(**body) kwargs check) reject unknown fields.
    if proposal.target_branch:
        payload["target_branch"] = proposal.target_branch
    # #315: only send resume_session_id when set — older agents without the
    # field reject unknown payload keys with a 400.
    if getattr(proposal, "resume_session_id", None):
        payload["resume_session_id"] = proposal.resume_session_id
    # #324: send the resolved provider name when it differs from the implicit
    # default ("claude").  Older agents that don't know about providers ignore
    # the field (AssignmentSpec(**body) accepts unknown kwargs since Python
    # 3.12 dataclasses don't reject extras — actually they DO reject extras,
    # but spec.provider was added in #425 so agents with that field already
    # accept it).  When the effective provider IS "claude", omitting the
    # field keeps the wire payload identical to pre-#324 for all default-
    # configured deployments (no-config parity requirement).
    if effective_provider_name and effective_provider_name != "claude":
        payload["provider"] = effective_provider_name

    resp = httpx.post(url, json=payload, timeout=15)
    resp.raise_for_status()
    result = resp.json()
    # #324: attach the resolved provider name to the response dict so callers
    # that record the dispatched assignment (cli.py, dashboard/server.py) can
    # persist it without re-resolving the config precedence chain.
    result["_provider_name"] = effective_provider_name
    return result


def dispatch_with_retry(
    proposal: Proposal,
    config: Config,
    *,
    max_retries: int = 3,
    backoff_base: float = 60.0,
    pull_repos: Iterable[str] = (),
    fresh_branch: bool = False,
    on_retry: callable | None = None,
) -> dict:
    """Dispatch with exponential backoff on transient failures."""
    from coord.network import classify_error, is_retryable

    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return dispatch(proposal, config, pull_repos=pull_repos, fresh_branch=fresh_branch)
        except httpx.HTTPError as exc:
            state, reason = classify_error(exc)
            if not is_retryable(state) or attempt == max_retries:
                raise
            wait = backoff_base * (2 ** attempt)
            if on_retry:
                on_retry(attempt + 1, max_retries, state, reason, wait)
            time.sleep(wait)
            last_exc = exc
        except ValueError:
            raise
    raise last_exc  # unreachable, but satisfies type checker


def compute_do_not_touch(
    proposal: Proposal,
    peers: Iterable[Proposal],
    in_flight: Iterable[dict] = (),
) -> list[tuple[str, str]]:
    """Compute (file, reason) pairs for other work touching `proposal.repo_name`.

    `peers` are other proposals being dispatched in the same batch.
    `in_flight` are records loaded from ~/.coord/dispatched.json (each with
    keys: machine_name, repo_name, files_likely).
    """
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def _add(machine_name: str, files: Iterable[str]) -> None:
        for f in files:
            key = (machine_name, f)
            if key in seen:
                continue
            seen.add(key)
            pairs.append((f, f"{machine_name} is working there"))

    for peer in peers:
        if peer is proposal:
            continue
        if peer.repo_name != proposal.repo_name:
            continue
        _add(peer.machine_name, peer.files_likely)

    for record in in_flight:
        if record.get("repo_name") != proposal.repo_name:
            continue
        if record.get("machine_name") == proposal.machine_name:
            continue
        _add(record.get("machine_name", "?"), record.get("files_likely", []))

    return pairs


def post_briefing(
    proposal: Proposal,
    config: Config,
    *,
    assignment_id: str = "pending",
    do_not_touch: Iterable[tuple[str, str]] = (),
) -> None:
    """Post the assignment briefing as a GitHub issue comment."""
    repo = config.repo(proposal.repo_name)
    if repo is None:
        raise ValueError(f"Unknown repo: {proposal.repo_name!r}")

    body = format_briefing(
        assignment_id=assignment_id,
        machine_name=proposal.machine_name,
        repo_name=proposal.repo_name,
        issue_number=proposal.issue_number,
        briefing=proposal.briefing,
        files_likely=proposal.files_likely,
        do_not_touch=do_not_touch,
    )
    github_ops.post_issue_comment(repo.github, proposal.issue_number, body)

    # Auto-tag the issue with pipeline_tracked_labels so the TUI's Pipeline
    # panel picks it up on the next `gh search issues` poll.  Without this,
    # manually filed issues stay invisible until the user remembers to
    # label them (we hit this filing quadraui#263).  Best-effort — never
    # fail the briefing post on a labeling error.
    tracked = config.pipeline.tracked_labels()
    if tracked:
        try:
            github_ops.add_issue_labels(repo.github, proposal.issue_number, tracked)
        except (RuntimeError, OSError):
            pass


def post_completion(
    *,
    assignment_id: str,
    machine_name: str,
    repo_github: str,
    repo_name: str,
    issue_number: int,
    exit_code: int,
    duration_seconds: float | None = None,
    log_path: str | None = None,
    summary: str = "",
) -> None:
    body = format_completion(
        assignment_id=assignment_id,
        machine_name=machine_name,
        repo_name=repo_name,
        issue_number=issue_number,
        exit_code=exit_code,
        duration_seconds=duration_seconds,
        log_path=log_path,
        summary=summary,
    )
    github_ops.post_issue_comment(repo_github, issue_number, body)


def post_failure(
    *,
    assignment_id: str,
    machine_name: str,
    repo_github: str,
    repo_name: str,
    issue_number: int,
    exit_code: int | None,
    duration_seconds: float | None = None,
    log_path: str | None = None,
    error: str = "",
) -> None:
    body = format_failure(
        assignment_id=assignment_id,
        machine_name=machine_name,
        repo_name=repo_name,
        issue_number=issue_number,
        exit_code=exit_code,
        duration_seconds=duration_seconds,
        log_path=log_path,
        error=error,
    )
    github_ops.post_issue_comment(repo_github, issue_number, body)


def post_advisory(
    *,
    assignment_id: str,
    machine_name: str,
    repo_github: str,
    repo_name: str,
    issue_number: int,
    duration_seconds: float | None = None,
    log_path: str | None = None,
    reason: str = "",
) -> None:
    body = format_advisory(
        assignment_id=assignment_id,
        machine_name=machine_name,
        repo_name=repo_name,
        issue_number=issue_number,
        duration_seconds=duration_seconds,
        log_path=log_path,
        reason=reason,
    )
    github_ops.post_issue_comment(repo_github, issue_number, body)
