# Agent operations

How to install, upgrade, diagnose, and recover the per-machine agent server.

## Publishing a release (PyPI)

**Releases are published by GitHub Actions, not by local `twine upload`.**
The PyPI token lives in the `PYPI_API_TOKEN` repo secret; it is not
available on any developer machine. Do not run `twine upload` locally —
it will hang on an interactive token prompt.

The release is triggered by **pushing a `v*` tag**
(`.github/workflows/publish.yml`). The workflow builds the sdist + wheel,
publishes to PyPI via `pypa/gh-action-pypi-publish`, and cuts a GitHub
release with auto-generated notes.

To cut a release:

```bash
# 1. Bump the version in BOTH places (they must match):
#    - pyproject.toml  → version = "X.Y.Z"
#    - coord/__init__.py → __version__ = "X.Y.Z"
# 2. Commit the bump, then push main:
git push origin main
# 3. Tag the bump commit and push the tag — THIS is what publishes:
git tag vX.Y.Z <bump-commit-sha>
git push origin vX.Y.Z
```

Watch the run:

```bash
gh run list --repo JDonaghy/claude-coordinator --workflow publish.yml --limit 1
gh run watch <run-id> --repo JDonaghy/claude-coordinator
```

PyPI propagation can lag a minute or two after the workflow goes green.
`pip install --upgrade` (and `coord agent update`) may report
`no_change` until the new version is visible — wait and retry rather
than assuming the publish failed.

**Anything that changes `coord/agent.py` (e.g. the worker system
prompts) only takes effect on agents after a release + rollout.**
Coordinator-only code (CLI, `notify.py`, parsers, TUI) is live from the
editable install the moment it's on disk — but agents run from PyPI, so
agent-side changes need this release flow plus the rollout below.

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

## Control-center daemon (`coord serve`, #584/#591)

The portable control center runs a **daemon** that fronts the one shared
`~/.coord/coord.db` over Tailscale, so `coord-tui` / `coord status` (and remote
`coord report-result`) on **any** machine render and drive the **same** board.
The daemon listens on **7435** (agent=7433, dashboard=7434). Run it on the
always-on box that owns the DB — **dellserver** for production.

Endpoints: `GET /healthz` (liveness, never auth-gated), `GET /board` (full
projection), `GET /config` (raw `coordinator.yml`), `POST /result` /
`POST /completion` (#590 write path — a remote session's result lands on the
shared DB). A thin client carries no `coord.db`/`coordinator.yml`; it reads both
from the daemon.

### Prerequisites (daemon host)

- **A coord build with `coord serve`.** It ships in the #584/#590 release and
  later. A PyPI install older than that has no `serve` command, so the daemon
  host must be on a release `>=` that cut (or, pre-release, an editable checkout
  of the branch — note the editable-drift caveats elsewhere in this doc).
