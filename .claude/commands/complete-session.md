Gracefully end the current coordinator session.

1. **Check for active work:**
   ```bash
   coord status
   ```
   If assignments are still running, ask: "N assignments still running. Wait for them, stop them, or shut down anyway?"
   - Wait: run `coord wait <id>` for each active assignment
   - Stop: run `coord stop <id>` for each active assignment
   - Shut down anyway: continue (workers keep running on the agent server)

2. **Post pending notifications:**
   ```bash
   coord notify
   ```

3. **Run session shutdown:**
   ```bash
   coord done
   ```
   This pulls repos, runs housekeeping commands, and saves session state.

4. **Show session summary:**
   ```bash
   coord session
   ```

5. **Check for unresolved work:**
   If any assignments are failed or stuck, note: "N assignments failed/stuck and weren't resolved. They'll show up on next startup via `coord resume`."

6. Close with: "Session complete. Board state saved. Safe to close."
