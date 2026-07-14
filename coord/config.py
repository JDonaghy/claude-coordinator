"""Parse and validate coordinator.yml."""

from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from coord.models import Machine, Repo, WorkerPermissionsConfig


DEFAULT_CONFIG_PATH = Path("coordinator.yml")

# Canonical config home — works on a machine that has no repo checkout, mirroring
# where ``~/.coord/coord.db`` and ``~/.coord/client.toml`` already live.  This is
# the recommended location; ``./coordinator.yml`` stays a development fallback.
USER_CONFIG_PATH = Path.home() / ".coord" / "coordinator.yml"


def resolve_config_path() -> Path:
    """Resolve which ``coordinator.yml`` to load when no explicit path is given.

    Search order (first existing file wins):

    1. ``$COORD_CONFIG`` (if set) — explicit override.
    2. ``~/.coord/coordinator.yml`` — the canonical home (no repo checkout needed).
    3. ``./coordinator.yml`` — CWD, for development / the repo checkout.

    When none exist the canonical home path is returned so the "not found" error
    points operators at the recommended location rather than at the CWD.
    """
    env = os.environ.get("COORD_CONFIG")
    if env:
        return Path(env).expanduser()
    for candidate in (USER_CONFIG_PATH, DEFAULT_CONFIG_PATH):
        if candidate.exists():
            return candidate
    return USER_CONFIG_PATH

# Safety-by-default: repos without explicit worker_permissions get this deny-list.
DEFAULT_DENY_COMMANDS: list[str] = [
    "Bash(gh *)",
    "Bash(git push --force *)",
    "Bash(git push -f *)",
    "Bash(git reset --hard *)",
    "Bash(git branch -D *)",
    "Bash(git checkout -- .)",
    "Bash(git clean -f *)",
    "Bash(rm -rf *)",
]


class ConfigError(Exception):
    """Raised when coordinator.yml is missing, malformed, or fails validation."""


@dataclass
class HooksConfig:
    on_round_complete: list[str] = field(default_factory=list)
    on_session_end: list[str] = field(default_factory=list)


@dataclass
class ReviewsConfig:
    """Adversarial code review settings.

    `enabled=True` by default. When enabled, `coord pr` auto-dispatches an
    adversarial review to a different machine after the PR worker is sent.
    Completion of a "work" assignment via reconciliation also triggers review
    dispatch automatically (see coord/review.py). Set `enabled: false` in
    coordinator.yml to opt out.
    """

    enabled: bool = True
    auto_dispatch: bool = True
    require_approval: bool = False
    reviewer_prompt: str = ""
    checklist: list[str] = field(default_factory=lambda: [
        "Check for platform-specific code in shared/cross-platform paths",
    ])
    repo_overrides: dict[str, list[str]] = field(default_factory=dict)
    # Flood guard (incident 2026-06-08): bound *bulk* review dispatch so a
    # backlog "unmasking" (e.g. removing a gate that had been suppressing
    # reviews) can't fire hundreds of metered `claude -p` reviews in one pass.
    # See coord.review.dispatch_pending_reviews.
    max_auto_dispatch_per_pass: int = 5  # cap reviews dispatched per reconcile/notify pass (0 = unbounded)
    flood_threshold: int = 12  # if more rows than this are pending review in one pass, refuse all (0 = no surge gate)
    allow_review_flood: bool = False  # override the surge gate (or set env COORD_ALLOW_REVIEW_FLOOD=1)


@dataclass
class ConcurrencyConfig:
    max_workers: int = 2
    stagger_seconds: float = 30.0
    backoff_base: float = 60.0
    max_retries: int = 3
    auto_reassign: bool = False
    stale_threshold: int = 3
    # Spawn `claude -p` through a transient `bash -c 'exec ...'` parent so the
    # immediate parent of claude is a short-lived shell. This is the upstream
    # headline fix for the daemon-spawn freeze (anthropics/claude-code#56268).
    bash_wrap_spawn: bool = True
    # First-output (TTFT) watchdog: if a worker produces zero output within
    # this many seconds, kill its process group and fail the assignment so the
    # auto_reassign path re-dispatches it. 0 disables the watchdog. This only
    # catches truly silent hangs — a rate-limited worker still emits output and
    # therefore passes the check.
    first_output_timeout: float = 600.0
    # Remote interactive-session staleness timeout (#588).  After a remote
    # ``claude-pty`` assignment has been running for longer than this many
    # hours, each reconcile pass probes the remote tmux session via SSH.  If
    # the session is dead (tmux has-session exits 1) the coordinator calls
    # ``finalize_remote_interactive_exit`` to push any commits and release the
    # machine slot.  If SSH is unreachable, a warning is emitted instead.
    # Default is 12 hours — generous enough that a genuinely long session is
    # never interrupted, but tight enough to catch orphaned rows from crashed
    # sessions overnight.  Set to 0 to disable the sweep entirely.
    interactive_session_timeout_hours: float = 12.0


@dataclass
class SmokeRule:
    """When a worker's diff touches any of `files`, the smoke machine must
    have all capabilities in `requires`.

    `files` patterns match by prefix against the relative paths returned by
    `gh pr view --json files`. A trailing `/` makes the prefix explicit; bare
    paths match if the touched path starts with the rule path (so `src/gtk`
    catches `src/gtk/foo.c` and `src/gtk_helpers.c`). Use `src/gtk/` to scope
    strictly to the directory.
    """

    files: list[str] = field(default_factory=list)
    requires: list[str] = field(default_factory=list)


@dataclass
class SmokeTestsConfig:
    """Smoke-test orchestration. Off by default — opt-in per project.

    `default_command` is the shell command the smoke agent runs (e.g.
    `make smoke` or `pytest tests/smoke`). Per-repo overrides flow through
    `Repo.test_command` already; this is the fallback when none is set.
    """

    auto_queue: bool = False
    default_command: str | None = None
    timeout_seconds: int = 600
    capability_rules: list[SmokeRule] = field(default_factory=list)


@dataclass
class AcceptanceDriverConfig:
    """One entry under ``acceptance.drivers.<repo_name>`` in coordinator.yml
    (#944, docs/ORACLE_LOOP.md), OR one entry in that repo's ``routes:`` list
    (#1125, in-repo path routing — see :class:`AcceptanceConfig.driver_for`).

    ``kind`` selects the framework-specific adapter that knows how to launch,
    drive, and parse a repo's sealed acceptance suite (``tui-tuidriver`` and
    ``cli-pytest`` are implemented; other kinds are declared here but
    rejected at run time by :mod:`coord.acceptance_drivers` until their
    issues land). ``run`` is the shell command that executes the suite and
    must print structured (JSON) verdicts to stdout — it may reference the
    ``{ms}`` template (substituted with the ``ms-NN`` milestone dirname by
    :func:`coord.acceptance_drivers.render_run_command`) to point at a
    milestone-scoped suite dir. ``mock`` is a glob (relative to the
    acceptance dir) for the viewable mock/assertion fixtures — ``*.screen``
    for ``tui-tuidriver``, ``*.out`` (expected CLI stdout) for
    ``cli-pytest`` — informational today, consumed by the future mock-author
    (#930). ``capability`` is the machine capability required to run this
    driver, intended to be routed the same way ``smoke_tests.capability_rules``
    routes smoke tests.

    ``match`` and ``routes`` implement #1125's in-repo path routing: a repo
    entry with a non-empty ``routes`` list is a *router* — its own
    ``kind``/``run``/``mock``/``capability`` are unused and each element of
    ``routes`` is itself an ``AcceptanceDriverConfig`` with ``match`` set (a
    repo-root-relative glob, e.g. ``"coord/**"``). A route entry's own
    ``routes`` is always empty — nesting one level is the whole feature, not
    a recursive router. See :meth:`AcceptanceConfig.driver_for` for the
    resolution rule.

    NOTE (#944 review): parsed and validated here, but not yet *consulted* —
    ``coord acceptance record``'s daemon-routed run (#944) always executes on
    whatever host ``resolve_board_service()`` points at, with no capability
    check. Wiring ``record`` through ``capability_rules``-style routing (the
    way ``coord test``'s ``pick_smoke_machine``/``match_rules`` route smoke
    runs to capable hardware) is left to #932, which owns dispatching the
    run stage.
    """

    kind: str = ""
    run: str = ""
    mock: str = ""
    capability: str = ""
    match: str = ""
    routes: list["AcceptanceDriverConfig"] = field(default_factory=list)