- **`~/coordinator.yml` present** on the daemon host (it serves this at
  `/config`; clients then need none — that's the point of #591).
- **`~/.coord/coord.db` present** (after the one-time cutover below).

### Install the service

```bash
mkdir -p ~/.config/systemd/user
cp deploy/coord-serve.service ~/.config/systemd/user/   # from a checkout, or scp it over
loginctl enable-linger "$USER"          # survive logout / reboot (same as coord-agent)
systemctl --user daemon-reload
systemctl --user enable --now coord-serve
```

### Bearer token (defence-in-depth)

Tailscale ACLs are the real boundary; a shared bearer token is belt-and-braces
(full per-user auth is #282). Set one on the production daemon:

```bash
openssl rand -hex 32 > ~/.coord/serve_token && chmod 600 ~/.coord/serve_token
systemctl --user restart coord-serve     # picks it up via resolve_serve_token()
```

The daemon resolves the token **flag > `$COORD_SERVE_TOKEN` > `~/.coord/serve_token`**.
Prefer the file/env — a `--token` on the command line leaks via `ps`. With no
token the daemon runs **open** (fine for dev; it logs a warning).

### Verify

```bash
curl -s http://<daemon-host>:7435/healthz                 # {"status":"ok",...}
curl -s -H "Authorization: Bearer $(cat ~/.coord/serve_token)" \
  http://<daemon-host>:7435/board | python3 -c 'import sys,json;b=json.load(sys.stdin);print("round",b["round_number"],"assignments",len(b["assignments"]))'
```

### Point clients at it

On every **client** machine (NOT the daemon host) — `~/.coord/client.toml`:

```toml
board_service = "http://<daemon-host>:7435"   # e.g. dellserver's stable tailnet IP/MagicDNS
token = "<the same secret>"                    # omit if the daemon runs open
```

Resolution is **flag > `$COORD_SERVICE_URL`/`$COORD_TOKEN` > `client.toml`**. The
client's `coord` must also be a build with the thin-client code (#584/#590). The
**daemon host must NOT have `client.toml`** (it owns the DB; a stray file would
make it a thin client of itself).

### One-time cutover / ETL (elitebook → dellserver)

The board DB currently lives on **elitebook**; #591 moves it to the always-on
**dellserver** and makes every other box a thin client. The DB is a single
SQLite file, so the "ETL" is a file copy + a parity check — do it during a quiet
window (no active dispatch):

```bash
# 1. Quiesce: stop driving the pipeline; let in-flight workers settle.
# 2. Copy the live DB to the daemon host. WAL-checkpoint first so the .db file
#    is self-contained (otherwise also copy coord.db-wal / coord.db-shm).
ssh elitebook '~/.coord-venv/bin/python -c "import sqlite3;c=sqlite3.connect(\"$HOME/.coord/coord.db\");c.execute(\"PRAGMA wal_checkpoint(TRUNCATE)\");c.close()"'
scp elitebook:~/.coord/coord.db dellserver:~/.coord/coord.db
scp elitebook:~/coordinator.yml dellserver:~/coordinator.yml
# 3. Start the daemon on dellserver (service above), verify /board parity:
#    round_number + assignment count match elitebook's `coord status`.
# 4. Flip every machine (incl. elitebook) to a thin client: write client.toml
#    pointing at dellserver:7435. REMOVE elitebook's client.toml only if it is
#    no longer the daemon host. If dellserver is the sole daemon, elitebook is
#    a client and DOES get a client.toml.
# 5. Verify each machine: `coord status` and `coord-tui` show the dellserver board.
# 6. Retire the per-host DBs only AFTER parity is confirmed (rename, don't rm,
#    until you've lived on the daemon for a bit): mv ~/.coord/coord.db ~/.coord/coord.db.retired
```

Parity check = the daemon's `/board` `round_number` and assignment count equal
the source's `coord status` before the flip. Keep the elitebook DB renamed (not
deleted) until the daemon has run clean for a day.

### Restart / logs

```bash
systemctl --user restart coord-serve
journalctl --user -u coord-serve -f
```

## Graphify graph: reseed a machine's local clone

`graphify-out/` is **not** tracked in git (claude-coordinator, vimcode, and quadraui all gitignore it as of 2026-06-07). Each repo's knowledge graph is a regenerable, machine-local cache rebuilt by the `post-commit` / `post-checkout` git hooks. PyPI agent installs have no clone and don't need this — it applies only to machines with a **local git checkout** of these repos (the dev machine, and any worker box that builds/tests them).

**One-time migration** — the first time a clone pulls the commit that stopped tracking `graphify-out/`, git wants to delete the now-untracked files, but the hooks keep them dirty, so the pull may abort with *"local changes would be overwritten"*. Discard the (regenerable) cache first, then pull:

```bash
cd <repo>            # e.g. ~/src/quadraui
rm -rf graphify-out  # safe — regenerable cache
git pull             # now clean
```

**Reseed the graph** — one-time per machine per repo. Restores the rich semantic + community graph (AST-only refresh is free on every commit thereafter):

```bash
/graphify            # in a Claude Code session at the repo root
# or headless:  graphify .
```

Then ensure the hooks are installed so the graph stays current after the seed:

```bash
graphify hook install   # idempotent; appends to any existing post-commit hook
```

Without the seed, queries have no `graph.json` to read until the next commit triggers an AST-only rebuild — and `post-checkout` will **not** bootstrap a graph when `graphify-out/` is absent, so the explicit one-time seed is required.

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

## Upgrade via the raw `/update` endpoint (reliable fallback)

`coord agent update` is a thin wrapper over the agent's `POST /update`
HTTP endpoint plus a 120 s "wait for it to come back" loop. That loop is
the source of the `✗ did not come back` **false negative**: the upgrade
triggers an `os.execv` restart, and on a slow machine (or slow pip) the
agent can take longer than 120 s to rebind the port — the CLI gives up
and reports failure even though the agent recovers seconds later and is
actually on the new version.

When that happens, drive the endpoint directly and poll `/health`
yourself — no artificial timeout:

```bash
# 1. Fire the upgrade. Returns 202 immediately; the pip install + restart
#    run in a background thread on the agent.
curl -s -X POST http://<host>:7433/update
# → {"status":"updating","mode":"pip install --upgrade"}

# 2. Poll /health until the version advances (the agent drops its socket
#    briefly during the execv restart — `curl` failing for a few seconds
#    is expected).
until [ "$(curl -s http://<host>:7433/health | python3 -c 'import sys,json;print(json.load(sys.stdin).get("version"))' 2>/dev/null)" = "<new-version>" ]; do
  sleep 3
done
echo "agent is on <new-version>"
```

Behaviour worth knowing:

- The endpoint **runs `pip install --upgrade --no-cache-dir
  claude-coordinator`** (or `git pull --ff-only` for an editable
  install) in a daemon-less background thread, then `os.execv`-restarts
  **only if the version actually changed**.
- If the installed version is already current it records
  `result: no_change` and does **not** restart — so hitting `/update` on
  an already-up-to-date agent is harmless (it just runs pip and returns).
- The full pip/git output is written to `~/.coord/last_update.log` on
  the agent; a short excerpt plus `mode` / `result` / `version_before` /
  `version_after` / `error` are surfaced under `last_update` in
  `/health`.

**Do not update an agent that is running a worker you care about** — the
`os.execv` restart kills in-flight `claude -p` subprocesses. Check
`curl -s http://<host>:7433/status` for a non-empty `active` list first,
or wait for the work to finish.

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

## Passwordless SSH between coordinator and agents

`coord pull-artifact` rsyncs built binaries from the agent's
`~/.coord/artifacts/` directory over SSH.  The coordinator machine must be
able to `ssh <agent-host>` without a password prompt for rsync to work.

### One-time setup (per coordinator→agent pair)

**On the coordinator machine** (where you run `coord plan` / `coord pull-artifact`):

```bash
# 1. Generate a key if you don't already have one.
ssh-keygen -t ed25519 -C "coord-coordinator" -f ~/.ssh/id_ed25519_coord
# (or reuse an existing key — just make sure it isn't passphrase-protected,
#  or add it to ssh-agent and keep ssh-agent running)

# 2. Copy the public key to every agent machine.
ssh-copy-id -i ~/.ssh/id_ed25519_coord.pub <agent-host>
# e.g.:
ssh-copy-id -i ~/.ssh/id_ed25519_coord.pub precision
ssh-copy-id -i ~/.ssh/id_ed25519_coord.pub elitebook
```

**Verify:**

```bash
ssh precision true && echo "OK"
```

No password prompt → you're done.

### First-time accept (StrictHostKeyChecking)

On the very first SSH to a new agent, the client may prompt:

```
The authenticity of host 'precision' can't be established.
Are you sure you want to continue connecting (yes/no/[fingerprint])?
```

Accept once (`yes`) or pre-accept all coordinator-managed hosts so
unattended `coord pull-artifact` calls never stall:

```bash
# Option A: accept once interactively (safest)
ssh precision true

# Option B: add StrictHostKeyChecking=accept-new to ~/.ssh/config
# so the first connection auto-accepts but rejects changed keys.
cat >> ~/.ssh/config <<'EOF'

Host precision elitebook dellserver
    StrictHostKeyChecking accept-new
EOF
```

### Required file permissions

SSH is strict about key file modes.  If `ssh-copy-id` created the key,
permissions are already correct.  If you manage keys manually:

```bash
chmod 700 ~/.ssh
chmod 600 ~/.ssh/id_ed25519_coord
chmod 644 ~/.ssh/id_ed25519_coord.pub
chmod 600 ~/.ssh/authorized_keys   # on each agent
```

### Design context

For background on why rsync-over-SSH was chosen over direct HTTP download
(signed URL vs key-auth tradeoffs, GC behaviour, TTL defaults), see the
original design in [GitHub issue #305](https://github.com/JDonaghy/claude-coordinator/issues/305).
