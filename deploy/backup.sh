#!/usr/bin/env bash
# Repeater backups. bootstrap.sh installs it as a daily cron job (/etc/cron.d/repeater-backup).
#
#   sudo bash backup.sh                      dump the database to /srv/repeater/backups, delete dumps
#                                            older than BACKUP_RETENTION_DAYS (14), then copy off-site
#                                            with rclone when RCLONE_REMOTE is set in deploy/.env.
#                                            A failure sends a Telegram alert (when configured).
#   sudo bash backup.sh pre-deploy           local dump only; deploy.sh runs it before it migrates
#                                            (repeater-predeploy-*, the newest 5 are kept)
#   sudo bash backup.sh status [--alert]     age of the last successful backup; exit 1 when older than
#                                            36 hours (--alert also tells Telegram)
#   sudo bash backup.sh restore FILE --yes [--resume]
#                                            replace the whole database with FILE. All in one
#                                            transaction (a failure leaves the live database untouched),
#                                            then runs the migrations, PAUSES EVERY WORKSPACE and starts
#                                            web only. The worker starts only with --resume, and even
#                                            then nothing is published until the workspaces are resumed
#                                            in the dashboard. REPEATER_IMAGE_TAG=previous selects the
#                                            release whose migrations are applied (rollbacks).
#
# Settings come from deploy/.env: BACKUP_RETENTION_DAYS, RCLONE_REMOTE, RCLONE_MEDIA_SYNC
# (weekly | daily | off), BACKUP_REMOTE_RETENTION_DAYS, RCLONE_MAX_DELETE, RCLONE_TRASH_DAYS,
# REPEATER_MEDIA_DIR, REPEATER_BACKUP_DIR, REPEATER_TELEGRAM_BOT_TOKEN / _CHAT_ID.
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"
PREFIX="repeater-db-"
PREDEPLOY_PREFIX="repeater-predeploy-"
PRERESTORE_PREFIX="repeater-prerestore-"
PREDEPLOY_KEEP=5
PRERESTORE_KEEP=3
# Safety dumps also expire by age: the privacy policy says data deleted from the service leaves
# the backups within 30 days (the off-site dumps expire after BACKUP_REMOTE_RETENTION_DAYS, 30).
SAFETY_DUMP_MAX_DAYS=30
STALE_AFTER_HOURS=36
RCLONE_TIMEOUTS=(--contimeout 30s --timeout 5m --retries 3)
TMP_DUMP=""
DUMP_FILE=""
FAILURE=""
ALERT_ON_FAILURE=0

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
warn() { printf '%s WARNING: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2; }
die() {
    FAILURE="$*"
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

BACKUP_DIR="${REPEATER_BACKUP_DIR:-$(env_get REPEATER_BACKUP_DIR "$ENV_FILE")}"
BACKUP_DIR="${BACKUP_DIR:-/srv/repeater/backups}"
STATUS_DIR="$BACKUP_DIR/status"
STATUS_FILE="$STATUS_DIR/last_success"

# The worker's stop_grace_period is its in-flight job grace (REPEATER_WORKER_SHUTDOWN_GRACE_S,
# default 600) plus 60 s; compose cannot add, so the scripts hand it the sum.
worker_stop_grace() {
    local grace
    grace="$(env_get REPEATER_WORKER_SHUTDOWN_GRACE_S "$ENV_FILE")"
    [[ "$grace" =~ ^[0-9]+$ ]] || grace=600
    printf '%s' "$((grace + 60))"
}

compose() {
    WORKER_STOP_GRACE_S="$(worker_stop_grace)" \
        docker compose --project-directory "$SCRIPT_DIR" -f "$SCRIPT_DIR/docker-compose.yml" --env-file "$ENV_FILE" "$@"
}

require_db_running() {
    [[ -f "$ENV_FILE" ]] || die "$ENV_FILE is missing"
    [[ -n "$(compose ps --status running -q db 2>/dev/null)" ]] || die "the db container is not running (docker compose ps)"
}

# A positive whole number from the env file (or the default).
env_days() {
    local key="$1" default="$2" value
    value="$(env_get "$key" "$ENV_FILE")"
    value="${value:-$default}"
    [[ "$value" =~ ^[0-9]+$ && "$value" -ge 1 ]] || die "$key must be a positive whole number"
    printf '%s' "$value"
}

# --------------------------------------------------------------------------- alerts and status

# Telegram message through the bot configured for the dashboard alerts. The token is read from
# deploy/.env and handed to curl on stdin (--config -), so it never appears in `ps` or in any log.
notify() {
    local text token chat
    token="$(env_get REPEATER_TELEGRAM_BOT_TOKEN "$ENV_FILE")"
    chat="$(env_get REPEATER_TELEGRAM_CHAT_ID "$ENV_FILE")"
    [[ -n "$token" && -n "$chat" ]] || return 0
    command -v curl >/dev/null || return 0
    [[ "$token" =~ ^[0-9]+:[A-Za-z0-9_-]+$ && "$chat" =~ ^-?[A-Za-z0-9_@]+$ ]] || {
        warn "the Telegram settings in deploy/.env look malformed; no alert sent"
        return 0
    }
    # The text sits inside a quoted curl config value: no quotes, backslashes or line breaks.
    text="$(printf '%s' "$1" | tr -d '"\\\r' | tr '\n' ' ' | cut -c1-400)"
    printf 'url = "https://api.telegram.org/bot%s/sendMessage"\ndata-urlencode = "chat_id=%s"\ndata-urlencode = "text=%s"\n' \
        "$token" "$chat" "$text" |
        curl -fsS --max-time 20 --config - >/dev/null 2>&1 || warn "the Telegram alert could not be delivered"
}

host_name() { hostname 2>/dev/null || echo "the Repeater server"; }

on_exit() {
    local status=$?
    [[ -z "$TMP_DUMP" ]] || rm -f "$TMP_DUMP"
    if ((status != 0 && ALERT_ON_FAILURE)); then
        notify "Repeater backup FAILED on $(host_name): ${FAILURE:-unexpected error, see journalctl -t repeater-backup}"
    fi
}

record_success() {
    install -d -m 0755 "$STATUS_DIR"
    local tmp
    tmp="$(mktemp "$STATUS_DIR/.last_success.XXXXXX")"
    printf '%s %s\n' "$(date -u +%s)" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >"$tmp"
    chmod 0644 "$tmp"
    mv -f "$tmp" "$STATUS_FILE"
}

# Exit 1 (and optionally a Telegram alert) when the last successful backup is too old. Without a
# record the clock starts when this was first asked, so a fresh install gets 36 hours.
status_cmd() {
    local alert=0 epoch now age_hours note=""
    case "${1:-}" in
        "") ;;
        --alert) alert=1 ;;
        *) die "unknown option for status: $1" ;;
    esac
    install -d -m 0700 "$BACKUP_DIR"
    install -d -m 0755 "$STATUS_DIR"
    now="$(date -u +%s)"
    if [[ -s "$STATUS_FILE" ]] && epoch="$(cut -d' ' -f1 "$STATUS_FILE")" && [[ "$epoch" =~ ^[0-9]+$ ]]; then
        :
    else
        if [[ ! -s "$STATUS_DIR/.first_seen" ]]; then date -u +%s >"$STATUS_DIR/.first_seen"; fi
        epoch="$(cat "$STATUS_DIR/.first_seen")"
        note=" (no successful backup recorded yet)"
    fi
    age_hours=$(((now - epoch) / 3600))
    if ((age_hours >= STALE_AFTER_HOURS)); then
        FAILURE="no successful backup for ${age_hours} hours${note}; run: sudo bash $SCRIPT_DIR/backup.sh and read journalctl -t repeater-backup"
        warn "$FAILURE"
        if ((alert)); then notify "Repeater backup is STALE on $(host_name): $FAILURE"; fi
        return 1
    fi
    log "the last successful backup is ${age_hours} hours old${note}"
}

