# claude-coordinator

Multi-agent coordinator that orchestrates Claude Code Managed Agent sessions as parallel workers on a GitHub repo.

## Setup

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY="sk-ant-..."
export GITHUB_TOKEN="ghp_..."  # Fine-grained PAT with Contents: Read and write
```

The `gh` CLI must be installed and authenticated.

## Usage

```bash
# Start a coordination session with 3 workers
python coordinator.py --repo JDonaghy/vimcode \
  --worker "desktop-a:gtk=yes" \
  --worker "server:gtk=no:note=no GTK builds" \
  --worker "cloud:gtk=yes:note=Managed Agent session"

# Resume a paused session
python coordinator.py --repo JDonaghy/vimcode --resume
```

## How it works

1. Coordinator reads CLAUDE.md, PROJECT_STATE.md, and open issues from the repo
2. Claude (Opus) proposes assignments for each idle worker, respecting file-conflict rules and worker constraints
3. You approve/modify each assignment interactively
4. Coordinator posts the briefing as a GitHub issue comment and fires a Managed Agent session
5. Workers clone the repo, read the briefing, do the work, push a branch
6. Coordinator polls for completion, then proposes next assignments — loop

## Architecture

```
coordinator.py  — Main CLI loop + coordinator brain (Claude API)
board.py        — Board state (workers, assignments, persistence)
workers.py      — Managed Agent session lifecycle (fire, poll, results)
github_ops.py   — GitHub operations via gh CLI
```

## File conflict rules

Encoded in the coordinator prompt (from vimcode's `docs/COORDINATOR.md`):
- Two workers never touch the same file concurrently
- `src/gtk/` files are treated as one unit
- `src/tui_main/` files are treated as one unit
- `src/render.rs` gets only one worker at a time
- `src/core/engine/` sub-modules can be parallelized
