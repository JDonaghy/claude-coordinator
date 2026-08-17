# portal-followup skill

**Trigger:** Investigating a customer-portal signoff/event that "hasn't shown up"
— a submission stuck on an old status, a customer event (e.g.
`signoff.approved`) that doesn't seem to have registered, or any troubleshooting
of `coord portal` state.

**Purpose:** Get a straight answer out of `coord portal outbox`/`events`/`sync`
without re-discovering #2336 the hard way — a normal-looking "nothing pending"
from these commands used to be indistinguishable from "the bridge genuinely has
nothing pending," because they read/write **the local machine's**
`~/.coord/coord.db` directly, and only the daemon host's copy is real.

---

## The one thing to know

`coord portal status` / `heartbeat` / `push` touch only the config + the portal
API — safe to run from anywhere.

`coord portal sync` / `outbox` / `events` / `enqueue-*` / `requeue` touch the
daemon's own `~/.coord/coord.db`. **Run them on the daemon host directly**
(`ssh <daemon-host>` first — check `~/.coord/client.toml`'s `board_service` for
which host that is, or ask `coord status` on this machine, which already routes
through it).

Since #2336, running one of these from a thin client (any machine with
`board_service` configured) no longer silently reads that machine's empty local
DB — it refuses outright:

```
Error: coord portal outbox must run on the daemon host (dellserver) — this
machine's ~/.coord/coord.db is not where the bridge lives (board_service is
configured in ~/.coord/client.toml, making this a thin client). Run it over
`ssh` on the daemon host instead. See coord/skills/portal-followup/SKILL.md.
```

If you see that error, you have your answer: SSH to the named host and re-run
the same command there.

## Steps

1. **Identify the daemon host.** `cat ~/.coord/client.toml` (if present) shows
   `board_service = "http://<host>:<port>"` — that host is where the real
   `coord.db` lives. If this machine has no `client.toml` at all, it may *be*
   the daemon host already; try the command locally first.

2. **Run the portal command on that host** (over `ssh`, or directly if you're
   already on it):

   ```
   ssh <daemon-host> 'coord portal outbox --all'
   ssh <daemon-host> 'coord portal events'
   ssh <daemon-host> 'coord portal sync --json'
   ```

3. **Cross-check credentials separately if a push/heartbeat looks like a 401.**
   `coord portal status --json` reports `credentials_set: false` whenever
   `BRIDGE_CLIENT_ID`/`BRIDGE_CLIENT_SECRET` are unset in *this shell's*
   environment, even if `coordinator.yml` has non-empty `${VAR}` strings for
   them (#2336) — a bare interactive `ssh <host> '...'` does not source
   `~/.coord/coord-serve.env`, but the actual `coord-serve` systemd unit does
   via `EnvironmentFile=`. A 401 from a manual `ssh` + `coord portal heartbeat`
   check does not necessarily mean the credential itself is bad — check
   `credentials_set` first before assuming the secret rotated.

4. **If a submission's outbox row is `HELD`,** that's the #835 ordering guard
   working as intended (announcing a status like `awaiting-signoff` before its
   design round applied would email the customer toward an empty screen), not
   a bug — resolve the blocking condition rather than trying to force the push.

5. **If a row is terminal (retired) after burning its retry budget,** fix the
   underlying cause first, then `coord portal requeue <submission_id> <seq>`
   (find the seq with `coord portal outbox --all`) — also a daemon-host command.

## Rules

- Never try to work around the "must run on the daemon host" error by editing
  `~/.coord/client.toml` to unset `board_service` on a thin client — that
  changes what *every other* `coord` command on that machine reads too, not
  just `portal`.
- Prefer `coord portal enqueue-status`/`enqueue-design-round`/`enqueue-question`
  over `coord portal push` for anything the customer will see — `push` bypasses
  the #835 ordering guard entirely.
