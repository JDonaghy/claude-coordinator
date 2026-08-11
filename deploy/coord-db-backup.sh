#!/usr/bin/env bash
# Interim coord.db snapshot to the external SSD (pending #1822).
#
# VACUUM INTO, not cp: it takes a consistent snapshot of a live SQLite database
# while coord-serve keeps writing. A plain cp of a WAL-mode db under load can
# capture a torn file, which is the failure mode you only discover at restore.
#
# Every snapshot is integrity-checked before it is allowed to count, and a
# failed check leaves the snapshot on disk named .REJECTED so it can be looked
# at rather than silently deleted.
#
# NOT a substitute for off-box backup: this protects against db corruption, a
# bad migration, accidental deletion and OS-disk failure. It does NOT protect
# against the machine being lost, stolen or burned. See #1822.
set -uo pipefail

SRC="${COORD_DB:-$HOME/.coord/coord.db}"
DEST_DIR="${COORD_BACKUP_DIR:-/media/crucial/coord-backups}"
RETAIN="${COORD_BACKUP_RETAIN:-168}"        # hourly x 7 days
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="$DEST_DIR/coord.db.$STAMP"

fail() { echo "coord-db-backup: FAILED: $*" >&2; exit 1; }

[ -f "$SRC" ] || fail "source db not found: $SRC"

# The mount must actually be a mount. If the SSD is unplugged, /media/crucial
# is still a directory on the root filesystem, and we would cheerfully write
# "backups" onto the very disk we are protecting against.
mountpoint -q "$(dirname "$DEST_DIR")" || fail "$(dirname "$DEST_DIR") is not a mountpoint — external SSD not mounted"

mkdir -p "$DEST_DIR" || fail "cannot create $DEST_DIR"

sqlite3 "$SRC" "VACUUM INTO '$OUT';" || fail "VACUUM INTO failed"

CHECK="$(sqlite3 "$OUT" 'PRAGMA integrity_check;' 2>&1)"
if [ "$CHECK" != "ok" ]; then
  mv "$OUT" "$OUT.REJECTED"
  fail "integrity_check on snapshot: $CHECK (kept as $OUT.REJECTED)"
fi

# Prove it is a coord db and not an empty file that passed integrity_check.
ROWS="$(sqlite3 "$OUT" 'SELECT COUNT(*) FROM assignments;' 2>&1)" || fail "snapshot has no assignments table: $ROWS"
case "$ROWS" in ''|*[!0-9]*) fail "unexpected assignments count: $ROWS";; esac
[ "$ROWS" -gt 0 ] || fail "snapshot has 0 assignments — refusing to count this as a backup"

ln -sfn "$OUT" "$DEST_DIR/coord.db.latest"

# Prune oldest beyond RETAIN. Never touches .REJECTED files.
mapfile -t OLD < <(ls -1 "$DEST_DIR"/coord.db.2* 2>/dev/null | grep -v '\.REJECTED$' | sort | head -n -"$RETAIN")
for f in "${OLD[@]:-}"; do [ -n "$f" ] && rm -f "$f"; done

SIZE="$(du -h "$OUT" | cut -f1)"
echo "coord-db-backup: ok $OUT ($SIZE, ${ROWS} assignments, $(ls -1 "$DEST_DIR"/coord.db.2* 2>/dev/null | grep -vc '\.REJECTED$') snapshots retained)"
