"""OpenCodeProvider: the ``opencode run`` concrete provider.

OpenCode (https://github.com/sst/opencode) is an open-source terminal coding
assistant that supports multiple AI backends (Anthropic, OpenAI, etc.) via the
user's own API keys.  This provider wraps ``opencode run`` for use as a coord
worker backend.

**CORRECTED AGAINST REAL CAPTURED OUTPUT (#1704).**  #1703 ran a real
``opencode`` 1.18.11 binary against real models (free and paid) and recorded
every finding in ``docs/OPENCODE_VERIFICATION.md``, replacing
``tests/fixtures/opencode_run_sample.jsonl`` with a verbatim successful
capture and adding ``tests/fixtures/opencode_run_failure_sample.jsonl`` for a
verbatim failing one.  This module is a follow-up correction: every
behavioural detail below cites that document.

**#1705 — per-spec-type agent definitions, deny-list enforcement PROVEN.**
#1704 left two things explicitly unfinished; both are done now:

* The concrete opencode agent definitions ``--agent`` references by name now
  exist, one committed markdown file per ``spec.type``, under
  :data:`AGENTS_ROOT` (``coord/agents/opencode/agents/<spec.type>.md``).
  Only ``work`` is authored so far (scope of #1705 — ``review`` and other
  types are separate issues, see :func:`_agent_definition_path`).  Dispatching
  an unauthored ``spec.type`` now raises :class:`OpenCodeAgentNotFoundError`
  from :meth:`OpenCodeProvider.build_command` — a hard, named Python-side
  error instead of a silent permissive fallback or an opaque CLI failure.
* ``capabilities().enforces_deny_list`` is now ``True`` for the reason
  :meth:`capabilities`'s docstring gives in full: real opencode 1.18.11 runs
  (not argv assertions) proved a ``gh`` bash call, an edit outside the
  worktree, and an edit under ``tests/acceptance/**`` are all genuinely
  blocked by ``coord/agents/opencode/agents/work.md``'s permission block —
  see the named tests in ``tests/test_providers.py``.

**Agent-file discovery mechanism, empirically verified (not guessed):**
:meth:`env` sets ``OPENCODE_CONFIG_DIR`` to :data:`AGENTS_ROOT` so opencode
discovers ``agents/<spec.type>.md`` there.  Two things were confirmed against
the real binary before relying on this, because both are safety-relevant:

1. A *flat* ``<dir>/<name>.md`` layout (no ``agents/`` subdirectory) is
   **silently invisible** to ``--agent`` — opencode only scans
   ``<OPENCODE_CONFIG_DIR>/agents/*.md`` (and the singular ``agent/`` alias).
   This is why the committed file lives at
   ``coord/agents/opencode/agents/work.md`` rather than the flatter
   ``coord/agents/opencode/work.md`` a first guess might reach for.
2. ``OPENCODE_CONFIG_DIR`` outranks a *worktree-local* ``.opencode/agents/``
   directory of the same agent name.  Verified by planting a conflicting
   ``.opencode/agents/work.md`` (wide-open ``bash``/``edit``/
   ``external_directory`` permissions) in a throwaway worktree and
   confirming the resolved rule list — and a live ``gh issue list`` call —
   were byte-for-byte unaffected.  This matters because the worktree a
   worker runs in is checked out from a repo coord does not fully control;
   without this precedence a target repo could ship its own permissive
   ``work`` agent and silently shadow coord's deny-baseline one.

Differences from :class:`~.claude.ClaudeProvider`:

* ``build_command()`` invokes
  ``opencode run --format json [--attach URL] [--model M] [--session S]
  --agent NAME --auto BRIEFING``.  No ``-p``, no ``--input-format``, no
  stream-json flags — see the method docstring for exactly why each flag is
  there and what it replaces.
* ``initial_input()`` returns ``b""`` — no stdin payload needed since the
  briefing travels on argv (confirmed: ``run [message..]`` is a genuine
  positional argv message, no stdin protocol observed —
  ``OPENCODE_VERIFICATION.md`` "Flag surface").
* ``capabilities().enforces_deny_list=True`` — see above and
  :meth:`capabilities`.
* ``capabilities().billing_mode="byo_key"`` — confirmed: OpenCode bills
  against the operator's own configured provider credentials, not
  Anthropic's ``claude -p`` credit pool (``OPENCODE_VERIFICATION.md``
  "Flag surface" / "every ASSUMPTION" table).
* ``capabilities().cost_reporting=True`` and
  ``capabilities().true_system_prompt=True`` — both flipped from the
  first-pass ``False`` now that real evidence backs them; see
  :meth:`capabilities` for the citations.
* ``env()`` always sets ``OPENCODE_CONFIG_DIR`` (agent-file discovery, see
  above) and ``OPENCODE_CONFIG`` (the OpenRouter upstream-routing pin, see
  ``coord/agents/opencode/routing.jsonc`` and :data:`ROUTING_PIN_PATH`).
* ``env()`` also seeds ``OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX`` to
  :data:`OUTPUT_TOKEN_MAX_DEFAULT` (#2321) — unlike the two variables
  above, this one *is* operator-overridable (it's a tuning knob, not a
  safety mechanism); see :meth:`env`'s docstring for why it sits on the
  opposite side of ``self._env`` from ``OPENCODE_CONFIG_DIR``/
  ``OPENCODE_CONFIG``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from coord.providers.base import Capabilities, Provider, WorkerSummary

if TYPE_CHECKING:
    from coord.agent import AssignmentSpec


# ── Module-level constants ─────────────────────────────────────────────────────

#: Default binary name for the OpenCode CLI.
#:
#: Confirmed: binary name ``opencode`` is correct
#: (``OPENCODE_VERIFICATION.md`` "Machine / version").  Override via
#: ``ProviderDef(type="opencode", binary="/path/to/opencode")``.
DEFAULT_OPENCODE_BINARY = "opencode"

#: Sentinel string whose presence in the log signals successful completion.
#:
#: CORRECTED (#1704): there is no ``session.complete`` event — confirmed by
#: running the *unmodified* first-pass provider against the real fixture in
#: #1703 (it extracted nothing).  The real terminal signal for a successful
#: run is the last ``step_finish`` event, whose nested ``part.reason`` is
#: ``"stop"`` (as opposed to ``"tool-calls"``, which ends every intermediate
#: turn and is followed by another ``step_start``).  See
#: ``OPENCODE_VERIFICATION.md`` "The real terminal/completion signal".
#:
#: This is a plain substring match — ``docs/OPENCODE_VERIFICATION.md``
#: explicitly flags that as fragile against key reordering and recommends a
#: structural check (parse the line, test ``part.reason == "stop"``)
#: instead.  :func:`_update_opencode_summary` below *does* do the structural
#: check for :attr:`WorkerSummary.stop_reason`; this constant is only used
#: by the reap thread's log-tailing early-completion heuristic (see
#: ``coord.agent._reap``), where the accepted risk is a missed/late
#: detection (the reap loop falls back to ``proc.wait()`` returning when the
#: process actually exits), never a wrong pass/fail verdict.
#:
#: A failing run has **no** terminal ``step_finish`` at all — it ends with a
#: top-level ``error`` event instead — so this marker deliberately does not
#: (and structurally cannot) match a failure. Failure detection is the
#: process exit code plus :func:`_update_opencode_summary`'s handling of the
#: ``error`` event, not this marker.
RESULT_MARKER = '"reason":"stop"'

#: Directory holding coord's committed opencode agent-discovery artifacts:
#: ``agents/<spec.type>.md`` (per-spec-type agent definitions) and
#: ``routing.jsonc`` (the OpenRouter upstream-routing pin).  Computed from
#: this module's own file location so it resolves correctly regardless of
#: where coord itself is installed/checked out — mirrors the pattern
#: ``tests/fixtures/`` paths use relative to ``tests/``.
#:
#: **Why an env var pointing here, not a copy into the worker's worktree**
#: (the other option the #1705 issue named): the worktree belongs to a
#: repo coord does not fully control, and ``OPENCODE_CONFIG_DIR`` was
#: empirically confirmed to outrank a same-named agent in that worktree's
#: own ``.opencode/agents/`` (see the module docstring) — so pointing at
#: this directory is both simpler (no per-dispatch file copy / no need to
#: touch the worktree-setup code path in ``coord/agent.py``) and safer (a
#: target repo cannot shadow it).
AGENTS_ROOT = Path(__file__).resolve().parent.parent / "agents" / "opencode"

#: The OpenRouter upstream-routing pin (#1705 added scope) — see that
#: file's own header comment for the full mechanism citation.  Threaded
#: onto the worker's environment as ``OPENCODE_CONFIG`` by :meth:`env`.
ROUTING_PIN_PATH = AGENTS_ROOT / "routing.jsonc"

#: Default value for ``OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX`` (#2321).
#:
#: opencode caps every request's output tokens at 32,000 (its internal
#: ``OY = 32000`` default) unless this environment variable overrides it —
#: and on a reasoning model that 32,000-token budget is *shared* with the
#: model's own reasoning, so a long reasoning block can consume the whole
#: cap before a single tool call is emitted.  This is not hypothetical:
#: space-invaders#1 (``8c95182b0749``) spent all 32,000 tokens on one
#: 113 KB reasoning block, was truncated (``"reason":"length"``), emitted
#: no tool call, and exited 0 with nothing on disk — the run this issue is
#: named for.
#:
#: opencode computes the *effective* cap as
#: ``min(model.limit.output, $OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX)`` —
#: it is a ceiling suggestion, not a per-model override.  Raising it can
#: never push a model's output above what that model's own
#: ``limit.output`` allows; it can only stop opencode's own 32,000
#: default from clamping a model that allows more.  That makes a single
#: generous constant safe across every model dispatched through this
#: provider — no need to look up ``limit.output`` per model at dispatch
#: time.
#:
#: Chosen as 384,000 — the largest ``limit.output`` among the opencode
#: model definitions coord currently routes through (``oc-cheap`` /
#: ``opencode/deepseek-v4-flash`` and ``oc-heavy`` /
#: ``opencode/deepseek-v4-pro``, both 384,000; ``oc-mid`` /
#: ``opencode/glm-5.2`` is lower at 131,072 and is naturally clamped to
#: its own true ceiling by the ``min()`` above — never pushed past it).
#:
#: Both fleet binaries already read this variable (1.18.11 on dellserver —
#: the machine that hit the space-invaders#1 truncation — and 1.18.12 on
#: precision), and it is not gated behind ``OPENCODE_EXPERIMENTAL`` the
#: way other ``experimental*``-named flags are (that gating uses a
#: different internal helper; this flag's parser reads the env var
#: directly, unconditionally). No version-floor probe is needed here and
#: none should be added later on speculation: on a binary that predates
#: this flag it is simply inert and the cap silently stays at 32,000 —
#: never a hard failure.
OUTPUT_TOKEN_MAX_DEFAULT = 384_000


class OpenCodeAgentNotFoundError(RuntimeError):
    """Raised when no committed opencode agent definition exists for a
    ``spec.type`` being dispatched.

    #1705: dispatching an unauthored spec type must be a **hard, named
    error**, never a silent permissive fallback (e.g. running with
    opencode's allow-everything built-in default agent) and never a bare
    CLI-level failure a few seconds into a worker's log that's hard to
    triage.  Only ``work`` is authored as of #1705 — other write-capable
    spec types (``review``, ``smoke``, ``conflict-fix``, ...) are separate,
    deliberately un-sped-up follow-up issues (see the module docstring).
    """

    def __init__(self, spec_type: str, expected_path: Path) -> None:
        self.spec_type = spec_type
        self.expected_path = expected_path
        super().__init__(
            f"no opencode agent definition for spec.type={spec_type!r}: "
            f"expected {expected_path} to exist. Author it under "
            f"{AGENTS_ROOT}/agents/ before dispatching this spec type "
            "through the opencode provider — see coord/agents/opencode/agents/work.md "
            "for the reference shape (deny-baseline permission block + "
            "system prompt) and #1705 for the naming/discovery contract."
        )


def _agent_definition_path(spec_type: str) -> Path:
    """Return the expected committed agent-file path for *spec_type*.

    Pure path computation, no filesystem access — split out from
    :func:`_agent_name_for_type` so tests can assert on the expected path
    independently of the existence check.
    """
    return AGENTS_ROOT / "agents" / f"{spec_type}.md"


def _agent_name_for_type(spec_type: str) -> str:
    """Map ``spec.type`` to the opencode ``--agent`` name coord will pass.

    **Naming contract (#1705, corrected from #1704's provisional
    ``coord-<type>`` guess): the ``--agent`` value is exactly ``spec_type``**
    (e.g. ``"work"``), because opencode's markdown agent-discovery derives
    the agent name from the filename minus its extension (confirmed against
    a real opencode 1.18.11 binary: a file at ``<OPENCODE_CONFIG_DIR>/agents/
    work.md`` is discovered as an agent literally named ``work``, not
    ``coord-work``) — see :data:`AGENTS_ROOT` and the module docstring.

    Raises:
        OpenCodeAgentNotFoundError: when ``coord/agents/opencode/agents/
            <spec_type>.md`` does not exist.  This is a **hard error**, not
            a silent fallback — see that exception's docstring.  As of
            #1705 only ``spec_type == "work"`` has a committed file; every
            other spec type raises until its own follow-up issue lands one.
    """
    path = _agent_definition_path(spec_type)
    if not path.is_file():
        raise OpenCodeAgentNotFoundError(spec_type, path)
    return spec_type


class OpenCodeProvider(Provider):
    """Concrete provider for ``opencode run`` (OpenCode workers).

    Corrected against a real ``opencode`` 1.18.11 capture (#1703 →
    ``docs/OPENCODE_VERIFICATION.md``).  #1705 finished what #1704 left
    open: the ``work`` agent definition is authored and its deny-list
    enforcement is proven against a real binary (see the module docstring
    and :meth:`capabilities`); ``enforces_deny_list`` is now ``True``.

    Args:
        binary: Override the worker binary name/path.  ``None`` falls back to
            :data:`DEFAULT_OPENCODE_BINARY` (``"opencode"``).
        attach_url: When set, passes ``--attach <attach_url>`` so the worker
            connects to an already-running OpenCode server instead of starting
            a new session.  Corresponds to ``ProviderDef.attach_url`` in
            ``coordinator.yml``.  ``None`` omits the flag (default headless
            ``opencode run`` starts its own session).  Confirmed end-to-end
            against a real ``opencode serve`` (``OPENCODE_VERIFICATION.md``
            "Flag surface").
        model: Fallback model id from the provider definition
            (``ProviderDef.model``) — e.g. an opencode ``provider/model``
            string such as ``"zhipuai/glm-4.6"``.  Used only when neither an
            explicit ``resolved_model`` nor ``spec.model`` is set — see
            :meth:`build_command`.
        env: Extra environment variables from the provider definition
            (``ProviderDef.env``, already ``${VAR}``-expanded by config
            parsing) — this is how an operator points OpenCode's own
            provider config (e.g. API keys) at a specific backend.  Merged
            with the (non-overridable — see :meth:`env`)
            ``OPENCODE_CONFIG_DIR`` / ``OPENCODE_CONFIG`` entries by
            :meth:`env`.
        extra_args: Additional argv entries from the provider definition
            (``ProviderDef.extra_args``).  Inserted after this method's own
            flags and before the trailing positional briefing argument.
            **Caveat confirmed in #1703:** array-typed opencode flags (e.g.
            ``--file``) greedily consume argv tokens the way yargs does —
            ``opencode run --file X "message"`` fails ("File not found:
            <message text>") because ``--file`` swallows the message. Such a
            flag only works placed *after* the briefing. If a future
            ``extra_args`` entry needs an array-typed flag, it cannot go
            through this constructor as-is; this is flagged here rather than
            silently mis-ordered.
    """

    def __init__(
        self,
        binary: str | None = None,
        *,
        attach_url: str | None = None,
        model: str | None = None,
        env: dict[str, str] | None = None,
        extra_args: list[str] | None = None,
    ) -> None:
        self._binary = binary
        self._attach_url = attach_url
        self._model = model
        self._env = dict(env) if env else {}
        self._extra_args = list(extra_args) if extra_args else []

    # ── Capabilities ──────────────────────────────────────────────────────────

    def capabilities(self) -> Capabilities:
        """Capabilities corrected against real captured evidence (#1703/#1704).

        Chosen values and rationale
        ~~~~~~~~~~~~~~~~~~~~~~~~~~~

        ``resume=True``
            Confirmed: ``--session <id>`` resumes a prior session (the
            resumed run's ``sessionID`` matched exactly and the model
            demonstrably had context of the prior turn); ``--continue``
            resumes the most recent session the same way.  See
            ``OPENCODE_VERIFICATION.md`` "Session id / resume".

        ``inject=False``
            **Still unknown** — mid-session stdin message injection into an
            already-running ``run --format json`` process was not exercised
            in the verification pass (see "Remaining unknowns").  Stays
            conservatively ``False`` until a future pass tests it.

        ``cost_reporting=True``
            Confirmed: cost is real and present on every ``step_finish``
            event (``part.cost``), verified non-zero across a real paid
            model (``deepseek/deepseek-chat``, 4 steps,
            ``0.00106932``/``0.0000449456``/``0.0000837256``/``0.0000344624``).
            The committed fixture uses a free-tier model so its own
            ``cost`` values are ``0`` — that's the model, not a parsing
            gap: :func:`_update_opencode_summary` sums ``part.cost`` across
            every ``step_finish`` event (there is no cumulative field), and
            the free-tier fixture's sum is correctly ``0.0``.  See
            ``OPENCODE_VERIFICATION.md`` "Token usage and cost — field
            paths".

        ``true_system_prompt=True``
            Confirmed via the live-fetched ``https://opencode.ai/config.json``
            schema: ``--agent <name>`` selects an agent definition whose
            ``AgentConfig.prompt: string`` field is a genuine system-prompt
            equivalent — not merely prepended user-message text.  See
            ``OPENCODE_VERIFICATION.md`` "every ASSUMPTION" table.  As of
            #1705 this is no longer just a mechanism claim: ``work.md``'s
            ``prompt`` body is what a real dispatched worker actually runs
            under (see :func:`_agent_name_for_type`).

        ``enforces_deny_list=True``
            **SAFETY GATE — flipped in #1705, justified by real runs, not
            argv assertions.**  OpenCode has a real per-tool
            ``allow``/``ask``/``deny`` permission system with genuine bash
            command pattern matching, empirically proven in #1703
            (``OPENCODE_VERIFICATION.md`` "Permission enforcement —
            empirical evidence").  #1705 closed the two gaps #1704 left
            open:

            1. ``coord/agents/opencode/agents/work.md`` is a committed
               deny-baseline agent config (catch-all rules first, specific
               overrides after — #1703's last-match-wins ordering trap,
               respected).
            2. Enforcement is proven **end-to-end against the real
               opencode 1.18.11 binary**, not asserted from argv, by named
               tests in ``tests/test_providers.py`` that each ``opencode
               run`` a real task through :meth:`build_command`/:meth:`env`
               (the actual production argv/env, not a hand-rolled one) and
               inspect the resulting NDJSON log for a genuine tool-call
               denial: ``test_opencode_work_agent_blocks_gh_end_to_end`` (a
               ``gh issue list`` bash call),
               ``test_opencode_work_agent_blocks_external_directory_access_end_to_end``
               (a read outside the worktree), and
               ``test_opencode_work_agent_blocks_tests_acceptance_edit_end_to_end``
               (an edit under the sealed oracle prefix) — plus a positive
               control, ``test_opencode_work_agent_allows_normal_edit_and_git_end_to_end``,
               confirming the deny rules don't collaterally block legitimate
               edit/git use.  All are skipped (not failed) when the
               ``opencode`` binary isn't on ``PATH``, matching this repo's
               existing convention for optional real-binary tests (see
               e.g. ``tests/test_graph_health.py``).

               **Load-bearing operational finding from authoring those
               tests, not covered by #1703's flag-surface table: opencode
               resolves its working directory from the inherited ``PWD``
               environment variable, not the real process cwd.** A bare
               ``subprocess.Popen(argv, cwd=X)`` with a stale ``PWD`` (e.g.
               copied from a long-running daemon's own environment, exactly
               what ``coord.agent._worker_subprocess_env`` does via
               ``dict(os.environ)``) makes every opencode tool call operate
               against the stale directory, not ``X`` — verified directly
               against the real binary.  Production dispatch is unaffected
               only because ``coord.agent._maybe_bash_wrap``'s
               ``bash -c 'exec ...'`` wrapper (``bash_wrap_spawn``, default
               ``True``, added for the unrelated #299 daemon-spawn-freeze
               mitigation) resets ``$PWD`` before ``exec``-ing into
               opencode — also verified directly.  This makes opencode
               dispatch silently depend on a flag whose docstring gives no
               hint that opencode needs it; flagged here rather than
               silently relied upon.  Out of scope to fix here: the fix (a
               ``--dir`` flag or explicit ``PWD`` correction) needs either
               the assignment's worktree path threaded into
               :meth:`build_command` or a ``PWD`` correction at the
               :meth:`env` call site, and both require the worktree path,
               which :class:`~coord.agent.AssignmentSpec` does not carry —
               only ``coord.agent.AgentServer._spawn`` (forbidden to touch
               under #1705's briefing) knows it.  Reported to the
               coordinator rather than worked around.

            Scope stays exactly what #1705 asked for: only ``spec.type ==
            "work"`` has a committed agent file.  Every other write-capable
            type (``review``, ``conflict-fix``, ``smoke``, ...) still
            cannot dispatch through this provider — not because of this
            capability flag anymore, but because
            :func:`_agent_name_for_type` raises
            :class:`OpenCodeAgentNotFoundError` for any ``spec.type``
            without a committed file, which :meth:`build_command` does not
            catch.  ``coord.agent.AgentServer.assign``'s
            ``WRITE_CAPABLE_SPEC_TYPES`` gate (unchanged by #1705) now
            passes this provider through for ``work``; the per-type file
            gap is what actually still blocks the rest, deliberately.

        ``billing_mode="byo_key"``
            Confirmed: OpenCode uses the operator's own provider credentials
            (``opencode providers login`` / ``opencode stats`` showing real
            accumulated cost against those credentials).  Not subject to
            the 2026-06-15 Anthropic metering change (#322).

        ``human_attended_only=False``
            Confirmed by inference, not a ToS determination: every capture
            in #1703 (``run --format json``, no TTY) behaved as pure batch
            automation — no prompts blocking on a TTY, and an ``ask``
            permission rule with no ``--auto`` fails *closed* (auto-rejects
            with a stderr line) rather than hanging.  See
            ``OPENCODE_VERIFICATION.md`` "``--auto`` semantics" scenario 1.
        """
        return Capabilities(
            resume=True,
            inject=False,
            cost_reporting=True,
            true_system_prompt=True,
            # SAFETY: flipped in #1705 — see docstring above for the named
            # end-to-end tests this is justified by.
            enforces_deny_list=True,
            billing_mode="byo_key",
            human_attended_only=False,
        )

    # ── Core methods ──────────────────────────────────────────────────────────

    def build_command(
        self,
        spec: "AssignmentSpec",
        *,
        resolved_model: str | None = None,
        system_prompt: str | None = None,
        allowed_tools: str | None = None,
        permission_mode: str = "acceptEdits",
    ) -> list[str]:
        """Build the ``opencode run`` argv for *spec*.

        Confirmed shape (#1703, ``OPENCODE_VERIFICATION.md`` "Flag
        surface")::

            opencode run --format json [--attach URL] [--model MODEL]
                [--session SESSION_ID] --agent NAME --auto BRIEFING

        ``BRIEFING`` is the final positional argument (a real positional
        argv message, confirmed — not a stdin protocol; see
        :meth:`initial_input`).

        Flags, each cited against captured evidence:

        * ``--format json`` — always.  Confirmed: ``--format default``
          emits "a human-formatted TUI-style transcript, not usable by a
          parser"; only ``json`` emits the NDJSON event stream
          :meth:`parse_log` understands.  Without it :meth:`result_marker`
          has nothing to match and :meth:`parse_log` extracts nothing.
        * ``--attach <url>`` — unchanged from the first pass, confirmed
          end-to-end against a real ``opencode serve``.
        * ``--model <value>`` — unchanged from the first pass, confirmed
          with two real ``provider/model`` strings.  Precedence: explicit
          *resolved_model* > ``spec.model`` > the provider definition's
          ``model`` (threaded in via ``__init__``, #1706).  Omitted when
          all three are ``None``.
        * ``--session <id>`` — unchanged from the first pass, confirmed to
          resume by id with matching ``sessionID`` and restored context.
          Omitted when ``spec.resume_session_id`` is ``None``.
        * ``--agent <name>`` — introduced in #1704, **now backed by a real
          committed agent file (#1705).**  Confirmed: ``--agent`` selects a
          named agent definition whose ``prompt``/``permission`` fields are
          a genuine system-prompt + tool-permission equivalent
          (schema-confirmed against ``https://opencode.ai/config.json``).
          This is why it **replaces** the ignored *system_prompt* /
          *allowed_tools* kwargs below rather than sitting alongside them —
          there is no opencode flag that accepts raw system-prompt text or
          a raw tool allowlist string the way ``claude -p`` does; the unit
          of configuration is a named agent.  The name is computed by
          :func:`_agent_name_for_type` from ``spec.type`` — see that
          function's docstring for the naming contract.  **Raises
          :class:`OpenCodeAgentNotFoundError`** (propagates out of this
          method — callers must not treat that as "fall back to something
          permissive") when ``spec.type`` has no committed agent file yet;
          as of #1705 that's every type except ``"work"``.
        * ``--auto`` — introduced in #1704, always passed.  Confirmed:
          ``--auto`` converts an agent's ``"ask"`` permission rules to
          auto-approve without overriding an explicit ``"deny"`` rule
          (``OPENCODE_VERIFICATION.md`` "``--auto`` semantics", scenarios
          2 & 3).  coord's workers are headless/unattended by design, so
          this flag is unconditional — the same way ``permission_mode``
          defaults to ``"acceptEdits"`` for :class:`~.claude.ClaudeProvider`.
          **Safety note:** opencode's permission model is deny-*list*
          semantics inverted from ``claude -p``'s allow-list — ``--auto``
          only ever *widens* what an ``"ask"`` rule would otherwise block;
          it can never widen past an explicit ``"deny"``.  As of #1705,
          with ``work.md``'s deny-baseline config actually in force
          (``bash: {"*": "deny", "git status*"/"git commit*"/"git push*"/...:
          "allow", "gh *": "deny", ...}``), this is exactly what keeps a
          real ``work`` dispatch headless instead of hanging on
          ``work.md``'s narrow bash allow-list / ``external_directory: deny``
          ``ask``-adjacent rules — see :meth:`capabilities`'s
          ``enforces_deny_list`` note for the tests that prove the explicit
          ``deny`` entries still hold under ``--auto``.

        Ignored kwargs
        ~~~~~~~~~~~~~~
        *system_prompt* and *allowed_tools* are accepted (matching the
        Provider ABC signature) but **silently ignored** — replaced by
        ``--agent`` as described above.  *permission_mode* is accepted but
        also ignored: it is a ``claude -p``-specific vocabulary
        (``"acceptEdits"``/``"bypassPermissions"``/...) with no opencode
        equivalent; ``--auto`` is unconditional instead of varying by value.

        Args:
            spec: The assignment spec being dispatched.
            resolved_model: The resolved model identifier to pass.  When
                provided, takes precedence over ``spec.model``.  ``None``
                falls back to ``spec.model``, then to the provider
                definition's ``model``; if all three are ``None``, the
                ``--model`` flag is omitted (OpenCode picks its configured
                default).
            system_prompt: Accepted but **ignored** — see "Ignored kwargs".
            allowed_tools: Accepted but **ignored** — see "Ignored kwargs".
            permission_mode: Accepted but **ignored** — see "Ignored kwargs".
        """
        binary = self._binary if self._binary is not None else DEFAULT_OPENCODE_BINARY

        # Precedence: explicit resolved_model > spec.model > provider-
        # definition model (ProviderDef.model, threaded in via __init__).
        if resolved_model is not None:
            effective_model = resolved_model
        elif spec.model is not None:
            effective_model = spec.model
        else:
            effective_model = self._model

        argv: list[str] = [binary, "run"]

        # Confirmed (#1703): only --format json emits a parseable NDJSON
        # stream; --format default is a human transcript.
        argv.extend(["--format", "json"])

        # When attach_url is set, connect to a running OpenCode server instead
        # of starting a new session.  Confirmed end-to-end against a real
        # `opencode serve`.
        if self._attach_url:
            argv.extend(["--attach", self._attach_url])

        # Confirmed: --model selects the AI model by 'provider/model' string.
        if effective_model:
            argv.extend(["--model", effective_model])

        # Confirmed: --session resumes a prior session by ID.
        if spec.resume_session_id:
            argv.extend(["--session", spec.resume_session_id])

        # New in #1704: --agent replaces system_prompt/allowed_tools (see
        # docstring above and _agent_name_for_type).
        argv.extend(["--agent", _agent_name_for_type(spec.type)])

        # New in #1704: unconditional, headless-safety flag (see docstring).
        argv.append("--auto")

        # #1706: provider-definition extra_args go after this method's own
        # flags but BEFORE the trailing positional briefing — OpenCode's
        # argv parsing assumes the briefing is the last argument (and, per
        # #1703, array-typed flags like --file must come AFTER it — see the
        # __init__ docstring's caveat about extra_args).
        if self._extra_args:
            argv.extend(self._extra_args)

        # Briefing is the final positional argument — passed on argv, NOT stdin.
        # Multi-line briefings are safe here because subprocess.Popen passes
        # the list directly to execv() (no shell interpolation).
        argv.append(spec.briefing)
        return argv

    def oneshot_command(
        self,
        *,
        system_prompt: str,
        output_format: str | None = "json",
    ) -> list[str]:
        """Best-effort one-shot argv for the OpenCode backend.

        LIMITATION, unchanged from the first pass and still real: OpenCode
        takes its briefing as a positional argv argument, not via stdin
        (confirmed, #1703), and this method's signature — inherited from the
        :class:`~.base.Provider` ABC — does not receive the user message, so
        it cannot be appended here either.  Callers (brain planning, the
        dashboard assistant) pipe the user message via stdin the way they do
        for :class:`~.claude.ClaudeProvider`; OpenCode will not see it.  Real
        one-shot semantics (a single prompt/response round with the message
        actually delivered) are not achievable through this ABC method for
        this backend — callers that need that should configure a
        :class:`~.claude.ClaudeProvider` backend instead, exactly as the
        first-pass docstring already said.

        What *is* corrected here (#1704): the returned argv now includes
        ``--format json`` when *output_format* is ``"json"`` (or whatever
        string is passed — forwarded verbatim, mirroring
        :meth:`~.claude.ClaudeProvider.oneshot_command`'s behaviour), so at
        least the output stream is the structured NDJSON shape
        :meth:`parse_log` understands rather than the unparseable human
        transcript.  This does **not** produce the ``{"result": ...}``
        top-level shape the brain's JSON-extraction path expects — opencode
        has no such wrapper (confirmed, #1703) — so callers still fall back
        to raw-stdout handling for this backend, as the first-pass docstring
        already documented.  ``--auto`` is always appended for the same
        headless-safety reason as :meth:`build_command`.

        *system_prompt* is still silently ignored: there is no CLI flag to
        inject ad hoc system-prompt text, only ``--agent <name>`` selecting
        a *pre-configured* agent (see :meth:`build_command`), and this
        method's signature has no assignment-spec context to compute an
        agent name from.

        Args:
            system_prompt: Accepted but **ignored** — see above.
            output_format: ``"json"`` (or any other string) is forwarded as
                ``--format <value>``.  ``None`` omits the flag entirely,
                falling back to opencode's human-transcript default —
                matching the dashboard-assistant streaming use case the
                same way ``output_format=None`` does for
                :class:`~.claude.ClaudeProvider`.
        """
        binary = self._binary if self._binary is not None else DEFAULT_OPENCODE_BINARY
        argv = [binary, "run"]
        if output_format is not None:
            argv.extend(["--format", output_format])
        argv.append("--auto")
        return argv

    def initial_input(self, spec: "AssignmentSpec") -> bytes:
        """Return an empty bytes object — the briefing travels on argv.

        Confirmed (#1703): ``run [message..]`` is a genuine positional argv
        message; no stdin-delivery protocol was observed.  Unlike
        :class:`~.claude.ClaudeProvider`, OpenCode receives its briefing as
        the final positional argument in :meth:`build_command`.  Returning
        ``b""`` (falsy) signals to the spawn path that nothing should be
        written to the worker's stdin pipe.
        """
        # Briefing is already embedded in the argv by build_command.
        # Return empty bytes so the spawn path's ``if initial_input:`` guard
        # skips the stdin write.
        return b""

    def result_marker(self) -> str:
        """Return the completion sentinel for OpenCode NDJSON logs.

        CORRECTED (#1704): see :data:`RESULT_MARKER`'s docstring for the
        full citation and the documented substring-fragility risk.
        """
        return RESULT_MARKER

    def env(self) -> dict[str, str]:
        """Extra environment variables for the worker subprocess.

        Three parts, deliberately split across **both sides** of
        ``self._env`` — that asymmetry is the whole point, see below:

        1. **#2321, seeded BEFORE ``self._env``, operator-overridable:**
           ``OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX`` defaults to
           :data:`OUTPUT_TOKEN_MAX_DEFAULT` (see that constant for the
           full rationale). Unlike part 3 below, this is a *tuning knob*,
           not a safety mechanism — an operator has a legitimate reason to
           pin it lower (e.g. to cap cost) or higher (a future model with
           a larger ``limit.output``), so it is seeded first and then
           allowed to be shadowed by ``self._env`` like any other
           ``ProviderDef.env`` entry.  ``coord/config.py``'s
           ``_parse_providers`` rejects a value at config-parse time that
           opencode's own parser would silently discard (non-integer,
           ``<= 0``, or containing whitespace/underscores) — see that
           function for the validation opencode itself performs.
        2. ``ProviderDef.env`` (already ``${VAR}``-expanded by config
           parsing, #1706) — how an operator points a named ``opencode``
           provider definition at a specific set of credentials without
           baking them into the machine's agent unit.  This is also where
           an operator's override of part 1 above lands, since dict
           ``.update()`` here happens after the default is seeded.
        3. **#1705, always set AFTER ``self._env``, NOT
           operator-overridable:** ``OPENCODE_CONFIG_DIR``
           (:data:`AGENTS_ROOT` — where the committed per-spec-type agent
           files live, see the module docstring for why an env var rather
           than a worktree file copy) and ``OPENCODE_CONFIG``
           (:data:`ROUTING_PIN_PATH` — the OpenRouter routing pin, see
           ``coord/agents/opencode/routing.jsonc``).  These are applied
           **after** ``self._env`` so a ``ProviderDef.env`` entry cannot
           accidentally (or maliciously, via a compromised
           ``coordinator.yml`` edit that isn't the security-reviewed
           agent-file path) shadow the deny-list discovery mechanism —
           see :class:`OpenCodeAgentNotFoundError`'s docstring for why
           silently losing this would be bad.  Verified empirically that
           ``OPENCODE_CONFIG_DIR`` is inert for a provider the operator
           hasn't configured credentials for (real opencode 1.18.11 run
           against a non-OpenRouter model with both variables set
           completed normally) — see
           ``test_opencode_routing_pin_inert_without_openrouter_credential_end_to_end``.

        The asymmetry: part 1 is a default an operator must be able to
        override (seeded *before* the merge, operator wins); part 3 is a
        safety mechanism an operator must never be able to override (set
        *after* the merge, coord wins).  Do not "fix" this into a single
        consistent ordering — the two groups need opposite precedence on
        purpose.

        Returns a fresh dict each call.
        """
        merged: dict[str, str] = {
            "OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX": str(OUTPUT_TOKEN_MAX_DEFAULT),
        }
        merged.update(self._env)  # operator wins over the tuning-knob default
        merged["OPENCODE_CONFIG_DIR"] = str(AGENTS_ROOT)  # coord wins, unchanged
        merged["OPENCODE_CONFIG"] = str(ROUTING_PIN_PATH)  # coord wins, unchanged
        return merged

    def parse_log(
        self, log_path: str | Path, tail_bytes: int = 65536
    ) -> WorkerSummary:
        """Parse an OpenCode NDJSON log file into a :class:`WorkerSummary`.

        CORRECTED (#1704) against the real event schema captured in #1703
        (``docs/OPENCODE_VERIFICATION.md`` "The real event schema" through
        "Session id / resume").  ``opencode run --format json`` emits one
        JSON object per line to stdout; the agent writes this stream
        verbatim to the log file.

        This method is **deliberately permissive** — it silently skips any
        line that is blank, not valid JSON, or an unrecognised event shape,
        and it NEVER raises regardless of log content.  This is unchanged
        from the first pass and still required: a truncated tail read
        (``tail_bytes > 0``) can produce a leading incomplete JSON line,
        which must be skipped, not raised.

        Real event shapes handled (see
        ``tests/fixtures/opencode_run_sample.jsonl`` and
        ``tests/fixtures/opencode_run_failure_sample.jsonl``):

        * Every event (any ``type``) carries a top-level ``sessionID``
          string from the first line onward — confirmed, there is no
          separate ``session.start``/``session.init`` event.  The first
          ``sessionID`` seen sets :attr:`WorkerSummary.session_id`.
        * ``{"type":"tool_use","part":{"tool":"...","state":{"status":...,
          "input":{...}}}}`` — the tool name updates
          :attr:`WorkerSummary.tools_used` /
          :attr:`WorkerSummary.last_tool`; a ``bash`` call's
          ``input.command`` is recorded in
          :attr:`WorkerSummary.bash_commands`; an ``edit``/``write`` call's
          ``input.filePath`` is recorded in
          :attr:`WorkerSummary.files_edited` (``edit`` confirmed against the
          real fixture; ``write`` extrapolated from the same ``filePath``
          convention ``read``/``edit`` both use — not independently
          confirmed, but the extraction is a no-op, never a crash, if that
          extrapolation is wrong).  A ``state.status == "error"`` whose
          ``state.error`` message mentions "permission" (the confirmed
          permission-denial error shape quotes the matching rules verbatim)
          is recorded in :attr:`WorkerSummary.permission_denials`.
        * ``{"type":"text","part":{"text":"..."}}`` — the assistant's final
          answer; confirmed to be the last ``text`` event before the
          terminal ``step_finish``, so each one overwrites
          :attr:`WorkerSummary.result_text` and the last write wins.
        * ``{"type":"step_finish","part":{"reason":...,"tokens":{...},
          "cost":...}}`` — one per completed turn/step.  Each occurrence
          increments :attr:`WorkerSummary.num_turns` by one and *sums*
          (never overwrites) ``part.cost`` into
          :attr:`WorkerSummary.total_cost_usd` and
          ``part.tokens.{input,output}`` /
          ``part.tokens.cache.{write,read}`` into
          :attr:`WorkerSummary.input_tokens` /
          :attr:`WorkerSummary.output_tokens` /
          :attr:`WorkerSummary.cache_creation_tokens` /
          :attr:`WorkerSummary.cache_read_tokens` — confirmed there is
          **no cumulative session-total field anywhere in the stream**, so
          summing per-step is the only correct total.  ``part.reason``
          overwrites :attr:`WorkerSummary.stop_reason` every time, so it
          ends at whatever the *last* ``step_finish`` reported — ``"stop"``
          for a normal completion.
        * ``{"type":"error","error":{"name":...,"data":{"message":...}}}``
          — a run-level failure (confirmed: the whole request/stream failed,
          no more events follow).  Sets
          :attr:`WorkerSummary.is_error` ``= True``,
          :attr:`WorkerSummary.stop_reason` ``= "error"``,
          :attr:`WorkerSummary.terminal_reason` from ``error.name``, and
          :attr:`WorkerSummary.result_text` from ``error.data.message``
          (the real captured message is itself a JSON string containing
          literal escaped quotes — preserved verbatim, not stripped).

        Known, named gap: **no field in any observed event carries a model
        identifier** (confirmed absent across every event type in both
        fixtures and every capture in #1703's pass).
        :attr:`WorkerSummary.model_used` is therefore never set by this
        method and stays ``None`` — this is evidenced absence, not an
        unexamined assumption.

        Args:
            log_path: Path to the worker's log file.
            tail_bytes: When > 0, only the last *tail_bytes* of the file are
                read (cheap live-polling).  Pass ``0`` for a full parse.

        Returns:
            A :class:`WorkerSummary` with whatever fields could be extracted.
            Returns a blank summary for a missing, empty, or unreadable file.
        """
        summary = WorkerSummary()
        p = Path(log_path)
        if not p.exists():
            return summary
        try:
            size = p.stat().st_size
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                if tail_bytes and size > tail_bytes:
                    f.seek(size - tail_bytes)
                    f.readline()  # discard the leading partial line
                text = f.read()
        except OSError:
            return summary

        for line in text.splitlines():
            if not line or not line.strip():
                continue
            try:
                data = json.loads(line)
            except (json.JSONDecodeError, ValueError, TypeError):
                # Non-JSON lines (e.g. "# argv=..." header comment written by the
                # agent, or plain-text error output) are silently skipped.
                continue
            if not isinstance(data, dict):
                continue
            _update_opencode_summary(summary, data)

        return summary


# ── Internal summary helper ────────────────────────────────────────────────────


def _update_opencode_summary(summary: WorkerSummary, data: dict) -> None:
    """Fold a single parsed NDJSON object into *summary* in-place.

    All field accesses use ``.get()`` with defensive type checks so that
    unexpected shapes produce at most a no-op, never a ``KeyError`` /
    ``AttributeError``.  Event shapes are the real ones captured in #1703 —
    see :meth:`OpenCodeProvider.parse_log`'s docstring for the full mapping
    and citations.
    """
    # Confirmed: sessionID is present on every event from line 1 onward —
    # capture the first one seen regardless of event type.
    if summary.session_id is None:
        sid = data.get("sessionID")
        if isinstance(sid, str) and sid:
            summary.session_id = sid

    event_type = data.get("type")
    if not isinstance(event_type, str):
        return

    part = data.get("part")
    part = part if isinstance(part, dict) else {}

    if event_type == "tool_use":
        tool = part.get("tool")
        if isinstance(tool, str) and tool:
            summary.tools_used.append(tool)
            summary.last_tool = tool

        state = part.get("state")
        state = state if isinstance(state, dict) else {}
        tool_input = state.get("input")
        tool_input = tool_input if isinstance(tool_input, dict) else {}

        if tool == "bash":
            cmd = tool_input.get("command")
            if isinstance(cmd, str) and cmd:
                summary.bash_commands.append(cmd)
                # #2236: same graph-usage instrumentation the claude parser
                # does. opencode carries the call and its output in one event,
                # so the outcome settles here instead of on a later
                # `tool_result`.
                from coord.worker_events import record_graphify_call  # noqa: PLC0415

                _out = state.get("output")
                record_graphify_call(
                    summary,
                    cmd,
                    output=_out if isinstance(_out, str) else None,
                    is_error=state.get("status") == "error",
                )
        elif tool in ("edit", "write"):
            fp = tool_input.get("filePath")
            if isinstance(fp, str) and fp:
                summary.files_edited.append(fp)

        if state.get("status") == "error":
            err = state.get("error")
            if isinstance(err, str) and err and "permission" in err.lower():
                summary.permission_denials.append(err)
        return

    if event_type == "text":
        text = part.get("text")
        if isinstance(text, str):
            summary.result_text = text
        return

    if event_type == "step_finish":
        reason = part.get("reason")
        if isinstance(reason, str) and reason:
            summary.stop_reason = reason

        summary.num_turns += 1

        tokens = part.get("tokens")
        tokens = tokens if isinstance(tokens, dict) else {}
        in_tok = tokens.get("input")
        if isinstance(in_tok, int):
            summary.input_tokens += in_tok
        out_tok = tokens.get("output")
        if isinstance(out_tok, int):
            summary.output_tokens += out_tok

        cache = tokens.get("cache")
        cache = cache if isinstance(cache, dict) else {}
        cache_write = cache.get("write")
        if isinstance(cache_write, int):
            summary.cache_creation_tokens += cache_write
        cache_read = cache.get("read")
        if isinstance(cache_read, int):
            summary.cache_read_tokens += cache_read

        cost = part.get("cost")
        if isinstance(cost, (int, float)):
            summary.total_cost_usd += float(cost)
        return

    if event_type == "error":
        summary.is_error = True
        summary.stop_reason = "error"
        error_obj = data.get("error")
        error_obj = error_obj if isinstance(error_obj, dict) else {}
        name = error_obj.get("name")
        if isinstance(name, str) and name:
            summary.terminal_reason = name
        err_data = error_obj.get("data")
        err_data = err_data if isinstance(err_data, dict) else {}
        message = err_data.get("message")
        if isinstance(message, str):
            summary.result_text = message
        return

    # step_start and any other/future event type carry nothing else this
    # method extracts — the sessionID capture above already ran.