@dataclass
class AcceptanceConfig:
    """``acceptance.drivers`` — repo name -> :class:`AcceptanceDriverConfig`."""

    drivers: dict[str, AcceptanceDriverConfig] = field(default_factory=dict)

    def driver_for(
        self, repo_name: str, path: str | None = None,
    ) -> AcceptanceDriverConfig | None:
        """Resolve *repo_name*'s acceptance driver, optionally routed by
        *path* (#1125, repo-root-relative — e.g. ``"coord/acceptance.py"``).

        - Unknown repo -> ``None``.
        - Repo entry has no ``routes`` (today's flat single-driver form,
          back-compat) -> the entry itself, regardless of *path*.
        - Repo entry has ``routes`` -> the **first** route whose ``match``
          glob matches *path* (``fnmatch`` semantics, e.g. ``"coord/**"``
          matches ``"coord/acceptance.py"``); first-match wins when more
          than one route's glob matches. ``path=None`` against a routed
          entry can't select a route, so it returns ``None`` rather than
          guessing one — callers that know they're driving a specific file
          (or an issue's manifest-mapped path) must pass it.

        Resolution rule when a milestone/issue's slice spans more than one
        route (#1125 review finding 4): this method makes no attempt to
        detect or merge across routes for a single call — it resolves
        exactly one *path* to exactly one route (or ``None``). A caller
        whose work spans multiple routes (e.g. a full-stack issue touching
        both ``coord/**`` and ``tui/**``) must pick ONE representative path
        for the invocation (or invoke once per route) rather than expect
        this method to fan out; callers driving a whole repo/milestone with
        no single path in hand (Gate A, sealing, briefing-injection) should
        use :meth:`has_driver` instead, which is path-independent by design.
        """
        entry = self.drivers.get(repo_name)
        if entry is None:
            return None
        if not entry.routes:
            return entry
        if path is None:
            return None
        for route in entry.routes:
            if fnmatch.fnmatch(path, route.match):
                return route
        return None

    def has_driver(self, repo_name: str) -> bool:
        """Path-independent "does this repo participate in the oracle loop
        at all" predicate (#1125 review finding 1).

        True when *repo_name* has ANY acceptance driver configured — flat
        or routed — regardless of which route a given path would resolve
        to. Use this for existence-only checks that must not silently flip
        the moment a repo adopts ``routes:`` — Gate A
        (``coord.milestone_dispatch.gate_a_status``), the ``tests/acceptance/``
        sealing/forbid list (``coord.dispatch.dispatch``), and the
        oracle-loop briefing-contract injection (``coord.dispatch.dispatch``)
        all only need "yes/no", never a concrete driver to run — use
        :meth:`driver_for` (with a *path*) for that.
        """
        return repo_name in self.drivers


@dataclass
class ModelsConfig:
    """Model tier selection and escalation ladder for workers.

    `default` is the model passed to ``claude -p`` when an assignment doesn't
    specify one.  `escalation` is an ordered list of model aliases (low →
    high); when a worker fails or gets stuck, the coordinator escalates to
    the next entry via `next_model`.  `labels` is a per-issue-label override
    (e.g. ``documentation: haiku``) consumed by the brain / planner.

    `versions` pins an alias to an exact model id, e.g.
    ``{sonnet: claude-sonnet-4-6, opus: claude-opus-4-7}``.  When set, the
    coordinator translates the alias to the exact id before passing it to
    ``claude -p --model`` on the worker.  Aliases not present in the map
    pass through unchanged, so ``claude -p`` falls back to its CLI default
    (which today is whatever the installed claude-cli treats as latest).
    """

    default: str = "sonnet"
    escalation: list[str] = field(
        default_factory=lambda: ["haiku", "sonnet", "opus"]
    )
    labels: dict[str, str] = field(default_factory=dict)
    versions: dict[str, str] = field(default_factory=dict)

    def next_model(self, current: str) -> str:
        """Return the next model in the escalation ladder.

        If *current* is already at the top of the ladder, or isn't on the
        ladder at all, return *current* unchanged.
        """
        try:
            idx = self.escalation.index(current)
        except ValueError:
            return current
        if idx + 1 < len(self.escalation):
            return self.escalation[idx + 1]
        return current

    def resolve(self, alias: str | None) -> str | None:
        """Resolve an alias to its pinned exact model id, if configured.

        Returns *alias* unchanged when no mapping exists, and ``None`` when
        *alias* is ``None`` (preserves the "omit --model" code path).
        """
        if alias is None:
            return None
        return self.versions.get(alias, alias)


@dataclass
class DispatchConfig:
    """Smart task-splitting configuration.

    When ``auto_split`` is ``True`` (the default), the ``coord approve``
    command analyses each proposal's ``files_likely`` list.  If the file
    count exceeds ``max_files_per_worker``, the work is shown to the user
    split into parallel/sequential chunks for confirmation before dispatch.

    Set ``auto_split: false`` to disable the splitting analysis entirely.

    When ``require_plan`` is ``True``, ``coord assign`` defaults to
    ``--plan-only`` behaviour — the worker reads the codebase and produces a
    structured plan without writing any code.  The user then runs
    ``coord approve-plan`` or ``coord reject-plan`` to act on the plan.
    Pass ``--no-plan`` to ``coord assign`` to override this default and
    dispatch a work assignment directly.  Assignments of type ``review``,
    ``smoke``, or ``plan`` are never affected by this setting.
    """

    max_files_per_worker: int = 8
    auto_split: bool = True
    require_plan: bool = False


# #846: default wall-clock thresholds (seconds) an assignment of a given
# `type` may run before `coord.notify.detect_needs_attention` flags it.
# Deliberately generous — this is a "human should glance at this" signal,
# not a kill switch (detection + surfacing only, see issue #846).
#
# These are all *headless* types — a `claude -p` worker converging toward a
# result with no one attending it live, so "running way longer than usual"
# is a meaningful stuck signal. `plan`/`mock-author`/`test-author` are
# lighter-weight than `work` (no code-writing convergence loop) but still
# headless, so they get their own explicit (rather than work-fallback)
# tuning. `conflict-fix` is dual-purpose — the automated #241 worker *and*
# the interactive `--merge-of` session share this type (see
# `coord.reconcile.is_interactive_merge_session`) — so it gets a little more
# headroom than `work` to cover a human resolving a semantic conflict.
#
# #1137 audit note: the #1133 follow-up asked whether `merge`/`fix` (the two
# types named in the original #846 ask but left unhandled by #1133) need
# their own entry. `merge` does NOT — there is no literal `type="merge"`
# (a dedicated value was tried and reverted, see
# `is_interactive_merge_session`'s docstring / tests/test_reap_merged_sessions.py
# DISCRIMINATOR NOTE); the interactive `--merge-of` session already shares
# `conflict-fix` above and is covered by its 60m threshold. `fix` (the
# interactive `--fix-of`/`--rework-of` human-attended session) DOES need
# handling — it shares `type="work"` with headless coding workers, so it
# can't get its own entry here either. Instead `attention_threshold_for`
# recognizes it via the same compound discriminator shape as
# `is_interactive_merge_session` — `provider_name="claude-pty"` +
# `review_of_assignment_id` set on a `type="work"` row — and reuses
# `conflict-fix`'s threshold (the same "human resolving someone else's
# feedback" scenario).
_DEFAULT_ATTENTION_THRESHOLDS: dict[str, float] = {
    "work": 45 * 60.0,
    "review": 15 * 60.0,
    "smoke": 20 * 60.0,
    "plan": 30 * 60.0,
    "mock-author": 30 * 60.0,
    "test-author": 30 * 60.0,
    "conflict-fix": 60 * 60.0,
}

# #1133: assignment types that are human-attended interactive sessions — a
# developer reading/thinking/typing at a live `claude` TTY (driven via
# `POST /inject/{id}` from the TUI), not a headless worker converging toward
# a result. These have no wall-clock "stuck" concept: a human legitimately
# spending hours reading an issue, chatting through a plan, or validating a
# diff is normal, not stalled (the #846 wall-clock check exists to catch a
# headless worker silently burning budget — see `attention_signal`'s
# docstring for the #448 motivation, which doesn't apply here). Exempt from
# the wall-clock signal unconditionally in `attention_threshold_for` —
# *not* merely by omission from `_DEFAULT_ATTENTION_THRESHOLDS` — so a user
# who overrides `pipeline.attention_thresholds.work` in `coordinator.yml`
# can't accidentally re-arm this check for a chat session via the
# fallback-to-"work" behaviour (see that method's docstring). A user who
# explicitly configures a threshold for one of these types still wins —
# this is a default exemption, not an unconditional one.
INTERACTIVE_SESSION_TYPES: frozenset[str] = frozenset({
    "chat",
    "troubleshoot",
    "audit",
    "milestone-chat",
    "refinement",
    "new-issue-chat",
    "test-chat",
})


