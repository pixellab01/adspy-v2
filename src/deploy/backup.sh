#!/usr/bin/env bash
# =============================================================================
#  AdSpy v2 - nightly backup of the SQLite datasets. Safe while the app runs.
#
#      sudo -u adspy /opt/adspy2/deploy/backup.sh            # run it once by hand
#      sudo -u adspy crontab -e                              # then add:
#      15 3 * * * /opt/adspy2/deploy/backup.sh >> /opt/adspy2/logs/backup.log 2>&1
#
#  WHY NOT cp: the database runs in WAL mode. The newest writes live in the
#  "-wal" file next to it, and a cp of the main file taken while the app is
#  writing is either missing them or torn in the middle of a page - a backup
#  that looks fine and does not open. SQLite's own online backup (".backup",
#  the same API Python exposes as Connection.backup) takes a consistent
#  snapshot without stopping the app and without blocking its single writer.
#
#  What it does, per dataset file that exists (OLD and NEW):
#     snapshot -> quick_check the snapshot -> gzip -> delete backups older than
#     KEEP_DAYS. A snapshot that fails the check is deleted and the script exits
#     non-zero, so cron mails / logs it instead of rotating good backups away.
#
#  Settings (environment, all optional):
#     ADSPY2_ROOT        default: the folder above this script
#     ADSPY2_BACKUP_DIR  default: $ADSPY2_ROOT/data/backups/nightly
#     KEEP_DAYS          default: 14
#
#  A backup on the same disk protects against a bad update, not against a dead
#  VPS. Copy the newest file off the server now and then (see DEPLOY-HI.md).
# =============================================================================
set -euo pipefail
umask 077

ROOT="${ADSPY2_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
DATA_DIR="$ROOT/data"
OUT_DIR="${ADSPY2_BACKUP_DIR:-$DATA_DIR/backups/nightly}"
KEEP_DAYS="${KEEP_DAYS:-14}"
STAMP="$(date +%Y%m%d-%H%M%S)"
PY="$ROOT/.venv/bin/python"
[[ -x "$PY" ]] || PY="$(command -v python3 || true)"

say() { printf '%s backup: %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }
die() { say "FAILED: $*" >&2; exit 1; }

[[ -d "$DATA_DIR" ]] || die "no data directory at $DATA_DIR"
[[ "$KEEP_DAYS" =~ ^[0-9]+$ ]] || die "KEEP_DAYS must be a number"
mkdir -p "$OUT_DIR"

# One run at a time: a slow backup must not be overtaken by the next night's.
LOCK="$OUT_DIR/.backup.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  die "another backup is running (or crashed) - remove $LOCK if no backup is in progress"
fi
trap 'rmdir "$LOCK" 2>/dev/null || true' EXIT

# snapshot SRC DEST  - online, consistent, WAL-safe.
snapshot() {
  local src="$1" dest="$2"
  if command -v sqlite3 >/dev/null 2>&1; then
    # No -readonly here: the shell would open the DESTINATION read-only too.
    # .backup only ever reads the source.
    sqlite3 "$src" ".timeout 30000" ".backup '$dest'"
  elif [[ -n "$PY" ]]; then
    "$PY" - "$src" "$dest" <<'PY'
import sqlite3, sys
src = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True, timeout=30)
dst = sqlite3.connect(sys.argv[2])
with dst:
    src.backup(dst, pages=2048, sleep=0.05)
dst.close(); src.close()
PY
  else
    die "neither sqlite3 nor python3 found - sudo apt install -y sqlite3"
  fi
}

# verify FILE - the snapshot must open and pass SQLite's own consistency check.
# The snapshot inherits WAL mode from the live file; it is switched to a plain
# rollback journal first so the .gz is ONE self-contained file with no -wal/-shm
# beside it.
verify() {
  local file="$1" answer
  if command -v sqlite3 >/dev/null 2>&1; then
    sqlite3 "$file" "PRAGMA journal_mode=DELETE;" >/dev/null 2>&1 || return 1
    answer="$(sqlite3 "$file" "PRAGMA quick_check;" 2>&1 | head -n1)"
  else
    answer="$("$PY" - "$file" <<'PY' 2>&1 | head -n1
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
conn.execute("PRAGMA journal_mode=DELETE")
print(conn.execute("PRAGMA quick_check").fetchone()[0])
conn.close()
PY
)"
  fi
  [[ "$answer" == "ok" ]]
}

made=0
for name in adspy2.sqlite3 adspy2-new.sqlite3; do
  src="$DATA_DIR/$name"
  [[ -f "$src" ]] || continue
  dest="$OUT_DIR/${name%.sqlite3}-$STAMP.sqlite3"

  say "snapshot $name"
  snapshot "$src" "$dest" || { rm -f "$dest"; die "could not snapshot $src"; }
  if ! verify "$dest"; then
    rm -f "$dest"
    die "snapshot of $name did not pass quick_check - old backups were NOT rotated"
  fi
  rm -f "$dest-wal" "$dest-shm"      # journal_mode is DELETE by now; these are husks
  gzip -f "$dest"
  say "wrote ${dest}.gz ($(du -h "${dest}.gz" | cut -f1))"
  made=$((made + 1))
done

# Which dataset was live is one line in a sidecar file; keep it with the backup.
if [[ -f "$DATA_DIR/active_dataset.txt" ]]; then
  cp "$DATA_DIR/active_dataset.txt" "$OUT_DIR/active_dataset-$STAMP.txt"
fi

(( made > 0 )) || die "no dataset file found in $DATA_DIR - nothing was backed up"

# Rotate only after at least one good backup exists from this run.
find "$OUT_DIR" -maxdepth 1 -type f \( -name 'adspy2*-*.sqlite3.gz' -o -name 'active_dataset-*.txt' \) \
     -mtime +"$KEEP_DAYS" -print -delete | while read -r gone; do say "rotated out $gone"; done

say "done - $made file(s), keeping $KEEP_DAYS days in $OUT_DIR"