# --------------------------------------------------------------------------- dumps

# Writes $BACKUP_DIR/<prefix><stamp>.sql.gz atomically and sets DUMP_FILE. With SOFT_FAIL=1 a
# failure is a warning and a non-zero return instead of the end of the script.
SOFT_FAIL=0
dump_failed() {
    rm -f "$TMP_DUMP"
    TMP_DUMP=""
    if ((SOFT_FAIL)); then
        warn "$*"
    else
        die "$*"
    fi
    return 1
}

dump_database() {
    local prefix="$1" stamp final
    stamp="$(date -u +%Y%m%dT%H%M%SZ)"
    final="$BACKUP_DIR/${prefix}${stamp}.sql.gz"
    TMP_DUMP="$(mktemp "$BACKUP_DIR/.${prefix}${stamp}.XXXXXX")"
    # --clean --if-exists makes the dump replace existing objects when it is loaded by hand.
    compose exec -T db pg_dump -U repeater -d repeater --no-owner --no-privileges --clean --if-exists |
        gzip -6 >"$TMP_DUMP" || {
        dump_failed "pg_dump failed; no backup written"
        return 1
    }
    gzip -t "$TMP_DUMP" || {
        dump_failed "the new dump is not a valid gzip file"
        return 1
    }
    # Only the header is read; the process substitution keeps gzip's SIGPIPE out of pipefail.
    grep -q 'PostgreSQL database dump' < <(gzip -dc "$TMP_DUMP" | head -c 4096) || {
        dump_failed "the new dump is empty or not a pg_dump file"
        return 1
    }
    chmod 600 "$TMP_DUMP"
    mv -f "$TMP_DUMP" "$final"
    TMP_DUMP=""
    DUMP_FILE="$final"
    log "database dump written: $final ($(du -h "$final" | cut -f1))"
}