@dataclass
class PipelineConfig:
    """Assignment lifecycle gate configuration.

    ``default_gates`` is the list of approval steps required for every work
    assignment unless overridden by an issue label.  ``labels`` maps GitHub
    issue label names to gate lists, allowing per-label overrides — e.g.
    a ``hotfix`` label could bypass review with ``hotfix: [merge]``.

    ``auto_loop`` enables the automated review → fix → re-review cycle.
    When ``True`` (default), a review that requests changes automatically
    dispatches a fix worker.  The fix worker then receives a fresh review,
    and the cycle continues until the review approves or
    ``max_review_iterations`` is reached.

    ``max_review_iterations`` is the maximum number of fix rounds before
    the auto-loop stops and posts a notice asking for manual intervention.
    Default is 5.

    ``escalate_fix_model`` controls whether auto-dispatched fix workers
    escalate the model on each bounce iteration.  When ``True`` (default),
    the first fix stays on ``models.default`` and each subsequent fix
    iteration climbs one rung up ``models.escalation`` (capped at the top).
    When ``False``, fix dispatches set no model (today's behaviour: the
    agent falls back to ``claude -p``'s default).

    ``attention_thresholds`` (#846) maps assignment ``type`` (``"work"``,
    ``"review"``, ``"smoke"``, ...) to a wall-clock duration (seconds) that
    an assignment may sit in ``status="running"`` before
    ``coord.notify.detect_needs_attention`` flags it. A type not present in
    the mapping falls back to ``_DEFAULT_ATTENTION_THRESHOLDS``, *unless*
    it's a human-attended interactive type (#1133,
    :data:`INTERACTIVE_SESSION_TYPES` — ``"chat"``, ``"troubleshoot"``,
    ``"audit"``, ``"milestone-chat"``, ``"refinement"``,
    ``"new-issue-chat"``, ``"test-chat"``), which is exempt from the
    wall-clock check entirely by default — see
    :meth:`attention_threshold_for`. An interactive ``--fix-of``/
    ``--rework-of`` session (#1137) is also recognized there, by
    ``provider_name``/``review_of_assignment_id`` rather than ``type``
    (it shares ``type="work"`` with headless coding workers), and reuses
    ``conflict-fix``'s threshold.

    ``convergence_rounds`` (#846) is the number of fix/review rounds
    (``Assignment.review_iteration``) an assignment may accumulate without
    reaching a green test verdict + approved review before it is flagged as
    non-converging (thrashing). Default 3.
    """

    default_gates: list[str] = field(default_factory=lambda: ["test", "review", "merge"])
    labels: dict[str, list[str]] = field(default_factory=dict)
    auto_loop: bool = True
    max_review_iterations: int = 5
    escalate_fix_model: bool = True
    attention_thresholds: dict[str, float] = field(
        default_factory=lambda: dict(_DEFAULT_ATTENTION_THRESHOLDS)
    )
    convergence_rounds: int = 3

    def attention_threshold_for(
        self,
        assignment_type: str,
        *,
        provider_name: str | None = None,
        review_of_assignment_id: str | None = None,
    ) -> float:
        """Wall-clock threshold (seconds) for *assignment_type*.

        Checked in order:

        1. **Interactive fix session** (#1137): ``assignment_type == "work"``
           with ``provider_name == "claude-pty"`` and
           ``review_of_assignment_id`` set (the optional keyword-only args,
           passed by callers that have the full assignment record; both
           default to ``None`` so existing callers that only know the type
           are unaffected). This mirrors
           :func:`coord.reconcile.is_interactive_merge_session`'s compound
           discriminator — a dedicated ``type="fix"`` was deliberately not
           introduced, for the same reason a dedicated ``type="merge"`` was
           reverted (see that function's docstring). A matching row defers
           to ``attention_threshold_for("conflict-fix")`` — the same "human
           resolving someone else's feedback, not writing from scratch"
           scenario that earned conflict-fix its extra headroom in #1133 —
           so an explicit user override of ``conflict-fix`` (but *not* of
           plain ``work``) still applies. Checked *before* the plain
           ``attention_thresholds`` lookup below for the same reason
           :data:`INTERACTIVE_SESSION_TYPES` is: ``attention_thresholds``
           always carries a built-in ``"work"`` entry (the dataclass default
           copies the whole ``_DEFAULT_ATTENTION_THRESHOLDS`` dict), so
           checking that dict first would make this branch unreachable.
        2. **Explicit override** — an ``attention_thresholds`` entry for
           *this exact* ``assignment_type`` (built-in default or
           user-configured) always wins.
        3. **Interactive session type** (#1133,
           :data:`INTERACTIVE_SESSION_TYPES`) — human-attended
           chat/troubleshoot/review-style sessions with no
           headless-convergence concept — exempted (``inf``, never flagged)
           rather than inheriting a headless-worker threshold.
        4. **Fallback to this config's own ``"work"`` entry** (so a user who
           only overrides ``work`` gets that value applied to unlisted
           *headless* types too, not the hardcoded default) — and only
           reaches for the hardcoded default when even ``"work"`` was never
           configured. This fallback is deliberately scoped to headless
           types by the ``INTERACTIVE_SESSION_TYPES`` check above it: unlike
           an unlisted headless type (probably work-like), an unlisted
           interactive type has no wall-clock-stuck concept at all, so
           silently reusing ``"work"``'s threshold for it would be a
           category error, not a reasonable guess.
        """
        if (
            assignment_type == "work"
            and provider_name == "claude-pty"
            and review_of_assignment_id is not None
        ):
            return self.attention_threshold_for("conflict-fix")
        if assignment_type in self.attention_thresholds:
            return self.attention_thresholds[assignment_type]
        if assignment_type in INTERACTIVE_SESSION_TYPES:
            return float("inf")
        return self.attention_thresholds.get(
            "work", _DEFAULT_ATTENTION_THRESHOLDS["work"]
        )

    def tracked_labels(self) -> list[str]:
        """Return the GitHub issue labels considered part of the pipeline.

        Always includes ``'coord'`` so normal coordinator-tagged issues appear
        in the pipeline panel regardless of per-label gate configuration.
        Additional labels come from the ``labels`` dict keys, sorted for
        stable ordering.
        """
        if not self.labels:
            return ["coord"]
        keys = sorted(self.labels.keys())
        if "coord" not in keys:
            keys = ["coord"] + keys
        return keys

    def gates_for_label(self, label: str | None) -> list[str]:
        """Return the gate list for a specific label, falling back to defaults.

        ``label`` may be ``None`` (no matching tracked label found on the
        issue) — in that case the configured ``default_gates`` are returned.
        """
        if label and label in self.labels:
            return list(self.labels[label])
        return list(self.default_gates)

    def test_precedes_review(self) -> bool:
        """True when the ``test`` gate is ordered *before* ``review`` in the
        default gate list — i.e. the smoke/test verdict gates review dispatch
        (Work → Test → Review), rather than gating only the merge.

        When both gates are present and ``test`` comes first, automatic review
        dispatch waits for a ``passed``/``skipped`` test verdict (see
        ``coord.review.dispatch_pending_reviews``); when ``review`` comes first
        (or either gate is absent) review fires on work completion as before.
        Consulted on the *default* policy only — mirrors the merge gate's
        ``requires_smoke``/``requires_review``, which also ignore per-label
        overrides because they operate on the default gate list.
        """
        gates = self.default_gates or []
        if "test" not in gates or "review" not in gates:
            return False
        return gates.index("test") < gates.index("review")


@dataclass
class MergeConfig:
    """Merge behaviour configuration.

    ``auto_drain`` enables automatic draining of READY merge-queue entries on
    each daemon passive tick.  **Default-off** — with no ``merge:`` block in
    ``coordinator.yml`` the daemon never merges automatically and existing
    behaviour is unchanged.

    When enabled, after the enqueue step in ``_tick_loop`` the daemon calls
    :func:`coord.serve_app._auto_drain_tick`, which evaluates the plan
    (review + smoke + CI gates) and merges exactly the entries marked
    ``READY``, in true ``sequence()`` order.  ``BLOCKED`` and terminal
    entries are never touched.  Every auto-merge is logged so the operator
    can audit what drained (#781).

    Set ``max_per_tick`` to cap how many merges the daemon may perform in a
    single tick (default ``0`` = unlimited).
    """

    auto_drain: bool = False
    max_per_tick: int = 0
    auto_reap_merged: bool = True


@dataclass
class MilestoneConfig:
    """Milestone-driven-workflow configuration (#767 / #769 Phase 1).

    ``auto_dispatch`` enables the daemon's tick loop to keep draining a
    milestone's declared work order after ``coord milestone dispatch``
    registers it: as issues reach a merged/terminal state, the newly-
    unblocked ready frontier is recomputed and dispatched automatically —
    no further human approval per issue, since the *declared* work order
    (the `## Work order` block) was the one-time approval unit.
    **Default-off** — with no ``milestone:`` block in ``coordinator.yml``
    the daemon never auto-dispatches and existing behaviour is unchanged;
    `coord milestone dispatch` still works as a one-shot manual drain.

    When enabled, :func:`coord.serve_app._milestone_drain_tick` runs on
    each daemon tick (after the reconcile step) for every milestone
    registered via a non-dry-run `coord milestone dispatch` call, and
    deregisters a milestone once its whole work order reaches a terminal
    state.

    Editing this wiring requires a **daemon restart** to take effect — the
    tick loop's closures are captured at ``coord serve`` startup time.
    """

    auto_dispatch: bool = False


@dataclass
class CiStoreConfig:
    """Backend selection for CI check visibility (#240).

    ``type`` is one of ``github`` (shell out to ``gh pr checks``) or
    ``none`` (always-empty :class:`coord.ci_store.NoOpCi`).  When the block
    is absent we default to ``github`` since it's a no-op upgrade for users
    who already have ``gh`` configured.  Future backends (GitLab, Buildkite)
    add new ``type`` values without breaking existing configs.
    """

    type: str = "github"


