#!/usr/bin/env bash
#
# Disaster-recovery restore for leads.db from the Litestream S3 replica.
#
# SAFE BY DEFAULT: restores to a timestamped file under /tmp and verifies it,
# WITHOUT touching the live database. Use --in-place only when you actually mean
# to replace production — it stops the services, backs up the current file, and
# restarts everything after a successful, integrity-checked restore.
#
# Usage:
#   ./restore.sh                 # dry restore to /tmp, verify, leave prod alone
#   ./restore.sh --in-place      # replace the live DB (stops/starts services)
#
# Env overrides:
#   LITESTREAM_CONFIG  (default: /etc/litestream.yml)
#   DB_PATH            (default: /home/ubuntu/automatizacionMB/data/leads.db)
#   APP_SERVICE        (default: prospeccion.service)
#   LITESTREAM_SERVICE (default: litestream.service)

set -euo pipefail

LITESTREAM_CONFIG="${LITESTREAM_CONFIG:-/etc/litestream.yml}"
DB_PATH="${DB_PATH:-/home/ubuntu/automatizacionMB/data/leads.db}"
APP_SERVICE="${APP_SERVICE:-prospeccion.service}"
LITESTREAM_SERVICE="${LITESTREAM_SERVICE:-litestream.service}"

IN_PLACE=0
[[ "${1:-}" == "--in-place" ]] && IN_PLACE=1

log() { printf '\n\033[1;34m==>\033[0m %s\n' "$*"; }
die() { printf '\n\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

command -v litestream >/dev/null || die "litestream is not installed or not on PATH."
command -v sqlite3   >/dev/null || die "sqlite3 is not installed."

# ── Verify a restored file: integrity + a sanity read of the business state ────
verify_db() {
    local file="$1"
    [[ -s "$file" ]] || die "Restored file is missing or empty: $file"

    local integrity
    integrity="$(sqlite3 "$file" 'PRAGMA integrity_check;')"
    [[ "$integrity" == "ok" ]] || die "Integrity check FAILED: $integrity"

    # suppression_list is the row we most need intact (CAN-SPAM). Read it.
    local suppressed leads
    suppressed="$(sqlite3 "$file" 'SELECT COUNT(*) FROM suppression_list;')"
    leads="$(sqlite3 "$file" 'SELECT COUNT(*) FROM leads;')"
    log "Verified OK — integrity=ok, leads=$leads, suppression_list=$suppressed"
}

if [[ "$IN_PLACE" -eq 0 ]]; then
    OUT="/tmp/leads_restored_$(date +%Y%m%d_%H%M%S).db"
    log "Safe restore (no impact on production) → $OUT"
    litestream restore -config "$LITESTREAM_CONFIG" -o "$OUT" "$DB_PATH"
    verify_db "$OUT"
    log "Done. Inspect $OUT. Re-run with --in-place to replace the live DB."
    exit 0
fi

# ── In-place restore: replace production ───────────────────────────────────────
log "IN-PLACE restore requested — this will REPLACE $DB_PATH"
read -r -p "Type 'RESTORE' to confirm: " confirm
[[ "$confirm" == "RESTORE" ]] || die "Aborted — confirmation not given."

log "Stopping services so nothing writes during the swap..."
sudo systemctl stop "$APP_SERVICE"        || die "Could not stop $APP_SERVICE"
sudo systemctl stop "$LITESTREAM_SERVICE" || die "Could not stop $LITESTREAM_SERVICE"

# Back up whatever is currently on disk before overwriting it.
if [[ -f "$DB_PATH" ]]; then
    BACKUP="${DB_PATH}.broken_$(date +%Y%m%d_%H%M%S)"
    log "Backing up current file → $BACKUP"
    mv "$DB_PATH" "$BACKUP"
    # WAL/SHM sidecars would shadow the restored DB — move them aside too.
    [[ -f "${DB_PATH}-wal" ]] && mv "${DB_PATH}-wal" "${BACKUP}-wal"
    [[ -f "${DB_PATH}-shm" ]] && mv "${DB_PATH}-shm" "${BACKUP}-shm"
fi

log "Restoring from S3 replica..."
litestream restore -config "$LITESTREAM_CONFIG" -o "$DB_PATH" "$DB_PATH"
verify_db "$DB_PATH"

log "Restarting services..."
sudo systemctl start "$LITESTREAM_SERVICE" || die "Could not start $LITESTREAM_SERVICE"
sudo systemctl start "$APP_SERVICE"        || die "Could not start $APP_SERVICE"

log "In-place restore complete. Live DB is back online and replicating."
