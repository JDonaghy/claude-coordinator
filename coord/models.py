"""Dataclasses for the coordinator: repos, machines, assignments, board."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# #316: pattern that distinguishes a file-path value for `new_issue_guidance`
# from inline markdown text.  Matches paths like `docs/ISSUE_GUIDANCE.md` or
# `GUIDANCE.txt` but not multi-line or space-containing strings.
#
# The negative lookaheads reject (a) traversal sequences (`../`) and (b) any
# value starting with `/` or `\` (an absolute path).  Both protections matter
# because `Path("/repo") / "/etc/passwd.md"` silently discards the base and
# returns `Path("/etc/passwd.md")`, so absolute paths would otherwise escape
# the repo root just as effectively as `../`.  `resolve_new_issue_guidance`
# adds a second belt-and-braces check via `Path.resolve()` containment.
_GUIDANCE_PATH_RE: re.Pattern[str] = re.compile(
    r"^(?![/\\])(?!.*\.\.[/\\])[\w./\-]+\.(md|txt)$", re.IGNORECASE
)


@dataclass
class WorkerPermissionsConfig:
    """Per-repo allow/deny lists for worker commands.

    When ``deny`` is non-empty the coordinator injects a "forbidden commands"
    section into the worker system prompt so that ``claude -p`` refuses to run
    the listed patterns.  An empty ``deny`` list (``deny: []``) means no
    restrictions.
    """

    allow: list[str] = field(default_factory=list)
    deny: list[str] = field(default_factory=list)


@dataclass
class Repo:
    name: str
    github: str
    depends_on: list[str] = field(default_factory=list)
    default_branch: str = "main"
    build_command: str | None = None
    test_command: str | None = None
    # #296: optional shell command to interactively run the app for manual
    # smoke testing.  Surfaced in the TUI Test stage detail panel so the
    # tester knows exactly what to launch.
    run_cmd: str | None = None
    worker_permissions: WorkerPermissionsConfig | None = None
    housekeeping: list[str] = field(default_factory=list)
    coordinator_only_files: list[str] = field(default_factory=list)
    # #268: repos a worker may reference for context but doesn't actually
    # build against.  Common cases: sister projects extracted from a
    # common ancestor (quadraui ← vimcode), reference implementations,
    # "lift X out of Y into Z" issues.
    #
    # Honoured by the freshness check (pulled alongside `depends_on`)
    # but ignored by the cycle detector — so a repo can list a sibling
    # that already points back via `depends_on` without tripping the
    # validator.  Reference entries do NOT walk transitively — they're
    # a flat list.
    reference_repos: list[str] = field(default_factory=list)
    # #316: per-repo guidance for drafting new GitHub issues. Accepts either
    # an inline markdown string OR a file path relative to the repo root
    # (e.g. `docs/ISSUE_GUIDANCE.md`). See `resolve_new_issue_guidance`.
    new_issue_guidance: str | None = None
    # #305: glob patterns (relative to the worktree root) for build artifacts
    # to stash before the worktree is removed.  Matches are copied to
    # ~/.coord/artifacts/<repo>/<branch>/ on the agent with latest-wins
    # semantics per (repo, branch) pair.  Files under 100 bytes or ending
    # in `.d` are excluded (dependency files, not binaries).
    artifact_paths: list[str] = field(default_factory=list)
    # #323: optional provider override for workers dispatched to this repo.
    # When set, overrides providers.default from coordinator.yml.  The value
    # must match a key in providers.definitions (or be "claude" which is
    # always implicit).  None means "use the global default".
    provider: str | None = None

    def resolve_new_issue_guidance(self, repo_path: Path) -> str:
        """Return the new-issue guidance string for this repo.

        Resolution order:
        1. If ``new_issue_guidance`` is ``None`` (or empty), return a
           generic default describing the required issue sections.
        2. If the value matches ``[\\w/.-]+\\.(md|txt)$`` **and** the file
           exists at ``repo_path / value``, return the file contents.
        3. If the pattern matches but the file is missing, return the value
           verbatim as inline text (so a misconfigured path is still visible
           to the worker rather than silently replaced).
        4. Otherwise, return the value verbatim (it is inline markdown).
        """
        _DEFAULT = (
            "Required sections: "
            "Title (active voice, ≤80 chars), "
            "What (1-3 sentences), "
            "Acceptance (bulleted, observable), "
            "Out of scope"
        )
        if not self.new_issue_guidance or not self.new_issue_guidance.strip():
            return _DEFAULT
        value = self.new_issue_guidance.strip()
        if _GUIDANCE_PATH_RE.match(value):
            # Belt-and-braces against an escape from `repo_path`: resolve the
            # candidate and the base, then confirm the candidate stays under
            # the base.  This guards against any future regex regression as
            # well as edge cases like symlinks pointing outside the tree.
            try:
                base = repo_path.resolve()
                candidate = (repo_path / value).resolve()
            except (OSError, RuntimeError):
                # Resolution failure (e.g. permission denied, symlink loop) —
                # treat as inline so we never silently read a surprising file.
                return value
            try:
                candidate.relative_to(base)
            except ValueError:
                # Path escapes the repo root — treat as inline rather than
                # reading a file outside the trusted tree.
                return value
            try:
                return candidate.read_text(encoding="utf-8", errors="replace")
            except (OSError, FileNotFoundError):
                # File missing — fall back to inline so the value is at least
                # surfaced in the prompt rather than silently defaulting.
                return value
        return value


@dataclass
class Machine:
    name: str
    host: str
    capabilities: list[str] = field(default_factory=list)
    repos: list[str] = field(default_factory=list)
    repo_paths: dict[str, str] = field(default_factory=dict)

    def can_work_on(self, repo_name: str) -> bool:
        return repo_name in self.repos

    def repo_path(self, repo_name: str) -> str | None:
        return self.repo_paths.get(repo_name)


@dataclass
class Assignment:
    machine_name: str
    repo_name: str
    issue_number: int
    issue_title: str
    files_allowed: list[str] = field(default_factory=list)
    files_forbidden: list[str] = field(default_factory=list)
    briefing: str = ""
    assignment_id: str | None = None
    status: str = "pending"  # pending | running | done | failed | advisory
    branch: str | None = None
    pr_url: str | None = None
    dispatched_at: float | None = None
    finished_at: float | None = None
    smoke_test: str | None = None  # None | pass | fail
    smoke_test_reason: str | None = None
    # "work" (default), "review", "plan", "smoke", or "conflict-fix".
    # Review assignments target an existing PR rather than implementing a
    # fresh issue. Plan assignments are read-only: the worker analyses the
    # codebase and outputs a structured plan without writing any code.
    # conflict-fix is dispatched when a merge fails with a mechanical
    # (non-semantic) conflict — the worker rebases, resolves obvious
    # additive merges, and force-pushes; the coordinator owns the retry.
    type: str = "work"
    review_target: str | None = None
    review_of_assignment_id: str | None = None
    unreachable_count: int = 0
    # Model tier the worker was dispatched with (e.g. "haiku", "sonnet",
    # "opus"). None means the worker used claude's default. Tracked on the
    # board so escalation in `coord fix` / `coord retry` / `coord resume-stuck`
    # can step up the ladder.
    model: str | None = None
    # Parsed structured plan from a plan-only worker (type="plan"). Stored as
    # a plain dict (the serialised form of WorkerPlan.to_dict()) so it round-
    # trips cleanly through JSON without a custom encoder.
    plan: dict | None = None
    # Review lifecycle state for type="work" assignments.
    # None  — not applicable (review/smoke/plan assignments, or pre-feature boards)
    # "pending"    — work done, review not yet dispatched
    # "dispatched" — review assignment is in flight
    # "done"       — review assignment completed
    review_state: str | None = None
    # Pipeline gate requirements — controls which approval steps are enforced.
    # Empty list means "use config.pipeline.default_gates".
    # Examples: ["review", "merge"], ["merge"], ["review", "smoke", "merge"]
    required_gates: list[str] = field(default_factory=list)
    # Auto-loop iteration counter. For the original work assignment this is 0.
    # Each fix worker dispatched by auto_loop increments this by 1. Used to
    # enforce pipeline.max_review_iterations and stop runaway loops.
    review_iteration: int = 0
    # Timestamp when review findings were successfully posted to GitHub (as a
    # PR review or issue comment).  None means findings have not been posted
    # yet — either the review is still running, the worker produced no
    # structured output, or notify never saw the completion event.
    review_posted_at: float | None = None
    # #200: human-driven Test gate verdict for type="work" assignments.
    # None | "passed" | "failed" | "skipped". Review auto-dispatch is gated on
    # this being passed/skipped (or no Test stage configured).
    test_state: str | None = None
    test_reason: str | None = None
    # #253: parsed adversarial-review verdict for type="review" assignments.
    # None | "approve" | "request-changes". Set when notify or auto_loop
    # extracts the structured REVIEW_VERDICT from the reviewer's log; consumed
    # by the merge-queue gate (`has_approved_review`) to refuse merging work
    # whose review has not approved.
    review_verdict: str | None = None
    # #821: SHA of the branch HEAD captured at the time the review assignment
    # ran.  When set, `has_approved_review` compares this against the merge
    # queue entry's `branch_head_sha` to reject stale approvals — if the
    # branch gained commits after the review ran, the approval no longer
    # covers the current HEAD and the entry is re-blocked until re-reviewed.
    # None for review assignments predating this field or where SHA tracking
    # is not available.
    review_head_sha: str | None = None
    # #208: parsed worker cost from the final stream-json `result` event.
    # None means "not yet captured" (older rows, in-flight workers, or
    # workers whose log lacked usage data).  Set on completion by
    # notify.py / reconcile via coord.usage.parse_usage_from_log.
    cost_usd: float | None = None
    # #252: worker-emitted smoke-test list parsed from the SMOKE_TESTS
    # block.  None = no block emitted (graceful TUI placeholder); [] =
    # explicit "(none — change is internal)"; non-empty list = bullets.
    smoke_tests: list[str] | None = None
    # #324: resolved provider name recorded at dispatch time so the TUI
    # can surface it in the assignment detail panel (#327).  None means
    # "dispatched before #324 landed or via a path that doesn't set this
    # field" — the TUI should show the implicit default ("claude") in that
    # case.  Always the *resolved* name (after the spec > repo > default
    # precedence chain), not just the raw proposal.provider field.
    provider_name: str | None = None
    # #546: token counts for automated (claude -p) assignments.  Parsed from
    # the final stream-json result event at the same time as cost_usd.  All
    # default to 0; interactive (Max/OAuth) sessions stay at 0 and the TUI
    # labels them "Max (subscription)" rather than projecting a dollar figure.
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    # #618: short one-liner written immediately when an interactive session
    # fails to launch (e.g. "branch already checked out at <path>").  Lets
    # the TUI explain the red box without any log file being present.
    # None for assignments that launched successfully.
    failure_reason: str | None = None
    # #944: Acceptance-gate verdict (oracle loop, docs/ORACLE_LOOP.md) for
    # type="work" assignments. None | "passed" | "failed" — set by `coord
    # acceptance record --issue N --sha <sha>`, the coordinator's external
    # re-run of the sealed suite against the pushed SHA (the trust gate a
    # headless worker's in-session "green" claim can't fake).
    acceptance_state: str | None = None
    acceptance_reason: str | None = None
    # SHA the last `acceptance record` verdict was recorded against — lets a
    # future gate detect staleness (new commits since the last record) the
    # same way review_head_sha detects a stale review approval.
    acceptance_sha: str | None = None


@dataclass
class Proposal:
    id: int
    machine_name: str
    repo_name: str
    issue_number: int
    issue_title: str
    rationale: str
    files_likely: list[str] = field(default_factory=list)
    briefing: str = ""
    # Optional model override. When None, the dispatcher falls back to
    # config.models.default.
    model: str | None = None
    # "work" (default) or "plan". Plan proposals dispatch read-only planning
    # workers that analyse the codebase and produce a structured plan without
    # writing any code.
    type: str = "work"
    # Pipeline gate requirements — mirrors Assignment.required_gates.
    # Set by the coordinator before dispatch so the ledger records intent.
    required_gates: list[str] = field(default_factory=list)
    # Optional explicit branch the agent must check out, bypassing the
    # slugified-title-derived branch name.  Used by follow-up dispatches
    # (pr, fix-up, continuation) so prefixed issue titles like
    # `[fix-1] …` or `[conflict-fix] …` don't push to a new orphan
    # branch — the worker must land commits on the parent assignment's
    # branch instead.
    target_branch: str | None = None
    # #315: when set, the dispatch payload includes `--resume <session_id>`
    # so the worker loads the prior claude conversation and continues it.
    # Only set by `coord chat-continue`; regular dispatches leave this None.
    resume_session_id: str | None = None
    # #324: optional provider override for this proposal's worker.  Mirrors
    # ``Repo.provider`` and ``AssignmentSpec.provider`` — uses the same
    # precedence chain: spec > repo > providers.default.  When None the
    # coordinator and agent both fall back to the global default.  Set by the
    # brain when a repo's configured provider should be overridden for this
    # specific dispatch.
    provider: str | None = None


@dataclass
class SplitChunk:
    title: str
    scope: str
    files_likely: list[str] = field(default_factory=list)


@dataclass
class SplitProposal:
    id: int
    repo_name: str
    issue_number: int
    issue_title: str
    rationale: str
    chunks: list[SplitChunk] = field(default_factory=list)


@dataclass
class Board:
    repos: list[Repo] = field(default_factory=list)
    machines: list[Machine] = field(default_factory=list)
    active: list[Assignment] = field(default_factory=list)
    completed: list[Assignment] = field(default_factory=list)
    round_number: int = 0

    def repo(self, name: str) -> Repo | None:
        return next((r for r in self.repos if r.name == name), None)

    def machine(self, name: str) -> Machine | None:
        return next((m for m in self.machines if m.name == name), None)

    def idle_machines(self) -> list[Machine]:
        busy = {a.machine_name for a in self.active if a.status == "running"}
        return [m for m in self.machines if m.name not in busy]

    def active_files_by_repo(self) -> dict[str, list[str]]:
        """Map of repo_name -> files currently being touched by running assignments."""
        result: dict[str, list[str]] = {}
        for a in self.active:
            if a.status != "running":
                continue
            result.setdefault(a.repo_name, []).extend(a.files_allowed)
        return result

    def mark_done(
        self,
        machine_name: str,
        branch: str | None = None,
        pr_url: str | None = None,
    ) -> Assignment | None:
        for a in self.active:
            if a.machine_name == machine_name and a.status == "running":
                a.status = "done"
                a.branch = branch
                a.pr_url = pr_url
                self.completed.append(a)
                self.active.remove(a)
                return a
        return None

    def mark_failed(self, machine_name: str) -> Assignment | None:
        for a in self.active:
            if a.machine_name == machine_name and a.status == "running":
                a.status = "failed"
                self.completed.append(a)
                self.active.remove(a)
                return a
        return None

    def find_by_id(self, assignment_id: str) -> Assignment | None:
        for a in self.active:
            if a.assignment_id == assignment_id:
                return a
        for a in self.completed:
            if a.assignment_id == assignment_id:
                return a
        return None

    def mark_done_by_id(
        self,
        assignment_id: str,
        branch: str | None = None,
        pr_url: str | None = None,
        finished_at: float | None = None,
    ) -> Assignment | None:
        for a in self.active:
            if a.assignment_id == assignment_id:
                a.status = "done"
                if branch is not None:
                    a.branch = branch
                if pr_url is not None:
                    a.pr_url = pr_url
                a.finished_at = finished_at
                self.completed.append(a)
                self.active.remove(a)
                return a
        return None

    def mark_failed_by_id(
        self,
        assignment_id: str,
        finished_at: float | None = None,
    ) -> Assignment | None:
        for a in self.active:
            if a.assignment_id == assignment_id:
                a.status = "failed"
                a.finished_at = finished_at
                self.completed.append(a)
                self.active.remove(a)
                return a
        return None

    def gc(self, keep: int = 50) -> int:
        """Remove oldest completed assignments beyond *keep*. Returns count removed."""
        if len(self.completed) <= keep:
            return 0
        by_time = sorted(self.completed, key=lambda a: a.finished_at or 0)
        to_remove = len(self.completed) - keep
        self.completed = by_time[to_remove:]
        return to_remove