@dataclass
class AuditConfig:
    """``audit:`` block (#1036/#1038) — the append-only ``audit_log``
    table's tunables.

    ``max_rows`` is a future retention cap, not a pruning sweep: when set
    above the default ``0`` (unlimited), :func:`coord.audit.record_audit`
    opportunistically deletes the oldest rows past that count after every
    insert.  ``0`` means keep everything forever — the default for this
    milestone, since retention policy is explicitly out of scope (see the
    issue's "Out of scope" section).

    ``level`` (#1038) selects how much of the audit taxonomy is captured:
    ``"business"`` records only real board transitions (dispatch, verdicts,
    merge, ...); ``"operational"`` (the default) additionally records the
    daemon-tick's autonomic actions (passive reconcile, merge-queue
    enqueue/drain, conflict-fix dispatch, housekeeping sweeps) tagged
    ``tier="operational"``, ``actor="daemon"``.  Business-tier rows are
    always recorded regardless of ``level`` — this only gates the
    operational tier.
    """

    max_rows: int = 0
    level: str = "operational"


@dataclass
class ModelRates:
    """Per-1M-token USD rates for one canonical model (#1118 ``pricing:`` block).

    Consumed by :mod:`coord.usage_rollup`'s cost estimator for legs that have
    no captured ``cost_usd``. All four fields default to ``0.0`` so a
    partially-specified override (e.g. only ``input``) still produces a
    valid (if incomplete) rate rather than raising.
    """

    input: float = 0.0
    output: float = 0.0
    cache_read: float = 0.0
    cache_creation: float = 0.0


def _default_pricing() -> dict[str, ModelRates]:
    """Built-in per-1M-token rates for the three canonical model tiers.

    Official Anthropic list pricing at time of writing (Sonnet/Opus/Haiku
    input+output list price; cache_read = 0.1x input, cache_creation = 1.25x
    input, the standard 5-minute-TTL cache economics) — verified against the
    live price list at review (#1118 review: the shipped Opus row previously
    regressed to 1/3 of the correct value; pinned exactly by
    ``test_pricing_absent_defaults_to_builtin_rates`` in
    ``tests/test_config_pricing.py`` so it can't silently drift again). A
    ``pricing:`` block in coordinator.yml overrides or extends any of these.
    """
    return {
        "sonnet": ModelRates(input=3.00, output=15.00, cache_read=0.30, cache_creation=3.75),
        "opus": ModelRates(input=15.00, output=75.00, cache_read=1.50, cache_creation=18.75),
        "haiku": ModelRates(input=1.00, output=5.00, cache_read=0.10, cache_creation=1.25),
    }


@dataclass
class PricingConfig:
    """``pricing:`` block (#1118) — per-canonical-model per-1M-token USD rates.

    ``models`` maps a canonical model key (``"sonnet"``, ``"opus"``,
    ``"haiku"``, or any operator-added key) to its :class:`ModelRates`. An
    absent ``pricing:`` block in coordinator.yml still yields the built-in
    defaults via :func:`_default_pricing`. A model key with no entry here
    (e.g. ``"(unknown)"``, or a genuinely unrecognized model string) has no
    rate — :mod:`coord.usage_rollup` treats that as "no estimate possible"
    and flags the group rather than silently reporting $0.
    """

    models: dict[str, ModelRates] = field(default_factory=_default_pricing)

    def rates_for(self, canonical_model: str) -> ModelRates | None:
        """Look up rates for a canonical model key, or ``None`` if unpriced."""
        return self.models.get(canonical_model)


@dataclass
class ProviderDef:
    """Definition of a single named worker-command provider.

    Corresponds to one entry under ``providers.definitions`` in
    ``coordinator.yml``.  All fields except ``type`` are optional.

    Attributes:
        type: Provider backend type.  Currently supported values are
            ``"claude"`` (legacy ``claude -p`` stream-json worker, the
            default) and ``"claude-pty"`` (interactive ``claude`` spawned
            inside a PTY for subscription-billed runs — see #425).  The
            authoritative list of registered backends is built by
            :func:`coord.providers.build_provider`.
        binary: Override the worker binary path/name.  ``None`` means the
            provider uses its own default (``"claude"`` for the claude
            backend).
        model: Pin this provider to a specific model id or alias.  Takes
            precedence over ``models.default`` for assignments routed to
            this provider.
        attach_url: Reserved for future attach-mode providers.
        env: Extra environment variables for the worker subprocess.
            Values may contain ``${VAR}`` placeholders which are expanded
            from :data:`os.environ` at parse time.
        extra_args: Additional command-line arguments appended to the
            worker argv.
    """

    type: str
    binary: str | None = None
    model: str | None = None
    attach_url: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    extra_args: list[str] = field(default_factory=list)


@dataclass
class ProvidersConfig:
    """Global provider registry.

    Parsed from the optional ``providers:`` block in ``coordinator.yml``.
    When the block is absent, ``default == "claude"`` and an implicit
    ``"claude"`` definition is present in ``definitions``.

    Attributes:
        default: The provider name used when no per-spec or per-repo
            override is set.  Defaults to ``"claude"``.
        definitions: Named provider definitions keyed by provider name.
            An implicit ``"claude"`` entry is always materialised if absent.
    """

    default: str = "claude"
    definitions: dict[str, ProviderDef] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Always ensure the implicit "claude" definition exists so callers
        # can look it up by name without checking for its presence.
        if "claude" not in self.definitions:
            self.definitions["claude"] = ProviderDef(type="claude")


@dataclass
class Config:
    repos: list[Repo]
    machines: list[Machine]
    hooks: HooksConfig = field(default_factory=HooksConfig)
    reviews: ReviewsConfig = field(default_factory=ReviewsConfig)
    concurrency: ConcurrencyConfig = field(default_factory=ConcurrencyConfig)
    smoke_tests: SmokeTestsConfig = field(default_factory=SmokeTestsConfig)
    acceptance: AcceptanceConfig = field(default_factory=AcceptanceConfig)
    models: ModelsConfig = field(default_factory=ModelsConfig)
    pipeline: PipelineConfig = field(default_factory=PipelineConfig)
    dispatch: DispatchConfig = field(default_factory=DispatchConfig)
    ci_store: CiStoreConfig = field(default_factory=CiStoreConfig)
    merge: MergeConfig = field(default_factory=MergeConfig)
    milestone: MilestoneConfig = field(default_factory=MilestoneConfig)
    providers: ProvidersConfig = field(default_factory=ProvidersConfig)
    audit: AuditConfig = field(default_factory=AuditConfig)
    pricing: PricingConfig = field(default_factory=PricingConfig)
    path: Path | None = None

    def repo(self, name: str) -> Repo | None:
        return next((r for r in self.repos if r.name == name), None)


def load(path: str | Path | None = None) -> Config:
    """Load and validate a coordinator.yml file.

    When ``path`` is None the location is resolved via
    :func:`resolve_config_path` (``$COORD_CONFIG`` → ``~/.coord/coordinator.yml``
    → ``./coordinator.yml``), so the tool works on a machine without a repo
    checkout.
    """
    p = Path(path).expanduser() if path is not None else resolve_config_path()
    if not p.exists():
        raise ConfigError(
            f"Config file not found: {p}. Create it at {USER_CONFIG_PATH} "
            f"(recommended — works without a repo checkout), pass --config <path>, "
            f"or set $COORD_CONFIG."
        )

    try:
        raw = yaml.safe_load(p.read_text())
    except yaml.YAMLError as e:
        raise ConfigError(f"Invalid YAML in {p}: {e}") from e

    if raw is None:
        raise ConfigError(f"Config file is empty: {p}")
    if not isinstance(raw, dict):
        raise ConfigError(f"Top-level config must be a mapping, got {type(raw).__name__}")

    repos = _parse_repos(raw.get("repos"))
    machines = _parse_machines(raw.get("machines"), repos)
    _validate_dependencies(repos)
    hooks = _parse_hooks(raw.get("hooks"))
    reviews = _parse_reviews(raw.get("reviews"), {r.name for r in repos})
    concurrency = _parse_concurrency(raw.get("concurrency"))
    smoke_tests = _parse_smoke_tests(raw.get("smoke_tests"))
    acceptance = _parse_acceptance(raw.get("acceptance"))
    models = _parse_models(raw.get("models"))
    pipeline = _parse_pipeline(raw.get("pipeline"))
    dispatch = _parse_dispatch(raw.get("dispatch"))
    ci_store = _parse_ci_store(raw.get("ci_store"))
    merge = _parse_merge(raw.get("merge"))
    milestone = _parse_milestone(raw.get("milestone"))
    providers = _parse_providers(raw.get("providers"))
    audit = _parse_audit(raw.get("audit"))
    pricing = _parse_pricing(raw.get("pricing"))

    return Config(
        repos=repos,
        machines=machines,
        hooks=hooks,
        reviews=reviews,
        concurrency=concurrency,
        smoke_tests=smoke_tests,
        acceptance=acceptance,
        models=models,
        pipeline=pipeline,
        dispatch=dispatch,
        ci_store=ci_store,
        merge=merge,
        milestone=milestone,
        providers=providers,
        audit=audit,
        pricing=pricing,
        path=p,
    )


