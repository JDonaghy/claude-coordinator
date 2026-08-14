# graphify on a new machine

How to get the codebase knowledge graph working on a machine that has never had it, and the
traps that make a half-installed setup look identical to a working one.

The graph is what `CLAUDE.md`'s *"query the graph first"* rule depends on. A machine where any
layer below is missing still answers architecture questions — just from grep, slowly, while
reporting no error at all. **Every failure mode here is silent.** That is the reason this
document exists.

## The four layers

| layer | where it lives | committed? |
|---|---|---|
| 1. the `graphify` CLI | `~/.local/bin/graphify` (pipx) | no — per machine |
| 2. the built graph | `<checkout>/graphify-out/` | no — gitignored, per checkout |
| 3. machine-local git hooks | `<checkout>/.git/hooks/` | **never** — pins an absolute interpreter path |
| 4. versioned hook shims | `<repo>/.githooks/` | **yes** — repo-tracked |

Layers 3 and 4 are both needed and are not alternatives. See *"`core.hooksPath` replaces
`.git/hooks` wholesale"* below — that misunderstanding is what silently killed graph rebuilds on
the operator box for about an hour.

## Runbook

### 1. Install the CLI (once per machine)

```bash
pipx install graphifyy          # NOTE: package is "graphifyy", binary is "graphify"
graphify --version              # 0.8.35 at time of writing
```

The double-`y` is not a typo. `pipx install graphify` installs something else.

### 2. Install the agent skill (once per machine, per tool)

```bash
graphify claude install         # CLAUDE.md section + PreToolUse hook for Claude Code
```

`graphify install --platform <p>` covers other tools (`codex`, `cursor`, `gemini`, `opencode`,
…); `graphify --help` lists them. This is what makes `/graphify` and the query-first behaviour
available to an interactive session.

### 3. Build the graph (once per checkout)

```bash
cd ~/src/<repo>
graphify update .               # AST-only re-extraction, no LLM, no API key
```

`graphify update` is the cheap path and is what the hooks run. A first-time build with semantic
edges is `graphify extract .` (needs an LLM backend); the AST-only graph is enough for
"where is X handled" / "what calls this".

**Do this before expecting the hooks to maintain anything.** Every graphify hook opens with

```sh
if [ ! -f "graphify-out/graph.json" ]; then exit 0
```

so on a checkout with no graph the hooks are a permanent, silent no-op. They maintain a graph;
they do not create one.

### 4. Install the machine-local hooks (once per checkout)

```bash
graphify hook install           # post-commit / post-checkout / post-merge into .git/hooks/
graphify hook status            # verify
```

These pin an absolute interpreter path (`#!/home/…/pipx/venvs/graphifyy/bin/python`) and are
therefore machine-specific and must never be committed.

### 5. Opt into the versioned shims (once per checkout)

```bash
git -C ~/src/<repo> config core.hooksPath .githooks
```

This is what gives **worker worktrees** a graph — `.githooks/post-checkout` symlinks the base
checkout's graph into each new `git worktree add`. Without it, workers are graph-blind (#1607)
while `CLAUDE.md` still instructs them to query the graph first.

**Order matters: step 4 before step 5.** The shims are thin — they `exec` the machine-local hook
of the same name and `exit 0` if it is absent (`.githooks/_lib.sh`, `gfy_chain`). Setting
`core.hooksPath` on a checkout with no machine-local hooks installs a working-looking
configuration that rebuilds nothing, silently.

