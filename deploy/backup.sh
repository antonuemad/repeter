#!/usr/bin/env bash
# Repeater backups. bootstrap.sh installs it as a daily cron job (/etc/cron.d/repeater-backup).
#
#   sudo bash backup.sh                      dump the database to /srv/repeater/backups, delete dumps
#                                            older than BACKUP_RETENTION_DAYS (14), then copy off-site
#                                            with rclone when RCLONE_REMOTE is set in deploy/.env
#   sudo bash backup.sh restore FILE --yes   restore a dump: stops web and worker, loads FILE,
#                                            runs migrations, starts them again
#
# Settings come from deploy/.env: BACKUP_RETENTION_DAYS, RCLONE_REMOTE,
# RCLONE_MEDIA_SYNC (weekly | daily | off), REPEATER_MEDIA_DIR.
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"
BACKUP_DIR="${REPEATER_BACKUP_DIR:-/srv/repeater/backups}"
PREFIX="repeater-db-"
RCLONE_TIMEOUTS=(--contimeout 30s --timeout 5m --retries 3)
TMP_DUMP=""

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
warn() { printf '%s WARNING: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2; }
die() {
    printf '%s ERROR: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2
    exit 1
}

# Value of KEY in the env file (last uncommented assignment, surrounding quotes removed).
env_get() {
    local key="$1" file="$2"
    [[ -f "$file" ]] || return 0
    KEY="$key" awk -v q="'" '
        index($0, ENVIRON["KEY"] "=") == 1 { v = substr($0, length(ENVIRON["KEY"]) + 2); found = 1 }
        END {
            if (!found) exit 0
            n = length(v)
            if (n >= 2 && ((substr(v, 1, 1) == q && substr(v, n, 1) == q) || (substr(v, 1, 1) == "\"" && substr(v, n, 1) == "\"")))
                v = substr(v, 2, n - 2)
            printf "%s", v
        }' "$file"
}

compose() {
    docker compose --project-directory "$SCRIPT_DIR" -f "$SCRIPT_DIR/docker-compose.yml" --env-file "$ENV_FILE" "$@"
}

require_db_running() {
    [[ -f "$ENV_FILE" ]] || die "$ENV_FILE is missing"
    [[ -n "$(compose ps --status running -q db 2>/dev/null)" ]] || die "the db container is not running (docker compose ps)"
}

retention_days() {
    local days
    days="$(env_get BACKUP_RETENTION_DAYS "$ENV_FILE")"
    days="${days:-14}"
    [[ "$days" =~ ^[0-9]+$ && "$days" -ge 1 ]] || die "BACKUP_RETENTION_DAYS must be a positive whole number"
    printf '%s' "$days"
}

dump_database() {
    local stamp final
    stamp="$(date -u +%Y%m%dT%H%M%SZ)"
    final="$BACKUP_DIR/${PREFIX}${stamp}.sql.gz"
    TMP_DUMP="$(mktemp "$BACKUP_DIR/.${PREFIX}${stamp}.XXXXXX")"
    trap 'rm -f "$TMP_DUMP"' EXIT
    # --clean --if-exists makes the dump replace existing objects when restored.
    compose exec -T db pg_dump -U repeater -d repeater --no-owner --no-privileges --clean --if-exists |
        gzip -6 >"$TMP_DUMP" || die "pg_dump failed; no backup written"
    gzip -t "$TMP_DUMP" || die "the new dump is not a valid gzip file"
    # Only the header is read; the process substitution keeps gzip's SIGPIPE out of pipefail.
    grep -q 'PostgreSQL database dump' < <(gzip -dc "$TMP_DUMP" | head -c 4096) ||
        die "the new dump is empty or not a pg_dump file"
    chmod 600 "$TMP_DUMP"
    mv -f "$TMP_DUMP" "$final"
    trap - EXIT
    TMP_DUMP=""
    log "database dump written: $final ($(du -h "$final" | cut -f1))"
}

# Runs only after a successful dump, so a broken backup never deletes the last good ones.
prune_old() {
    local days="$1"
    find "$BACKUP_DIR" -maxdepth 1 -type f -name "${PREFIX}*.sql.gz" -mmin "+$((days * 1440))" -print -delete |
        while read -r f; do log "deleted old dump $f"; done
}

offsite_copy() {
    local remote media_mode media_dir
    remote="$(env_get RCLONE_REMOTE "$ENV_FILE")"
    [[ -n "$remote" ]] || return 0
    command -v rclone >/dev/null || die "RCLONE_REMOTE is set but rclone is not installed (apt-get install rclone, then rclone config)"
    log "copying dumps to $remote/db"
    rclone sync "$BACKUP_DIR" "$remote/db" --include "${PREFIX}*.sql.gz" "${RCLONE_TIMEOUTS[@]}" ||
        die "rclone sync of the database dumps failed"
    media_mode="$(env_get RCLONE_MEDIA_SYNC "$ENV_FILE")"
    media_mode="${media_mode:-weekly}"
    case "$media_mode" in
        off) return 0 ;;
        weekly) [[ "$(date +%u)" == 7 ]] || return 0 ;;
        daily) ;;
        *) die "RCLONE_MEDIA_SYNC must be weekly, daily or off" ;;
    esac
    media_dir="$(env_get REPEATER_MEDIA_DIR "$ENV_FILE")"
    media_dir="${media_dir:-/srv/repeater/media}"
    log "copying media to $remote/media"
    rclone sync "$media_dir" "$remote/media" --exclude "uploads/**" --exclude ".cookies/**" "${RCLONE_TIMEOUTS[@]}" ||
        die "rclone sync of the media library failed"
}

backup() {
    install -d -m 0700 "$BACKUP_DIR"
    exec 9>"$BACKUP_DIR/.backup.lock"
    flock -n 9 || die "another backup is running"
    require_db_running
    local days
    days="$(retention_days)"
    dump_database
    prune_old "$days"
    offsite_copy
    log "backup finished"
}

restore() {
    local file="${1:-}" confirm="${2:-}"
    [[ -n "$file" ]] || die "usage: backup.sh restore FILE --yes"
    [[ -f "$file" ]] || die "no such file: $file"
    [[ "$confirm" == --yes ]] || die "restoring replaces the whole database; add --yes to confirm"
    gzip -t "$file" || die "$file is not a valid gzip file"
    require_db_running
    log "stopping web and worker"
    compose stop web worker
    log "restoring $file"
    if ! gzip -dc "$file" | compose exec -T db psql -U repeater -d repeater -v ON_ERROR_STOP=1 -q >/dev/null; then
        warn "the restore failed; web and worker stay stopped. Fix the problem and run the restore again."
        exit 1
    fi
    log "bringing the schema up to date and starting web and worker"
    compose run --rm --no-TTY migrate
    compose up -d web worker
    log "restore finished"
}

main() {
    case "${1:-backup}" in
        backup) backup ;;
        restore)
            shift
            restore "$@"
            ;;
        -h | --help) awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0" ;;
        *) die "unknown command: $1 (see --help)" ;;
    esac
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
