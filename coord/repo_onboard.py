"""Repo onboarding: the five layers a repo must clear to actually be part of
the fleet, and a verifier that answers *is it true right now* (#2220).

Adding a repo is ~14 steps across five layers — config, machines, GitHub, the
repo's own contents, and the graph — and **every one of them fails silently**.
Nothing reports a half-onboarded repo; it just behaves like a working one that
never quite gets anywhere. ``stick-demo#1`` went terminally ``blocked`` after
burning both drive attempts because dellserver's agent had never re-read config
(#2219) — ``coord config``, ``coord status`` and ``coord assign --dry-run`` all
said the dispatch was fine, because all three read *config*, and config was
correct. Only the live agent knew better.

That is the design constraint this module is built around: **read live state,
not config.** A finding here is only worth having if it could not have been
produced by reading ``coordinator.yml`` alone.

The five layers, and what each one silently costs when missed:

1. **Config** — the ``repos:`` entry and each machine's ``repos:`` list. A
   ``default_branch`` that doesn't match the repo's real default routes worker
   PRs to the wrong base.
2. **Machines** — the ``~/src/<repo>`` clone (the *worker worktree base*, not a
   convenience checkout) and an agent that has actually re-read config since
   the repo was added. The repo list is frozen at process start (#2219).
3. **GitHub** — the ``coord`` label (without it issues are live but invisible
   to the Pipeline), the tier labels (model routing), and at least one workflow
   triggering on ``pull_request`` (without it ``expects_checks()`` reads "CI
   exists" while zero checks ever arrive, and ``checks_absent`` blocks *every*
   merge in that repo, forever).
4. **Repo contents** — a ``CLAUDE.md`` (the Test agent auto-loads it and the
   adversarial review prompt is assembled from it; without one, reviews enforce
   nothing) and a Test-stage command the runner can resolve.
5. **Graph** — graphify build + hook install + ``core.hooksPath``, in that
   order. A half-installed machine looks identical to a working one; it just
   answers from grep.

Shape mirrors :mod:`coord.fleet_config_health` and :mod:`coord.graph_health`:
a **facts** layer that does the I/O, a **pure** evaluator over those facts, and
a renderer. The split is what makes the black-box test in
``tests/test_repo_onboard_2220.py`` possible — a seeded fleet with a
deliberately half-onboarded repo produces one distinct, *named* finding per
defect, with no network and no live agents.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# ── Severities ───────────────────────────────────────────────────────────────
CRIT = "crit"
WARN = "warn"
OK = "ok"
UNKNOWN = "unknown"

_SEVERITY_MARK = {CRIT: "✗ CRIT", WARN: "⚠ WARN", OK: "✓", UNKNOWN: "?"}
_SEVERITY_RANK = {CRIT: 0, WARN: 1, UNKNOWN: 2, OK: 3}

# The five layers, in onboarding order. Rendered in this order so the report
# reads like the runbook it replaces.
LAYERS: tuple[str, ...] = ("config", "machines", "github", "contents", "graph")

# GitHub labels onboarding creates / requires.
COORD_LABEL = "coord"
TIER_LABELS: tuple[str, ...] = ("tier:small", "tier:large")


@dataclass(frozen=True)
class Finding:
    """One checkable statement about one layer of one repo's onboarding.

    ``check`` is a **stable dotted id** (``machines.agent_repo_skew``), not a
    rendered string: it is what the black-box test asserts on, what a future
    ``--json`` consumer keys off, and what keeps "one distinct, named finding
    per defect" from decaying into a generic failure line.
    """

    layer: str
    check: str
    severity: str
    summary: str
    subject: str | None = None
    fix: str | None = None

    @property
    def is_problem(self) -> bool:
        return self.severity in (CRIT, WARN)


# ── Facts (the I/O boundary) ─────────────────────────────────────────────────


@dataclass
class MachineFacts:
    """What one machine says about one repo — config side AND live side.

    ``published_repos``/``degraded`` come straight out of that agent's
    ``/health``. They are the whole point: ``published_repos`` is
    ``AgentServer._servable_repos()``'s FILTERED list, so a repo missing from
    it is missing for one of two very different reasons, and the remedies do
    not overlap (see :func:`evaluate_machines`).
    """

    name: str
    declared: bool = False
    repo_path: str | None = None
    reachable: bool = True
    unreachable_reason: str | None = None
    # None when unreachable (or when the agent predates the field) — never [].
    published_repos: list[str] | None = None
    degraded: dict[str, str] = field(default_factory=dict)
    config_free: str | None = None


@dataclass
class GithubFacts:
    """Live GitHub state for the repo. Every ``*_error`` is a *distinct* value
    from "absent": a probe that couldn't run must never render as a clean
    pass, and must never render as the defect either (#1525's rule, applied
    here)."""

    slug: str | None = None
    labels: list[str] | None = None
    labels_error: str | None = None
    default_branch: str | None = None
    default_branch_error: str | None = None
    workflow_count: int | None = None
    # Names/paths of workflows whose `on:` includes `pull_request`.
    pr_triggered_workflows: list[str] | None = None
    workflow_error: str | None = None
    claude_md_present: bool | None = None
    claude_md_error: str | None = None


@dataclass
class GraphFacts:
    """Graph readiness on the machine running the check. ``probed=False`` means
    there is no local clone here to look at — a skip, not a pass."""

    probed: bool = False
    repo_path: str | None = None
    built: bool = False
    fresh: bool = False
    detail: str | None = None
    hooks_installed: bool = False
    hooks_detail: str | None = None
    # #2236: does the repo TRACK `.githooks/post-checkout` at all? Two
    # distinct failures hide behind `hooks_installed=False`, and they take
    # opposite fixes: a repo that ships the hook but hasn't pointed
    # `core.hooksPath` at it needs one `git config`; a repo that never ported
    # the hook (coord-portal, stick-demo) needs the files first — running
    # `git config core.hooksPath .githooks` there points git at a directory
    # that does not exist, which silently disables ALL hooks for that
    # checkout. Telling an operator the wrong one is worse than saying
    # nothing, so the fix line has to know which it is.
    hooks_shipped: bool = False


@dataclass
class RepoFacts:
    """Everything :func:`evaluate` needs, and nothing it has to fetch itself."""

    name: str
    configured: bool = False
    github: str | None = None
    config_default_branch: str | None = None
    config_develop_branch: str | None = None
    build_command: str | None = None
    # The RESOLVED Test-stage command (`coord.smoke.resolve_smoke_command`),
    # not the raw `test_command` — the gate's question, not the field's.
    smoke_command: str | None = None
    smoke_command_source: str | None = None
    capability_rule_count: int = 0
    has_acceptance_driver: bool = False
    machines: list[MachineFacts] = field(default_factory=list)
    gh: GithubFacts = field(default_factory=GithubFacts)
    graph: GraphFacts = field(default_factory=GraphFacts)


@dataclass
class RepoDoctorReport:
    repo_name: str
    findings: list[Finding] = field(default_factory=list)

    @property
    def crits(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == CRIT]

    @property
    def warns(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == WARN]

    @property
    def ok(self) -> bool:
        """No CRIT. Warnings do not gate — they are onboarding residue an
        operator can accept (no capability rules, no acceptance driver), not
        the states that make a repo silently un-dispatchable."""
        return not self.crits

    def for_layer(self, layer: str) -> list[Finding]:
        return [f for f in self.findings if f.layer == layer]


# ── Gathering the facts (the only I/O in this module) ────────────────────────


def workflow_triggers_on_pull_request(content: str) -> bool:
    """True when a GitHub Actions workflow's YAML *content* has a
    ``pull_request`` trigger.

    Pure, and deliberately not a substring search: ``pull_request`` appears in
    ``github.event.pull_request.number`` expressions inside steps of workflows
    that only trigger on ``push``, which is exactly the repo shape this check
    exists to catch — a grep would call it green.

    YAML 1.1 (which PyYAML implements) parses the bare key ``on`` as the
    *boolean* ``True``, so the trigger block lands under the key ``True``, not
    ``"on"``. Quoted (``"on":``) and flow spellings land under ``"on"``. Both
    are accepted; a workflow file that fails to parse is reported as "no
    pull_request trigger found" by returning ``False`` — the caller
    distinguishes that from "could not read" by whether it got content at all.
    """
    import yaml  # noqa: PLC0415 — keep module import-light

    try:
        doc = yaml.safe_load(content)
    except yaml.YAMLError:
        return False
    if not isinstance(doc, dict):
        return False
    triggers = doc.get(True, doc.get("on"))
    if triggers is None:
        return False
    if isinstance(triggers, str):
        return triggers == "pull_request"
    if isinstance(triggers, list):
        return "pull_request" in triggers
    if isinstance(triggers, dict):
        return "pull_request" in triggers
    return False


def gather_github_facts(
    slug: str | None,
    *,
    default_branch_hint: str | None = None,
    ops=None,
) -> GithubFacts:
    """Probe live GitHub state for *slug* (``owner/repo``).

    *ops* is the :mod:`coord.github_ops` module by default and exists purely
    as a test seam — every call below is a ``gh`` shell-out otherwise.

    Each probe is independently guarded: a repo whose labels are unreadable
    still gets a real workflow verdict. Failures are recorded as ``*_error``
    strings, never as absence — see :class:`GithubFacts`.
    """
    facts = GithubFacts(slug=slug)
    if not slug:
        return facts
    if ops is None:
        from coord import github_ops as ops  # noqa: PLC0415

    try:
        facts.default_branch = ops.get_repo_default_branch(slug)
    except Exception as exc:  # noqa: BLE001 — a probe, never a crash
        facts.default_branch_error = str(exc)

    try:
        facts.labels = ops.list_repo_labels(slug)
    except Exception as exc:  # noqa: BLE001
        facts.labels_error = str(exc)

    branch = facts.default_branch or default_branch_hint or "main"

    try:
        workflows = ops.list_repo_workflows(slug)
        facts.workflow_count = len(workflows)
        pr_triggered: list[str] = []
        unreadable: list[str] = []
        for wf in workflows:
            path = wf.get("path") or ""
            name = wf.get("name") or path or "?"
            if not path:
                continue
            try:
                content = ops.get_repo_file(slug, path, branch)
            except Exception:  # noqa: BLE001
                unreadable.append(name)
                continue
            if workflow_triggers_on_pull_request(content):
                pr_triggered.append(name)
        facts.pr_triggered_workflows = pr_triggered
        if unreadable and not pr_triggered:
            # Never let an unreadable workflow file masquerade as a proven
            # "nothing triggers on pull_request" CRIT.
            facts.workflow_error = (
                f"could not read {len(unreadable)} workflow file(s) "
                f"({', '.join(unreadable)}) on branch {branch!r}"
            )
    except Exception as exc:  # noqa: BLE001
        facts.workflow_error = str(exc)

    try:
        facts.claude_md_present = ops.repo_file_exists(slug, "CLAUDE.md", branch)
    except Exception as exc:  # noqa: BLE001
        facts.claude_md_error = str(exc)

    return facts


def gather_graph_facts(repo_path: Path | None, default_branch: str = "main") -> GraphFacts:
    """Graph readiness for a *local* clone. ``probed=False`` when there is no
    clone on this machine — a skip, never a pass."""
    if repo_path is None or not repo_path.exists():
        return GraphFacts(probed=False, repo_path=str(repo_path) if repo_path else None)

    from coord import graph_health  # noqa: PLC0415

    facts = GraphFacts(probed=True, repo_path=str(repo_path))
    try:
        st = graph_health.graph_status(repo_path, default_branch)
    except Exception as exc:  # noqa: BLE001
        facts.detail = f"graph probe failed: {exc}"
        return facts
    facts.built = bool(st.present)
    facts.fresh = bool(st.present and not st.stale)
    if st.unknown_reason:
        facts.detail = st.unknown_reason
    elif st.stale:
        facts.detail = f"built from {st.built_sha}, HEAD is {st.head_sha}"

    try:
        ok, detail = graph_health.hooks_path_status(repo_path)
    except Exception as exc:  # noqa: BLE001
        ok, detail = False, f"hooks probe failed: {exc}"
    facts.hooks_installed = bool(ok)
    facts.hooks_detail = detail
    try:
        # Same question `hooks_path_status` already answers internally —
        # consume its `hooks_file_present` helper rather than re-running the
        # `.is_file()` check independently (#2236 review: split-brain risk).
        facts.hooks_shipped = graph_health.hooks_file_present(repo_path)
    except OSError:
        facts.hooks_shipped = False
    return facts


def machine_facts_from_statuses(cfg, repo_name: str, statuses) -> list[MachineFacts]:
    """Fold ``coord.network.check_all`` results into :class:`MachineFacts`.

    Only machines whose ``coordinator.yml`` ``repos:`` list names *repo_name*
    matter here — a machine that never declared the repo is not half-onboarded,
    it is deliberately out of scope.
    """
    by_name = {s.machine.name: s for s in statuses}
    out: list[MachineFacts] = []
    for m in cfg.machines:
        if repo_name not in (m.repos or []):
            continue
        mf = MachineFacts(
            name=m.name, declared=True, repo_path=m.repo_path(repo_name)
        )
        st = by_name.get(m.name)
        if st is None:
            mf.reachable = False
            mf.unreachable_reason = "not probed"
            out.append(mf)
            continue
        if not st.is_online:
            mf.reachable = False
            mf.unreachable_reason = st.reason or "offline"
            out.append(mf)
            continue
        health = st.health or {}
        mf.config_free = health.get("config_free")
        published = health.get("repos")
        mf.published_repos = list(published) if isinstance(published, list) else None
        mf.degraded = dict(health.get("degraded") or {})
        out.append(mf)
    return out


def gather_facts(
    cfg,
    repo_name: str,
    *,
    statuses=None,
    probe_github: bool = True,
    local_clone: Path | None = None,
    ops=None,
) -> RepoFacts:
    """Assemble every fact :func:`evaluate` needs for *repo_name*.

    *statuses* are ``coord.network.MachineStatus`` objects the caller already
    fetched (``coord doctor`` has them for free — folding this in must not cost
    a second round trip per machine). *probe_github* off skips every ``gh``
    call, which is what makes the ``coord doctor`` wiring cheap: the config and
    machine layers alone already catch the stick-demo#1 shape.
    """
    from coord.smoke import resolve_smoke_command  # noqa: PLC0415

    repo = cfg.repo(repo_name)
    facts = RepoFacts(name=repo_name, configured=repo is not None)
    if repo is None:
        return facts

    facts.github = repo.github or None
    facts.config_default_branch = repo.default_branch
    facts.config_develop_branch = repo.develop_branch
    facts.build_command = repo.build_command

    smoke_cfg = getattr(cfg, "smoke_tests", None)
    if smoke_cfg is not None:
        resolved = resolve_smoke_command(repo, smoke_cfg)
        facts.smoke_command = resolved.command
        facts.smoke_command_source = resolved.source
        facts.capability_rule_count = len(smoke_cfg.capability_rules or [])

    acceptance = getattr(cfg, "acceptance", None)
    drivers = getattr(acceptance, "drivers", None) or {}
    facts.has_acceptance_driver = repo_name in drivers

    facts.machines = machine_facts_from_statuses(cfg, repo_name, statuses or [])

    if probe_github:
        facts.gh = gather_github_facts(
            facts.github, default_branch_hint=facts.config_default_branch, ops=ops
        )

    if local_clone is not None:
        facts.graph = gather_graph_facts(
            local_clone, facts.config_default_branch or "main"
        )

    return facts


def local_clone_path(cfg, repo_name: str, machine_name: str | None = None) -> Path | None:
    """Best guess at this machine's clone of *repo_name*, or ``None``.

    Prefers this machine's own ``repo_paths`` entry in ``coordinator.yml``;
    falls back to the fleet convention ``~/src/<repo>``. Returns ``None`` when
    neither exists on disk, so the graph layer reports "not probed here"
    instead of inventing a failure about a machine that legitimately has no
    clone.
    """
    import socket  # noqa: PLC0415

    name = machine_name or socket.gethostname()
    for m in cfg.machines:
        if m.name != name:
            continue
        configured = m.repo_path(repo_name)
        if configured:
            p = Path(configured).expanduser()
            if p.exists():
                return p
    fallback = Path.home() / "src" / repo_name
    return fallback if fallback.exists() else None


# ── Pure evaluation, one function per layer ──────────────────────────────────


def evaluate_config(facts: RepoFacts) -> list[Finding]:
    """Layer 1 — is the ``coordinator.yml`` entry there and coherent?

    The one check here that is *not* derivable from config alone —
    ``config.default_branch_mismatch`` — compares the configured branch with
    the repo's REAL default read from GitHub, because trusting the flag is
    exactly how worker PRs end up silently based on the wrong branch.
    """
    out: list[Finding] = []
    if not facts.configured:
        out.append(Finding(
            layer="config",
            check="config.repo_missing",
            severity=CRIT,
            summary=(
                f"no repos[] entry named {facts.name!r} in coordinator.yml — "
                "nothing can be dispatched for this repo at all"
            ),
            fix=(
                f"coord repo add {facts.name} --github <owner/repo>  "
                "(writes the entry into the coord-settings checkout)"
            ),
        ))
        return out

    out.append(Finding(
        layer="config", check="config.repo_present", severity=OK,
        summary=f"repos[] entry present (github: {facts.github or '?'})",
    ))

    if not facts.github:
        out.append(Finding(
            layer="config", check="config.github_missing", severity=CRIT,
            summary=(
                f"repos[{facts.name}].github is unset — every `gh` call for "
                "this repo (issues, labels, PRs, the merge gate) has no "
                "repository to address"
            ),
            fix="set `github: owner/repo` on the repos[] entry",
        ))

    declaring = [m.name for m in facts.machines if m.declared]
    if not declaring:
        out.append(Finding(
            layer="config", check="config.no_machines", severity=CRIT,
            summary=(
                "no machine's `repos:` list includes this repo — it is "
                "configured but unroutable; every dispatch will fail to find "
                "a machine"
            ),
            fix=(
                "add the repo to the `repos:` list of each machine that "
                "should serve it in coordinator.yml"
            ),
        ))
    else:
        out.append(Finding(
            layer="config", check="config.machines_declared", severity=OK,
            summary=f"declared on {len(declaring)} machine(s): {', '.join(declaring)}",
        ))

    real = facts.gh.default_branch
    configured = facts.config_default_branch
    if facts.gh.default_branch_error:
        out.append(Finding(
            layer="config", check="config.default_branch_unknown", severity=UNKNOWN,
            summary=(
                f"could not read the repo's real default branch from GitHub — "
                f"{facts.gh.default_branch_error}; `default_branch: "
                f"{configured}` is unverified, not confirmed"
            ),
        ))
    elif real and configured and real != configured:
        out.append(Finding(
            layer="config", check="config.default_branch_mismatch", severity=CRIT,
            summary=(
                f"coordinator.yml says `default_branch: {configured}` but "
                f"{facts.github} really defaults to {real!r} — worker PRs "
                "silently route to the wrong base"
            ),
            fix=f"set `default_branch: {real}` on the repos[] entry",
        ))
    elif real and configured:
        out.append(Finding(
            layer="config", check="config.default_branch_ok", severity=OK,
            summary=f"default_branch {configured!r} matches the repo's real default",
        ))

    return out


def evaluate_machines(facts: RepoFacts) -> list[Finding]:
    """Layer 2 — the clone and the agent, read from ``/health``, not config.

    Three failure modes that a config read cannot tell apart, each with a
    remedy that does **not** fix the other two:

    * ``machines.agent_repo_skew`` — the repo is in this machine's
      ``coordinator.yml`` ``repos:`` list, the agent is up, and ``/health``
      neither serves it nor calls it degraded. That means the agent has not
      re-read config since the repo was added (#2219). **This is the
      stick-demo#1 finding.** Fix: restart ``coord-agent`` there.
    * ``machines.clone_missing`` — the agent knows about the repo and reports
      it degraded because the configured path is not on disk. Restarting
      changes nothing; the machine needs the clone.
    * ``machines.repo_path_missing`` — no ``repo_paths`` entry at all. That is
      a config repair, not a clone or a restart.
    """
    out: list[Finding] = []
    for m in facts.machines:
        if not m.declared:
            continue
        if not m.reachable:
            out.append(Finding(
                layer="machines", check="machines.unreachable", severity=UNKNOWN,
                subject=m.name,
                summary=(
                    f"{m.name}: agent unreachable ({m.unreachable_reason or 'offline'}) "
                    "— its repo list, clone and degraded state are all unknown, "
                    "not clean"
                ),
            ))
            continue

        if m.config_free:
            # An ephemeral/config-free worker publishes no repos by design
            # (#1801) — the coordinator supplies them at dispatch time. Absence
            # here is the designed shape, not skew.
            out.append(Finding(
                layer="machines", check="machines.config_free", severity=UNKNOWN,
                subject=m.name,
                summary=(
                    f"{m.name}: agent is running config-free ({m.config_free}) — "
                    "repo readiness there cannot be read from /health; repos "
                    "come from the coordinator at dispatch time"
                ),
            ))
            continue

        published = m.published_repos
        if published is None:
            out.append(Finding(
                layer="machines", check="machines.health_incomplete", severity=UNKNOWN,
                subject=m.name,
                summary=(
                    f"{m.name}: /health published no `repos` field at all — "
                    "the agent predates it; upgrade with `coord agent update` "
                    "before trusting this layer"
                ),
            ))
            continue

        if facts.name in published:
            out.append(Finding(
                layer="machines", check="machines.servable", severity=OK,
                subject=m.name,
                summary=f"{m.name}: agent is live and serving {facts.name}",
            ))
            continue

        reason = m.degraded.get(facts.name)
        if reason is None:
            out.append(Finding(
                layer="machines", check="machines.agent_repo_skew", severity=CRIT,
                subject=m.name,
                summary=(
                    f"{m.name}: coordinator.yml declares {facts.name!r} for this "
                    f"machine but its live /health advertises {sorted(published)} "
                    "and does not call it degraded — the agent has not re-read "
                    "config since the repo was added (#2219). Every dispatch "
                    "here is refused while `coord config`/`coord status`/"
                    "`coord assign --dry-run` all still show it as supported."
                ),
                fix=f"restart coord-agent on {m.name} (it freezes its repo list at start)",
            ))
        elif "does not exist" in reason:
            out.append(Finding(
                layer="machines", check="machines.clone_missing", severity=CRIT,
                subject=m.name,
                summary=(
                    f"{m.name}: no clone at the configured path — {reason}. "
                    "This is the worker worktree BASE, not a convenience "
                    "checkout: without it every worktree add fails. Restarting "
                    "the agent will not fix it."
                ),
                fix=(
                    f"clone the repo to {m.repo_path or '<repo_paths entry>'} "
                    f"on {m.name}"
                ),
            ))
        else:
            out.append(Finding(
                layer="machines", check="machines.repo_path_missing", severity=CRIT,
                subject=m.name,
                summary=(
                    f"{m.name}: agent reports {facts.name!r} degraded — {reason}. "
                    "A config repair, not a clone or a restart."
                ),
                fix=f"add repo_paths[{facts.name}] for {m.name} in coordinator.yml",
            ))

    return out


def _has_label(labels: list[str], name: str) -> bool:
    # GitHub label names are case-insensitive for uniqueness purposes.
    return any(lbl.lower() == name.lower() for lbl in labels)


def evaluate_github(facts: RepoFacts) -> list[Finding]:
    """Layer 3 — labels and the ``pull_request`` trigger.

    The workflow check is the expensive-to-learn one: a repo with workflows
    that all trigger on ``push`` only makes
    :meth:`coord.ci_github.GitHubCi.expects_checks` answer "CI exists" while
    zero checks ever arrive for a PR, so ``checks_absent`` blocks every merge
    in that repo forever. A repo with *no* workflows at all is fine — that is
    the honest "no CI configured" case ``expects_checks`` handles correctly.
    """
    out: list[Finding] = []

    if facts.gh.labels_error:
        out.append(Finding(
            layer="github", check="github.labels_unknown", severity=UNKNOWN,
            summary=(
                f"could not list labels — {facts.gh.labels_error}; the `coord` "
                "and tier labels are unverified, not confirmed"
            ),
        ))
    elif facts.gh.labels is not None:
        labels = facts.gh.labels
        if _has_label(labels, COORD_LABEL):
            out.append(Finding(
                layer="github", check="github.coord_label_present", severity=OK,
                summary=f"`{COORD_LABEL}` label exists",
            ))
        else:
            out.append(Finding(
                layer="github", check="github.coord_label_missing", severity=CRIT,
                summary=(
                    f"no `{COORD_LABEL}` label in {facts.github} — issues in "
                    "this repo are live but INVISIBLE to the Pipeline; nothing "
                    "will ever pick them up and nothing will say why"
                ),
                fix=f"gh label create {COORD_LABEL} --repo {facts.github}",
            ))

        missing_tiers = [t for t in TIER_LABELS if not _has_label(labels, t)]
        if missing_tiers:
            out.append(Finding(
                layer="github", check="github.tier_labels_missing", severity=WARN,
                summary=(
                    f"missing tier label(s) {missing_tiers} — model routing "
                    "falls back to the default tier for every issue in this repo"
                ),
                fix=" && ".join(
                    f"gh label create {t} --repo {facts.github}" for t in missing_tiers
                ),
            ))
        else:
            out.append(Finding(
                layer="github", check="github.tier_labels_present", severity=OK,
                summary=f"tier labels present ({', '.join(TIER_LABELS)})",
            ))

    if facts.gh.workflow_error:
        out.append(Finding(
            layer="github", check="github.workflows_unknown", severity=UNKNOWN,
            summary=(
                f"could not read workflows — {facts.gh.workflow_error}; whether "
                "any triggers on `pull_request` is unverified"
            ),
        ))
    elif facts.gh.workflow_count is not None:
        if facts.gh.workflow_count == 0:
            out.append(Finding(
                layer="github", check="github.no_workflows", severity=WARN,
                summary=(
                    "no GitHub Actions workflows at all — the merge gate will "
                    "correctly treat this repo as having no CI, so nothing "
                    "blocks; but nothing verifies a merge either"
                ),
            ))
        elif facts.gh.pr_triggered_workflows:
            out.append(Finding(
                layer="github", check="github.pull_request_trigger_present", severity=OK,
                summary=(
                    f"{len(facts.gh.pr_triggered_workflows)} workflow(s) trigger "
                    f"on pull_request: {', '.join(facts.gh.pr_triggered_workflows)}"
                ),
            ))
        else:
            out.append(Finding(
                layer="github", check="github.no_pull_request_trigger", severity=CRIT,
                summary=(
                    f"{facts.gh.workflow_count} workflow(s) exist but NONE "
                    "trigger on `pull_request` — expects_checks() reads 'CI "
                    "exists' while zero checks ever arrive, so `checks_absent` "
                    "blocks EVERY merge in this repo, forever"
                ),
                fix=(
                    "add `on: pull_request:` to the repo's CI workflow (or "
                    "remove the workflows entirely if the repo genuinely has "
                    "no CI)"
                ),
            ))

    return out


def evaluate_contents(facts: RepoFacts) -> list[Finding]:
    """Layer 4 — what the repo itself must carry."""
    out: list[Finding] = []

    if facts.gh.claude_md_error:
        out.append(Finding(
            layer="contents", check="contents.claude_md_unknown", severity=UNKNOWN,
            summary=f"could not check for CLAUDE.md — {facts.gh.claude_md_error}",
        ))
    elif facts.gh.claude_md_present is False:
        out.append(Finding(
            layer="contents", check="contents.claude_md_missing", severity=CRIT,
            summary=(
                "no CLAUDE.md in the repo — the Test agent auto-loads it and "
                "the adversarial review prompt is assembled from it, so every "
                "review in this repo enforces nothing while still returning a "
                "verdict"
            ),
            fix="add a CLAUDE.md at the repo root (`/init` inside Claude Code drafts one)",
        ))
    elif facts.gh.claude_md_present:
        out.append(Finding(
            layer="contents", check="contents.claude_md_present", severity=OK,
            summary="CLAUDE.md present",
        ))

    if not facts.smoke_command:
        out.append(Finding(
            layer="contents", check="contents.test_command_unresolved", severity=CRIT,
            summary=(
                "the Test stage cannot resolve a command for this repo "
                "(no repos[].ci_command, no smoke_tests.default_command, no "
                "repos[].test_command) — it does not know what to run, and "
                "`scripts/coord-test-runner.sh` REFUSES rather than reporting "
                "a silent green"
            ),
            fix=(
                f"set `ci_command` (preferred) or `test_command` on "
                f"repos[{facts.name}] in coordinator.yml"
            ),
        ))
    else:
        out.append(Finding(
            layer="contents", check="contents.test_command_resolved", severity=OK,
            summary=(
                f"Test stage runs `{facts.smoke_command}` "
                f"(from {facts.smoke_command_source})"
            ),
        ))

    if not facts.build_command:
        out.append(Finding(
            layer="contents", check="contents.build_command_missing", severity=WARN,
            summary=(
                "no `build_command` — workers get no build step to verify "
                "against before declaring done"
            ),
        ))

    # NOTE: `smoke_tests.capability_rules` are keyed by PATH PREFIX, not by
    # repo (see `coord.config.SmokeRule`), so nothing in coordinator.yml says
    # which rules belong to which repo — this deliberately reports the
    # fleet-wide count rather than inventing a per-repo attribution the config
    # cannot support. A fleet with zero rules cannot route ANY repo's changed
    # files to capable hardware, which is the state worth flagging.
    if facts.capability_rule_count == 0:
        out.append(Finding(
            layer="contents", check="contents.no_capability_rules", severity=WARN,
            summary=(
                "no `smoke_tests.capability_rules` are configured at all — "
                "changed files cannot route to capable hardware, so the Test "
                "stage may land on a machine that cannot run them"
            ),
        ))
    else:
        out.append(Finding(
            layer="contents", check="contents.capability_rules_present", severity=OK,
            summary=(
                f"{facts.capability_rule_count} smoke capability rule(s) "
                "configured fleet-wide (rules are path-keyed, not per-repo — "
                "check that one covers this repo's paths)"
            ),
        ))

    return out


def evaluate_graph(facts: RepoFacts) -> list[Finding]:
    """Layer 5 — graphify. Four sub-layers that all fail silently; a
    half-installed machine looks identical to a working one, it just answers
    from grep."""
    out: list[Finding] = []
    g = facts.graph
    if not g.probed:
        out.append(Finding(
            layer="graph", check="graph.not_probed", severity=UNKNOWN,
            summary=(
                "no local clone of this repo on the machine running this check "
                "— graph readiness not probed here (run `coord repo doctor` on "
                "a machine that has the clone)"
            ),
        ))
        return out

    if not g.built:
        out.append(Finding(
            layer="graph", check="graph.not_built", severity=WARN,
            summary=(
                f"no graphify artifact for {g.repo_path} — "
                f"{g.detail or 'graphify has never been built here'}; queries "
                "silently degrade to grep"
            ),
            fix="graphify build  (see docs/GRAPHIFY_SETUP.md)",
        ))
    elif not g.fresh:
        out.append(Finding(
            layer="graph", check="graph.stale", severity=WARN,
            summary=f"graph is stale — {g.detail or 'built sha is behind HEAD'}",
            fix="graphify build",
        ))
    else:
        out.append(Finding(
            layer="graph", check="graph.fresh", severity=OK,
            summary="graph artifact is current with HEAD",
        ))

    if not g.hooks_installed and not g.hooks_shipped:
        # #2236: the repo never ported the hook. `git config core.hooksPath
        # .githooks` here would point git at a directory that does not exist
        # and disable every hook in the checkout, so the fix is to bring the
        # files over FIRST — worktrees of this repo get no linked graph until
        # then, however the machine is configured.
        out.append(Finding(
            layer="graph", check="graph.hooks_not_ported", severity=WARN,
            summary=(
                f"this repo ships no .githooks/post-checkout — "
                f"{g.hooks_detail or 'the worktree graph bootstrap does not exist here'}; "
                "worktrees get no linked graph, so every worker on this repo "
                "silently falls back to grep"
            ),
            fix=(
                "port .githooks/ (_lib.sh, post-checkout, post-commit, post-merge) "
                "from claude-coordinator into this repo, THEN "
                "git config core.hooksPath .githooks  (see docs/GRAPHIFY_SETUP.md)"
            ),
        ))
    elif not g.hooks_installed:
        out.append(Finding(
            layer="graph", check="graph.hooks_missing", severity=WARN,
            summary=(
                f"graphify git hooks are not wired up — "
                f"{g.hooks_detail or 'core.hooksPath is unset or points elsewhere'}; "
                "the graph will silently stop tracking commits"
            ),
            fix="graphify hook install  (then set core.hooksPath — order matters)",
        ))
    else:
        out.append(Finding(
            layer="graph", check="graph.hooks_installed", severity=OK,
            summary="graphify hooks installed",
        ))

    return out


def evaluate(facts: RepoFacts) -> RepoDoctorReport:
    """Run all five layers over *facts*. Pure — no I/O, no network."""
    findings: list[Finding] = []
    findings.extend(evaluate_config(facts))
    if facts.configured:
        findings.extend(evaluate_machines(facts))
        findings.extend(evaluate_github(facts))
        findings.extend(evaluate_contents(facts))
        findings.extend(evaluate_graph(facts))
    return RepoDoctorReport(repo_name=facts.name, findings=findings)


# ── Rendering ────────────────────────────────────────────────────────────────


_LAYER_TITLES = {
    "config": "1 — config (coordinator.yml in the coord-settings checkout)",
    "machines": "2 — machines (live /health, not config)",
    "github": "3 — GitHub (labels + pull_request trigger)",
    "contents": "4 — repo contents",
    "graph": "5 — graph",
}


def format_report(report: RepoDoctorReport, *, verbose: bool = False) -> list[str]:
    """Human-readable lines for ``coord repo doctor``.

    Without ``verbose`` the OK findings are collapsed to a per-layer count —
    the point of the command is the *residue*, and a wall of green hides it.
    """
    lines: list[str] = [f"repo: {report.repo_name}"]
    for layer in LAYERS:
        entries = report.for_layer(layer)
        if not entries:
            continue
        lines.append("")
        lines.append(f"{_LAYER_TITLES.get(layer, layer)}:")
        shown = [f for f in entries if verbose or f.severity != OK]
        oks = len(entries) - len([f for f in entries if f.severity != OK])
        for f in sorted(shown, key=lambda f: _SEVERITY_RANK.get(f.severity, 9)):
            lines.append(f"  {_SEVERITY_MARK.get(f.severity, '·')} [{f.check}] {f.summary}")
            if f.fix and f.severity in (CRIT, WARN):
                lines.append(f"        fix: {f.fix}")
        if oks and not verbose:
            lines.append(f"  ✓ {oks} check(s) passed")
    lines.append("")
    lines.append(summary_line(report))
    return lines


def summary_line(report: RepoDoctorReport) -> str:
    """The machine-readable trailer, mirroring ``CONFIG_PROVENANCE:`` /
    ``GRAPH_HEALTH:``."""
    unknown = len([f for f in report.findings if f.severity == UNKNOWN])
    return (
        f"REPO_DOCTOR: repo={report.repo_name} "
        f"crit={len(report.crits)} warn={len(report.warns)} unknown={unknown} "
        f"ok={'true' if report.ok else 'false'}"
    )


# The layers ``coord doctor`` folds in. Deliberately just the one that reads
# LIVE state (each agent's `/health`) — see :func:`doctor_summary_lines`.
DOCTOR_LIVE_LAYERS: tuple[str, ...] = ("machines",)


def doctor_summary_lines(
    report: RepoDoctorReport, *, layers: tuple[str, ...] = DOCTOR_LIVE_LAYERS
) -> list[tuple[bool, str]]:
    """Compact ``(is_problem, line)`` pairs for folding into ``coord doctor``.

    Two filters, both deliberate.

    **CRIT only.** Warnings (no ``capability_rules``, no ``build_command``, a
    stale graph) are real onboarding residue, but they are states a fleet can
    run in indefinitely and deliberately. Surfacing them here would put every
    long-standing repo in the fleet report forever, and a report that is always
    red is a report nobody reads.

    **Live layers only** (``machines``). ``coord doctor``'s value-add for this
    feature is precisely the half of the story that *cannot* be read from
    ``coordinator.yml``: whether each agent's ``/health`` actually serves the
    repo, and whether the clone is on disk. Every config-derivable CRIT — a
    missing ``test_command``, a repo no machine declares — is equally visible
    from a config read that costs nothing and needs no fleet, so duplicating it
    into the fleet report buys no new information while burying the finding
    that does. ``coord repo doctor <name>`` reports all five layers; this
    reports the one that needed a live probe to discover.
    """
    out: list[tuple[bool, str]] = []
    for f in report.findings:
        if f.severity != CRIT or f.layer not in layers:
            continue
        mark = _SEVERITY_MARK[f.severity]
        out.append((True, f"  {mark} repo {report.repo_name}: [{f.check}] {f.summary}"))
    return out