**The repo must actually ship `.githooks/`.** claude-coordinator does. quadraui and vimcode are
the in-flight ports (quadraui#512 / PR #513, vimcode#611 / PR #612). `coord diagnose --graph`
says so explicitly when the directory is missing.

### 6. Verify

```bash
coord diagnose --graph
```

The one command that checks all of it. It compares each checkout's `GRAPH_REPORT.md` source
commit against `HEAD`, and flags an unset or wrong `core.hooksPath`:

```
── claude-coordinator
  ✓ /home/john/src/claude-coordinator: graph in sync (built from 7a7195ae)
      graph.json age: 2.8h
  ⚠ core.hooksPath is unset — worktrees on this machine will NOT get a linked graph.
GRAPH_HEALTH: checkouts=3 stale=0
```

If it reports STALE, run `graphify update .` in that checkout.

## Measuring whether workers actually use it (#2212 / #2236)

Every reaped worker's log ends with a line the agent writes itself:

```
# reap: done (exit_code=0 status=DONE graphify_invocations=1 graph_present=1)
# graphify_query: outcome=hit results=81 cmd='graphify query "where is X handled"'
```

* **`graphify_invocations=N`** (#2212) — how many Bash calls this leg invoked the CLI in.
* **`graph_present=0|1`** (#2236) — whether the worktree had a **resolvable**
  `graphify-out/graph.json` at reap time. Without it, `graphify_invocations=0` is ambiguous:
  the worker prompt tells workers with no graph to *"skip straight to grep, silently"*, so a
  repo that never onboarded (no graph, no `.githooks/post-checkout` — coord-portal and
  stick-demo, as of this writing) produces zero-invocation legs that were **obeying** the rule,
  not ignoring it. `graphify_invocations=0 graph_present=0` is a coverage gap; `=0` with
  `graph_present=1` is a habit gap. Only the second is something prompt wording can move.
* **`# graphify_query:`** (#2236) — one line per call, with `outcome=hit|empty|error|unknown`,
  the result count, and the command. `hit` vs `empty` separates *"queried and got an answer"*
  from *"queried, got nothing, fell back to grep"* — opposite fixes: `empty` points at graph
  coverage/quality, and no amount of prompting helps there.

Count them straight off the fleet's logs:

```bash
grep -ho 'graphify_invocations=[0-9]* graph_present=[01]' ~/.coord/logs/*.log | sort | uniq -c
grep -ho 'graphify_query: outcome=[a-z]*' ~/.coord/logs/*.log | sort | uniq -c
```

**`outcome=hit` means non-empty, not useful.** The counter cannot see relevance: a query
against a graph that indexed the wrong things returns rows and scores as a hit. A run of `hit`
outcomes that workers still abandon after one query is the signal that the graph's *content*,
not the worker's habit, is what needs work — cross-check with `coord diagnose --graph` for a
graph built from a stale SHA.

## Traps

**`core.hooksPath` REPLACES `.git/hooks` wholesale.** Git stops looking in `.git/hooks`
entirely. graphify installs three hooks (`post-commit`, `post-checkout`, `post-merge`), so all
three need a counterpart in `.githooks/` or they are silently disabled. Shipping only
`post-checkout` killed the commit/merge rebuilds on the operator box for about an hour — caught
only because `coord diagnose --graph` reported the repo's own graph STALE right after a merge.

**Never `rm -rf graphify-out`.** The directory is invisible to git *because of a tracked file
inside it*: `graphify-out/.gitignore` contains `*` / `!.gitignore`. Delete it and git sees a
deleted tracked file, `.gitignore` can no longer suppress anything, and coord's worktree-rescue
sweep (#1394) commits the wreckage onto the worker's branch. This shipped once (#1617): it
polluted every worker worktree on all three machines within ~35 minutes, cost a wasted review
plus a fix round, and forced `core.hooksPath` off fleet-wide as the mitigation. The corrected
hook symlinks the graph's **entries into** the directory and never replaces the directory
itself.

**`.githooks/**` is a fifth deploy surface, and it is the fast one.** The four documented
lanes (`~/.coord-venv` + PyPI, `coord-serve` restart, `cargo build`, `~/.coord-cli-venv`) all
exist to make deploys slow and deliberate. Repo-tracked hooks take effect on the **next fetch**,
on every machine, with no release and no restart. The failure mode is the opposite of the usual
one: not *"a merged fix is not a live fix"* but *"a merged hook is live everywhere immediately"*.
Treat a `.githooks/` change as a fleet-wide deploy.

**In a worktree the graph is the base checkout's, not yours.** Rebuilds are deliberately
disabled in linked worktrees (a rebuild there would overwrite the shared graph from a feature
branch), and the graph reflects the base checkout's HEAD. Trust it for *"where is X handled"*,
never for *"did my change land"*.

**The hooks drift and cannot prevent it.** They `exit 0` during rebase/merge/cherry-pick, so a
merge agent's proactive rebase never rebuilds; nothing fires at all for `git reset --hard`;
every failure path is a silent `exit 0` behind a detached background process; concurrent
triggers coalesce. Treat them as an optimisation and check freshness with
`coord diagnose --graph` rather than assuming.

## Fleet state (2026-07-30)

`core.hooksPath` is **unset on all three machines** — the deliberate mitigation for #1617, which
is now fixed on `main` (`c4d1f35`). Re-enabling requires each base checkout to pull that fix
first, because git resolves a relative `core.hooksPath` against the worktree being created, so a
branch predating the fix still runs the old hook.

Per-machine, per-checkout:

```bash
git -C ~/src/<repo> pull
git -C ~/src/<repo> config core.hooksPath .githooks
coord diagnose --graph          # confirm
```

vimcode's checkout currently has `core.hooksPath` set to an absolute
`/home/john/src/vimcode/.git/hooks` rather than unset — functionally equivalent to the default
today, but it will need clearing to `.githooks` when vimcode#611 lands.