def _parse_repos(raw: Any) -> list[Repo]:
    if raw is None:
        raise ConfigError("Config must define 'repos'")
    if not isinstance(raw, list):
        raise ConfigError("'repos' must be a list")
    if not raw:
        raise ConfigError("'repos' must contain at least one repo")

    repos: list[Repo] = []
    seen: set[str] = set()
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ConfigError(f"repos[{i}] must be a mapping, got {type(entry).__name__}")
        name = entry.get("name")
        github = entry.get("github")
        if not name or not isinstance(name, str):
            raise ConfigError(f"repos[{i}].name is required (string)")
        if not github or not isinstance(github, str):
            raise ConfigError(f"repos[{i}].github is required (string, 'owner/repo')")
        if "/" not in github:
            raise ConfigError(
                f"repos[{i}].github must be 'owner/repo', got {github!r}"
            )
        if name in seen:
            raise ConfigError(f"duplicate repo name: {name!r}")
        seen.add(name)

        depends_on = entry.get("depends_on", []) or []
        if not isinstance(depends_on, list) or not all(isinstance(d, str) for d in depends_on):
            raise ConfigError(f"repos[{i}].depends_on must be a list of repo names")

        default_branch = entry.get("default_branch", "main")
        if not isinstance(default_branch, str):
            raise ConfigError(f"repos[{i}].default_branch must be a string")

        build_command = entry.get("build_command")
        if build_command is not None and not isinstance(build_command, str):
            raise ConfigError(f"repos[{i}].build_command must be a string")
        test_command = entry.get("test_command")
        if test_command is not None and not isinstance(test_command, str):
            raise ConfigError(f"repos[{i}].test_command must be a string")
        # #296: run_cmd — optional shell command to launch the app for manual
        # smoke testing.  Surfaced in the TUI Test stage detail panel.
        run_cmd = entry.get("run_cmd")
        if run_cmd is not None and not isinstance(run_cmd, str):
            raise ConfigError(f"repos[{i}].run_cmd must be a string")

        worker_permissions = _parse_worker_permissions(entry.get("worker_permissions"), i)

        housekeeping = entry.get("housekeeping", []) or []
        if not isinstance(housekeeping, list) or not all(isinstance(h, str) for h in housekeeping):
            raise ConfigError(f"repos[{i}].housekeeping must be a list of strings")

        coordinator_only_files = entry.get("coordinator_only_files", []) or []
        if not isinstance(coordinator_only_files, list) or not all(isinstance(f, str) for f in coordinator_only_files):
            raise ConfigError(f"repos[{i}].coordinator_only_files must be a list of strings")

        # #268: reference_repos — sibling repos a worker may reference
        # for context but doesn't actually build against.
        reference_repos = entry.get("reference_repos", []) or []
        if not isinstance(reference_repos, list) or not all(isinstance(r, str) for r in reference_repos):
            raise ConfigError(f"repos[{i}].reference_repos must be a list of repo names")

        # #316: new_issue_guidance — inline markdown or repo-relative file path.
        new_issue_guidance = entry.get("new_issue_guidance")
        if new_issue_guidance is not None and not isinstance(new_issue_guidance, str):
            raise ConfigError(f"repos[{i}].new_issue_guidance must be a string")

        # #305: artifact_paths — glob patterns for build artifacts to stash.
        artifact_paths_raw = entry.get("artifact_paths", []) or []
        if not isinstance(artifact_paths_raw, list):
            raise ConfigError(f"repos[{i}].artifact_paths must be a list of strings")
        for j, p in enumerate(artifact_paths_raw):
            if not isinstance(p, str):
                raise ConfigError(
                    f"repos[{i}].artifact_paths[{j}] must be a string, "
                    f"got {type(p).__name__}"
                )
        artifact_paths: list[str] = list(artifact_paths_raw)

        # #323: optional per-repo provider override.
        repo_provider = entry.get("provider")
        if repo_provider is not None and not isinstance(repo_provider, str):
            raise ConfigError(f"repos[{i}].provider must be a string")

        repos.append(
            Repo(
                name=name,
                github=github,
                depends_on=depends_on,
                default_branch=default_branch,
                build_command=build_command,
                test_command=test_command,
                run_cmd=run_cmd,
                worker_permissions=worker_permissions,
                housekeeping=housekeeping,
                coordinator_only_files=coordinator_only_files,
                reference_repos=reference_repos,
                new_issue_guidance=new_issue_guidance,
                artifact_paths=artifact_paths,
                provider=repo_provider,
            )
        )
    return repos


def _parse_worker_permissions(raw: Any, repo_index: int) -> WorkerPermissionsConfig:
    """Parse the ``worker_permissions`` block for a single repo.

    When *raw* is ``None`` (key absent from YAML), the default deny-list is
    applied — safety by default.  An explicit ``deny: []`` clears restrictions.
    """
    if raw is None:
        return WorkerPermissionsConfig(deny=list(DEFAULT_DENY_COMMANDS))

    if not isinstance(raw, dict):
        raise ConfigError(
            f"repos[{repo_index}].worker_permissions must be a mapping"
        )

    allow = raw.get("allow", []) or []
    if not isinstance(allow, list) or not all(isinstance(a, str) for a in allow):
        raise ConfigError(
            f"repos[{repo_index}].worker_permissions.allow must be a list of strings"
        )

    deny = raw.get("deny", []) or []
    if not isinstance(deny, list) or not all(isinstance(d, str) for d in deny):
        raise ConfigError(
            f"repos[{repo_index}].worker_permissions.deny must be a list of strings"
        )

    return WorkerPermissionsConfig(allow=allow, deny=deny)


def _parse_machines(raw: Any, repos: list[Repo]) -> list[Machine]:
    if raw is None:
        raise ConfigError("Config must define 'machines'")
    if not isinstance(raw, list):
        raise ConfigError("'machines' must be a list")
    if not raw:
        raise ConfigError("'machines' must contain at least one machine")

    repo_names = {r.name for r in repos}
    machines: list[Machine] = []
    seen: set[str] = set()
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ConfigError(f"machines[{i}] must be a mapping, got {type(entry).__name__}")
        name = entry.get("name")
        host = entry.get("host")
        if not name or not isinstance(name, str):
            raise ConfigError(f"machines[{i}].name is required (string)")
        if not host or not isinstance(host, str):
            raise ConfigError(f"machines[{i}].host is required (string, tailscale hostname)")
        if name in seen:
            raise ConfigError(f"duplicate machine name: {name!r}")
        seen.add(name)

        capabilities = entry.get("capabilities", []) or []
        if not isinstance(capabilities, list) or not all(isinstance(c, str) for c in capabilities):
            raise ConfigError(f"machines[{i}].capabilities must be a list of strings")

        machine_repos = entry.get("repos", []) or []
        if not isinstance(machine_repos, list) or not all(isinstance(r, str) for r in machine_repos):
            raise ConfigError(f"machines[{i}].repos must be a list of repo names")

        unknown = [r for r in machine_repos if r not in repo_names]
        if unknown:
            raise ConfigError(
                f"machines[{i}] ({name!r}) references unknown repos: {unknown}"
            )

        repo_paths = entry.get("repo_paths", {}) or {}
        if not isinstance(repo_paths, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in repo_paths.items()
        ):
            raise ConfigError(f"machines[{i}].repo_paths must be a mapping of repo name → local path")
        unknown_paths = [r for r in repo_paths if r not in repo_names]
        if unknown_paths:
            raise ConfigError(
                f"machines[{i}] ({name!r}) repo_paths references unknown repos: {unknown_paths}"
            )

        machines.append(
            Machine(
                name=name,
                host=host,
                capabilities=capabilities,
                repos=machine_repos,
                repo_paths=repo_paths,
            )
        )
    return machines


KNOWN_HOOKS = {"close_merged_issues", "summary_report"}


def _parse_hooks(raw: Any) -> HooksConfig:
    if raw is None:
        return HooksConfig()
    if not isinstance(raw, dict):
        raise ConfigError("'hooks' must be a mapping")
    hooks = HooksConfig()
    for event_name in ("on_round_complete", "on_session_end"):
        entries = raw.get(event_name)
        if entries is None:
            continue
        if not isinstance(entries, list) or not all(isinstance(e, str) for e in entries):
            raise ConfigError(f"hooks.{event_name} must be a list of hook names")
        unknown = [e for e in entries if e not in KNOWN_HOOKS]
        if unknown:
            raise ConfigError(
                f"hooks.{event_name} references unknown hooks: {unknown}. "
                f"Known: {sorted(KNOWN_HOOKS)}"
            )
        setattr(hooks, event_name, entries)
    return hooks