# Runs only after a successful dump, so a broken backup never deletes the last good ones.
prune_old() {
    local days="$1"
    find "$BACKUP_DIR" -maxdepth 1 -type f -name "${PREFIX}*.sql.gz" -mmin "+$((days * 1440))" -print -delete |
        while read -r f; do log "deleted old dump $f"; done
}

# Deletes the safety dumps (pre-deploy, pre-restore) older than SAFETY_DUMP_MAX_DAYS.
prune_safety_dumps() {
    local prefix f
    for prefix in "$PREDEPLOY_PREFIX" "$PRERESTORE_PREFIX"; do
        while read -r f; do
            log "deleted old dump $f"
        done < <(find "$BACKUP_DIR" -maxdepth 1 -type f -name "${prefix}*.sql.gz" \
            -mmin "+$((SAFETY_DUMP_MAX_DAYS * 1440))" -print -delete)
    done
}

# Keep only the newest $2 dumps with prefix $1 (the timestamps in the names sort by age).
prune_keep() {
    local prefix="$1" keep="$2" f
    while read -r f; do
        rm -f "$f"
        log "deleted old dump $f"
    done < <(find "$BACKUP_DIR" -maxdepth 1 -type f -name "${prefix}*.sql.gz" | sort -r | tail -n "+$((keep + 1))")
}

# --------------------------------------------------------------------------- off-site copy

# True when the remote path holds at least one file (any depth).
remote_has_files() {
    local first
    first="$(rclone lsf "$1" --files-only -R "${RCLONE_TIMEOUTS[@]}" 2>/dev/null | head -n 1 || true)"
    [[ -n "$first" ]]
}

# Copies the dumps (never mirrors them) and expires remote dumps by age. Deleting nothing that
# the local disk merely lacks is the point: a half-restored or empty server must not be able to
# wipe the off-site history.
offsite_dumps() {
    local remote="$1" keep recent
    keep="$(env_days BACKUP_REMOTE_RETENTION_DAYS 30)"
    log "copying dumps to $remote/db"
    rclone copy "$BACKUP_DIR" "$remote/db" --include "${PREFIX}*.sql.gz" "${RCLONE_TIMEOUTS[@]}" ||
        die "rclone copy of the database dumps failed"
    # Expire only when something newer than the cut-off exists remotely, so a stalled copy can
    # never age the whole history out.
    recent="$(rclone lsf "$remote/db" --include "${PREFIX}*.sql.gz" --max-age "${keep}d" "${RCLONE_TIMEOUTS[@]}" | head -n 1 || true)"
    if [[ -n "$recent" ]]; then
        rclone delete "$remote/db" --include "${PREFIX}*.sql.gz" --min-age "${keep}d" "${RCLONE_TIMEOUTS[@]}" ||
            die "rclone could not expire old off-site dumps"
    fi
}

