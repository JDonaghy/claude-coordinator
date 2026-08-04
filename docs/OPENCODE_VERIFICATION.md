# OpenCode verification pass (#1703)

This document replaces assumption with captured evidence for the opencode
backend. It does **not** change `coord/providers/opencode.py` — that
correction is the follow-up issue's job. Everything below was produced by
running a real `opencode` binary against real (free and paid) models in a
throwaway git repository; nothing here is hand-written or inferred from docs
alone except where explicitly marked "from published schema" (the JSON Schema
served at `https://opencode.ai/config.json`, fetched live and cross-checked
against `opencode agent list` output from the same binary).

## Machine / version

- Host: `dellserver` (fleet machine), Linux 6.8.0-124-generic x86_64.
- `opencode --version`: **1.18.11**
- Binary location: `/home/john/.opencode/bin/opencode`
- Models used for capture: `opencode/big-pickle` (opencode's own free-tier
  model, used for most captures to avoid burning API credit) and
  `deepseek/deepseek-chat` (a real paid provider, used specifically to
  confirm non-zero `cost` reporting — the account already had DeepSeek
  credentials configured via `opencode providers login`).
- Captured: 2026-08-03.

## Fixtures

- `tests/fixtures/opencode_run_sample.jsonl` — **replaced**, verbatim capture
  of a successful run: `opencode run --format json --model opencode/big-pickle
  "Add a subtract(a, b) function to math_utils.py that returns a - b. Keep it
  minimal."` in a throwaway repo containing a one-function `math_utils.py`.
  12 lines: `glob` → `read` → `edit` → final `text` → terminal `step_finish`.