def _parse_reviews(raw: Any, repo_names: set[str]) -> ReviewsConfig:
    if raw is None:
        return ReviewsConfig()
    if not isinstance(raw, dict):
        raise ConfigError("'reviews' must be a mapping")

    cfg = ReviewsConfig()
    for bool_field in ("enabled", "auto_dispatch", "require_approval", "allow_review_flood"):
        if bool_field in raw:
            value = raw[bool_field]
            if not isinstance(value, bool):
                raise ConfigError(f"reviews.{bool_field} must be a boolean")
            setattr(cfg, bool_field, value)

    for int_field in ("max_auto_dispatch_per_pass", "flood_threshold"):
        if int_field in raw:
            value = raw[int_field]
            # bool is a subclass of int — reject it explicitly so a stray
            # `flood_threshold: true` doesn't silently become 1.
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ConfigError(f"reviews.{int_field} must be a non-negative integer")
            setattr(cfg, int_field, value)

    if "reviewer_prompt" in raw:
        value = raw["reviewer_prompt"]
        if not isinstance(value, str):
            raise ConfigError("reviews.reviewer_prompt must be a string")
        cfg.reviewer_prompt = value

    checklist = raw.get("checklist", []) or []
    if not isinstance(checklist, list) or not all(isinstance(c, str) for c in checklist):
        raise ConfigError("reviews.checklist must be a list of strings")
    cfg.checklist = checklist

    overrides = raw.get("repo_overrides", {}) or {}
    if not isinstance(overrides, dict):
        raise ConfigError("reviews.repo_overrides must be a mapping of repo → list of strings")
    for repo_name, items in overrides.items():
        if not isinstance(repo_name, str):
            raise ConfigError("reviews.repo_overrides keys must be repo names")
        if repo_name not in repo_names:
            raise ConfigError(
                f"reviews.repo_overrides references unknown repo: {repo_name!r}"
            )
        if not isinstance(items, list) or not all(isinstance(i, str) for i in items):
            raise ConfigError(
                f"reviews.repo_overrides[{repo_name}] must be a list of strings"
            )
    cfg.repo_overrides = overrides
    return cfg


def _parse_concurrency(raw: Any) -> ConcurrencyConfig:
    if raw is None:
        return ConcurrencyConfig()
    if not isinstance(raw, dict):
        raise ConfigError("'concurrency' must be a mapping")
    cfg = ConcurrencyConfig()
    for key in (
        "max_workers", "stagger_seconds", "backoff_base", "max_retries",
        "stale_threshold", "first_output_timeout", "interactive_session_timeout_hours",
    ):
        val = raw.get(key)
        if val is None:
            continue
        if key in ("max_retries", "max_workers", "stale_threshold"):
            if not isinstance(val, int) or val < 0:
                raise ConfigError(f"concurrency.{key} must be a non-negative integer")
        else:
            # bool is a subclass of int — reject it explicitly for numeric keys.
            if isinstance(val, bool) or not isinstance(val, (int, float)) or val < 0:
                raise ConfigError(f"concurrency.{key} must be a non-negative number")
        setattr(cfg, key, val)
    if "auto_reassign" in raw:
        val = raw["auto_reassign"]
        if not isinstance(val, bool):
            raise ConfigError("concurrency.auto_reassign must be a boolean")
        cfg.auto_reassign = val
    if "bash_wrap_spawn" in raw:
        val = raw["bash_wrap_spawn"]
        if not isinstance(val, bool):
            raise ConfigError("concurrency.bash_wrap_spawn must be a boolean")
        cfg.bash_wrap_spawn = val
    return cfg


def _parse_smoke_tests(raw: Any) -> SmokeTestsConfig:
    if raw is None:
        return SmokeTestsConfig()
    if not isinstance(raw, dict):
        raise ConfigError("'smoke_tests' must be a mapping")

    cfg = SmokeTestsConfig()
    if "auto_queue" in raw:
        value = raw["auto_queue"]
        if not isinstance(value, bool):
            raise ConfigError("smoke_tests.auto_queue must be a boolean")
        cfg.auto_queue = value

    if "default_command" in raw:
        value = raw["default_command"]
        if value is not None and not isinstance(value, str):
            raise ConfigError("smoke_tests.default_command must be a string")
        cfg.default_command = value

    if "timeout_seconds" in raw:
        value = raw["timeout_seconds"]
        if not isinstance(value, int) or value <= 0:
            raise ConfigError("smoke_tests.timeout_seconds must be a positive integer")
        cfg.timeout_seconds = value

    rules_raw = raw.get("capability_rules", []) or []
    if not isinstance(rules_raw, list):
        raise ConfigError("smoke_tests.capability_rules must be a list")
    rules: list[SmokeRule] = []
    for i, entry in enumerate(rules_raw):
        if not isinstance(entry, dict):
            raise ConfigError(
                f"smoke_tests.capability_rules[{i}] must be a mapping"
            )
        files = entry.get("files", []) or []
        requires = entry.get("requires", []) or []
        if not isinstance(files, list) or not all(isinstance(f, str) for f in files):
            raise ConfigError(
                f"smoke_tests.capability_rules[{i}].files must be a list of strings"
            )
        if not isinstance(requires, list) or not all(isinstance(r, str) for r in requires):
            raise ConfigError(
                f"smoke_tests.capability_rules[{i}].requires must be a list of strings"
            )
        if not files:
            raise ConfigError(
                f"smoke_tests.capability_rules[{i}].files must be non-empty"
            )
        if not requires:
            raise ConfigError(
                f"smoke_tests.capability_rules[{i}].requires must be non-empty"
            )
        rules.append(SmokeRule(files=files, requires=requires))
    cfg.capability_rules = rules
    return cfg


def _parse_acceptance(raw: Any) -> AcceptanceConfig:
    """Parse the ``acceptance:`` block (#944, docs/ORACLE_LOOP.md).

    ``acceptance.drivers`` maps a local repo name (as declared under
    ``repos:``) to its driver config. Absent entirely -> no repo has a sealed
    acceptance suite, and ``coord acceptance run/record`` refuses with a
    clear error rather than guessing a default.
    """
    if raw is None:
        return AcceptanceConfig()
    if not isinstance(raw, dict):
        raise ConfigError("'acceptance' must be a mapping")

    drivers_raw = raw.get("drivers", {}) or {}
    if not isinstance(drivers_raw, dict):
        raise ConfigError(
            "acceptance.drivers must be a mapping of repo name -> driver config"
        )

    drivers: dict[str, AcceptanceDriverConfig] = {}
    for repo_name, entry in drivers_raw.items():
        if not isinstance(entry, dict):
            raise ConfigError(f"acceptance.drivers[{repo_name!r}] must be a mapping")

        routes_raw = entry.get("routes")
        if routes_raw is not None:
            # #1125 review finding 5: a routed entry's flat kind/run/mock/
            # capability fields are unused (each route carries its own) — an
            # operator who sets both almost certainly meant one or the
            # other, so reject it rather than silently discarding the flat
            # fields.
            flat_fields = [
                f for f in ("kind", "run", "mock", "capability") if entry.get(f)
            ]
            if flat_fields:
                raise ConfigError(
                    f"acceptance.drivers[{repo_name!r}] sets both 'routes' "
                    f"and flat field(s) {flat_fields!r} — a routed entry's "
                    "driver is entirely per-route; remove the flat fields "
                    "(they would otherwise be silently ignored)"
                )
            drivers[repo_name] = AcceptanceDriverConfig(
                routes=_parse_acceptance_routes(repo_name, routes_raw),
            )
            continue

        kind = entry.get("kind")
        if not kind or not isinstance(kind, str):
            raise ConfigError(f"acceptance.drivers[{repo_name!r}].kind is required")

        run = entry.get("run")
        if not run or not isinstance(run, str):
            raise ConfigError(f"acceptance.drivers[{repo_name!r}].run is required")

        mock = entry.get("mock", "") or ""
        if not isinstance(mock, str):
            raise ConfigError(f"acceptance.drivers[{repo_name!r}].mock must be a string")

        capability = entry.get("capability", "") or ""
        if not isinstance(capability, str):
            raise ConfigError(
                f"acceptance.drivers[{repo_name!r}].capability must be a string"
            )

        drivers[repo_name] = AcceptanceDriverConfig(
            kind=kind, run=run, mock=mock, capability=capability,
        )

    return AcceptanceConfig(drivers=drivers)


