#!/usr/bin/env bash
# Build and (re)start Repeater on this host. CI runs it over SSH after syncing the code;
# bootstrap.sh runs it on the first install. It never modifies deploy/.env.
#
#   sudo bash /srv/repeater/app/deploy/deploy.sh [options]
#
# Options:
#   --pull            also pull newer base images: python (rebuilds the app image), and the postgres
#                     and caddy images (the database restarts briefly when its image changed)
#   --refresh-ytdlp   force the newest yt-dlp now (otherwise refreshed on the first deploy of each day)
#   --no-build        restart with the existing image (rollback: REPEATER_IMAGE_TAG=previous ... --no-build)
# Environment:
#   HEALTH_TIMEOUT    seconds to wait for the containers to become healthy (default 240)
#   SKIP_PREDEPLOY_BACKUP=1   migrate without the pre-deploy database dump (not recommended)
#
# Before migrating, a dump of the running database is taken (backup.sh pre-deploy, the newest 5 are
# kept). The previous release stays available as repeater-app:previous (it moves only when a build
# produces a different image) and as repeater-app:build-<time> (the newest 4).
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"
COMPOSE_FILE="$SCRIPT_DIR/docker-compose.yml"
IMAGE_REPO="repeater-app"
APP_UID=10001
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-240}"
BUILD_TAGS_KEEP=4
REQUIRED_KEYS=(POSTGRES_PASSWORD REPEATER_SECRET_KEY REPEATER_TOKEN_ENCRYPTION_KEYS REPEATER_BASE_URL)

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWARNING:\033[0m %s\n' "$*" >&2; }
die() {
    printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2
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
        docker compose --project-directory "$SCRIPT_DIR" -f "$COMPOSE_FILE" --env-file "$ENV_FILE" "$@"
}

preflight() {
    [[ -f "$ENV_FILE" ]] || die "$ENV_FILE is missing: run deploy/bootstrap.sh first"
    if [[ -n "$(find "$ENV_FILE" -perm /077 2>/dev/null)" ]]; then
        warn "$ENV_FILE is readable by other users; fix with: chmod 600 $ENV_FILE"
    fi
    local key missing=()
    for key in "${REQUIRED_KEYS[@]}"; do
        [[ -n "$(env_get "$key" "$ENV_FILE")" ]] || missing+=("$key")
    done
    ((${#missing[@]} == 0)) || die "empty in deploy/.env: ${missing[*]} (bootstrap.sh generates them)"
    [[ "$(env_get POSTGRES_PASSWORD "$ENV_FILE")" =~ ^[A-Za-z0-9._~-]+$ ]] ||
        die "POSTGRES_PASSWORD must be URL-safe (letters, digits, . _ ~ -); it is part of the database URL"
    command -v docker >/dev/null || die "docker is not installed: run deploy/bootstrap.sh"
    docker compose version >/dev/null 2>&1 || die "the docker compose plugin is missing: run deploy/bootstrap.sh"
    compose config --quiet || die "docker-compose.yml or deploy/.env is invalid (see above)"
}

# The media directory must exist and belong to the app user before the containers mount it.
ensure_media_dir() {
    local dir owner
    dir="$(env_get REPEATER_MEDIA_DIR "$ENV_FILE")"
    dir="${dir:-/srv/repeater/media}"
    if [[ ! -d "$dir" ]]; then
        install -d -m 0750 "$dir"
        chown "$APP_UID:$APP_UID" "$dir" 2>/dev/null || warn "cannot chown $dir to uid $APP_UID; run this script as root"
    fi
    owner="$(stat -c '%u' "$dir")"
    [[ "$owner" == "$APP_UID" ]] ||
        warn "$dir belongs to uid $owner, not $APP_UID: the worker cannot write videos there (fix: chown -R $APP_UID:$APP_UID $dir)"
}

image_id() { docker image inspect --format '{{.Id}}' "$1" 2>/dev/null || true; }

# What an image IS: its filesystem layers and the settings that matter at run time. The image ID is
# no use for this: with the containerd image store (Docker 28+ on a fresh install) every build gets
# a new ID, because the manifest carries build provenance, even when every layer came from cache.
# Compose labels (they name the service that was built last) are left out on purpose.
image_content() {
    local info
    info="$(docker image inspect --format '{{json .RootFS.Layers}} {{json .Config.Env}} {{json .Config.Cmd}} {{json .Config.Entrypoint}} {{json .Config.Healthcheck}}' "$1" 2>/dev/null)" || return 0
    printf '%s' "$info" | sha256sum | cut -d' ' -f1
}

# :previous moves only when a build really produced a different release. Re-running a deploy (a
# retry after a failed health check, a credentials update, a docs-only push) rebuilds the identical
# image from cache and must never turn the rollback target into the current release.
# The image :latest pointed to is pinned with a temporary tag before the build: an image that loses
# its last tag is deleted at once by the containerd image store, and with it the rollback target.
record_release() {
    local old_content="$1" new_content="$2" tag candidate="$IMAGE_REPO:rollback-candidate"
    if [[ -n "$old_content" && "$old_content" == "$new_content" ]]; then
        log "the build produced the same image as before: $IMAGE_REPO:previous is left as it is"
    else
        if docker image inspect "$candidate" >/dev/null 2>&1; then
            if docker image tag "$candidate" "$IMAGE_REPO:previous"; then
                log "the release before this one is $IMAGE_REPO:previous"
            else
                warn "could not tag $IMAGE_REPO:previous"
            fi
        fi
        docker image tag "$IMAGE_REPO:latest" "$IMAGE_REPO:build-$(date -u +%Y%m%d%H%M%S)" || warn "could not tag this release"
        while read -r tag; do
            docker rmi "$IMAGE_REPO:$tag" >/dev/null 2>&1 || true
        done < <(docker image ls "$IMAGE_REPO" --format '{{.Tag}}' | grep '^build-' | sort -r | tail -n "+$((BUILD_TAGS_KEEP + 1))")
    fi
    docker rmi "$candidate" >/dev/null 2>&1 || true
}

build_image() {
    local pull="$1" refresh="$2" old_id old_content
    old_id="$(image_id "$IMAGE_REPO:latest")"
    old_content="$(image_content "$IMAGE_REPO:latest")"
    if [[ -n "$old_id" ]]; then docker image tag "$old_id" "$IMAGE_REPO:rollback-candidate"; fi
    # Changes once a day (or now with --refresh-ytdlp): rebuilds only the yt-dlp layer.
    if [[ "$refresh" == 1 ]]; then
        YTDLP_REFRESH="$(date -u +%Y%m%d%H%M%S)"
    else
        YTDLP_REFRESH="$(date -u +%Y%m%d)"
    fi
    export YTDLP_REFRESH
    local args=(build)
    if [[ "$pull" == 1 ]]; then args+=(--pull); fi
    log "building the image (yt-dlp refresh key $YTDLP_REFRESH)"
    compose "${args[@]}" migrate web worker
    record_release "$old_content" "$(image_content "$IMAGE_REPO:latest")"
}

# Images without a build section (postgres, and caddy when its profile is on) are only ever pulled
# when missing; --pull refreshes them. `up -d` then recreates the containers whose image changed.
pull_images() {
    log "pulling newer images for the services that are not built here (db, caddy)"
    compose pull --ignore-buildable ||
        die "could not pull the newer images (network, or the registry's rate limit); nothing was changed"
}

# A dump of the live database before the migrations touch it. This is the restore point for
# rolling back a release that changed the schema (backup.sh restore ... --yes).
predeploy_backup() {
    if [[ "${SKIP_PREDEPLOY_BACKUP:-0}" == 1 ]]; then
        warn "SKIP_PREDEPLOY_BACKUP=1: migrating without a pre-deploy database dump"
        return 0
    fi
    if [[ -z "$(compose ps --status running -q db 2>/dev/null)" ]]; then
        if ! docker volume inspect "${COMPOSE_PROJECT_NAME:-repeater}_pg_data" >/dev/null 2>&1; then
            log "no database yet (first install): no pre-deploy backup"
            return 0
        fi
        log "the database container is stopped: starting it for the pre-deploy backup"
        compose up -d --wait db || die "could not start the database for the pre-deploy backup"
    fi
    log "taking the pre-deploy database backup"
    bash "$SCRIPT_DIR/backup.sh" pre-deploy ||
        die "the pre-deploy backup failed, so nothing was migrated. Fix it (disk space? the error above), or deploy anyway with SKIP_PREDEPLOY_BACKUP=1"
}

container_health() {
    local id
    id="$(compose ps -q "$1" 2>/dev/null | head -1)" || true
    [[ -n "$id" ]] || {
        echo "missing"
        return 0
    }
    docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$id" 2>/dev/null || echo "missing"
}

wait_healthy() {
    local deadline=$((SECONDS + HEALTH_TIMEOUT)) web worker
    log "waiting up to ${HEALTH_TIMEOUT}s for web and worker to report healthy"
    while ((SECONDS < deadline)); do
        web="$(container_health web)"
        worker="$(container_health worker)"
        if [[ "$web" == healthy && "$worker" == healthy ]]; then
            log "web and worker are healthy"
            return 0
        fi
        if [[ "$web" == unhealthy || "$web" == exited || "$worker" == exited ]]; then
            break
        fi
        sleep 3
    done
    warn "not healthy (web: ${web:-?}, worker: ${worker:-?}); recent logs follow"
    compose ps -a || true
    compose logs --no-color --tail 100 migrate web worker || true
    return 1
}

check_http() {
    command -v curl >/dev/null || return 0
    local host port
    host="$(env_get REPEATER_WEB_BIND "$ENV_FILE")"
    port="$(env_get REPEATER_WEB_PORT "$ENV_FILE")"
    if curl -fsS --max-time 10 "http://${host:-127.0.0.1}:${port:-8090}/healthz" >/dev/null; then
        log "http://${host:-127.0.0.1}:${port:-8090}/healthz answers"
    else
        warn "http://${host:-127.0.0.1}:${port:-8090}/healthz does not answer from the host"
    fi
}

# A bind-mounted file keeps pointing at the old inode after rsync replaces it, so a changed
# Caddyfile needs a recreated container rather than a reload.
refresh_caddy() {
    local id
    id="$(compose ps -q caddy 2>/dev/null | head -1)" || true
    [[ -n "$id" ]] || return 0
    if ! docker exec "$id" cat /etc/caddy/Caddyfile 2>/dev/null | cmp -s - "$SCRIPT_DIR/Caddyfile"; then
        log "Caddyfile changed: recreating caddy"
        compose up -d --no-deps --force-recreate caddy
    fi
}

prune_images() {
    # Only dangling layers of this image; other projects' images are left alone.
    docker image prune -f --filter "label=org.opencontainers.image.title=repeater" >/dev/null || true
}

main() {
    local pull=0 refresh=0 build=1
    while (($# > 0)); do
        case "$1" in
            --pull) pull=1 ;;
            --refresh-ytdlp) refresh=1 ;;
            --no-build) build=0 ;;
            -h | --help)
                awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"
                return 0
                ;;
            *) die "unknown option: $1 (see --help)" ;;
        esac
        shift
    done

    cd "$SCRIPT_DIR"
    preflight
    ensure_media_dir
    if ((pull)); then pull_images; fi
    if ((build)); then
        export REPEATER_IMAGE_TAG=latest
        build_image "$pull" "$refresh"
    else
        log "skipping the build; using image $IMAGE_REPO:${REPEATER_IMAGE_TAG:-latest}"
    fi

    predeploy_backup
    log "applying database migrations"
    compose run --rm --no-TTY migrate ||
        die "migrations failed; the running version was left untouched. If this release is older than the database schema (a rollback), restore the pre-deploy dump instead: REPEATER_IMAGE_TAG=${REPEATER_IMAGE_TAG:-latest} bash $SCRIPT_DIR/backup.sh restore <dump> --yes"

    log "starting the new version"
    compose up -d --remove-orphans
    refresh_caddy
    wait_healthy || die "deploy finished but the containers are not healthy"
    check_http
    prune_images
    compose ps --format 'table {{.Service}}\t{{.Status}}\t{{.Ports}}'
    log "deploy complete. Roll back with: REPEATER_IMAGE_TAG=previous bash $SCRIPT_DIR/deploy.sh --no-build"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