- `tests/fixtures/opencode_run_failure_sample.jsonl` — **new**, verbatim
  capture of a failing run: the same task issued against
  `opencode/deepseek-v4-flash-free`, which made one `glob` tool call and then
  hit a real `503` from the model's request queue mid-session
  (`"Streaming response failed: [503] The request queue is full."`), exit
  code 1. This is a more representative "failing run" than a same-line
  invalid-model rejection because it shows genuine partial progress before
  the hard failure — which is the harder case for a coordinator's log parser
  to get right. (A second, simpler failure mode — `opencode run --model
  deepseek/does-not-exist-model "hi"` — produced a single top-level `error`
  event with no prior progress and exit code 1; not included as a fixture
  file but noted here since it's a distinct shape worth handling.)

Both fixtures are raw stdout captured with `> file.jsonl 2>stderr.log`; stderr
was empty in both captures (errors surface as `type:"error"` events on
stdout, not on stderr).

## The real event schema

Every line is one JSON object. Five **top-level** `type` values were observed
across ~15 real runs (successful, failing, permission-denied, resumed,
forked, file-attached, server-attached):

| top-level `type` | meaning |
|---|---|
| `step_start` | a new assistant turn/step begins |
| `tool_use` | a tool call, with its full input/output/error inline |
| `text` | an assistant text chunk |
| `step_finish` | a turn/step ends — carries `part.reason`, `part.tokens`, `part.cost` |
| `error` | a run-level failure (the whole request/stream failed) |

Every event (success or error) carries a top-level `sessionID` string —
**there is no separate `session.start`/`session.init` event**; the session id
is present on line 1 already. This directly contradicts the current
`opencode.py` docstring's assumption that `session_id` arrives via a
dedicated `session.start` event.

Note the naming split: the **top-level** `type` uses underscores
(`step_finish`, `tool_use`), but the **nested** `part.type` uses hyphens
(`step-finish`, `step-start`, `tool`, `text`). A parser keying off `part.type`
instead of the top-level `type` will silently match nothing.

There is no `session.complete` event, ever. **This means
`RESULT_MARKER = '"type":"session.complete"'` never matches real output —
confirmed by running the fixture through the unmodified provider (see "What
current `opencode.py` actually extracts" below).**

### The real terminal/completion signal

The last event of a successful run is always a `step_finish` whose
`part.reason == "stop"` (as opposed to `part.reason == "tool-calls"`, which
ends every intermediate turn that made a tool call and is followed by another
`step_start`). Concretely:

```json
{"type":"step_finish","timestamp":...,"sessionID":"...","part":{"id":"...","reason":"stop","snapshot":"...","messageID":"...","sessionID":"...","type":"step-finish","tokens":{"total":8446,"input":107,"output":19,"reasoning":0,"cache":{"write":0,"read":8320}},"cost":0}}
```

Recommendation for the follow-up issue: match on `part.reason == "stop"`
structurally (parse each line and check the field), not on a raw substring
like `'"reason":"stop"'` — a substring match is fragile against key reordering
and is not defensively safe the way the current `RESULT_MARKER` approach
assumes.

A failed run (the whole request/stream errors out) instead ends with a
top-level `error` event and **no terminal `step_finish` at all**:

```json
{"type":"error","timestamp":...,"sessionID":"...","error":{"name":"UnknownError","data":{"message":"\"Streaming response failed: [503] The request queue is full.\""}}}
```

So completion detection needs two cases, not one marker: `step_finish` with
`reason:"stop"` (success) or a top-level `error` event (failure) — and a
process that exits without emitting either (killed by timeout — see the
buffering finding below) is a third case coord must already handle via exit
code / process reaping, not log content.

### Token usage and cost — field paths

Usage is **per-step**, not summarized once at the end. Every `step_finish`
event carries:

```json
"part": {
  "tokens": {"total": 7879, "input": 207, "output": 120, "reasoning": 0,
             "cache": {"write": 0, "read": 7552}},
  "cost": 0.0000837256
}
```

confirmed with a real paid model (`deepseek/deepseek-chat`) across a 4-step
run — costs were non-zero and varied per step
(`0.00106932`, `0.0000449456`, `0.0000837256`, `0.0000344624`). Free-tier
runs (`opencode/big-pickle`, `opencode/deepseek-v4-flash-free`) always report
`"cost": 0`.

There is **no cumulative session-total field** anywhere in the stream. A
correct `total_cost_usd` / total token count is the **sum of `part.cost` /
`part.tokens.*` across every `step_finish` event** for the session, not a
single read from a final event. This is a structural difference from the
current `_update_opencode_summary()`, which reads a single `usage` object off
one assumed `session.complete` event.

### Tool calls, text, and errors → `WorkerSummary` mapping

- Tool calls: `{"type":"tool_use","part":{"type":"tool","tool":"<name>","callID":"...","state":{"status":"completed"|"error","input":{...},"output":...|"error":"..."}}}`.
  Observed `tool` values: `glob`, `read`, `edit`, `write`, `bash`. `state.status`
  is `"completed"` on success; on a **permission-denied** call it is
  `"error"` with `state.error` containing a human-readable message that
  literally quotes the matching permission rules (see below) — this is a
  rich, structured signal coord could surface directly instead of guessing
  from stderr.
- Text: `{"type":"text","part":{"type":"text","text":"..."}}` — the assistant's
  final answer is the last `text` event before the terminal `step_finish`.
- Errors: two distinct shapes, not one:
  1. Run-level: top-level `{"type":"error","error":{"name":...,"data":{"message":...}}}`
     — the whole run failed, no more events follow.
  2. Tool-level: `tool_use` event with `state.status == "error"` — one tool
     call failed (e.g. permission denied), but the run continues; the model
     typically explains the failure to the user in the next `text` event and
     finishes normally with `reason:"stop"`.

### Session id / resume

Confirmed: `sessionID` is present on **every** event from the first line,
formatted like `ses_036b4a104ffeIOILOMFtWVIoOb`. `--session <id>` (also `-s`)
correctly resumes: a follow-up run with `--session <id>` reused the exact
same `sessionID` and had context of the prior turn (asked "what function did
you just add?", correctly answered "subtract(a, b)"). `--continue` (`-c`,
continue the *last* session with no id needed) also confirmed working the
same way.

## Flag surface — verified against the real 1.18.11 binary

| Flag | Status | Notes |
|---|---|---|
| `run [message..]` | **confirmed** | message is a positional array; briefing text works multi-word/multi-line |
| `--format default\|json` | **confirmed** | `json` emits the raw NDJSON event stream documented above; `default` emits a human-formatted TUI-style transcript, not usable by a parser |
| `--model` / `-m` (`provider/model`) | **confirmed** | e.g. `opencode/big-pickle`, `deepseek/deepseek-chat` |
| `--session` / `-s` | **confirmed** | resumes by id, `sessionID` stays identical across the resumed run |
| `--continue` / `-c` | **confirmed** | resumes the most recent session with no id argument |
| `--fork` | **confirmed** | used with `--session`; produces a **new** `sessionID` branched from the given session (verified: base session's `sessionID` did not appear anywhere in the forked run's output) |
| `--agent` | **confirmed** | selects a named agent (see Permissions below); an agent can carry its own `"prompt"` field — this **is** a real system-prompt equivalent (see Corrections below) |
| `--file` / `-f` | **confirmed, with a real ordering gotcha** | both `message` and `--file` are yargs array-type options with greedy consumption. `opencode run --file X "message"` and `opencode run --file=X "message"` both fail — `--file` swallows the message text as additional file arguments ("File not found: <message text>"). It only works as `opencode run "message" --file X` (flag placed **after** the positional message). This matters for the follow-up provider issue: if `build_command()` ever needs to append an array-typed flag via `extra_args`, that flag must be placed after the trailing briefing, not before it — the opposite of the current implementation's ordering. |
| `--attach` | **confirmed** | tested end-to-end: started `opencode serve --port <p> --hostname 127.0.0.1`, then `opencode run --attach http://127.0.0.1:<p> ...` against it; the task ran on the attached server and returned correctly |
| `--auto` | **confirmed**, see Permissions below | |

## Permission enforcement — empirical evidence

opencode's permission system is configured per-agent via a project
`opencode.jsonc`'s `"agent"` block (schema confirmed live against
`https://opencode.ai/config.json`: `Config.agent.<name>.permission`, each
permission either a single `ask`/`allow`/`deny` string or an object mapping
glob-style patterns — e.g. `"git *"` — to `ask`/`allow`/`deny`). `opencode
agent list` prints the fully-resolved, ordered rule list for every agent,
including built-ins, which is how the rule-precedence bug below was caught.

### Rule precedence is last-match-wins, not first-match-wins

This was **not** assumed — it was discovered empirically by making a mistake.
An agent was first configured with:

```json
"bash": {"git *": "allow", "gh *": "deny", "*": "ask"}
```

(specific rules listed *before* the catch-all). Running `git status` under
this agent produced:

```
! permission requested: bash (git status); auto-rejecting
```

i.e. the specific `"git *": "allow"` rule was **not** applied — the catch-all
`"*": "ask"` (listed last) won. Reordering to catch-all-first:

```json
"bash": {"*": "ask", "git *": "allow", "gh *": "deny"}
```

made `git status` run successfully (verified: real `git status` output
returned in the transcript). **Rule order in the config matters and it is
last-rule-wins**, confirming the issue's premise. This is directly relevant
to the follow-up issue's deny-list flip: coord's generated permission config
must place catch-all rules first and specific overrides after, or the
overrides silently do nothing.

### Deny hides the tool entirely; a pattern-specific deny surfaces as a failed tool call

With `"bash": "deny"` (whole tool denied), a task that obviously wanted a
shell command never attempted one — the model's own response was *"I don't
have a bash/terminal tool available in this environment"* and it fell back to
the `read` tool to list a directory instead. No `tool_use` event for `bash`
appears anywhere in the transcript at all (see
`docs/` capture referenced by this file's git history — the raw run is not
committed as a fixture since it duplicates the schema already captured, but
the transcript is reproducible with the `no-bash` agent config below).

With a **pattern-specific** deny (`"gh *": "deny"`, `"git *": "allow"`), the
tool call was attempted and the model **did** see it fail:

```json
{"type":"tool_use","part":{"type":"tool","tool":"bash","state":{
  "status":"error",
  "input":{"command":"gh issue list"},
  "error":"The user has specified a rule which prevents you from using this specific tool call. Here are some of the relevant rules [{\"permission\":\"*\",\"action\":\"allow\",\"pattern\":\"*\"},{\"permission\":\"bash\",\"pattern\":\"*\",\"action\":\"ask\"},{\"permission\":\"bash\",\"pattern\":\"git *\",\"action\":\"allow\"},{\"permission\":\"bash\",\"pattern\":\"gh *\",\"action\":\"deny\"}]"
}}}
```

The model then explained the restriction to the user in a follow-up `text`
event and finished normally (`reason:"stop"`) — a denied tool call does not
abort the run. Meanwhile `git status` under the identical agent executed and
returned real output. **This is concrete, reproducible proof that opencode's
`bash` permission does per-command pattern matching**, directly contradicting
the current `opencode.py` module docstring's claim that "coord's worker deny
list ... has no equivalent in OpenCode's CLI."

### `--auto` semantics: deny-list, confirmed exactly as described

Four scenarios, all real runs:

1. **`ask` rule, no `--auto`, headless (`--format json`, no TTY):** auto-rejected
   with a stderr warning (`! permission requested: bash (echo hello);
   auto-rejecting`), zero output produced by the denied call. This is the
   critical finding for **"what happens with no permission config at all"**
   in one sense: opencode's own `"ask"` action **fails closed** in a
   non-interactive context — there is no hang, no crash, just an
   auto-rejection with an explanatory stderr line.
2. **`ask` rule, with `--auto`:** the same command ran to completion, no
   stderr warning, `echo hello` actually executed (`state.status:"completed"`,
   `output:"hello\n"`). Confirms `--auto` converts `ask`→auto-approve.
3. **explicit `deny` rule, with `--auto`:** `gh issue list` under the
   `git-allow/gh-deny` agent **still failed** with the identical
   permission-rule error shown above, even with `--auto` passed. Confirms
   `--auto` does **not** override an explicit `deny` — it only resolves
   `ask` — matching the CLI help text verbatim ("auto-approve permissions
   that are not explicitly denied").
4. **No permission config at all** (the default `build` primary agent, no
   `opencode.jsonc` agent block): `bash` ran completely freely, no ask, no
   warning, no `--auto` needed. `opencode agent list` shows every built-in
   agent starts from a base rule `{"permission":"*","action":"allow","pattern":"*"}`
   — **opencode's out-of-the-box default is allow-everything.** This is the
   single most important finding for the deny-list flip issue: opencode does
   **not** ship a safe-by-default posture. coord must supply an explicit,
   correctly-ordered deny-shaped `opencode.jsonc`/`--agent` config to get any
   enforcement at all; enforcement is not automatic just because the
   *capability* exists.

## Operational finding not in the original scope, but load-bearing: stdout buffering

Discovered while trying to capture an "aborted mid-run" fixture. When
`opencode run --format json` writes to a **pipe/file redirect** (not a TTY),
its stdout is **fully block-buffered**, not line-buffered. A run that takes
~40+ seconds and makes multiple tool calls produced **zero bytes** in the
output file for the entire duration — `stat`/`wc -l` polled every 2 seconds
for 40 seconds showed `size=0 lines=0` throughout — with all output flushing
at once only when the process exited normally. A `SIGTERM` sent mid-run
killed the process with **no output at all** having been flushed (`wc -l` →
0), even though the run had been actively generating and calling tools for
several seconds.

**Consequence for coord:** any live-tailing approach (`parse_log(...,
tail_bytes=N)` polled while the worker is still running, as coord's reap
thread does for other providers) will see **nothing** until the opencode
process exits or the OS pipe buffer fills (~64KB) — "live" progress
monitoring is effectively unavailable for this backend as currently invoked.
If the follow-up issue wants live progress, it needs to either wrap the
invocation in something that forces line-buffered/unbuffered stdout (e.g.
`stdbuf -oL opencode run ...`, if that actually forces flushing through
opencode's own internal buffering — **not verified, flagged as an unknown
below**) or accept that progress is only visible after process exit.

## Every `ASSUMPTION:` in `coord/providers/opencode.py` — confirmed / corrected / unknown

| Assumption in the file | Status | Finding |
|---|---|---|
| `opencode` is on PATH (`DEFAULT_OPENCODE_BINARY`) | **confirmed** | binary name `opencode` is correct |
| `RESULT_MARKER = '"type":"session.complete"'` | **corrected** | no such event exists; real terminal signal is `step_finish` with `part.reason == "stop"` (success) or a top-level `error` event (failure) — see above |
| `opencode run [--model MODEL] [--session SESSION_ID] BRIEFING` invocation shape | **confirmed**, with one caveat | subcommand `run`, briefing as trailing positional, `--model`/`--session` all work as assumed; but if a future array-typed flag (e.g. `--file`) is ever added to `extra_args`, it must come **after** the briefing, not before (see flag table) |
| `--model` selects `provider/model` | **confirmed** | verified with `opencode/big-pickle` and `deepseek/deepseek-chat` |
| `--session SESSION_ID` resumes | **confirmed** | |
| `--attach <URL>` connects to a running server | **confirmed** | end-to-end tested against `opencode serve` |
| `initial_input()` returning `b""` (briefing on argv, not stdin) | **confirmed** | `run [message..]` is genuinely a positional argv message, no stdin protocol observed |
| `capabilities().enforces_deny_list=False` / "no equivalent in OpenCode's CLI" | **corrected** | false — opencode has a real per-tool `allow`/`ask`/`deny` permission system with bash command pattern matching, empirically proven above (see Permissions section) — the follow-up issue should design the deny-list mapping onto opencode's `agent.<name>.permission` config, watching the last-match-wins ordering trap |
| `capabilities().true_system_prompt=False` / no `--system-prompt` equivalent | **corrected** | `--agent <name>` selects an agent definition that can carry a `"prompt"` field acting as a real system prompt (confirmed via schema: `AgentConfig.prompt: string`); it is not literally a `--system-prompt` flag, but the capability exists and can be wired through a generated per-run `opencode.jsonc` or a temp agent file |
| `capabilities().cost_reporting=False` / usage field paths unverified | **corrected — paths now known, but shape is wrong** | usage/cost is real and present, but it is **per-`step_finish` event** (`part.tokens.{input,output,reasoning,cache.{read,write}}`, `part.cost`), not a single `usage` object on one final event; a correct implementation must sum across all `step_finish` events for the session |
| `capabilities().billing_mode="byo_key"` | **confirmed** | operator's own provider credentials (verified via `opencode providers list` / `opencode stats` showing real accumulated cost) |
| `capabilities().resume=True` via `--session` | **confirmed** | |
| `capabilities().inject=False` / no mid-session stdin injection | **still unknown** | not exercised in this pass — no evidence either way; `run` takes its message on argv, and no stdin-injection mechanism was discovered, but this was not specifically tested against a long-running attached session |
| `capabilities().human_attended_only=False` | **confirmed by inference, not directly tested** | `run --format json` behaves as pure batch automation in every capture (no TUI, no prompts blocking on a TTY; `ask` permissions fail closed rather than hang) — consistent with headless automation, but no explicit ToS-style check was performed since that's outside this issue's scope |
| Event shapes in `_update_opencode_summary` (`session.start`, `message.complete`, `session.complete`) | **corrected — none of these exist** | real shapes are `step_start`/`tool_use`/`text`/`step_finish`/`error`; running the *unmodified* `parse_log()` against the new real fixture confirms it extracts nothing (`session_id=None, model_used=None, num_turns=0, total_cost_usd=0.0`) — pinned by `test_opencode_parse_log_extracts_nothing_from_real_fixture` in `tests/test_providers.py` |

## Remaining unknowns (explicitly not resolved by this pass)

- **Mid-session stdin injection** (`capabilities().inject`): not exercised.
  No evidence for or against a message-injection mechanism into an
  already-running `run --format json` process.
- **Whether `stdbuf -oL` (or similar) actually forces per-line flushing**
  through opencode's own internal buffering layer, or whether the buffering
  observed is inside the Node/Bun runtime itself and unaffected by external
  `stdbuf` wrapping. Not tested.
- **Behavior on a genuinely long-running (multi-minute) session** — all
  captures here completed in well under a minute; timeout/reap interaction
  with the buffering finding above is inferred, not directly observed at
  coord's real worker-timeout scale.
- **`--variant` and `--thinking` flags** (model reasoning effort, thinking
  blocks) — present in `--help` output, not exercised; out of scope for the
  flag table this issue asked to verify but noted for completeness.
- **Whether `opencode.jsonc` at the repo root vs. a `--agent`-only CLI
  invocation is the right mechanism for coord to inject a deny-shaped
  permission config per-assignment** — both work, but which fits coord's
  per-run (not per-repo) invocation model better is a design question for the
  follow-up issue, not something this verification pass resolves.
- **`human_attended_only`** — no ToS-style check was performed (out of
  scope); the "confirmed by inference" note above should not be read as a
  legal/ToS determination.

## Non-interactive credential verification (#1777)

The rest of this document is #1703's pass (log/event schema). This section
adds a second, independent verification pass for #1777 — the ephemeral-Azure
prerequisite — answering the one question #1703 didn't touch: **can opencode
authenticate without `opencode auth login` writing `~/.local/share/opencode/auth.json`
interactively?** A VM that lives four hours cannot run an interactive login.

Same rigor as above: verified against the **real, pinned 1.18.11 binary**
(downloaded standalone via the official installer, `--version 1.18.11`, into
an isolated `$HOME` so it could not read or write the operator's real
`~/.local/share/opencode/auth.json`), not inferred from docs. Captured
2026-08-03.

### Finding: `OPENCODE_API_KEY` is a real, generic, env-var credential path

Static analysis of the shipped binary (it bundles the models.dev provider
catalog) shows every provider entry carries an `env` array naming the
environment variable(s) that satisfy it, e.g.:

```
anthropic:{id:"anthropic",env:["ANTHROPIC_API_KEY"],npm:"@ai-sdk/anthropic",...}
deepseek:{id:"deepseek",env:["DEEPSEEK_API_KEY"],npm:"@ai-sdk/openai-compatible",...}
opencode:{id:"opencode",env:["OPENCODE_API_KEY"],npm:"@ai-sdk/openai-compatible",
          api:"https://opencode.ai/zen/v1",name:"OpenCode Zen",...}
```

and a generic detection loop (not special-cased to one provider) runs at
startup: `for(let[z,X]of Object.entries(h))for(let J of X.env)if(process.env[J])G.push({provider:X.name||z,envVar:J})`.
This is exactly the mechanism the fleet's own fixtures already exercise —
`opencode/big-pickle` is a model on the `opencode` (OpenCode Zen) provider,
whose credential env var is `OPENCODE_API_KEY`.

This was then confirmed **live**, not just read out of the binary:

**1. `opencode providers list` (aliased `auth` — `opencode providers`'s
`--help` shows `[aliases: auth]`, so `opencode auth list` is the identical
command) with an isolated `$HOME` containing no `auth.json` at all, and only
`OPENCODE_API_KEY` exported:**

```
$ HOME=/tmp/oc-pin-test OPENCODE_API_KEY=<redacted> opencode providers list
┌  Credentials  ~/.local/share/opencode/auth.json
│
└  0 credentials

┌  Environment
│
●  OpenCode Go   OPENCODE_API_KEY
│
●  OpenCode Zen  OPENCODE_API_KEY
│
└  2 environment variables
```

Zero file-backed credentials, two env-detected ones. This satisfies the
issue's acceptance criterion literally: `opencode auth list` on a
freshly-booted worker (no login ever run) shows a working credential.

**2. A real `opencode run`, same isolated `$HOME`, same env var, no
`auth.json` anywhere on disk before or after:**

```
$ HOME=/tmp/oc-pin-test OPENCODE_API_KEY=<redacted> \
    opencode run --format json --model opencode/big-pickle \
    "reply with exactly the word: pong"
$ echo $?
0
```

Output stream (3 lines, matching the shapes already documented above):

```
{"type":"step_start", ...}
{"type":"text","part":{"text":"pong",...}}
{"type":"step_finish","part":{"reason":"stop",...}}
```

`find /tmp/oc-pin-test -iname auth.json` → no matches, before or after the
run. This is exactly the surface `OpenCodeProvider.parse_log` consumes
post-#1704: a session, and a terminal `step_finish` with `part.reason=="stop"`
— produced with **zero credential material ever touching disk**, which is
precisely what "arrives at boot, not baked into the image" requires.

**Conclusion: no `auth.json` is needed.** An `OPENCODE_API_KEY` environment
variable, present in the worker process's environment at the moment `opencode
run` executes, is sufficient — for the `opencode` (Zen) provider specifically,
which is what the fleet already uses for `opencode/big-pickle`. (Whether the
same env-var path is wired up for other providers, e.g. `deepseek`, is implied
by the same generic mechanism but was not separately re-verified live here —
out of scope for this pass, which only needed to prove *a* non-interactive
path exists.)

### Finding: the install location is fixed, and it is NOT already on a worker's PATH

The official installer (`curl -fsSL https://opencode.ai/install | bash`)
hardcodes `INSTALL_DIR=$HOME/.opencode/bin` — read from its source directly,
2026-08-03. Neither `--binary <path>` (changes the download source, not the
destination) nor any environment variable redirects it. `--no-modify-path`
only skips appending to interactive shell rc files (`.bashrc`/`.zshrc`/etc.),
which a systemd unit never sources anyway.

This is the concrete reason the standing fleet needed a
`20-opencode-path.conf` PATH drop-in for `coord-agent`: nothing else put
`~/.opencode/bin` on that unit's `Environment=PATH=` line. `provision-worker.sh`
avoids needing the same drop-in on ephemeral workers by symlinking
`~/.opencode/bin/opencode` into `~/.local/bin` at provision time — a directory
already on the `coord-agent` unit's PATH (`deploy/coord-agent.service`) because
the Claude Code CLI install already relies on it landing there via `npm config
set prefix ~/.local`.

### What #1777 does *not* resolve

The env var must still **reach** the worker process's environment at boot.
Today only `anthropic-api-key`, `github-token` and `tailscale-oauth-secret`
are fetched from Key Vault and exported by `coord-secrets` (cloud-init, the
**easy-azure** repo — out of this repo's reach). `bootstrap-shared.sh` now
prompts for a fourth secret, `opencode-api-key`, through the same `set_secret`
helper as the other three — but nothing on the worker side consumes it yet.
That is a **hand-off**, not a gap this issue closes: extending
`coord-secrets`/cloud-init to also export `OPENCODE_API_KEY` needs a change in
`easy-azure`, which this issue's worker cannot touch. See
`docs/EPHEMERAL_WORKERS.md` for the explicit note.

### How to reproduce the credential check

```bash
# Install the exact pinned version into an isolated HOME so it can't touch
# your real ~/.local/share/opencode/auth.json.
mkdir -p /tmp/oc-pin-test
HOME=/tmp/oc-pin-test bash -c "$(curl -fsSL https://opencode.ai/install)" \
    -- --version 1.18.11 --no-modify-path

export OPENCODE_API_KEY=<a real OpenCode Zen key>
HOME=/tmp/oc-pin-test /tmp/oc-pin-test/.opencode/bin/opencode providers list
# -> "0 credentials" under Credentials, "2 environment variables" under
#    Environment (OpenCode Go, OpenCode Zen), both keyed by OPENCODE_API_KEY

mkdir -p /tmp/oc-pin-throwaway && cd /tmp/oc-pin-throwaway && git init -q
HOME=/tmp/oc-pin-test /tmp/oc-pin-test/.opencode/bin/opencode run \
    --format json --model opencode/big-pickle "reply with exactly: pong"
# -> exit 0, step_start / text("pong") / step_finish(reason:"stop")
find /tmp/oc-pin-test -iname auth.json   # -> no matches
```

## How to reproduce

```bash
mkdir /tmp/oc-throwaway && cd /tmp/oc-throwaway && git init -q
echo 'def add(a, b):\n    return a + b\n' > math_utils.py
git add -A && git commit -q -m init

opencode run --format json --model opencode/big-pickle \
  "Add a subtract(a, b) function to math_utils.py that returns a - b. Keep it minimal." \
  > run.jsonl

cat > opencode.jsonc <<'EOF'
{
  "$schema": "https://opencode.ai/config.json",
  "agent": {
    "no-gh": {
      "mode": "primary",
      "prompt": "You are a coding assistant.",
      "permission": {
        "bash": {"*": "ask", "git *": "allow", "gh *": "deny"},
        "edit": "allow", "read": "allow"
      }
    }
  }
}
EOF
opencode run --format json --model opencode/big-pickle --agent no-gh \
  "Run 'gh issue list' via bash." > run-denied.jsonl
```

## Addendum (#1705): agent-file discovery, precedence, and a load-bearing PWD finding

Captured against the same `opencode` 1.18.11 binary (`dellserver`... this pass
run from a different fleet machine with the same version installed;
`opencode --version` reconfirmed `1.18.11` before capturing). Everything
below is a real run against the real binary, not inferred from docs — except
the OpenRouter upstream-routing mechanism, which is explicitly flagged as
NOT verified end-to-end (no OpenRouter credential was available on the
capturing machine — see `coord/agents/opencode/routing.jsonc`'s header
comment for what *was* verified: syntactic inertness against a
non-OpenRouter model).

**Markdown agent-file discovery requires an `agents/` (or singular `agent/`)
subdirectory — a flat file is invisible.** `https://opencode.ai/docs/agents`
documents `.opencode/agents/<name>.md` / `~/.config/opencode/agents/<name>.md`;
this was verified to also apply to a custom `OPENCODE_CONFIG_DIR`: a file at
`<dir>/agents/work.md` is discovered as agent `work`, but the same file at
`<dir>/work.md` (no subdirectory) is not discovered at all (confirmed via
`opencode agent list` showing no `work` entry). This is why
`coord/agents/opencode/agents/work.md` has the extra `agents/` path segment.

**`OPENCODE_CONFIG_DIR` outranks a same-named agent in the target
repo/worktree's own `.opencode/agents/`.** Verified by planting a
conflicting `.opencode/agents/work.md` (wide-open `bash`/`edit`/
`external_directory` permissions) inside a throwaway worktree that also had
`OPENCODE_CONFIG_DIR` pointed at coord's real `work.md`: the resolved rule
list (`opencode agent list`) and a live `gh --version` bash call were both
unaffected by the conflicting file — coord's deny-baseline rules still won.
Matters because the worktree belongs to a repo coord doesn't fully control.

**Load-bearing, previously-undocumented finding: opencode resolves its
working directory from the inherited `PWD` environment variable, not the
real process cwd.** `opencode run --help` documents a `--dir` flag ("directory
to run in") that was not exercised in #1703's flag-surface table at all.
Verified directly: `subprocess.Popen(argv, cwd=X, env={"PWD": stale, ...})`
made a `bash` tool's `pwd` call print `stale`, not `X`; deleting `PWD` from
the child env (or routing the same argv through `bash -c 'exec ...'`, which
resets `$PWD` to the real cwd on exec) made it print `X` correctly. This is
silently relied upon by coord's real dispatch path today, purely as a side
effect of `coord.agent._maybe_bash_wrap`'s `bash -c 'exec ...'` wrapper
(`bash_wrap_spawn`, default `True`) — added for the unrelated #299
daemon-spawn-freeze mitigation, not for this reason. See
`coord/providers/opencode.py`'s `capabilities()` docstring
(`enforces_deny_list` note) for the full citation and why this wasn't fixed
in `coord/providers/opencode.py` itself (the fix needs the assignment's
worktree path, which isn't available where the fix would need to live
without touching `coord/agent.py`, forbidden under #1705's briefing).

**OpenRouter upstream-routing pin mechanism — from decompiled binary
strings, NOT a live captured request (no OpenRouter credential available;
see requirement 3's own text acknowledging today's fleet has none).**
Extracting readable strings from the installed `opencode` binary
(`strings ~/.opencode/bin/opencode`) and reading the surrounding minified
source showed: (1) `provider.<id>.options` (a `ProviderConfig.options`
object, schema-open beyond the documented `apiKey`/`baseURL`/`timeout`
keys) is threaded into an AI-SDK `providerOptions.<id>` argument for every
request resolved against that provider, model-agnostic; (2) for
`id: "openrouter"` specifically (`npm: "@openrouter/ai-sdk-provider"`),
that provider's own `doGenerate`/`doStream` spreads
`providerOptions.openrouter` (minus a separately-handled `cacheControl`
key) directly into the request body sent to OpenRouter's REST API — which
is exactly where OpenRouter's own documented `provider: {order,
allow_fallbacks, ...}` field lives. `coord/agents/opencode/routing.jsonc`
sets `provider.openrouter.options.provider.allow_fallbacks = false` on
this basis. What *was* verified end-to-end: a real `opencode run` with
`OPENCODE_CONFIG` pointed at this file, against a non-OpenRouter model
(`opencode/big-pickle`), completed normally with no error — the dangling
config is inert for credentials that aren't OpenRouter, which is every
fleet machine today.
