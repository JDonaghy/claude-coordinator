"""Agent server core: spawns `claude -p` subprocesses for assignments.

The HTTP layer is in `coord.agent_app`. This module is transport-agnostic and
tests can drive it directly without standing up a real server.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterable


DEFAULT_STATE_DIR = Path.home() / ".coord"
DEFAULT_WORKER_BINARY = "claude"


def _dir_size(path: Path) -> int:
    """Return total bytes consumed by all regular files under *path*.

    Silently skips entries that can't be stat'd (deleted mid-walk, permission
    errors, etc.).  Returns 0 when *path* does not exist.
    """
    total = 0
    try:
        for p in path.rglob("*"):
            try:
                st = p.stat()
                if stat.S_ISREG(st.st_mode):
                    total += st.st_size
            except OSError:
                pass
    except OSError:
        pass
    return total

# Stamp captured at module import so `health()` can report when THIS
# process started. exec_restart() replaces the image, so the new
# process re-imports this module and the stamp updates — letting the
# CLI detect a real restart vs the old agent still answering.
_PROCESS_STARTED_AT: float = time.time()

# Statuses
PENDING = "pending"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"


# ── Reap tuning ───────────────────────────────────────────────────────────────
# claude-cli sometimes does not exit after emitting its final
# `{"type":"result"}` message — a child process (MCP server, tool subprocess)
# holds the session's process group open and proc.wait() blocks indefinitely.
# The reap thread detects logical completion from the log and force-kills the
# group after a grace period. See #228 for the underlying bug.
_REAP_POLL_INTERVAL = 5.0        # seconds between proc.wait timeout attempts
_REAP_GRACE_AFTER_RESULT = 30.0  # grace period after result line before SIGTERM
_REAP_MAX_WAIT = 2 * 60 * 60.0   # absolute max wait (2 hours) — last-resort safety net
_RESULT_LINE_MARKER = b'"type":"result"'

# First-output (TTFT) watchdog default and the distinct exit code used when it
# fires, so `_reap` records the assignment as FAILED (any non-zero exit) and the
# `concurrency.auto_reassign` path re-dispatches it. See #299 and the upstream
# daemon-spawn stall report (anthropics/claude-code#56268).
_FIRST_OUTPUT_TIMEOUT = 600.0    # seconds of zero output before the watchdog kills
NO_FIRST_OUTPUT_EXIT = 124       # exit code reported when the TTFT watchdog fires


def _append_log_line(log_path: str, line: str) -> None:
    """Best-effort append of a single line to the assignment log. Never raises."""
    try:
        with open(log_path, "a") as fh:
            fh.write(line)
    except OSError:
        pass


def _killpg_safe(pid: int, sig: int) -> None:
    """`os.killpg` that swallows already-gone/permission errors."""
    try:
        os.killpg(pid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _log_has_result(log_path: str) -> bool:
    """Return True if the worker's stream-json log contains a final result event."""
    try:
        with open(log_path, "rb") as f:
            return _RESULT_LINE_MARKER in f.read()
    except OSError:
        return False


def _log_has_output(log_path: str) -> bool:
    """Return True once the worker has produced any output beyond the spawn header.

    `_spawn` writes `# ...` comment lines (the argv header and any pull notes)
    before the worker starts; the worker's stream-json output is never a
    `#`-comment. So the watchdog considers the worker to have produced output
    as soon as the log contains any non-blank, non-`#`-comment line. A
    rate-limited worker emits turn / `[rate_limit]` events, so it trips this
    check and is never killed by the TTFT watchdog — only truly silent (zero
    output) hangs are caught.
    """
    try:
        with open(log_path, "rb") as f:
            for raw in f:
                line = raw.strip()
                if not line:
                    continue
                if line.startswith(b"#"):
                    continue
                return True
    except OSError:
        return False
    return False


def _maybe_bash_wrap(argv: list[str], enabled: bool) -> list[str]:
    """Optionally wrap *argv* in a transient `bash -c 'exec ...'` parent.

    When enabled, the immediate parent of `claude` is a short-lived bash that
    `exec`s into claude — same PID, so `start_new_session`, `proc.pid`, the
    stdin pipe, and process-group kills all behave identically to a bare
    spawn. This is the upstream headline fix for the daemon-spawn freeze
    (anthropics/claude-code#56268). When disabled, the bare argv is returned.
    """
    if not enabled:
        return argv
    return ["bash", "-c", "exec " + shlex.join(argv)]


