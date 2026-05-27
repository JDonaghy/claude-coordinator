# Agent operations

How to install, upgrade, diagnose, and recover the per-machine agent server.

## Install a new agent (first time)

On the target machine:

```bash
curl -sSL https://raw.githubusercontent.com/JDonaghy/claude-coordinator/main/install-agent.sh | bash -s -- --machine <name> --port 7433
```

This creates `~/.coord-venv`, installs `claude-coordinator` from **PyPI**, writes a `coord-agent` systemd user unit, and starts it. The agent does NOT need a git clone of the repo — the `~/src/claude-coordinator` directory should only exist on the machine where you actually develop the coordinator itself.

Verify:

```bash
curl -s http://<host>:7433/health | python3 -m json.tool
```

The `version` field should match the latest PyPI release.

## Routine upgrade (all agents)

From the coordinator machine:

```bash
coord agent update --all
```

This POSTs to `/update` on every machine in `coordinator.yml`. Each agent runs `pip install --upgrade claude-coordinator` and re-execs the process. The CLI waits up to 120 s for each agent to come back online and reports `version_before → version_after`.

To target one machine:

```bash
coord agent update --machine precision
```

## Diagnose a failed upgrade

If `coord agent update` reports `✗ did not come back` or the version doesn't advance, query the machine's `/health` and read `last_update`:

```bash
curl -s http://<host>:7433/health | python3 -c "
import json, sys
d = json.load(sys.stdin)
lu = d.get('last_update', {})
print('version:', d.get('version'))
print('mode:', lu.get('mode'))
print('result:', lu.get('result'))
print('error:', lu.get('error'))
"
```

The `mode` field is the key diagnostic:

- **`pip install --upgrade`** — normal PyPI install. Failures here usually mean PyPI propagation lag (`result: no_change`) or a network issue.
- **`editable (git pull)`** — the agent was installed from a local git clone via `pip install -e .` instead of from PyPI. This is the legacy/dev setup and is the source of most upgrade failures (detached HEAD, missing branch, local commits, conflicts, etc.). **Convert it to a PyPI install** (see below).

## Convert an editable install to PyPI (the most common fix)

When `last_update.mode` is `editable (git pull)`, the agent's venv has a `pip install -e .` pointing at a local clone. To switch to PyPI:

```bash
ssh <host>
~/.coord-venv/bin/pip uninstall -y claude-coordinator
~/.coord-venv/bin/pip install --upgrade claude-coordinator
systemctl --user restart coord-agent
```

After this, the `~/src/claude-coordinator` clone on that machine is no longer used by the agent and can be deleted. The next `coord agent update` will use the `pip install --upgrade` path, which doesn't depend on local git state.

Verify:

```bash
curl -s http://<host>:7433/health | python3 -c "import sys, json; d = json.load(sys.stdin); print(d['version'])"
```

## Manual restart (after editing files in-place)

```bash
systemctl --user restart coord-agent
```

The restart picks up whatever is currently installed in `~/.coord-venv` (re-reads from disk). Use this when the agent process is wedged or holding stale code that a `/update` couldn't replace.

## Watch the agent log

```bash
journalctl --user -u coord-agent -f
```

## Known issues

- **#280** (fixed in 0.4.11) — `/update` would crash on startup if a worktree directory had been cleaned out from under the agent, leaving the process on the old version even though pip succeeded.
- **Editable install on detached HEAD** — `git pull --ff-only` fails because there is no current branch. The fix is to convert to a PyPI install (above); don't try to repair the local git state on an agent machine.

## Adding the conversion to many machines at once

If you have several editable installs to convert, you can script it (assumes SSH is set up):

```bash
for host in precision elitebook dellserver; do
  ssh $host '~/.coord-venv/bin/pip uninstall -y claude-coordinator && ~/.coord-venv/bin/pip install --upgrade claude-coordinator && systemctl --user restart coord-agent'
done
```