# A mirror of the library whose deletions are safe: removed or replaced files move to
# media-trash/<date> (kept RCLONE_TRASH_DAYS days), at most RCLONE_MAX_DELETE per run, and an
# empty local library is never synced over a non-empty remote.
offsite_media() {
    local remote="$1" media_dir max_delete trash_days
    media_dir="$(env_get REPEATER_MEDIA_DIR "$ENV_FILE")"
    media_dir="${media_dir:-/srv/repeater/media}"
    [[ -d "$media_dir" ]] || die "the media directory $media_dir does not exist; media not copied off-site"
    max_delete="$(env_days RCLONE_MAX_DELETE 500)"
    trash_days="$(env_days RCLONE_TRASH_DAYS 7)"
    if [[ -z "$(find "$media_dir" -type f ! -path "$media_dir/uploads/*" ! -path "$media_dir/.cookies/*" -print -quit)" ]] &&
        remote_has_files "$remote/media"; then
        die "the local media directory is empty but $remote/media is not: not syncing (restore with: rclone copy $remote/media $media_dir)"
    fi
    log "syncing media to $remote/media (deleted files go to $remote/media-trash for $trash_days days)"
    rclone sync "$media_dir" "$remote/media" --backup-dir "$remote/media-trash/$(date -u +%Y%m%d)" \
        --max-delete "$max_delete" --exclude "uploads/**" --exclude ".cookies/**" "${RCLONE_TIMEOUTS[@]}" ||
        die "rclone sync of the media library failed (more than $max_delete deletions? see RCLONE_MAX_DELETE)"
    offsite_expire_trash "$remote" "$trash_days"
}

# media-trash/<YYYYMMDD> directories older than $2 days are purged (by name: file times are the
# originals', not the day they were trashed).
offsite_expire_trash() {
    local remote="$1" days="$2" cutoff dir
    cutoff="$(date -u -d "$days days ago" +%Y%m%d)"
    while read -r dir; do
        dir="${dir%/}"
        [[ "$dir" =~ ^[0-9]{8}$ && "$dir" < "$cutoff" ]] || continue
        rclone purge "$remote/media-trash/$dir" "${RCLONE_TIMEOUTS[@]}" || warn "could not purge $remote/media-trash/$dir"
    done < <(rclone lsf "$remote/media-trash" --dirs-only "${RCLONE_TIMEOUTS[@]}" 2>/dev/null || true)
}

offsite_copy() {
    local remote media_mode
    remote="$(env_get RCLONE_REMOTE "$ENV_FILE")"
    [[ -n "$remote" ]] || return 0
    command -v rclone >/dev/null || die "RCLONE_REMOTE is set but rclone is not installed (apt-get install rclone, then rclone config)"
    offsite_dumps "$remote"
    media_mode="$(env_get RCLONE_MEDIA_SYNC "$ENV_FILE")"
    media_mode="${media_mode:-weekly}"
    case "$media_mode" in
        off) return 0 ;;
        weekly) [[ "$(date +%u)" == 7 ]] || return 0 ;;
        daily) ;;
        *) die "RCLONE_MEDIA_SYNC must be weekly, daily or off" ;;
    esac
    offsite_media "$remote"
}

# --------------------------------------------------------------------------- commands

# Takes the backup lock: the cron job fails at once when another backup runs, deploys and
# restores wait for it.
prepare_backup_dir() {
    local wait="${1:-0}"
    install -d -m 0700 "$BACKUP_DIR"
    exec 9>"$BACKUP_DIR/.backup.lock"
    if ((wait > 0)); then
        flock -w "$wait" 9 || die "another backup is still running after ${wait}s"
    else
        flock -n 9 || die "another backup is running"
    fi
}

backup() {
    ALERT_ON_FAILURE=1
    prepare_backup_dir
    require_db_running
    local days
    days="$(env_days BACKUP_RETENTION_DAYS 14)"
    dump_database "$PREFIX"
    prune_old "$days"
    prune_safety_dumps
    offsite_copy
    record_success
    log "backup finished"
}

pre_deploy() {
    prepare_backup_dir 900
    require_db_running
    dump_database "$PREDEPLOY_PREFIX"
    prune_keep "$PREDEPLOY_PREFIX" "$PREDEPLOY_KEEP"
    prune_safety_dumps
    log "pre-deploy backup finished: $DUMP_FILE (roll back with: REPEATER_IMAGE_TAG=previous bash $SCRIPT_DIR/backup.sh restore $DUMP_FILE --yes)"
}

# Pausing is part of the load transaction when the dump's schema already has the column; it is
# repeated after the migrations for dumps that predate it.
PAUSE_IN_DUMP_SQL="DO \$\$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'public' AND table_name = 'workspaces' AND column_name = 'paused') THEN UPDATE public.workspaces SET paused = true; END IF; END \$\$;"

restore() {
    local file="" confirm=0 resume=0 arg tag stamp
    for arg in "$@"; do
        case "$arg" in
            --yes) confirm=1 ;;
            --resume) resume=1 ;;
            -*) die "unknown restore option: $arg" ;;
            *)
                [[ -z "$file" ]] || die "restore takes exactly one file"
                file="$arg"
                ;;
        esac
    done
    [[ -n "$file" ]] || die "usage: backup.sh restore FILE --yes [--resume]"
    [[ -f "$file" ]] || die "no such file: $file"
    ((confirm)) || die "restoring replaces the whole database; add --yes to confirm"
    gzip -t "$file" || die "$file is not a valid gzip file"
    grep -q 'PostgreSQL database dump' < <(gzip -dc "$file" | head -c 4096) || die "$file is not a pg_dump file"
    require_db_running
    tag="${REPEATER_IMAGE_TAG:-latest}"
    docker image inspect "repeater-app:$tag" >/dev/null 2>&1 ||
        die "the image repeater-app:$tag does not exist (REPEATER_IMAGE_TAG selects the release whose migrations run after the restore)"
    prepare_backup_dir 900

    log "taking a safety dump of the current database first"
    if SOFT_FAIL=1 dump_database "$PRERESTORE_PREFIX"; then
        prune_keep "$PRERESTORE_PREFIX" "$PRERESTORE_KEEP"
        prune_safety_dumps
    else
        warn "no safety dump of the current database could be taken; the restore itself is atomic and leaves it untouched on failure"
    fi

    log "stopping web and worker"
    compose stop web worker
    log "restoring $file in one transaction (a failure leaves the current database untouched)"
    if ! {
        # (the NOTICE lines that DROP ... CASCADE prints for every table are noise)
        printf 'SET client_min_messages = warning;\nBEGIN;\nDROP SCHEMA public CASCADE;\nCREATE SCHEMA public;\n'
        gzip -dc "$file"
        printf '\n%s\nCOMMIT;\n' "$PAUSE_IN_DUMP_SQL"
    } | compose exec -T db psql -U repeater -d repeater -v ON_ERROR_STOP=1 -q >/dev/null; then
        warn "the restore failed and was rolled back: the database is as it was before. web and worker stay stopped."
        warn "fix the problem and run the restore again (or start them with: docker compose up -d web worker)."
        exit 1
    fi

    log "applying the migrations of repeater-app:$tag"
    compose run --rm --no-TTY migrate ||
        die "the restore succeeded but the migrations failed. Do NOT start the worker: workspaces may not be paused yet. Fix the migration (or use REPEATER_IMAGE_TAG=<release matching the dump>) and run the restore again."
    compose exec -T db psql -U repeater -d repeater -v ON_ERROR_STOP=1 -q -c "UPDATE workspaces SET paused = true" >/dev/null ||
        die "could not pause the workspaces after the restore; web and worker stay stopped"

    if ((resume)); then
        log "starting web and the worker (--resume)"
        compose up -d web worker
    else
        log "starting web only; the worker stays stopped (add --resume, or start it yourself, once you have checked)"
        compose up -d web
    fi
    stamp="$(basename "$file" | grep -oE '[0-9]{8}T[0-9]{6}Z' | head -n 1 || true)"
    log "restore finished. ALL WORKSPACES ARE PAUSED: nothing is scheduled or published until you resume them."
    log "Dump taken at ${stamp:-an unknown time}. Posts that went out after that time are SCHEDULED again in the restored data: check every connected account for them, cancel or delete the duplicates, then resume each workspace in the dashboard (Settings > pause)."
    if ((resume)); then
        log "The worker is running (imports, encodes and polling continue)."
    else
        log "The worker is stopped. Start it with: docker compose --env-file $ENV_FILE -f $SCRIPT_DIR/docker-compose.yml up -d worker"
    fi
}

main() {
    case "${1:-backup}" in
        backup)
            trap on_exit EXIT
            backup
            ;;
        pre-deploy)
            trap on_exit EXIT
            pre_deploy
            ;;
        status)
            shift
            trap on_exit EXIT
            status_cmd "$@"
            ;;
        restore)
            shift
            trap on_exit EXIT
            restore "$@"
            ;;
        -h | --help) awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0" ;;
        *) die "unknown command: $1 (see --help)" ;;
    esac
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