def _parse_acceptance_routes(
    repo_name: str, routes_raw: Any,
) -> list[AcceptanceDriverConfig]:
    """Parse ``acceptance.drivers.<repo_name>.routes`` (#1125) into a list of
    ``AcceptanceDriverConfig`` route entries, each with ``match`` set.

    Each element is validated the same way as a flat driver entry
    (``kind``/``run`` required, ``mock``/``capability`` optional strings),
    plus a required ``match`` glob.
    """
    if not isinstance(routes_raw, list) or not routes_raw:
        raise ConfigError(
            f"acceptance.drivers[{repo_name!r}].routes must be a non-empty list"
        )

    routes: list[AcceptanceDriverConfig] = []
    for i, route_entry in enumerate(routes_raw):
        if not isinstance(route_entry, dict):
            raise ConfigError(
                f"acceptance.drivers[{repo_name!r}].routes[{i}] must be a mapping"
            )

        match = route_entry.get("match")
        if not match or not isinstance(match, str):
            raise ConfigError(
                f"acceptance.drivers[{repo_name!r}].routes[{i}].match is required"
            )

        kind = route_entry.get("kind")
        if not kind or not isinstance(kind, str):
            raise ConfigError(
                f"acceptance.drivers[{repo_name!r}].routes[{i}].kind is required"
            )

        run = route_entry.get("run")
        if not run or not isinstance(run, str):
            raise ConfigError(
                f"acceptance.drivers[{repo_name!r}].routes[{i}].run is required"
            )

        mock = route_entry.get("mock", "") or ""
        if not isinstance(mock, str):
            raise ConfigError(
                f"acceptance.drivers[{repo_name!r}].routes[{i}].mock must be a string"
            )

        capability = route_entry.get("capability", "") or ""
        if not isinstance(capability, str):
            raise ConfigError(
                f"acceptance.drivers[{repo_name!r}].routes[{i}].capability must be a string"
            )

        routes.append(
            AcceptanceDriverConfig(
                kind=kind, run=run, mock=mock, capability=capability, match=match,
            )
        )

    return routes


def _parse_models(raw: Any) -> ModelsConfig:
    if raw is None:
        return ModelsConfig()
    if not isinstance(raw, dict):
        raise ConfigError("'models' must be a mapping")

    cfg = ModelsConfig()
    if "default" in raw:
        value = raw["default"]
        if not isinstance(value, str) or not value:
            raise ConfigError("models.default must be a non-empty string")
        cfg.default = value

    if "escalation" in raw:
        value = raw["escalation"]
        if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
            raise ConfigError("models.escalation must be a list of non-empty strings")
        cfg.escalation = list(value)

    if "labels" in raw:
        value = raw["labels"]
        if not isinstance(value, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in value.items()
        ):
            raise ConfigError(
                "models.labels must be a mapping of label name → model alias"
            )
        cfg.labels = dict(value)

    if "versions" in raw:
        value = raw["versions"]
        if not isinstance(value, dict) or not all(
            isinstance(k, str) and k and isinstance(v, str) and v
            for k, v in value.items()
        ):
            raise ConfigError(
                "models.versions must be a mapping of alias → exact model id"
            )
        cfg.versions = dict(value)

    return cfg


def _parse_pipeline(raw: Any) -> PipelineConfig:
    if raw is None:
        return PipelineConfig()
    if not isinstance(raw, dict):
        raise ConfigError("'pipeline' must be a mapping")

    cfg = PipelineConfig()

    if "default_gates" in raw:
        value = raw["default_gates"]
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ConfigError("pipeline.default_gates must be a list of strings")
        cfg.default_gates = list(value)

    if "labels" in raw:
        value = raw["labels"]
        if not isinstance(value, dict):
            raise ConfigError("pipeline.labels must be a mapping of label → list of strings")
        for k, v in value.items():
            if not isinstance(k, str):
                raise ConfigError("pipeline.labels keys must be strings")
            if not isinstance(v, list) or not all(isinstance(g, str) for g in v):
                raise ConfigError(
                    f"pipeline.labels[{k!r}] must be a list of gate name strings"
                )
        cfg.labels = {k: list(v) for k, v in value.items()}

    if "auto_loop" in raw:
        value = raw["auto_loop"]
        if not isinstance(value, bool):
            raise ConfigError("pipeline.auto_loop must be a boolean")
        cfg.auto_loop = value

    if "max_review_iterations" in raw:
        value = raw["max_review_iterations"]
        if not isinstance(value, int) or value < 1:
            raise ConfigError("pipeline.max_review_iterations must be a positive integer")
        cfg.max_review_iterations = value

    if "escalate_fix_model" in raw:
        value = raw["escalate_fix_model"]
        if not isinstance(value, bool):
            raise ConfigError("pipeline.escalate_fix_model must be a boolean")
        cfg.escalate_fix_model = value

    if "attention_thresholds" in raw:
        value = raw["attention_thresholds"]
        if not isinstance(value, dict):
            raise ConfigError(
                "pipeline.attention_thresholds must be a mapping of "
                "assignment type -> duration (e.g. '45m', '15m', or seconds)"
            )
        parsed: dict[str, float] = {}
        for k, v in value.items():
            if not isinstance(k, str):
                raise ConfigError("pipeline.attention_thresholds keys must be strings")
            parsed[k] = _parse_duration_seconds(
                v, context=f"pipeline.attention_thresholds[{k!r}]"
            )
        cfg.attention_thresholds = parsed

    if "convergence_rounds" in raw:
        value = raw["convergence_rounds"]
        if not isinstance(value, int) or value < 1:
            raise ConfigError("pipeline.convergence_rounds must be a positive integer")
        cfg.convergence_rounds = value

    return cfg


_DURATION_UNIT_SECONDS: dict[str, float] = {
    "s": 1.0,
    "m": 60.0,
    "h": 3600.0,
    "d": 86400.0,
}


