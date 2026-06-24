"""OpenCodeProvider: the ``opencode run`` concrete provider.

OpenCode (https://github.com/sst/opencode) is an open-source terminal coding
assistant that supports multiple AI backends (Anthropic, OpenAI, etc.) via the
user's own API keys.  This provider wraps ``opencode run`` for use as a coord
worker backend.

**IMPORTANT — UNVERIFIED IMPLEMENTATION.**  ``opencode`` was not installed on
the build machine when this provider was written.  Every assumption about the
OpenCode CLI interface and NDJSON output shape is documented inline below.  The
provider has been implemented defensively:

* :meth:`parse_log` never raises — unrecognised lines are silently skipped.
* :meth:`result_marker` is an assumed NDJSON event type; see its docstring.
* :meth:`build_command` uses an assumed ``opencode run BRIEFING`` invocation
  pattern; flag names may need adjustment against a real binary.

**A real-output verification pass is required before routing production
workers through this provider.**  Run ``opencode run "say hi" > /tmp/oc.txt``
on a machine with opencode installed, compare the actual NDJSON shape against
:data:`RESULT_MARKER` and :func:`_update_opencode_summary`, and update both
as needed.  See ``tests/fixtures/opencode_run_sample.jsonl`` for the assumed
schema.

Differences from :class:`~.claude.ClaudeProvider`:

* ``build_command()`` invokes ``opencode run BRIEFING`` (briefing on argv).
  No ``-p``, no ``--input-format``, no stream-json flags.
* ``initial_input()`` returns ``b""`` — no stdin payload needed since the
  briefing travels on argv.
* ``capabilities()`` reports ``enforces_deny_list=False`` (SAFETY: OpenCode
  has no equivalent to Claude's ``--allowedTools`` / ``--permission-mode`` deny
  list, so the safety gate in :meth:`coord.agent.AgentServer.assign` will
  refuse write-capable assignment types (``work``, ``review``,
  ``conflict-fix``) on this provider until the wiring issue #324 verifies
  deny-list enforcement end-to-end).
* ``capabilities()`` reports ``billing_mode="byo_key"`` — OpenCode uses the
  operator's own API keys; cost is not metered against Anthropic's
  ``claude -p`` credit pool.
* ``capabilities()`` reports ``cost_reporting=False`` — OpenCode may embed
  usage data in its NDJSON output, but the field paths are unverified; coord
  sets this to ``False`` until a real-run parse is confirmed.
* ``capabilities()`` reports ``true_system_prompt=False`` — OpenCode has no
  documented ``--system-prompt`` flag equivalent in this first pass.  The
  ``system_prompt`` kwarg accepted by :meth:`build_command` is accepted but
  silently ignored.  Flip to ``True`` once an OpenCode flag is identified and
  wired in.
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
#: ASSUMPTION: ``opencode`` is on PATH.  Override via
#: ``ProviderDef(type="opencode", binary="/path/to/opencode")``.
DEFAULT_OPENCODE_BINARY = "opencode"

#: Sentinel string whose presence in the log signals successful completion.
#:
#: ASSUMPTION: OpenCode emits a ``{"type":"session.complete", ...}`` NDJSON
#: event as the last structured output when a run finishes successfully.
#: This mirrors the approach used by :class:`~.claude.ClaudeProvider` whose
#: marker is the stream-json ``"type":"result"`` event.
#:
#: **Must be verified against real opencode output.**  If the actual
#: completion event differs (e.g. ``"type":"session.idle"`` or a plain-text
#: sentinel), update this constant and re-run the test suite.
RESULT_MARKER = '"type":"session.complete"'


class OpenCodeProvider(Provider):
    """Concrete provider for ``opencode run`` (OpenCode workers).

    This is the **first-pass adapter** for the OpenCode backend.  It
    implements the full :class:`~coord.providers.base.Provider` ABC so the
    registry is complete, but several capabilities flags are set
    conservatively pending a real-output verification run (see module
    docstring).

    Args:
        binary: Override the worker binary name/path.  ``None`` falls back to
            :data:`DEFAULT_OPENCODE_BINARY` (``"opencode"``).
        attach_url: When set, passes ``--attach <attach_url>`` so the worker
            connects to an already-running OpenCode server instead of starting
            a new session.  Corresponds to ``ProviderDef.attach_url`` in
            ``coordinator.yml``.  ``None`` omits the flag (default headless
            ``opencode run`` starts its own session).
    """

    def __init__(
        self,
        binary: str | None = None,
        *,
        attach_url: str | None = None,
    ) -> None:
        self._binary = binary
        self._attach_url = attach_url

    # ── Capabilities ──────────────────────────────────────────────────────────

    def capabilities(self) -> Capabilities:
        """Conservatively declared capabilities for the OpenCode backend.

        Chosen values and rationale
        ~~~~~~~~~~~~~~~~~~~~~~~~~~~

        ``resume=True``
            ASSUMPTION: OpenCode supports session resume via a ``--session
            SESSION_ID`` flag.  The :meth:`build_command` implementation
            wires this flag when ``spec.resume_session_id`` is set.  Flip to
            ``False`` if OpenCode's headless mode has no resume concept.

        ``inject=False``
            No mid-session stdin message injection path exists for OpenCode
            in this first pass.  Flip to ``True`` if a future wiring issue
            identifies a mechanism.

        ``cost_reporting=False``
            ASSUMPTION: OpenCode may include usage data (``cost_usd``, token
            counts) in its NDJSON output under
            ``{"type":"session.complete","usage":{...}}``.  However, the
            field paths are unverified against a real run.
            :meth:`parse_log` makes a best-effort extraction attempt; this
            flag is conservatively ``False`` so the TUI shows "n/a" rather
            than a potentially incorrect dollar figure.  Flip to ``True``
            once :meth:`parse_log` is confirmed against live output.

        ``true_system_prompt=False``
            OpenCode has no documented ``--system-prompt`` flag equivalent
            in this first pass.  The ``system_prompt`` kwarg is accepted by
            :meth:`build_command` but silently ignored.  Flip to ``True``
            once an OpenCode flag is identified, wired in, and tested.

        ``enforces_deny_list=False``
            **SAFETY GATE.**  coord's worker deny list (passed via
            ``--allowedTools`` / ``--permission-mode`` for Claude) has no
            equivalent in OpenCode's CLI.  Setting this to ``False`` means
            :meth:`coord.agent.AgentServer.assign` will refuse write-capable
            assignment types (``work``, ``review``, ``conflict-fix``,
            ``smoke``) on this provider.  Only read-only types (``plan``,
            ``refinement``, ``test-chat``, ``new-issue-chat``) may use it
            until deny-list enforcement is wired and verified.  Do NOT flip
            this to ``True`` without first confirming that OpenCode respects
            the tool-restriction mechanism end-to-end.

        ``billing_mode="byo_key"``
            OpenCode uses the operator's own API keys (Anthropic, OpenAI,
            etc.) configured in its own credentials store.  Runs are NOT
            billed against Anthropic's ``claude -p`` credit pool, so this
            backend is not subject to the 2026-06-15 metering change (#322).
            ``"byo_key"`` is the correct routing signal for the Track-3
            mitigation: the coordinator and TUI prefer a non-metered backend.

        ``human_attended_only=False``
            OpenCode's headless ``run`` mode is designed for unattended
            automation.  No ToS compliance concern analogous to Claude's
            interactive subscription path (#437) applies here.
        """
        return Capabilities(
            # ASSUMPTION: --session flag enables resume; see build_command.
            resume=True,
            # No mid-session injection path for OpenCode in this pass.
            inject=False,
            # Conservatively False until parse_log is verified against live output.
            cost_reporting=False,
            # No --system-prompt equivalent identified yet; accepted but ignored.
            true_system_prompt=False,
            # SAFETY: OpenCode does not enforce coord's deny list.
            enforces_deny_list=False,
            # Uses operator's own API keys — not subject to claude -p metering.
            billing_mode="byo_key",
            # Headless run mode is automatable; no ToS gate needed.
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

        ASSUMPTION: OpenCode's non-interactive interface is::

            opencode run [--model MODEL] [--session SESSION_ID] BRIEFING

        where ``BRIEFING`` is the final positional argument containing the
        full task description.  The briefing is passed on argv, NOT via
        stdin, which is why :meth:`initial_input` returns ``b""``.

        Flag name assumptions (to be verified against real binary):

        * ``--model`` — select model by name or alias (e.g.
          ``claude-sonnet-4-5``).  Omitted when *resolved_model* and
          ``spec.model`` are both ``None``.
        * ``--session SESSION_ID`` — resume a prior session by ID.  Omitted
          when ``spec.resume_session_id`` is ``None``.

        Ignored kwargs
        ~~~~~~~~~~~~~~
        *system_prompt*, *allowed_tools*, and *permission_mode* are accepted
        (matching the Provider ABC signature) but **silently ignored**.
        OpenCode has no direct equivalents in this first pass.
        ``capabilities().true_system_prompt=False`` and
        ``capabilities().enforces_deny_list=False`` advertise this to
        callers.

        Args:
            spec: The assignment spec being dispatched.
            resolved_model: The resolved model identifier to pass.  When
                provided, takes precedence over ``spec.model``.  ``None``
                falls back to ``spec.model``; if that is also ``None``, the
                ``--model`` flag is omitted (OpenCode picks its configured
                default).
            system_prompt: Accepted but **ignored** — no OpenCode equivalent
                in this first pass.
            allowed_tools: Accepted but **ignored** — no OpenCode equivalent
                in this first pass.
            permission_mode: Accepted but **ignored** — no OpenCode
                equivalent in this first pass.
        """
        binary = self._binary if self._binary is not None else DEFAULT_OPENCODE_BINARY

        # resolved_model takes precedence; fall back to spec.model.
        effective_model = resolved_model if resolved_model is not None else spec.model

        # ASSUMPTION: subcommand is "run" for non-interactive / headless mode.
        argv: list[str] = [binary, "run"]

        # When attach_url is set, connect to a running OpenCode server instead
        # of starting a new session.  ASSUMPTION: flag is ``--attach <URL>``.
        if self._attach_url:
            argv.extend(["--attach", self._attach_url])

        # ASSUMPTION: --model flag selects the AI model by name.
        if effective_model:
            argv.extend(["--model", effective_model])

        # ASSUMPTION: --session flag resumes a prior session by ID.
        if spec.resume_session_id:
            argv.extend(["--session", spec.resume_session_id])

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

        LIMITATION: OpenCode has no ``--system-prompt`` flag and takes its
        briefing as a positional argv argument rather than via stdin.  The
        *system_prompt* and *output_format* kwargs are therefore silently
        ignored — OpenCode will not receive the system prompt and will not
        emit the ``{"result": ...}`` JSON shape that the brain expects.

        This method returns ``[binary, "run"]`` without the user message
        because ``oneshot_command()`` does not receive the user message as
        an argument.  The brain's ``call_claude()`` pipes the user message
        via stdin; OpenCode will ignore it and produce its own output
        (which :func:`coord.brain.call_claude` returns as raw stdout after
        the JSON-extraction fallback path).

        Callers that need a fully functional one-shot path should configure
        a :class:`~.claude.ClaudeProvider` backend instead of OpenCode.

        ASSUMPTION: ``opencode run`` is the headless subcommand.  This
        must be verified against a real ``opencode`` binary.

        Args:
            system_prompt: Accepted but **ignored** — OpenCode has no
                ``--system-prompt`` equivalent.
            output_format: Accepted but **ignored** — OpenCode does not
                emit ``{"result": ...}`` JSON.
        """
        binary = self._binary if self._binary is not None else DEFAULT_OPENCODE_BINARY
        # ASSUMPTION: 'run' is the non-interactive / headless subcommand.
        # system_prompt and output_format are silently dropped; see docstring.
        return [binary, "run"]

    def initial_input(self, spec: "AssignmentSpec") -> bytes:
        """Return an empty bytes object — the briefing travels on argv.

        Unlike :class:`~.claude.ClaudeProvider`, OpenCode receives its
        briefing as the final positional argument in :meth:`build_command`
        rather than via a stream-json user message on stdin.  Returning
        ``b""`` (falsy) signals to the spawn path that nothing should be
        written to the worker's stdin pipe.
        """
        # Briefing is already embedded in the argv by build_command.
        # Return empty bytes so the spawn path's ``if initial_input:`` guard
        # skips the stdin write.
        return b""

    def result_marker(self) -> str:
        """Return the assumed completion sentinel for OpenCode NDJSON logs.

        ASSUMPTION: ``opencode run`` emits a ``{"type":"session.complete",
        ...}`` NDJSON event as its last structured output line on success.
        This substring (``"type":"session.complete"``) is searched verbatim
        in the log bytes by the reap thread to detect completion.

        **Must be verified against real opencode output.**  See the module
        docstring and ``tests/fixtures/opencode_run_sample.jsonl`` for the
        assumed schema.
        """
        return RESULT_MARKER

    def env(self) -> dict[str, str]:
        """No extra environment variables required for ``opencode run``.

        OpenCode reads its own API key configuration from its credentials
        store (typically ``~/.config/opencode/`` or via ``OPENCODE_*``
        environment variables).  The coordinator does not need to inject any
        variables in this first pass.
        """
        return {}

    def parse_log(
        self, log_path: str | Path, tail_bytes: int = 65536
    ) -> WorkerSummary:
        """Parse an OpenCode NDJSON log file into a :class:`WorkerSummary`.

        ASSUMPTION: ``opencode run`` emits one JSON object per line (NDJSON)
        to stdout.  The agent writes this stream verbatim to the log file.

        This method is **deliberately permissive** — it silently skips any
        line that is blank, not valid JSON, or an unrecognised event shape.
        It NEVER raises regardless of log content.  This matches the contract
        described in the Provider ABC and is required because:

        1. The actual OpenCode output shape was not verified at implementation
           time; real output may differ from the assumed schema.
        2. Partial / truncated tail reads (``tail_bytes > 0``) can produce
           a leading incomplete JSON line which must be skipped, not raised.

        Assumed event shapes parsed (see ``tests/fixtures/opencode_run_sample.jsonl``):

        * ``{"type":"session.start","session_id":"...","model":"..."}`` —
          sets :attr:`WorkerSummary.session_id` and
          :attr:`WorkerSummary.model_used`.
        * ``{"type":"message.complete","num_turns":N}`` — updates turn count.
        * ``{"type":"session.complete","num_turns":N,"usage":{"cost_usd":X,
          "input_tokens":N,"output_tokens":N}}`` — updates turn count and
          (if present) cost / token counters.

        All other event types are accepted and silently ignored.

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
    ``AttributeError``.

    ASSUMPTION: OpenCode NDJSON event shapes as documented in
    ``tests/fixtures/opencode_run_sample.jsonl``.  Every extraction point
    is marked with ``# ASSUMPTION:`` to make verification easy.
    """
    event_type = data.get("type")
    if not isinstance(event_type, str):
        return

    # ASSUMPTION: session.start carries session_id and model.
    if event_type == "session.start":
        sid = data.get("session_id")
        if isinstance(sid, str) and sid:
            summary.session_id = sid
        model = data.get("model")
        if isinstance(model, str) and model and not summary.model_used:
            summary.model_used = model
        return

    # ASSUMPTION: message.complete carries num_turns for the assistant turn.
    if event_type == "message.complete":
        turns = data.get("num_turns")
        if isinstance(turns, int) and turns > summary.num_turns:
            summary.num_turns = turns
        # session_id may also appear here — capture it if not yet set.
        if summary.session_id is None:
            sid = data.get("session_id")
            if isinstance(sid, str) and sid:
                summary.session_id = sid
        return

    # ASSUMPTION: session.complete carries num_turns, and an optional usage
    # sub-object with cost_usd, input_tokens, output_tokens.
    if event_type == "session.complete":
        turns = data.get("num_turns")
        if isinstance(turns, int) and turns > summary.num_turns:
            summary.num_turns = turns
        if summary.session_id is None:
            sid = data.get("session_id")
            if isinstance(sid, str) and sid:
                summary.session_id = sid

        # ASSUMPTION: usage sub-object with cost_usd and token counts.
        usage = data.get("usage")
        if isinstance(usage, dict):
            cost = usage.get("cost_usd") or usage.get("total_cost_usd")
            if isinstance(cost, (int, float)):
                summary.total_cost_usd = float(cost)
            in_tok = usage.get("input_tokens")
            if isinstance(in_tok, int) and in_tok > 0:
                summary.input_tokens = in_tok
            out_tok = usage.get("output_tokens")
            if isinstance(out_tok, int) and out_tok > 0:
                summary.output_tokens = out_tok

        # stop_reason equivalent: no standard field identified yet.
        # ASSUMPTION: if present, carry it from a "reason" or "stop_reason" key.
        stop = data.get("stop_reason") or data.get("reason")
        if isinstance(stop, str) and stop:
            summary.stop_reason = stop
        return
