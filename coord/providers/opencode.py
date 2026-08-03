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
verbatim failing one.  This module is the follow-up correction: every
behavioural detail below cites that document.  Two things it does **not**
attempt (explicitly out of scope, tracked as separate issues in the
opencode-backend epic):

* The concrete ``coord-<spec.type>`` opencode agent definitions
  (``opencode.jsonc``) that ``--agent`` below references by name.  Until
  those land, real dispatch through this provider will fail at the CLI
  level (``--agent`` naming an agent that doesn't exist yet) — see
  :meth:`OpenCodeProvider.build_command`.
* Flipping ``capabilities().enforces_deny_list`` to ``True``.  OpenCode DOES
  have a real deny-capable permission system (see the module docstring's
  "Permissions" citation below), but proving coord's *generated* config
  enforces it end-to-end is that same follow-up issue's job, not this one's.

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
* ``capabilities().enforces_deny_list=False`` — **SAFETY GATE**, see above.
* ``capabilities().billing_mode="byo_key"`` — confirmed: OpenCode bills
  against the operator's own configured provider credentials, not
  Anthropic's ``claude -p`` credit pool (``OPENCODE_VERIFICATION.md``
  "Flag surface" / "every ASSUMPTION" table).
* ``capabilities().cost_reporting=True`` and
  ``capabilities().true_system_prompt=True`` — both flipped from the
  first-pass ``False`` now that real evidence backs them; see
  :meth:`capabilities` for the citations.
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


def _agent_name_for_type(spec_type: str) -> str:
    """Map ``spec.type`` to the opencode ``--agent`` name coord will pass.

    Naming contract: ``coord-<spec.type>`` (e.g. ``coord-work``,
    ``coord-plan``, ``coord-review``, ``coord-test-chat``). This mirrors the
    existing ``spec.type``-keyed branching :class:`~.claude.ClaudeProvider`
    uses to pick a system prompt / allowed-tools pair (see
    ``coord/providers/claude.py::build_command``) — the same dimension
    (assignment type) selects the opencode-side equivalent, just via a named
    agent instead of raw flag values.

    **Not yet backed by a real ``opencode.jsonc``.**  The companion
    agent-definitions issue in the opencode-backend epic is responsible for
    actually authoring ``coord-work`` / ``coord-plan`` / etc. agent configs
    (deny-baseline permission blocks, real prompts). Until that lands, a
    real ``opencode run --agent coord-<type> ...`` invocation through this
    provider will fail at the CLI level with an unknown-agent error — this
    is a known, named gap (not a silent one): this provider corrects the
    *shape* of the invocation per #1703's captured evidence; wiring a real
    agent behind that shape is deliberately out of scope for #1704 (see the
    module docstring).
    """
    return f"coord-{spec_type}"


class OpenCodeProvider(Provider):
    """Concrete provider for ``opencode run`` (OpenCode workers).

    Corrected against a real ``opencode`` 1.18.11 capture (#1703 →
    ``docs/OPENCODE_VERIFICATION.md``).  Two things remain deliberately
    unfinished — see the module docstring: the actual ``coord-<type>``
    agent definitions (companion issue), and flipping
    ``capabilities().enforces_deny_list`` (same companion issue, after
    end-to-end enforcement is proven).

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
            provider config (e.g. API keys) at a specific backend.  Returned
            verbatim by :meth:`env`.
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
            ``OPENCODE_VERIFICATION.md`` "every ASSUMPTION" table.  This is
            a capability claim (the mechanism is real), independent of
            whether coord has actually authored the ``coord-<type>`` agent
            files yet (see :func:`_agent_name_for_type`).

        ``enforces_deny_list=False``
            **SAFETY GATE — deliberately left unchanged in this issue.**
            OpenCode DOES have a real per-tool ``allow``/``ask``/``deny``
            permission system with genuine bash command pattern matching —
            empirically proven in #1703 (``OPENCODE_VERIFICATION.md``
            "Permission enforcement — empirical evidence").  But two things
            are still missing before coord can claim *enforcement*: (1) the
            actual ``coord-<type>`` agent configs that encode a deny-shaped
            baseline (companion agent-definitions issue), and (2) proof that
            coord's generated config places the catch-all rule *first* and
            overrides *after* it — #1703 found rule precedence is
            **last-match-wins**, so a naively-ordered deny list silently
            does nothing.  Until both are verified end-to-end,
            :meth:`coord.agent.AgentServer.assign` continues to refuse
            write-capable assignment types (``work``, ``review``,
            ``conflict-fix``, ``smoke``) on this provider.

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
            # SAFETY: see docstring above — real mechanism exists, coord-side
            # enforcement is unproven until the companion issue lands.
            enforces_deny_list=False,
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
        * ``--agent <name>`` — **new in #1704.**  Confirmed: ``--agent``
          selects a named agent definition whose ``prompt``/``permission``
          fields are a genuine system-prompt + tool-permission equivalent
          (schema-confirmed against ``https://opencode.ai/config.json``).
          This is why it **replaces** the ignored *system_prompt* /
          *allowed_tools* kwargs below rather than sitting alongside them —
          there is no opencode flag that accepts raw system-prompt text or
          a raw tool allowlist string the way ``claude -p`` does; the unit
          of configuration is a named agent.  The name is computed by
          :func:`_agent_name_for_type` from ``spec.type`` — see that
          function's docstring for the naming contract and the (explicit,
          named) gap that the actual agent config files don't exist yet.
        * ``--auto`` — **new in #1704, always passed.**  Confirmed:
          ``--auto`` converts an agent's ``"ask"`` permission rules to
          auto-approve without overriding an explicit ``"deny"`` rule
          (``OPENCODE_VERIFICATION.md`` "``--auto`` semantics", scenarios
          2 & 3).  coord's workers are headless/unattended by design, so
          this flag is unconditional — the same way ``permission_mode``
          defaults to ``"acceptEdits"`` for :class:`~.claude.ClaudeProvider`.
          **Safety note** (see the issue and module docstring): opencode's
          permission model is deny-*list* semantics inverted from
          ``claude -p``'s allow-list — ``--auto`` only ever *widens* what an
          ``"ask"`` rule would otherwise block; it can never widen past an
          explicit ``"deny"``.  Today, with no ``coord-<type>`` agent config
          yet authored, this is close to a no-op (opencode's built-in
          default agent has no permission block at all — confirmed
          allow-everything by default, ``OPENCODE_VERIFICATION.md``
          "``--auto`` semantics" scenario 4) — it only becomes load-bearing
          once the companion agent-definitions issue lands a deny-baseline
          config, at which point it is exactly what keeps this provider
          headless instead of hanging on every ``ask`` rule.

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
        """Extra environment variables from the provider definition (#1706).

        OpenCode reads its own API key configuration from its credentials
        store (typically ``~/.config/opencode/`` or via ``OPENCODE_*``
        environment variables) — ``ProviderDef.env`` (already
        ``${VAR}``-expanded by config parsing) is exactly how an operator
        points a named ``opencode`` provider definition at a specific set of
        credentials without baking them into the machine's agent unit.
        Returns a copy; empty dict when the provider was constructed with no
        ``env`` (matches pre-#1706 behaviour for no-config deployments).
        """
        return dict(self._env)

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