def _parse_duration_seconds(value: Any, *, context: str) -> float:
    """Parse a duration into seconds. Accepts a bare number (seconds) or a
    string like ``"45m"``, ``"15m"``, ``"2h"``, ``"90s"``. Used for
    ``pipeline.attention_thresholds`` (#846)."""
    if isinstance(value, bool):
        raise ConfigError(f"{context} must be a number of seconds or a duration string")
    if isinstance(value, (int, float)):
        if value <= 0:
            raise ConfigError(f"{context} must be a positive duration")
        return float(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text and text[-1] in _DURATION_UNIT_SECONDS and text[:-1].strip():
            number_part = text[:-1].strip()
            try:
                number = float(number_part)
            except ValueError:
                pass
            else:
                if number <= 0:
                    raise ConfigError(f"{context} must be a positive duration")
                return number * _DURATION_UNIT_SECONDS[text[-1]]
        try:
            number = float(text)
        except ValueError:
            raise ConfigError(
                f"{context} must be a number of seconds or a duration string "
                f"like '45m', '15m', '2h' (got {value!r})"
            ) from None
        if number <= 0:
            raise ConfigError(f"{context} must be a positive duration")
        return number
    raise ConfigError(f"{context} must be a number of seconds or a duration string")


def _parse_dispatch(raw: Any) -> DispatchConfig:
    if raw is None:
        return DispatchConfig()
    if not isinstance(raw, dict):
        raise ConfigError("'dispatch' must be a mapping")

    cfg = DispatchConfig()

    if "max_files_per_worker" in raw:
        value = raw["max_files_per_worker"]
        if not isinstance(value, int) or value < 1:
            raise ConfigError("dispatch.max_files_per_worker must be a positive integer")
        cfg.max_files_per_worker = value

    if "auto_split" in raw:
        value = raw["auto_split"]
        if not isinstance(value, bool):
            raise ConfigError("dispatch.auto_split must be a boolean")
        cfg.auto_split = value

    if "require_plan" in raw:
        value = raw["require_plan"]
        if not isinstance(value, bool):
            raise ConfigError("dispatch.require_plan must be a boolean")
        cfg.require_plan = value

    return cfg


def _parse_ci_store(raw: Any) -> CiStoreConfig:
    if raw is None:
        return CiStoreConfig()
    if not isinstance(raw, dict):
        raise ConfigError("'ci_store' must be a mapping")

    cfg = CiStoreConfig()
    if "type" in raw:
        value = raw["type"]
        if not isinstance(value, str) or value not in ("github", "none"):
            raise ConfigError("ci_store.type must be one of: github, none")
        cfg.type = value
    return cfg


def _parse_merge(raw: Any) -> MergeConfig:
    """Parse the optional ``merge:`` block from coordinator.yml.

    An absent block returns ``MergeConfig()`` — ``auto_drain=False`` —
    preserving existing behaviour: the daemon never merges automatically.
    """
    if raw is None:
        return MergeConfig()
    if not isinstance(raw, dict):
        raise ConfigError("'merge' must be a mapping")

    cfg = MergeConfig()
    if "auto_drain" in raw:
        value = raw["auto_drain"]
        if not isinstance(value, bool):
            raise ConfigError("merge.auto_drain must be a boolean")
        cfg.auto_drain = value
    if "max_per_tick" in raw:
        value = raw["max_per_tick"]
        if not isinstance(value, int) or value < 0:
            raise ConfigError("merge.max_per_tick must be a non-negative integer")
        cfg.max_per_tick = value
    if "auto_reap_merged" in raw:
        value = raw["auto_reap_merged"]
        if not isinstance(value, bool):
            raise ConfigError("merge.auto_reap_merged must be a boolean")
        cfg.auto_reap_merged = value
    return cfg


def _parse_milestone(raw: Any) -> MilestoneConfig:
    """Parse the optional ``milestone:`` block from coordinator.yml.

    An absent block returns ``MilestoneConfig()`` — ``auto_dispatch=False`` —
    preserving existing behaviour: the daemon never auto-drains a milestone's
    work order; `coord milestone dispatch` still dispatches the ready
    frontier once per invocation.
    """
    if raw is None:
        return MilestoneConfig()
    if not isinstance(raw, dict):
        raise ConfigError("'milestone' must be a mapping")

    cfg = MilestoneConfig()
    if "auto_dispatch" in raw:
        value = raw["auto_dispatch"]
        if not isinstance(value, bool):
            raise ConfigError("milestone.auto_dispatch must be a boolean")
        cfg.auto_dispatch = value
    return cfg


_VALID_AUDIT_LEVELS = ("business", "operational")


def _parse_audit(raw: Any) -> AuditConfig:
    """Parse the optional ``audit:`` block from coordinator.yml (#1036/#1038).

    An absent block returns ``AuditConfig()`` — ``max_rows=0`` (unlimited)
    and ``level="operational"`` — preserving existing behaviour:
    ``coord.audit.record_audit`` never trims and captures both tiers.
    """
    if raw is None:
        return AuditConfig()
    if not isinstance(raw, dict):
        raise ConfigError("'audit' must be a mapping")

    cfg = AuditConfig()
    if "max_rows" in raw:
        value = raw["max_rows"]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ConfigError("audit.max_rows must be a non-negative integer")
        cfg.max_rows = value
    if "level" in raw:
        value = raw["level"]
        if not isinstance(value, str) or value not in _VALID_AUDIT_LEVELS:
            raise ConfigError(
                f"audit.level must be one of {_VALID_AUDIT_LEVELS!r}, got {value!r}"
            )
        cfg.level = value
    return cfg


_PRICING_RATE_FIELDS = ("input", "output", "cache_read", "cache_creation")


def _parse_pricing(raw: Any) -> PricingConfig:
    """Parse the optional ``pricing:`` block from coordinator.yml (#1118).

    An absent block returns ``PricingConfig()`` — the built-in sonnet/opus/
    haiku defaults from :func:`_default_pricing`. Each entry under
    ``pricing:`` overrides or extends a canonical model key; unspecified
    rate fields on an *existing* key (e.g. ``opus``) keep the built-in
    default rather than being zeroed, so an operator can bump just
    ``pricing.opus.output`` without restating the other three rates. A
    wholly new model key starts from ``ModelRates()`` (all zero) and is
    filled in from whatever fields are given.
    """
    models = _default_pricing()
    if raw is None:
        return PricingConfig(models=models)
    if not isinstance(raw, dict):
        raise ConfigError("'pricing' must be a mapping of model name -> rates")

    for model_key, entry in raw.items():
        if not isinstance(model_key, str) or not model_key:
            raise ConfigError("pricing keys must be non-empty strings")
        if not isinstance(entry, dict):
            raise ConfigError(f"pricing[{model_key!r}] must be a mapping")

        base = models.get(model_key, ModelRates())
        rates = ModelRates(
            input=base.input,
            output=base.output,
            cache_read=base.cache_read,
            cache_creation=base.cache_creation,
        )
        for rate_field in _PRICING_RATE_FIELDS:
            if rate_field not in entry:
                continue
            value = entry[rate_field]
            if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
                raise ConfigError(
                    f"pricing[{model_key!r}].{rate_field} must be a non-negative number"
                )
            setattr(rates, rate_field, float(value))
        models[model_key] = rates

    return PricingConfig(models=models)


_ENV_VAR_RE = re.compile(r"\$\{([^}]+)\}")


def _expand_env_vars(value: str) -> str:
    """Expand ``${VAR}`` placeholders in *value* using :data:`os.environ`.

    Unset variables are left as-is (e.g. ``${MISSING}`` stays
    ``"${MISSING}"``).  Only ``${VAR}`` syntax is supported — bare ``$VAR``
    is not expanded.
    """

    def _replace(m: re.Match) -> str:  # type: ignore[type-arg]
        var = m.group(1)
        return os.environ.get(var, m.group(0))

    return _ENV_VAR_RE.sub(_replace, value)


def _parse_providers(raw: Any) -> ProvidersConfig:
    """Parse the optional ``providers:`` block from coordinator.yml.

    An absent block returns ``ProvidersConfig()`` — ``default == "claude"``
    and an implicit ``"claude"`` definition present.  An explicit block may
    override ``default`` and/or add named definitions.  Values in
    ``definitions[*].env`` undergo ``${VAR}`` expansion against
    :data:`os.environ`.
    """
    if raw is None:
        return ProvidersConfig()
    if not isinstance(raw, dict):
        raise ConfigError("'providers' must be a mapping")

    cfg = ProvidersConfig()

    if "default" in raw:
        value = raw["default"]
        if not isinstance(value, str) or not value:
            raise ConfigError("providers.default must be a non-empty string")
        cfg.default = value

    defs_raw = raw.get("definitions", {}) or {}
    if not isinstance(defs_raw, dict):
        raise ConfigError("providers.definitions must be a mapping")

    for def_name, def_raw in defs_raw.items():
        if not isinstance(def_name, str):
            raise ConfigError("providers.definitions keys must be strings")
        if not isinstance(def_raw, dict):
            raise ConfigError(
                f"providers.definitions[{def_name!r}] must be a mapping"
            )

        ptype = def_raw.get("type")
        if not ptype or not isinstance(ptype, str):
            raise ConfigError(
                f"providers.definitions[{def_name!r}].type is required (string)"
            )

        binary = def_raw.get("binary")
        if binary is not None and not isinstance(binary, str):
            raise ConfigError(
                f"providers.definitions[{def_name!r}].binary must be a string"
            )

        model = def_raw.get("model")
        if model is not None and not isinstance(model, str):
            raise ConfigError(
                f"providers.definitions[{def_name!r}].model must be a string"
            )

        attach_url = def_raw.get("attach_url")
        if attach_url is not None and not isinstance(attach_url, str):
            raise ConfigError(
                f"providers.definitions[{def_name!r}].attach_url must be a string"
            )

        env_raw = def_raw.get("env", {}) or {}
        if not isinstance(env_raw, dict):
            raise ConfigError(
                f"providers.definitions[{def_name!r}].env must be a mapping"
            )
        for k, v in env_raw.items():
            if not isinstance(k, str) or not isinstance(v, str):
                raise ConfigError(
                    f"providers.definitions[{def_name!r}].env must map strings to strings"
                )
        # Expand ${VAR} in env values.
        env: dict[str, str] = {k: _expand_env_vars(v) for k, v in env_raw.items()}

        extra_args_raw = def_raw.get("extra_args", []) or []
        if not isinstance(extra_args_raw, list) or not all(
            isinstance(a, str) for a in extra_args_raw
        ):
            raise ConfigError(
                f"providers.definitions[{def_name!r}].extra_args must be a list of strings"
            )
        extra_args: list[str] = list(extra_args_raw)

        cfg.definitions[def_name] = ProviderDef(
            type=ptype,
            binary=binary,
            model=model,
            attach_url=attach_url,
            env=env,
            extra_args=extra_args,
        )

    # Belt-and-suspenders: ProvidersConfig.__post_init__ already
    # materialises the implicit "claude" entry when ProvidersConfig() is
    # constructed above (line ~904), so this branch is unreachable under
    # current code.  Kept as a guard against future refactors that might
    # construct ProvidersConfig differently (e.g. via dict-update or
    # bypassing __post_init__ with object.__new__) — the invariant
    # "definitions always contains 'claude'" is load-bearing for
    # resolve_provider_name() callers that look up the definition
    # without checking presence first.
    if "claude" not in cfg.definitions:
        cfg.definitions["claude"] = ProviderDef(type="claude")

    return cfg


def _validate_dependencies(repos: list[Repo]) -> None:
    from coord.deps import detect_cycles

    repo_names = {r.name for r in repos}
    for r in repos:
        unknown = [d for d in r.depends_on if d not in repo_names]
        if unknown:
            raise ConfigError(
                f"repo {r.name!r} depends_on unknown repos: {unknown}"
            )
        if r.name in r.depends_on:
            raise ConfigError(f"repo {r.name!r} cannot depend on itself")

        # #268: reference_repos go through the same name-resolution as
        # depends_on but DO NOT feed into the cycle detector — the
        # intent is precisely to allow back-references (vimcode →
        # quadraui in depends_on; quadraui → vimcode in reference_repos)
        # that would be cycles if treated as build deps.
        unknown_ref = [r2 for r2 in r.reference_repos if r2 not in repo_names]
        if unknown_ref:
            raise ConfigError(
                f"repo {r.name!r} reference_repos unknown repos: {unknown_ref}"
            )
        if r.name in r.reference_repos:
            raise ConfigError(
                f"repo {r.name!r} cannot reference itself"
            )

    cycles = detect_cycles(repos)
    if cycles:
        cycle_str = " → ".join(cycles[0])
        raise ConfigError(f"circular dependency detected: {cycle_str}")
