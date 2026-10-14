#!/usr/bin/env bash
# Build and (re)start Repeater on this host. CI runs it over SSH after syncing the code;
# bootstrap.sh runs it on the first install. It never modifies deploy/.env.
#
#   sudo bash /srv/repeater/app/deploy/deploy.sh [options]
#
# Options:
#   --pull            also pull newer base images (python, postgres, caddy) before building
#   --refresh-ytdlp   force the newest yt-dlp now (otherwise refreshed on the first deploy of each day)
#   --no-build        restart with the existing image (rollback: REPEATER_IMAGE_TAG=previous ... --no-build)
# Environment:
#   HEALTH_TIMEOUT    seconds to wait for the containers to become healthy (default 240)
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"
COMPOSE_FILE="$SCRIPT_DIR/docker-compose.yml"
IMAGE_REPO="repeater-app"
APP_UID=10001
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-240}"
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

compose() {
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

build_image() {
    local pull="$1" refresh="$2"
    if docker image inspect "$IMAGE_REPO:latest" >/dev/null 2>&1; then
        docker image tag "$IMAGE_REPO:latest" "$IMAGE_REPO:previous"
    fi
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
    if ((build)); then
        export REPEATER_IMAGE_TAG=latest
        build_image "$pull" "$refresh"
    else
        log "skipping the build; using image $IMAGE_REPO:${REPEATER_IMAGE_TAG:-latest}"
    fi

    log "applying database migrations"
    compose run --rm --no-TTY migrate || die "migrations failed; the running version was left untouched"

    log "starting the new version"
    compose up -d --remove-orphans
    refresh_caddy
    wait_healthy || die "deploy finished but the containers are not healthy"
    check_http
    prune_images
    compose ps --format 'table {{.Service}}\t{{.Status}}\t{{.Ports}}'
    log "deploy complete"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
