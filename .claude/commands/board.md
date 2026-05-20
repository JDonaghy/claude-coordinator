Show the current assignment board.

1. Run `coord status` to get machine and assignment state.
2. Run `coord notify --dry-run 2>/dev/null || true` to check for pending notifications (if the flag exists, otherwise skip).
3. Format the output as a board:

```
| ID | Machine | Repo | Issue | Model | Status |
|----|---------|------|-------|-------|--------|
```

Include:
- All active assignments (running, pending)
- Recently completed assignments (last 5)
- Machine connectivity (online/offline)

If no assignments are active, say "Board is clear — all machines idle."