def _wait_for_proc_or_result(
    proc: subprocess.Popen,
    log_path: str,
    *,
    poll_interval: float = _REAP_POLL_INTERVAL,
    grace_after_result: float = _REAP_GRACE_AFTER_RESULT,
    max_wait: float = _REAP_MAX_WAIT,
    first_output_timeout: float = _FIRST_OUTPUT_TIMEOUT,
    killpg: Callable[[int, int], None] = _killpg_safe,
    log_has_result: Callable[[str], bool] = _log_has_result,
    log_has_output: Callable[[str], bool] = _log_has_output,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    """Wait for `proc` to exit; force-kill its process group if it hangs after
    the worker emitted its final result event.

    Returns the worker's exit code. Always returns within roughly `max_wait`
    seconds even if the process group refuses to die. If the worker's result
    line was observed before we killed it, returns 0 — the work is logically
    complete, only the runtime is being torn down.

    First-output (TTFT) watchdog: if ``first_output_timeout > 0`` and the
    worker produces no output at all within that many seconds, its process
    group is killed and :data:`NO_FIRST_OUTPUT_EXIT` is returned so `_reap`
    marks the assignment FAILED. Once any output is seen the watchdog is
    satisfied permanently — it never re-arms — so a slow-but-emitting (e.g.
    rate-limited) worker is never killed by it. See #299.

    The keyword-only parameters exist for tests to inject short timeouts and
    mock kill/clock behavior.
    """
    start = clock()
    result_seen_at: float | None = None
    output_seen = False

    while True:
        try:
            return proc.wait(timeout=poll_interval)
        except subprocess.TimeoutExpired:
            pass

        elapsed = clock() - start

        # First-output / TTFT watchdog: catch a worker that emits zero bytes.
        # Once any output is seen the watchdog is satisfied forever (never
        # re-armed) so slow-but-emitting workers pass.
        if first_output_timeout > 0 and not output_seen:
            if log_has_output(log_path):
                output_seen = True
            elif elapsed >= first_output_timeout:
                _append_log_line(
                    log_path,
                    f"# reap: no first output in {first_output_timeout:.0f}s — "
                    "killing process group (suspected daemon-spawn stall)\n",
                )
                killpg(proc.pid, signal.SIGKILL)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
                return NO_FIRST_OUTPUT_EXIT

        # Detect logical completion: worker emitted its final result event.
        if result_seen_at is None and log_has_result(log_path):
            result_seen_at = clock()
            _append_log_line(
                log_path,
                "# reap: worker emitted result; awaiting clean exit\n",
            )

        if result_seen_at is not None and clock() - result_seen_at >= grace_after_result:
            # Worker logically done but process group still alive — force-kill.
            _append_log_line(
                log_path,
                f"# reap: SIGTERM process group after {grace_after_result:.0f}s grace\n",
            )
            killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _append_log_line(
                    log_path,
                    "# reap: SIGKILL process group (SIGTERM ignored)\n",
                )
                killpg(proc.pid, signal.SIGKILL)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    _append_log_line(
                        log_path,
                        "# reap: process group survived SIGKILL; abandoning wait\n",
                    )
            return 0  # Worker's work was complete before we killed the runtime.

        if elapsed >= max_wait:
            # Absolute safety net: worker never emitted a result and ran past
            # the max-wait cap. Treat as failed and kill the group.
            _append_log_line(
                log_path,
                f"# reap: SIGKILL after {max_wait:.0f}s max-wait without result line\n",
            )
            killpg(proc.pid, signal.SIGKILL)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            return 137  # SIGKILL convention


@dataclass
class AssignmentSpec:
    """What the coordinator hands to an agent. Stable shape on the wire."""

    repo_name: str
    repo_path: str
    issue_number: int
    issue_title: str
    briefing: str
    files_allowed: list[str] = field(default_factory=list)
    files_forbidden: list[str] = field(default_factory=list)
    branch: str | None = None
    pull_repos: list[str] = field(default_factory=list)
    artifact_paths: list[str] = field(default_factory=list)
    # #352: per-repo new-issue guidance (only for type="new-issue-chat").
    new_issue_guidance: str = ""
    # "work" (default) or "review". The agent treats both the same — what
    # differs is the briefing and (for reviewers) the system prompt.
    type: str = "work"
    # Optional override of WORKER_SYSTEM_PROMPT. Reviewers need a different
    # system prompt because they're allowed to run `gh pr review` while
    # workers are not.
    system_prompt: str | None = None
    # PR number being reviewed (only set for type="review").
    review_target: str | None = None
    # Command patterns the worker must not run (prompt-level enforcement).
    deny_commands: list[str] = field(default_factory=list)
    # Claude model tier alias (e.g. "haiku", "sonnet", "opus"). When None,
    # the worker command omits --model so claude -p picks its default.
    model: str | None = None
    # When True, ignore existing issue-N-* branches and create a fresh branch
    # from the default branch. Used by --force dispatch to avoid stale branches.
    fresh_branch: bool = False
    # #target_branch: override the slugified-title-derived branch name with
    # an explicit existing branch.  Used by the auto-loop's fix dispatch so
    # the fix worker pushes commits to the ORIGINAL work's branch (and the
    # same PR gets the fix) instead of creating a new orphan branch from
    # the `[fix-N]` issue-title prefix.  When set, the agent checks out
    # this branch directly instead of deriving from issue_number + title.
    target_branch: str | None = None
    # #315: when set, pass `--resume <session_id>` to claude -p so it loads
    # the prior conversation and continues it.  The `briefing` field IS the
    # new user message; claude reads the prior conversation via --resume and
    # then sees this as the next user turn.  Only set for chat-continue
    # re-dispatches; regular work/plan/review dispatches leave this None.
    resume_session_id: str | None = None


class _GitError(RuntimeError):
    pass


def _git(cwd: Path, *args: str, timeout: float = 15.0) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise _GitError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _safe_realpath(path: str) -> str:
    try:
        return os.path.realpath(path)
    except OSError:
        return path


def _worker_subprocess_env(
    base_env: dict[str, str] | None = None,
    *,
    prefix: str | None = None,
    base_prefix: str | None = None,
) -> dict[str, str]:
    """Environment for worker `claude -p` subprocesses, with the agent's own
    venv removed (#402).

    The agent runs from a venv whose ``bin`` is first on PATH (systemd unit),
    and workers are spawned with ``cwd`` inside an ephemeral
    ``~/.coord/worktrees/<id>`` checkout. Without sanitizing the environment, a
    worker that runs ``pip install -e .`` (e.g. following the repo's CLAUDE.md
    dev step) resolves ``pip`` to the *agent's* venv and pins its editable
    finder to the worktree. When the worktree is reaped the agent crash-loops
    with ``ModuleNotFoundError: No module named 'coord'``.

    Dropping the agent's venv ``bin`` from PATH (and clearing ``VIRTUAL_ENV`` /
    ``PYTHONHOME``) forces a worker's ``pip``/``python`` to its own venv instead
    of the agent's. Only strips when the agent is actually running inside a venv
    (``prefix != base_prefix``) so a system-Python agent never loses
    ``/usr/bin`` & co.
    """
    env = dict(os.environ if base_env is None else base_env)
    pfx = sys.prefix if prefix is None else prefix
    base_pfx = sys.base_prefix if base_prefix is None else base_prefix

    if pfx and base_pfx and _safe_realpath(pfx) != _safe_realpath(base_pfx):
        venv_bin = _safe_realpath(os.path.join(pfx, "bin"))
        path = env.get("PATH", "")
        if path:
            kept = [
                part
                for part in path.split(os.pathsep)
                if part and _safe_realpath(part) != venv_bin
            ]
            env["PATH"] = os.pathsep.join(kept)

    env.pop("VIRTUAL_ENV", None)
    env.pop("PYTHONHOME", None)
    return env


def _slugify(text: str, max_len: int = 40) -> str:
    """Convert *text* to a URL/branch-safe slug."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len].rstrip("-")


def _sanitize_branch(branch: str) -> str:
    """Sanitize a git branch name for use as a filesystem / URL path component.

    Replaces any character that isn't alphanumeric, ``-``, ``_``, or ``.``
    with a dash.  This converts slashes (``feature/my-thing`` →
    ``feature-my-thing``) and any other URL-unsafe characters.  The result
    is safe to use as a single path segment (no embedded ``/``).
    """
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", branch).strip("-")


@dataclass
class AgentAssignment:
    """Server-side record. Carries the spec plus runtime metadata."""

    id: str
    spec: AssignmentSpec
    status: str = PENDING
    pid: int | None = None
    started_at: float | None = None
    finished_at: float | None = None
    exit_code: int | None = None
    log_path: str | None = None
    error: str | None = None
    branch: str | None = None
    worktree_path: str | None = None
    # #315: claude session ID captured from the `system.init` event in the
    # worker log.  Set by `_reap` after the worker exits.  Exposed via
    # `/status` and persisted in the agent state JSON so it survives agent
    # restart.  The coordinator reads it from the `/status` response and
    # writes it to the coordinator DB (see coord/notify.py).
    claude_session_id: str | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


WORKER_SYSTEM_PROMPT = """\
You are a Claude Code worker executing an assignment from the coordinator.

Rules:
- Do NOT run gh commands. The coordinator owns all GitHub interactions \
(issues, PRs, comments). Use regular git commands only.
- Stay within the files listed in your briefing. If you need to touch \
other files, do so only if strictly necessary and note it.
- If the briefing lists forbidden files, do NOT read or modify them. \
They are managed by the coordinator.
- You are already on a feature branch. Commit your work to this branch. \
Push with `git push origin HEAD`. \
NEVER commit or push to main or develop directly. \
Do NOT open a PR — the coordinator handles that.

Before writing any code, verify the feature or fix isn't already implemented. \
Grep for relevant function names, check existing modules, and read related files. \
If it already exists, report back instead of reimplementing.

Progress reporting:
- After each significant step (first build, test run, approach change), \
output a status line in exactly this format:
  STATUS: [what you just did] → [what you're about to do] → [confidence: high/medium/low]
- If you've tried 2 approaches and neither worked, STOP and output:
  STUCK: [what you tried] [why it failed] [what you think the blocker is]
  Then wait for guidance rather than trying a third approach.

Before declaring done:
- Run the project's build command (detect it from the repo: \
`cargo build` for Cargo.toml, `pytest` for pyproject.toml with pytest, \
`make` for Makefile, `npm run build` for package.json, etc.).
- If the build emits warnings — unused vars, dead code, deprecated APIs, \
ambiguous lifetimes, missing docs on public items — FIX THEM. \
Compiler warnings are part of the diff you're shipping; the human \
shouldn't have to clean up after you. Treat warnings as failures for \
the purposes of "done".
- If a warning genuinely can't be fixed in scope (third-party crate, \
intentional `#[allow]` with reason, a deferred refactor flagged \
elsewhere), explicitly call it out in your final message with the \
reason. Don't silently ship warnings.
- Re-run the build after fixes to confirm clean output.
- Run the project's test command (`cargo test`, `pytest`, etc.) and \
confirm it passes before declaring done.

#252: before exiting, emit a SMOKE_TESTS block telling the human what to \
manually verify.  You changed the code; you know what's worth poking.

  SMOKE_TESTS:
  - [scenario] — [how to trigger] — [what to look for]
  - [scenario] — [how to trigger] — [what to look for]
  END_SMOKE_TESTS

Keep it to 2-5 items, one bullet per line.  Each bullet has three \
em-dash-separated parts: the scenario, the trigger, and the success \
signal.  Prefer scenarios that exercise the changed code paths, not \
generic app sanity.  Include any commands the human should re-run on \
their hardware (e.g. `cargo test --features gtk` when only that build \
exercises the changed delegation).

If the change is purely internal — no user-visible behaviour, no new \
codepaths the existing test suite already covered — emit exactly:

  SMOKE_TESTS: (none — change is internal)
  END_SMOKE_TESTS\
"""

WORKER_PLAN_PROMPT = """\
You are a Claude Code planning worker. Read the codebase and produce a \
structured implementation plan. Do NOT write code, create files, or modify \
anything — read and analyse only.

Output your plan using exactly these headings:

FILES_READ: <comma-separated list of every file you examined>
FILES_MODIFY: <comma-separated list of files that would need to change>
APPROACH: <concise description of the implementation approach (3-5 sentences)>
RISKS: <potential blockers, conflicts, or tricky areas>
ESTIMATE: <rough complexity: trivial | small | medium | large>

Then emit a SMOKE_TESTS block — what the human should manually verify after \
the work lands. You know the intent at planning time; you don't yet know \
which diff lines will exist, but you do know which user-visible behaviours \
this change is meant to affect. Author smoke tests against intent, not \
mechanism.

  SMOKE_TESTS:
  - [scenario] — [how to trigger] — [what to look for]
  - [scenario] — [how to trigger] — [what to look for]
  END_SMOKE_TESTS

Keep it to 2-5 items, one bullet per line. Each bullet has three \
em-dash-separated parts: the scenario, the trigger, and the success signal. \
Include any commands the human should re-run on their hardware (e.g. \
`cargo test --features gtk` when only that build exercises the change).

If the change is purely internal — no user-visible behaviour, automated \
tests already cover the affected paths — emit exactly:

  SMOKE_TESTS: (none — change is internal)
  END_SMOKE_TESTS

Rules:
- Do NOT run gh commands.
- Do NOT write, edit, or create any files.
- Do NOT commit or push anything.
- Use Read and Bash (read-only commands like grep, find, cat) only.
- After reading the issue body and relevant code, output the plan and stop.\
"""

REFINEMENT_SYSTEM_PROMPT = """\
You are a refinement assistant helping a developer scope a GitHub issue \
before any code is written. You are NOT a worker — you do not implement, \
edit, or create files. Your job is to clarify intent.

The first user message contains the issue body, recent comments, the repo's \
CLAUDE.md, and a top-level file-tree snapshot. Use the Read tool to inspect \
specific files when the conversation calls for it.

In each reply:
- Ask focused clarifying questions about scope, acceptance, and edge cases \
the issue doesn't yet pin down. One or two questions per turn — do not flood.
- When you propose files or modules the change would touch, name them \
explicitly so the developer can confirm or correct.
- Surface unknowns: behaviours that depend on context the issue doesn't \
mention, places where existing code could conflict, follow-up work the \
change might imply.
- Keep replies short. The developer is typing live; long monologues slow \
the loop.

Rules:
- Do NOT run gh, git, npm, cargo, or any tool that mutates the repository or \
the GitHub state. Use Read only.
- Do NOT write or edit files. Do NOT propose a diff.
- Do NOT decide the issue is ready on the developer's behalf. They mark it \
ready by closing the chat with Done.
- If asked to write code, decline politely and reframe as "what behaviour \
should that code produce?" — refinement is about intent, not implementation.\
"""

TEST_CHAT_SYSTEM_PROMPT = """\
You are a test-stage assistant helping a developer validate a code change \
before it moves to review. You are NOT a code-writing worker — you do not \
implement, commit, or push. Your job is to help the developer understand \
what to test and why.

The first user message contains the PR diff, the most recent build log, \
the worker's SMOKE_TESTS block, the repo's run command (if any), and the \
repo's CLAUDE.md. Use the Read tool to inspect specific files and the Bash \
tool to run read-only diagnostic commands (builds, tests, lint) when the \
conversation calls for it.

In each reply:
- Explain what the diff changes and which behaviours to verify.
- Surface which smoke-test bullets are highest-risk given the diff.
- Suggest specific manual steps or automated checks (commands, test filters).
- If a build or test command fails, help the developer diagnose the root cause.
- Keep replies focused. The developer is validating live; long walls of \
text slow the loop.

Rules:
- Do NOT run gh commands. The coordinator owns all GitHub interactions.
- Do NOT run git push, git commit, or any command that writes to the repo.
- Do NOT write or edit files.
- Do NOT call coord sub-commands.
- Do NOT decide the change is ready on the developer's behalf — they \
record Pass/Fail via the TUI (P=pass / F=fail).\
"""

NEW_ISSUE_CHAT_SYSTEM_PROMPT = """\
You are a new-issue assistant helping a developer draft a well-structured \
GitHub issue before it is filed. You are NOT a worker — you do not \
implement, edit, or create files. Your job is to help articulate what \
should be built or fixed.

The first user message contains:
- The repo's CLAUDE.md (project conventions and rules)
- Per-repo issue guidance (required sections, style rules)
- A list of recently open issues (for near-duplicate detection)

Your goal is to guide the developer through a focused conversation and \
produce a finished issue draft. When the draft is ready, present it in \
this exact format:

  TITLE: <active-voice title, ≤80 chars>
  ---
  <full issue body in Markdown>

In each reply:
- Ask ONE or TWO focused questions per turn — do not flood with a wall \
of questions.
- Flag if the described issue closely resembles an existing open issue.
- Keep replies short. The developer is typing live.

Rules:
- Do NOT call `gh issue create`, `gh pr`, or any mutating `gh` command. \
The developer's client handles submission — your job is to produce the draft.
- Do NOT write, edit, or commit any files.
- Do NOT implement the feature described in the issue.
- Use `Read` and read-only `Bash` commands (e.g. `grep`, `find`, `cat`) \
to look up relevant code context when the conversation calls for it.\
"""

# Deny list applied to new-issue-chat workers.  Allows read-only gh
# (e.g. `gh issue list`, `gh issue view`) while blocking all mutations.
NEW_ISSUE_CHAT_DENY_COMMANDS: list[str] = [
    "Bash(gh issue create *)",
    "Bash(gh issue delete *)",
    "Bash(gh issue edit *)",
    "Bash(gh pr create *)",
    "Bash(gh pr merge *)",
    "Bash(gh pr close *)",
    "Bash(gh pr edit *)",
    "Bash(gh repo *)",
    "Bash(git push *)",
    "Bash(git commit *)",
    "Bash(git reset --hard *)",
    "Bash(git branch -D *)",
    "Bash(git checkout -- .)",
    "Bash(git clean -f *)",
    "Bash(rm -rf *)",
]


WorkerCommandBuilder = Callable[[AssignmentSpec], list[str]]


def build_deny_prompt(deny_commands: list[str]) -> str:
    """Format a deny-list into a system prompt section.

    Returns an empty string when *deny_commands* is empty so callers can
    unconditionally append the result.
    """
    if not deny_commands:
        return ""

    # Strip the "Bash(...)" wrapper for readability in the prompt while
    # keeping the original pattern for reference.
    lines: list[str] = []
    for pattern in deny_commands:
        # Show the human-friendly command inside Bash(...)
        inner = pattern
        if inner.startswith("Bash(") and inner.endswith(")"):
            inner = inner[5:-1]
        lines.append(f"- {inner}")

    return (
        "\n\nFORBIDDEN COMMANDS — you must NEVER run these:\n"
        + "\n".join(lines)
        + "\n"
        + "If you need to do something that resembles a forbidden command, STOP and output:\n"
        + "  STUCK: need to run [command] but it's on the deny-list"
    )


def default_worker_command(spec: AssignmentSpec, *, binary: str = DEFAULT_WORKER_BINARY) -> list[str]:
    """Build the argv for invoking the worker on this assignment.

    Uses ``--output-format stream-json --verbose`` for structured one-event-
    per-line log output that :mod:`coord.worker_events` parses for real-time
    observability.  Also uses ``--input-format stream-json`` so the worker
    reads turn-by-turn user messages from stdin — the orchestrator writes
    the initial briefing as a JSON line in :meth:`AgentServer._spawn`, and
    can later inject additional messages via :meth:`AgentServer.inject_message`.

    For ``type="plan"`` specs the worker gets :data:`WORKER_PLAN_PROMPT` as
    its system prompt and only ``Read,Bash`` in ``--allowedTools`` — no
    Edit/Write tools so it cannot modify the repository.
    """
    if spec.type == "plan":
        system_prompt = spec.system_prompt if spec.system_prompt else WORKER_PLAN_PROMPT
        allowed_tools = "Read,Bash"
    elif spec.type == "refinement":
        # #264: refinement is a developer-driven chat for scoping an issue.
        # Read-only — no Edit/Write/Bash, since this session must not mutate
        # the repo or shell out to gh.  The developer drives the conversation
        # via inject_message; the worker just asks clarifying questions.
        system_prompt = spec.system_prompt if spec.system_prompt else REFINEMENT_SYSTEM_PROMPT
        allowed_tools = "Read"
    elif spec.type == "test-chat":
        # #314 Phase B: test-stage chat for validating a completed work
        # assignment.  Allows Read + Bash for read-only diagnostics (builds,
        # tests, lint) but blocks write-side commands via deny_commands.
        system_prompt = spec.system_prompt if spec.system_prompt else TEST_CHAT_SYSTEM_PROMPT
        system_prompt += build_deny_prompt(spec.deny_commands)
        allowed_tools = "Read,Bash"
    elif spec.type == "new-issue-chat":
        # #316: new-issue-chat helps the developer draft a new GitHub issue.
        # Read + Bash allowed (read-only lookups like grep/find/gh issue list);
        # a deny list blocks all mutations (gh issue create, git push, etc.)
        # so the coordinator's TUI handles the actual gh submission.
        system_prompt = spec.system_prompt if spec.system_prompt else NEW_ISSUE_CHAT_SYSTEM_PROMPT
        system_prompt += build_deny_prompt(NEW_ISSUE_CHAT_DENY_COMMANDS)
        # #352: append per-repo new-issue guidance when provided.
        if spec.new_issue_guidance:
            system_prompt += (
                "\n\nThe user's repo has the following guidance for new-issue drafts. "
                "Follow it: ask focused questions matched to the required sections, "
                "then produce a finalised issue body using the same structure. "
                "Do not invent sections that aren't there; do not omit required sections "
                "(mark them `(TBD)` if the conversation hasn't covered them yet).\n\n"
                + spec.new_issue_guidance
            )
        allowed_tools = "Read,Bash"
    else:
        system_prompt = spec.system_prompt if spec.system_prompt else WORKER_SYSTEM_PROMPT
        system_prompt += build_deny_prompt(spec.deny_commands)
        allowed_tools = "Read,Edit,Write,Bash"

    # NOTE: briefing is NOT passed as a positional arg — it is written to
    # stdin as the first stream-json user message by ``_spawn``.
    argv = [
        binary, "-p",
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--verbose",
        "--system-prompt", system_prompt,
        "--allowedTools", allowed_tools,
        "--permission-mode", "acceptEdits",
    ]
    if spec.model:
        argv.extend(["--model", spec.model])
    # #315: when resuming a prior chat session, load the prior conversation so
    # the model has full context.  The briefing field IS the new user message;
    # claude sees it as the next user turn after the restored history.
    if spec.resume_session_id:
        argv.extend(["--resume", spec.resume_session_id])
    return argv


def _user_message_line(text: str) -> bytes:
    """Encode a user message as a single stream-json line (with newline)."""
    payload = {"type": "user", "message": {"role": "user", "content": text}}
    return (json.dumps(payload) + "\n").encode("utf-8")


class AgentServer:
    """Owns assignment state and subprocesses. Thread-safe."""

    def __init__(
        self,
        *,
        machine_name: str,
        capabilities: Iterable[str] = (),
        repos: Iterable[str] = (),
        state_dir: Path = DEFAULT_STATE_DIR,
        worker_command: WorkerCommandBuilder | None = None,
        repo_paths: dict[str, str] | None = None,
        bash_wrap_spawn: bool = True,
        first_output_timeout: float = _FIRST_OUTPUT_TIMEOUT,
        # #305: per-repo artifact glob patterns; repo_name → list of globs.
        # Populated from coordinator.yml Repo.artifact_paths at startup.
        artifact_paths: dict[str, list[str]] | None = None,
    ) -> None:
        self.machine_name = machine_name
        self.capabilities = list(capabilities)
        self.repos = list(repos)
        self.repo_paths = dict(repo_paths or {})
        self.artifact_paths: dict[str, list[str]] = dict(artifact_paths or {})
        self.state_dir = Path(state_dir)
        self.log_dir = self.state_dir / "logs"
        self.state_path = self.state_dir / "agent_state.json"
        self.worker_command = worker_command or default_worker_command
        # Daemon-spawn stall mitigations (#299). bash_wrap_spawn routes the
        # spawn through a transient `bash -c 'exec ...'` parent; the TTFT
        # watchdog kills workers that emit zero output within the timeout.
        self.bash_wrap_spawn = bash_wrap_spawn
        self.first_output_timeout = first_output_timeout

        self._lock = threading.Lock()
        self._assignments: dict[str, AgentAssignment] = {}
        self._processes: dict[str, subprocess.Popen] = {}
        self._threads: dict[str, threading.Thread] = {}

        # Cache for /health worktree_bytes — recomputing it walks every
        # file under ~/.coord/worktrees on every /health call, which is
        # tens or hundreds of thousands of stat syscalls when worktrees
        # contain node_modules / target / etc.  Cache for a few seconds
        # so polling clients don't pin the agent in an rglob.
        self._worktree_bytes_cache: tuple[float, int] | None = None  # (computed_at, bytes)
        self._worktree_bytes_ttl: float = 30.0  # seconds
        # #305: cache for /health artifact_bytes — artifact dirs are smaller
        # than worktrees but still warrant a short TTL to avoid hammering
        # the filesystem on every health poll.
        self._artifact_bytes_cache: tuple[float, int] | None = None  # (computed_at, bytes)
        self._artifact_bytes_ttl: float = 30.0  # seconds

        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._load_state()

    # ── Public API ──────────────────────────────────────────────────────────

    def health(self) -> dict:
        with self._lock:
            active = sum(1 for a in self._assignments.values() if a.status == RUNNING)
            completed = sum(
                1
                for a in self._assignments.values()
                if a.status in (DONE, FAILED, CANCELLED)
            )
        worktree_bytes = self._cached_worktree_bytes()
        artifact_bytes = self._cached_artifact_bytes()
        return {
            "machine": self.machine_name,
            "capabilities": self.capabilities,
            "repos": self.repos,
            "active": active,
            "completed": completed,
            # Monotonic-ish stamp of when THIS Python process started.
            # exec_restart replaces the image so this changes across an
            # /update — letting the CLI distinguish "old agent still
            # responding" from "new agent has come back online".
            "agent_started_at": _PROCESS_STARTED_AT,
            # Total disk usage of all git worktrees managed by this agent.
            "worktree_bytes": worktree_bytes,
            # #305: total disk usage of all stashed artifact directories.
            "artifact_bytes": artifact_bytes,
        }

    def _cached_worktree_bytes(self) -> int:
        """Return total worktree disk usage with a short TTL cache.

        Recomputing on every /health call is too expensive — a real worktree
        with ``node_modules`` / ``target`` / build outputs can need hundreds
        of thousands of stat syscalls per call, and the TUI polls /health
        with a 2 s timeout (see ``tui/src/app.rs`` health refresh).  A short
        TTL keeps the number trustworthy without pinning the agent in an
        rglob.
        """
        worktree_base = self.state_dir / "worktrees"
        now = time.time()
        cached = self._worktree_bytes_cache
        if cached is not None and (now - cached[0]) < self._worktree_bytes_ttl:
            return cached[1]
        size = _dir_size(worktree_base)
        # Single-writer assignment is atomic in CPython; no lock needed.
        self._worktree_bytes_cache = (now, size)
        return size

    def _cached_artifact_bytes(self) -> int:
        """Return total artifact disk usage with a short TTL cache.

        Mirrors :meth:`_cached_worktree_bytes` — keeps /health polling
        cheap even when artifacts accumulate many small files.
        """
        artifacts_base = self.state_dir / "artifacts"
        now = time.time()
        cached = self._artifact_bytes_cache
        if cached is not None and (now - cached[0]) < self._artifact_bytes_ttl:
            return cached[1]
        size = _dir_size(artifacts_base)
        self._artifact_bytes_cache = (now, size)
        return size

    def _stash_artifacts(self, assignment: AgentAssignment) -> None:
        """Copy build artifacts from a worktree into the persistent stash.

        Called immediately before worktree removal so the compiled outputs
        survive the cleanup.  Only acts on DONE assignments (successful
        workers) with a recorded branch and at least one configured glob
        pattern for the repo.

        The stash is at ``~/.coord/artifacts/<repo>/<sanitized_branch>/``.
        A ``.assignment_id`` marker is written so the manifest endpoint can
        report which assignment produced the stash.  Existing stash contents
        for the same (repo, branch) pair are overwritten — latest-wins.
        """
        if assignment.status != DONE:
            return
        if not assignment.worktree_path:
            return
        repo_name = assignment.spec.repo_name
        patterns = assignment.spec.artifact_paths or self.artifact_paths.get(repo_name, [])
        if not patterns:
            return
        branch = assignment.branch or assignment.spec.branch
        if not branch:
            return

        sanitized = _sanitize_branch(branch)
        stash_dir = self.state_dir / "artifacts" / repo_name / sanitized
        stash_dir.mkdir(parents=True, exist_ok=True)

        wt_path = Path(assignment.worktree_path)
        if not wt_path.exists():
            return

        copied = 0
        for pattern in patterns:
            # Reject patterns containing ".." — Path.glob("../foo") succeeds
            # in Python 3.12+ and can reach outside the worktree.  The
            # ValueError guard below does NOT catch this.  artifact_paths comes
            # from trusted config, but an explicit check is cheap insurance.
            if ".." in Path(pattern).parts:
                continue
            try:
                matches = list(wt_path.glob(pattern))
            except (ValueError, OSError):
                continue
            for src in matches:
                if not src.is_file():
                    continue
                # Skip by suffix (.d = compiler dependency files)
                if src.suffix == ".d":
                    continue
                try:
                    st = src.stat()
                except OSError:
                    continue
                # Skip tiny files (< 100 bytes — not a real binary)
                if st.st_size < 100:
                    continue
                dst = stash_dir / src.name
                try:
                    shutil.copy2(src, dst)
                    copied += 1
                except (OSError, shutil.Error):
                    pass

        # Touch the stash directory so its mtime reflects this stash run.
        # mkdir(exist_ok=True) is a no-op when the directory already exists,
        # meaning a re-stash (e.g. after a review cycle on the same branch)
        # would leave the original Day-1 mtime in place — causing _gc_artifacts
        # to evict the refreshed stash prematurely.
        try:
            stash_dir.touch()
        except OSError:
            pass

        # Write the assignment_id marker so the manifest endpoint can surface
        # which build produced this stash without iterating all assignments.
        try:
            (stash_dir / ".assignment_id").write_text(assignment.id)
        except OSError:
            pass

        # Invalidate the artifact_bytes cache so health() picks up the new files.
        self._artifact_bytes_cache = None

        if assignment.log_path:
            _append_log_line(
                assignment.log_path,
                f"# stash: {copied} artifact(s) → {stash_dir}\n",
            )

    def _gc_artifacts(self, ttl_days: float = 3.0) -> int:
        """Remove artifact stash directories older than *ttl_days* days.

        Uses the stash directory's ``mtime`` as the age proxy — each
        successful ``_stash_artifacts`` call touches the stash directory
        explicitly after copying, so the TTL is effectively a "last-written"
        window even when re-stashing an existing branch.

        Returns the count of directories removed.
        """
        artifacts_base = self.state_dir / "artifacts"
        if not artifacts_base.exists():
            return 0

        cutoff = time.time() - ttl_days * 86400
        removed = 0

        try:
            repo_dirs = list(artifacts_base.iterdir())
        except OSError:
            return 0

        for repo_dir in repo_dirs:
            if not repo_dir.is_dir():
                continue
            try:
                branch_dirs = list(repo_dir.iterdir())
            except OSError:
                continue
            for branch_dir in branch_dirs:
                if not branch_dir.is_dir():
                    continue
                try:
                    mtime = branch_dir.stat().st_mtime
                except OSError:
                    continue
                if mtime < cutoff:
                    try:
                        shutil.rmtree(branch_dir, ignore_errors=True)
                        removed += 1
                    except OSError:
                        pass

        if removed:
            # Invalidate the artifact_bytes cache after GC.
            self._artifact_bytes_cache = None

        return removed

    # Accepted character set for HTTP path parameters forwarded to the
    # filesystem.  Must not contain ``..``, ``/``, or any shell-special
    # characters.  Both repo names and sanitized branch names satisfy this
    # pattern in practice.
    _SAFE_PATH_COMPONENT = re.compile(r"^[a-zA-Z0-9._-]+$")

    def artifact_manifest(self, repo: str, branch: str) -> dict | None:
        """Return the artifact manifest for a stash, or ``None`` if missing.

        *branch* must already be sanitized (i.e. the path component form,
        no slashes).  Returns a dict with keys ``files``, ``total_bytes``,
        and ``built_by_assignment_id``, or ``None`` when no stash exists.

        Returns ``None`` (→ 404) when *repo* or *branch* contain path-traversal
        sequences (``..``, ``/``, or characters outside ``[a-zA-Z0-9._-]``).
        The agent server is Tailscale-only, not internet-facing, but rejecting
        malformed params is cheap and prevents any node from probing the
        artifacts directory structure.
        """
        if (
            not self._SAFE_PATH_COMPONENT.match(repo)
            or not self._SAFE_PATH_COMPONENT.match(branch)
        ):
            return None
        stash_dir = self.state_dir / "artifacts" / repo / branch
        if not stash_dir.exists():
            return None

        files = []
        for f in sorted(stash_dir.iterdir()):
            if not f.is_file() or f.name.startswith("."):
                continue
            try:
                st = f.stat()
                files.append({"name": f.name, "size": st.st_size, "mtime": st.st_mtime})
            except OSError:
                pass

        aid_path = stash_dir / ".assignment_id"
        built_by: str | None = None
        try:
            built_by = aid_path.read_text().strip()
        except OSError:
            pass

        total_bytes = sum(item["size"] for item in files)
        return {"files": files, "total_bytes": total_bytes, "built_by_assignment_id": built_by}

    def clean_worktrees(self, *, recent_secs: float = 300.0) -> dict:
        """Remove git worktrees for assignments in terminal states.

        Idempotent — safe to call multiple times.  Skips worktrees for:
        - Running or pending assignments (still in use by a worker).
        - Assignments whose ``finished_at`` timestamp is within
          *recent_secs* seconds of now (default 5 min) — protects against
          racing with a worker that just finished/was cancelled.
        - Directories whose ``mtime`` is within *recent_secs* of now —
          this catches the window between ``_setup_worktree`` creating
          the directory and ``assign()`` registering the assignment in
          ``self._assignments``.  Without it, a ``clean_worktrees`` call
          that snapshots ``_assignments`` mid-spawn would treat the
          freshly-created tree as orphaned and ``git worktree remove`` it
          out from under the worker.

        Returns ``{"cleaned": N, "kept": M, "bytes_freed": B}``.
        """
        worktree_base = self.state_dir / "worktrees"
        if not worktree_base.exists():
            return {"cleaned": 0, "kept": 0, "bytes_freed": 0}

        now = time.time()

        with self._lock:
            assignments = dict(self._assignments)

        cleaned = 0
        kept = 0
        bytes_freed = 0

        for entry in worktree_base.iterdir():
            if not entry.is_dir():
                continue
            assignment_id = entry.name
            a = assignments.get(assignment_id)

            # Never touch worktrees for running/pending assignments.
            if a is not None and a.status in (RUNNING, PENDING):
                kept += 1
                continue

            # Skip recently-finished assignments — the worker process may
            # still be tearing down and have open file handles in the tree.
            if a is not None and a.finished_at is not None:
                age = now - a.finished_at
                if age < recent_secs:
                    kept += 1
                    continue

            # Skip directories that were created very recently even when
            # we don't (yet) have an assignment record.  This closes the
            # race window between `_setup_worktree` (which makes the dir)
            # and the `with self._lock: self._assignments[id] = …` insert
            # in `assign()` — if `clean_worktrees` snapshots _assignments
            # in that window, the worktree looks orphaned but the worker
            # is still inside `git worktree add`.
            if a is None:
                try:
                    mtime = entry.stat().st_mtime
                except OSError:
                    mtime = None
                if mtime is not None and (now - mtime) < recent_secs:
                    kept += 1
                    continue

            # Compute size before removal so the caller knows bytes freed.
            dir_size = _dir_size(entry)

            # #305: stash any configured artifacts before removing the
            # worktree.  Idempotent — if the reap thread already stashed
            # these files, _stash_artifacts is a no-op (the worktree won't
            # exist or the stash dir is simply overwritten).
            if a is not None:
                self._stash_artifacts(a)

            # Try a proper git worktree remove first (updates the main
            # repo's worktree bookkeeping).  Fall back to brute-force rmtree
            # if git isn't available or the main repo has moved.
            removed = False
            if a is not None:
                repo_path_str = self.repo_paths.get(a.spec.repo_name)
                if repo_path_str:
                    repo_path = Path(repo_path_str)
                    try:
                        _git(repo_path, "worktree", "remove", str(entry), "--force")
                        removed = True
                    except (_GitError, OSError):
                        pass

            if not removed:
                try:
                    shutil.rmtree(entry, ignore_errors=True)
                    removed = True
                except OSError:
                    pass

            if removed:
                bytes_freed += dir_size
                cleaned += 1
            else:
                kept += 1

        # #305: GC old artifact stashes in the same pass so callers don't
        # need a separate endpoint.  Default TTL is 3 days.
        self._gc_artifacts()

        return {"cleaned": cleaned, "kept": kept, "bytes_freed": bytes_freed}

    def list_assignments(self) -> dict:
        from coord.worker_events import is_stream_json, parse_log

        with self._lock:
            assignments = list(self._assignments.values())
        active = []
        completed = []
        for a in assignments:
            d = a.to_dict()
            if a.status == RUNNING:
                try:
                    prog = self.progress(a.id)
                except Exception:
                    prog = None
                if prog:
                    d["progress"] = prog
                # Tail-read stream-json log for live summary fields.
                if a.log_path and is_stream_json(a.log_path):
                    try:
                        summary = parse_log(a.log_path)
                    except Exception:
                        summary = None
                    if summary is not None:
                        d["model_used"] = summary.model_used
                        d["turns"] = summary.num_turns
                        d["cost_so_far"] = summary.total_cost_usd
                        d["last_tool"] = summary.last_tool
                        d["rate_limited"] = summary.rate_limited
                active.append(d)
            else:
                # For terminal assignments, parse the whole log (tail_bytes=0)
                # so we can report final totals reliably.
                if a.log_path and is_stream_json(a.log_path):
                    try:
                        summary = parse_log(a.log_path, tail_bytes=0)
                    except Exception:
                        summary = None
                    if summary is not None:
                        d["model_used"] = summary.model_used
                        d["total_cost_usd"] = summary.total_cost_usd
                        d["num_turns"] = summary.num_turns
                        d["stop_reason"] = summary.stop_reason
                completed.append(d)
        return {"active": active, "completed": completed}

    def list_repos(self) -> dict[str, dict]:
        """Return local HEAD / branch / dirty flag for each configured repo.

        Per-repo errors (missing path, not a git repo, etc.) come back as an
        `error` field rather than failing the whole call — the coordinator
        wants a complete picture across machines even when one is broken.
        """
        result: dict[str, dict] = {}
        for repo_name in self.repos:
            path_str = self.repo_paths.get(repo_name)
            if not path_str:
                result[repo_name] = {"error": "no repo_path configured for this machine"}
                continue
            path = Path(path_str).expanduser()
            if not path.exists():
                result[repo_name] = {"error": f"path does not exist: {path}"}
                continue
            try:
                sha = _git(path, "rev-parse", "HEAD")
                branch = _git(path, "rev-parse", "--abbrev-ref", "HEAD")
                porcelain = _git(path, "status", "--porcelain")
            except _GitError as e:
                result[repo_name] = {"error": str(e), "path": str(path)}
                continue
            result[repo_name] = {
                "sha": sha,
                "branch": branch,
                "dirty": bool(porcelain.strip()),
                "path": str(path),
            }
        return result

    def assign(self, spec: AssignmentSpec) -> AgentAssignment:
        """Accept an assignment and spawn the worker. Returns immediately."""
        if self.repos and spec.repo_name not in self.repos:
            raise ValueError(
                f"this agent does not handle repo {spec.repo_name!r} "
                f"(supported: {self.repos})"
            )

        repo_path = Path(spec.repo_path).expanduser()
        if not repo_path.exists():
            raise ValueError(f"repo path does not exist: {repo_path}")

        if spec.pull_repos:
            unknown = [r for r in spec.pull_repos if r not in self.repo_paths]
            if unknown:
                raise ValueError(
                    f"pull_repos references repos with no repo_path on this agent: {unknown}"
                )

        assignment = AgentAssignment(
            id=uuid.uuid4().hex[:12],
            spec=spec,
            status=PENDING,
        )
        assignment.log_path = str(self.log_dir / f"{assignment.id}.log")

        if spec.type in ("plan", "refinement", "test-chat", "new-issue-chat"):
            # Read-only run (plan, refinement, test-chat, or new-issue-chat) —
            # skip worktree creation, run directly in the main repo checkout.
            # No branch is created or modified. For chat sessions (#315 / #314
            # / #316), the stable cwd is also required so claude-cli's
            # `--resume <session_id>` finds the prior session file on
            # subsequent turns: claude scopes sessions by cwd (mangled into
            # ~/.claude/projects/<cwd-key>/), and a per-assignment worktree
            # gives every turn a different cwd.
            with self._lock:
                self._assignments[assignment.id] = assignment
            self._persist()

            if spec.pull_repos:
                thread = threading.Thread(
                    target=self._pull_then_spawn,
                    args=(assignment, repo_path),
                    daemon=True,
                    name=f"agent-pull-{assignment.id}",
                )
                thread.start()
            else:
                self._spawn(assignment, repo_path)
            return assignment

        # Create worktree for isolation
        try:
            worktree_path = self._setup_worktree(assignment, repo_path)
        except (_GitError, OSError) as e:
            assignment.status = FAILED
            assignment.error = f"worktree setup failed: {e}"
            assignment.finished_at = time.time()
            with self._lock:
                self._assignments[assignment.id] = assignment
            self._persist()
            return assignment  # Don't raise — let coordinator see the failure

        assignment.worktree_path = str(worktree_path)

        with self._lock:
            self._assignments[assignment.id] = assignment
        self._persist()

        if spec.pull_repos:
            thread = threading.Thread(
                target=self._pull_then_spawn,
                args=(assignment, worktree_path),
                daemon=True,
                name=f"agent-pull-{assignment.id}",
            )
            thread.start()
        else:
            self._spawn(assignment, worktree_path)
        return assignment

    def cancel(self, assignment_id: str) -> AgentAssignment:
        """Terminate a running assignment. Idempotent for already-finished work."""
        with self._lock:
            assignment = self._assignments.get(assignment_id)
            if assignment is None:
                raise KeyError(assignment_id)
            proc = self._processes.get(assignment_id)

        if assignment.status not in (PENDING, RUNNING):
            return assignment

        if proc is not None and proc.poll() is None:
            # Kill the whole process group (proc was spawned with
            # start_new_session=True so proc.pid is the pgid). proc.terminate()
            # alone leaves MCP subprocess children alive and the cancel hangs.
            _killpg_safe(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _killpg_safe(proc.pid, signal.SIGKILL)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass

        with self._lock:
            assignment.status = CANCELLED
            assignment.finished_at = time.time()
        self._persist()

        # Clean up worktree after cancellation
        self._cleanup_worktree(assignment)

        return assignment

    def inject_message(self, assignment_id: str, text: str) -> None:
        """Inject a new user message into a running worker via its stdin.

        Raises :class:`KeyError` when the assignment doesn't exist on this
        agent, :class:`RuntimeError` when the worker isn't running, and
        :class:`BrokenPipeError` when the worker closed its stdin (e.g.
        already finished or crashed).

        The worker picks up the message at its next turn boundary — between
        tool calls, not mid-tool.  Each injection appends a `# inject:`
        marker to the assignment log for traceability.
        """
        with self._lock:
            assignment = self._assignments.get(assignment_id)
            if assignment is None:
                raise KeyError(assignment_id)
            if assignment.status != RUNNING:
                raise RuntimeError(
                    f"assignment {assignment_id} is {assignment.status!r}, not running"
                )
            proc = self._processes.get(assignment_id)
        if proc is None or proc.stdin is None or proc.poll() is not None:
            raise BrokenPipeError(
                f"worker for {assignment_id} has no open stdin (process exited?)"
            )
        try:
            proc.stdin.write(_user_message_line(text))
            proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            raise BrokenPipeError(str(e)) from e
        # Trace the injection in the log so users can correlate later.
        if assignment.log_path:
            try:
                with open(assignment.log_path, "a") as fh:
                    fh.write(f"# inject: {text}\n")
            except OSError:
                pass

    def get(self, assignment_id: str) -> AgentAssignment | None:
        with self._lock:
            return self._assignments.get(assignment_id)

    def progress(self, assignment_id: str) -> dict | None:
        """Parse progress signals from the worker's log file."""
        from coord.progress import parse_progress

        a = self.get(assignment_id)
        if a is None or a.log_path is None:
            return None
        return parse_progress(a.log_path).to_dict()

    def wait_for(self, assignment_id: str, timeout: float = 10.0) -> AgentAssignment:
        """Block until an assignment leaves RUNNING. Test helper."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                a = self._assignments.get(assignment_id)
            if a is None:
                raise KeyError(assignment_id)
            if a.status != RUNNING and a.status != PENDING:
                return a
            time.sleep(0.05)
        raise TimeoutError(f"assignment {assignment_id} still {a.status} after {timeout}s")

    # ── Internals ──────────────────────────────────────────────────────────

    def _setup_worktree(self, assignment: AgentAssignment, repo_path: Path) -> Path:
        """Create a git worktree for this assignment. Returns the worktree path."""
        worktree_base = self.state_dir / "worktrees"
        worktree_path = worktree_base / assignment.id

        # Clean up stale worktree if it exists
        if worktree_path.exists():
            try:
                _git(repo_path, "worktree", "remove", str(worktree_path), "--force")
            except (_GitError, FileNotFoundError, OSError):
                shutil.rmtree(worktree_path, ignore_errors=True)

        worktree_path.parent.mkdir(parents=True, exist_ok=True)

        # Prune administrative entries for worktrees whose directories were
        # removed out-of-band (e.g. a crash before clean_worktrees ran) so a
        # stale entry can't block `worktree add` below (#389 hygiene).
        try:
            _git(repo_path, "worktree", "prune")
        except _GitError:
            pass

        # Determine if `origin` is configured.  In production it always is;
        # only test fixtures + local-only repos lack a remote.  When origin
        # is present we MUST branch from a concrete `origin/<default>` SHA
        # to prevent unpushed local commits on `<default>` from riding into
        # the worker's branch (issue #255).
        try:
            _git(repo_path, "remote", "get-url", "origin")
            has_origin = True
        except _GitError:
            has_origin = False

        # Fetch latest only when we have a remote — keeps the offline /
        # test path silent.
        if has_origin:
            try:
                _git(repo_path, "fetch", "origin")
            except _GitError:
                pass  # transient — falls through to the rev-parse check below

        default_branch = assignment.spec.branch or "main"
        if has_origin:
            # #255: resolve to a concrete SHA from origin so unpushed local
            # commits on `<default>` can't sneak into the worker's branch.
            # If fetch failed AND origin/<default> isn't already known
            # locally, this raises — surfacing a real "couldn't reach
            # origin" condition rather than papering over it.
            try:
                start_point = _git(
                    repo_path, "rev-parse", f"origin/{default_branch}",
                ).strip()
            except _GitError as exc:
                raise _GitError(
                    f"_setup_worktree: cannot resolve origin/{default_branch} "
                    f"in {repo_path}. The remote is configured but the ref "
                    f"is missing — check network connectivity and that the "
                    f"repo's default_branch in coordinator.yml matches the "
                    f"actual branch on origin. ({exc})"
                ) from exc
        else:
            # No remote — fall back to the local branch (test fixtures, etc.)
            start_point = default_branch

        # #255: warn (in the assignment log) if local `<default>` has commits
        # that aren't on origin.  Those commits are NOT in the worker's
        # branch — that's the whole point of #255 — but the user should know
        # they have unpushed WIP sitting on this machine so they don't lose it.
        if has_origin and assignment.log_path:
            try:
                ahead = _git(
                    repo_path, "rev-list", "--count",
                    f"origin/{default_branch}..{default_branch}",
                ).strip()
                if ahead and ahead != "0":
                    msg = (
                        f"# warning: {default_branch} on this machine has {ahead} "
                        f"commit(s) ahead of origin/{default_branch}.  Those "
                        f"commits are NOT in the worker's branch (#255).  "
                        f"Push them when convenient so they aren't lost.\n"
                    )
                    try:
                        with open(assignment.log_path, "a") as fh:
                            fh.write(msg)
                    except OSError:
                        pass
            except _GitError:
                # Local `<default>` may not exist (fresh clone) — silent.
                pass

        # Branch name for this assignment.  When `target_branch` is set
        # (auto-loop fix dispatch path), use it verbatim — the caller
        # knows the exact branch they want the worker to check out, and
        # we must NOT derive a new name from the (possibly `[fix-N]`-
        # prefixed) issue title or the fix would land on an orphan
        # branch instead of the original PR's branch.
        if assignment.spec.target_branch:
            branch_name = assignment.spec.target_branch
        else:
            branch_name = (
                f"issue-{assignment.spec.issue_number}-"
                f"{_slugify(assignment.spec.issue_title)}"
            )

        # Decide the base for the worker's branch.  Trusted sources, in order
        # (#389 — a leftover LOCAL branch from a prior failed assignment on
        # this machine must never be reused: branching a new worker off it
        # silently reverts merged work, as happened to #357/#319 when
        # precision was parked on a stale `issue-194` branch):
        #   1. origin/<branch> — a real remote branch (retry/continuation).
        #      Check it out and hard-reset to the remote tip so a divergent
        #      local copy of the branch can't ride in.
        #   2. local <branch>, but ONLY when this repo has no remote (test
        #      fixtures / local-only repos) — nothing more authoritative exists.
        #   3. otherwise branch fresh from `start_point` (origin/<default>),
        #      deleting any untrusted local leftover with the same name first.
        origin_has_branch = False
        local_has_branch = False
        if not assignment.spec.fresh_branch:
            if has_origin:
                try:
                    _git(
                        repo_path, "rev-parse", "--verify",
                        f"refs/remotes/origin/{branch_name}",
                    )
                    origin_has_branch = True
                except _GitError:
                    pass
            try:
                _git(
                    repo_path, "rev-parse", "--verify",
                    f"refs/heads/{branch_name}",
                )
                local_has_branch = True
            except _GitError:
                pass

        if origin_has_branch:
            # Continuation/retry — force the worktree's branch to the remote
            # tip (#389), discarding any divergent local copy of the branch.
            _git(
                repo_path, "worktree", "add", "-B", branch_name,
                str(worktree_path), f"origin/{branch_name}",
            )
        elif local_has_branch and not has_origin:
            # Local-only repo (no remote) — reuse the local branch as before.
            _git(repo_path, "worktree", "add", str(worktree_path), branch_name)
        else:
            # Fresh branch, OR an untrusted local-only leftover in a repo that
            # has a remote (#389).  Delete any colliding local branch so `-b`
            # won't fail and so the worker starts from origin/<default>.
            if local_has_branch and assignment.log_path:
                try:
                    with open(assignment.log_path, "a") as fh:
                        fh.write(
                            f"# warning: discarding leftover local branch "
                            f"{branch_name!r} (not on origin) and branching "
                            f"fresh from {start_point[:12]} (#389)\n"
                        )
                except OSError:
                    pass
            try:
                _git(repo_path, "branch", "-D", branch_name)
            except _GitError:
                pass
            _git(
                repo_path, "worktree", "add", "-b", branch_name,
                str(worktree_path), start_point,
            )

        return worktree_path

    def _cleanup_worktree(self, assignment: AgentAssignment) -> None:
        """Remove the worktree for a finished assignment. Best-effort."""
        if not assignment.worktree_path:
            return
        wt_path = Path(assignment.worktree_path)
        repo_path = Path(assignment.spec.repo_path).expanduser()
        try:
            if wt_path.exists():
                _git(repo_path, "worktree", "remove", str(wt_path), "--force")
        except _GitError:
            try:
                shutil.rmtree(wt_path, ignore_errors=True)
                _git(repo_path, "worktree", "prune")
            except _GitError:
                pass

    def _pull_then_spawn(self, assignment: AgentAssignment, repo_path: Path) -> None:
        """Pull each dep before spawning the worker. Logs to the assignment log.

        On any failure: mark the assignment FAILED and skip spawn. The HTTP
        client polls status to discover this.
        """
        with open(assignment.log_path, "w") as log_fh:
            log_fh.write(
                f"# pulling dependencies: {assignment.spec.pull_repos}\n"
            )
            for dep_name in assignment.spec.pull_repos:
                dep_path_str = self.repo_paths.get(dep_name)
                if not dep_path_str:
                    msg = f"no repo_path configured for dependency {dep_name!r}"
                    log_fh.write(f"# pull failed: {msg}\n")
                    self._fail(assignment, msg)
                    return
                dep_path = Path(dep_path_str).expanduser()
                log_fh.write(f"# git -C {dep_path} pull --ff-only\n")
                log_fh.flush()
                try:
                    output = _git(dep_path, "pull", "--ff-only")
                except _GitError as e:
                    log_fh.write(f"# pull failed for {dep_name}: {e}\n")
                    self._fail(assignment, f"pull failed for {dep_name}: {e}")
                    return
                log_fh.write(output + "\n")
            log_fh.write("# all pulls succeeded; starting worker\n")
        self._spawn(assignment, repo_path)

    def _fail(self, assignment: AgentAssignment, error: str) -> None:
        with self._lock:
            assignment.status = FAILED
            assignment.error = error
            assignment.finished_at = time.time()
        self._persist()

    def _spawn(self, assignment: AgentAssignment, repo_path: Path) -> None:
        argv = self.worker_command(assignment.spec)
        log_fh = open(assignment.log_path, "a")  # noqa: SIM115 — handle closed in _reap

        argv_oneline = shlex.join(argv).replace("\n", "\\n")
        header = (
            f"# agent={self.machine_name} repo={assignment.spec.repo_name} "
            f"issue=#{assignment.spec.issue_number} "
            f"argv={argv_oneline}\n"
        )
        log_fh.write(header)
        log_fh.flush()

        # Optionally route the spawn through a transient `bash -c 'exec ...'`
        # parent (#299). `exec` keeps the PID, so start_new_session, proc.pid,
        # the stdin pipe, and process-group kills all behave as for a bare
        # spawn — only the immediate parent of claude changes.
        spawn_argv = _maybe_bash_wrap(argv, self.bash_wrap_spawn)

        try:
            proc = subprocess.Popen(
                spawn_argv,
                cwd=str(repo_path),
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                stdin=subprocess.PIPE,
                start_new_session=True,
                # #402: strip the agent's own venv from the worker's PATH so a
                # worker's `pip install -e .` can't clobber the agent's runtime
                # venv from a soon-to-be-reaped worktree.
                env=_worker_subprocess_env(),
            )
        except (FileNotFoundError, OSError) as e:
            log_fh.write(f"\n# spawn failed: {e}\n")
            log_fh.close()
            with self._lock:
                assignment.status = FAILED
                assignment.error = str(e)
                assignment.finished_at = time.time()
            self._persist()
            return

        # Send the initial briefing as the first stream-json user message.
        # If this fails (worker exited immediately), let `_reap` capture the
        # exit code — we just stop trying to write.
        #
        # #315: this line does double duty — for a regular dispatch it IS the
        # initial briefing; for a --resume re-dispatch (`spec.resume_session_id`
        # set) it is the next user turn written into the restored conversation.
        # Either way the worker sees it as a stream-json user message.
        try:
            assert proc.stdin is not None
            proc.stdin.write(_user_message_line(assignment.spec.briefing))
            proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            log_fh.write(f"\n# failed to send initial briefing: {e}\n")

        with self._lock:
            assignment.status = RUNNING
            assignment.pid = proc.pid
            assignment.started_at = time.time()
            self._processes[assignment.id] = proc

        thread = threading.Thread(
            target=self._reap,
            args=(assignment.id, proc, log_fh, assignment.log_path),
            daemon=True,
            name=f"agent-reap-{assignment.id}",
        )
        with self._lock:
            self._threads[assignment.id] = thread
        thread.start()
        self._persist()

    def _reap(
        self,
        assignment_id: str,
        proc: subprocess.Popen,
        log_fh,
        log_path: str,
    ) -> None:
        # Use a polling wait that handles claude-cli's well-known habit of
        # not exiting after emitting its final result event (a child of the
        # process group keeps the session alive). See #228.
        exit_code = _wait_for_proc_or_result(
            proc, log_path, first_output_timeout=self.first_output_timeout
        )
        log_fh.close()

        # Capture the branch the worker left the repo on. For worktree-based
        # assignments we read from the worktree; for legacy assignments (no
        # worktree_path) we fall back to the main repo clone.
        captured_branch: str | None = None
        with self._lock:
            assignment = self._assignments.get(assignment_id)
        if assignment is not None:
            # Determine where to read branch info from
            if assignment.worktree_path:
                check_path = Path(assignment.worktree_path)
            else:
                check_path = Path(assignment.spec.repo_path).expanduser()

            if check_path.exists():
                try:
                    head = _git(check_path, "rev-parse", "--abbrev-ref", "HEAD")
                except _GitError:
                    head = ""
                if head and head != "HEAD":
                    # `HEAD` here means detached; ignore.
                    spec_default = assignment.spec.branch
                    if spec_default is None or head != spec_default:
                        captured_branch = head

            # Best-effort push of the worktree branch.  The worker is
            # responsible for pushing per its briefing, so this is a
            # belt-and-suspenders safety net only.  We use a generous
            # timeout (60 s) but MUST NOT let a hung push block the
            # status update — so we catch both _GitError *and*
            # subprocess.TimeoutExpired and treat both as non-fatal.
            if assignment.worktree_path:
                wt_path = Path(assignment.worktree_path)
                if wt_path.exists() and exit_code == 0:
                    try:
                        with open(assignment.log_path, "a") as reopen:
                            reopen.write("\n# reap: push starting\n")
                        _git(wt_path, "push", "-u", "origin", "HEAD", timeout=60.0)
                        try:
                            with open(assignment.log_path, "a") as reopen:
                                reopen.write("# reap: push completed\n")
                        except OSError:
                            pass
                    except (_GitError, subprocess.TimeoutExpired) as e:
                        try:
                            with open(assignment.log_path, "a") as reopen:
                                reopen.write(f"# reap: push failed ({e})\n")
                        except OSError:
                            pass

        # This block MUST always run regardless of push outcome so that
        # the assignment transitions out of 'running'.
        try:
            with open(assignment.log_path, "a") as reopen:
                reopen.write("# reap: updating status\n")
        except (OSError, AttributeError):
            pass

        with self._lock:
            assignment = self._assignments.get(assignment_id)
            if assignment is None:
                return
            assignment.exit_code = exit_code
            assignment.finished_at = time.time()
            if captured_branch is not None:
                assignment.branch = captured_branch
            # Cancel sets status before this runs; respect it.
            if assignment.status == RUNNING:
                assignment.status = DONE if exit_code == 0 else FAILED
            self._processes.pop(assignment_id, None)

        # #315: parse the log for the worker's claude session_id (from the
        # `system.init` event emitted by `claude -p --output-format stream-json`).
        # Done OUTSIDE the lock so the log parse (I/O + JSON) doesn't stall
        # other threads; the field write is the only mutation, and assignment
        # objects are only dropped under the lock so the reference is safe.
        if assignment is not None and assignment.claude_session_id is None:
            try:
                from coord.worker_events import is_stream_json, parse_log  # noqa: PLC0415
                lp = assignment.log_path
                if lp and is_stream_json(lp):
                    summary = parse_log(lp, tail_bytes=0)
                    if summary.session_id:
                        assignment.claude_session_id = summary.session_id
            except Exception:  # noqa: BLE001
                pass  # best-effort; a missing session_id just means chat-continue will refuse

        self._persist()
        try:
            with open(assignment.log_path, "a") as reopen:
                final_status = assignment.status if assignment else "unknown"
                reopen.write(f"# reap: done (exit_code={exit_code} status={final_status})\n")
        except (OSError, AttributeError):
            pass

        # #305: stash artifacts BEFORE removing the worktree so the compiled
        # outputs survive cleanup.  Only runs for DONE assignments (workers
        # that exited cleanly) with configured artifact_paths for this repo.
        if assignment is not None:
            self._stash_artifacts(assignment)

        # Clean up worktree AFTER updating status
        if assignment is not None:
            self._cleanup_worktree(assignment)

    def _persist(self) -> None:
        with self._lock:
            data = {
                "machine": self.machine_name,
                "capabilities": self.capabilities,
                "repos": self.repos,
                "assignments": [a.to_dict() for a in self._assignments.values()],
            }
        try:
            tmp = self.state_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, indent=2))
            os.replace(tmp, self.state_path)
        except (FileNotFoundError, OSError):
            pass

    def _load_state(self) -> None:
        if not self.state_path.exists():
            return
        try:
            data = json.loads(self.state_path.read_text())
        except (OSError, json.JSONDecodeError):
            return
        for entry in data.get("assignments", []):
            spec_data = entry.pop("spec", None)
            if spec_data is None:
                continue
            spec = AssignmentSpec(**spec_data)
            a = AgentAssignment(spec=spec, **entry)
            # Any process running pre-restart is gone.
            if a.status in (PENDING, RUNNING):
                a.status = FAILED
                a.error = "agent restarted; subprocess lost"
                if a.finished_at is None:
                    a.finished_at = time.time()
            self._assignments[a.id] = a

        # Prune stale worktrees on startup
        self._prune_worktrees()

    def _prune_worktrees(self) -> None:
        """Ask git to prune stale worktree bookkeeping for each known repo.

        Tolerates missing or inaccessible repo directories — ``subprocess.run``
        raises ``FileNotFoundError`` (not ``_GitError``) when its *cwd* doesn't
        exist, so we catch ``(FileNotFoundError, OSError)`` as well.  This
        prevents a stale worktree entry from crashing the agent on startup
        (e.g. after ``exec_restart`` when one of the repo paths has gone away).
        """
        seen_paths: set[str] = set()
        for path_str in self.repo_paths.values():
            if path_str in seen_paths:
                continue
            seen_paths.add(path_str)
            try:
                _git(Path(path_str).expanduser(), "worktree", "prune")
            except (_GitError, FileNotFoundError, OSError):
                pass

    def shutdown(self, *, kill_running: bool = False) -> None:
        """Best-effort cleanup. Used by tests and graceful shutdown."""
        with self._lock:
            procs = list(self._processes.items())
        for aid, proc in procs:
            if proc.poll() is None:
                if kill_running:
                    try:
                        os.killpg(proc.pid, signal.SIGTERM)
                    except (ProcessLookupError, PermissionError):
                        pass
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass
        with self._lock:
            for aid, thread in list(self._threads.items()):
                thread.join(timeout=1)
            self._threads.clear()
