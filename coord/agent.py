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
import subprocess
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterable


DEFAULT_STATE_DIR = Path.home() / ".coord"
DEFAULT_WORKER_BINARY = "claude"

# Statuses
PENDING = "pending"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"


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


def _slugify(text: str, max_len: int = 40) -> str:
    """Convert *text* to a URL/branch-safe slug."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len].rstrip("-")


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
- You are already on a feature branch. Commit your work to this branch. \
Push with `git push origin HEAD`. \
NEVER commit or push to main or develop directly. \
Do NOT open a PR — the coordinator handles that.

Progress reporting:
- After each significant step (first build, test run, approach change), \
output a status line in exactly this format:
  STATUS: [what you just did] → [what you're about to do] → [confidence: high/medium/low]
- If you've tried 2 approaches and neither worked, STOP and output:
  STUCK: [what you tried] [why it failed] [what you think the blocker is]
  Then wait for guidance rather than trying a third approach.\
"""

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
    """Build the argv for invoking the worker on this assignment."""
    system_prompt = spec.system_prompt if spec.system_prompt else WORKER_SYSTEM_PROMPT
    system_prompt += build_deny_prompt(spec.deny_commands)
    argv = [
        binary, "-p",
        "--system-prompt", system_prompt,
        "--allowedTools", "Read,Edit,Write,Bash",
        "--permission-mode", "acceptEdits",
    ]
    if spec.model:
        argv.extend(["--model", spec.model])
    argv.append(spec.briefing)
    return argv


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
    ) -> None:
        self.machine_name = machine_name
        self.capabilities = list(capabilities)
        self.repos = list(repos)
        self.repo_paths = dict(repo_paths or {})
        self.state_dir = Path(state_dir)
        self.log_dir = self.state_dir / "logs"
        self.state_path = self.state_dir / "agent_state.json"
        self.worker_command = worker_command or default_worker_command

        self._lock = threading.Lock()
        self._assignments: dict[str, AgentAssignment] = {}
        self._processes: dict[str, subprocess.Popen] = {}
        self._threads: dict[str, threading.Thread] = {}

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
        return {
            "machine": self.machine_name,
            "capabilities": self.capabilities,
            "repos": self.repos,
            "active": active,
            "completed": completed,
        }

    def list_assignments(self) -> dict:
        with self._lock:
            assignments = list(self._assignments.values())
        active = []
        completed = []
        for a in assignments:
            d = a.to_dict()
            if a.status == RUNNING:
                prog = self.progress(a.id)
                if prog:
                    d["progress"] = prog
                active.append(d)
            else:
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
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
            except ProcessLookupError:
                pass

        with self._lock:
            assignment.status = CANCELLED
            assignment.finished_at = time.time()
        self._persist()

        # Clean up worktree after cancellation
        self._cleanup_worktree(assignment)

        return assignment

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
            except _GitError:
                shutil.rmtree(worktree_path, ignore_errors=True)

        worktree_path.parent.mkdir(parents=True, exist_ok=True)

        # Fetch latest
        try:
            _git(repo_path, "fetch", "origin")
        except _GitError:
            pass  # offline is OK, work from local state

        # Determine start point — prefer origin/<branch>, fall back to local
        default_branch = assignment.spec.branch or "main"
        start_point = f"origin/{default_branch}"
        try:
            _git(repo_path, "rev-parse", "--verify", start_point)
        except _GitError:
            # No remote tracking branch — use local branch as start point
            start_point = default_branch

        # Branch name for this assignment
        branch_name = f"issue-{assignment.spec.issue_number}-{_slugify(assignment.spec.issue_title)}"

        # Check if branch already exists (locally or on remote — retry scenario)
        branch_exists = False
        for ref in (f"origin/{branch_name}", branch_name):
            try:
                _git(repo_path, "rev-parse", "--verify", ref)
                branch_exists = True
                break
            except _GitError:
                continue

        if branch_exists:
            # Branch exists — check it out in the worktree
            _git(repo_path, "worktree", "add", str(worktree_path), branch_name)
        else:
            # Branch doesn't exist — create new worktree with new branch
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

        header = (
            f"# agent={self.machine_name} repo={assignment.spec.repo_name} "
            f"issue=#{assignment.spec.issue_number} "
            f"argv={shlex.join(argv)}\n"
        )
        log_fh.write(header)
        log_fh.flush()

        try:
            proc = subprocess.Popen(
                argv,
                cwd=str(repo_path),
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
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

        with self._lock:
            assignment.status = RUNNING
            assignment.pid = proc.pid
            assignment.started_at = time.time()
            self._processes[assignment.id] = proc

        thread = threading.Thread(
            target=self._reap,
            args=(assignment.id, proc, log_fh),
            daemon=True,
            name=f"agent-reap-{assignment.id}",
        )
        with self._lock:
            self._threads[assignment.id] = thread
        thread.start()
        self._persist()

    def _reap(self, assignment_id: str, proc: subprocess.Popen, log_fh) -> None:
        exit_code = proc.wait()
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

            # Push the branch from worktree before cleanup
            if assignment.worktree_path:
                wt_path = Path(assignment.worktree_path)
                if wt_path.exists() and exit_code == 0:
                    try:
                        _git(wt_path, "push", "-u", "origin", "HEAD")
                    except _GitError as e:
                        try:
                            with open(assignment.log_path, "a") as reopen:
                                reopen.write(f"\n# push failed: {e}\n")
                        except OSError:
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
        self._persist()

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
        """Ask git to prune stale worktree bookkeeping for each known repo."""
        seen_paths: set[str] = set()
        for path_str in self.repo_paths.values():
            if path_str in seen_paths:
                continue
            seen_paths.add(path_str)
            try:
                _git(Path(path_str).expanduser(), "worktree", "prune")
            except _GitError:
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
